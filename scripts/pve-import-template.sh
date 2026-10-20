#!/usr/bin/env bash
# Import a standalone QCOW2 cloud image as a Proxmox VE template.
set -Eeuo pipefail

usage() {
  cat <<'USAGE'
Usage:
  pve-import-template.sh <image-file> [vm-id=9000] [storage=local-lvm] [bridge=vmbr0]

Environment:
  TEMPLATE_NAME  Override the template name. The default is the image basename
                 without .qcow2; non-alphanumeric characters become hyphens.
                 Use a hostname label of 1-63 characters.

Requirements:
  Run this script as root on the target Proxmox VE node.
  The image must be a standalone QCOW2 file. Backing files are not accepted.
  The selected storage must be active on this node and support VM images.

Notes:
  This script uses the host CPU model. Migrate clones only between nodes with
  compatible, homogeneous CPUs.
  This script does not set a password. Configure an SSH key after you clone
  the template.
  If a command fails after VM creation, the script stops and leaves that VM in
  place. The script never removes an existing or partial VM automatically.
USAGE
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

run() {
  if ! "$@"; then
    printf 'ERROR: Command failed:' >&2
    printf ' %q' "$@" >&2
    printf '\nERROR: Import stopped. The script did not remove any VM.\n' >&2
    exit 1
  fi
}

contains_forbidden_path_delimiter() {
  [[ "$1" == *,* || "$1" == *$'\n'* || "$1" == *$'\r'* ]]
}

if [[ "${1:-}" == '-h' || "${1:-}" == '--help' ]]; then
  usage
  exit 0
fi

if (( $# < 1 || $# > 4 )); then
  usage >&2
  exit 1
fi

IMAGE_FILE=$1
VM_ID=9000
STORAGE=local-lvm
BRIDGE=vmbr0

if (( $# >= 2 )); then
  VM_ID=$2
fi
if (( $# >= 3 )); then
  STORAGE=$3
fi
if (( $# >= 4 )); then
  BRIDGE=$4
fi

if (( EUID != 0 )); then
  die 'Run this script as root on the Proxmox VE node.'
fi

for command in qemu-img qm pvesh ip readlink hostname python3; do
  command -v "$command" >/dev/null 2>&1 || die "Required command not found: $command"
done

[[ "$VM_ID" =~ ^[1-9][0-9]{2,8}$ ]] || die "VM ID must be 100-999999999 without leading zeros: $VM_ID"

[[ "$STORAGE" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "Invalid storage name: $STORAGE"
[[ "$BRIDGE" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "Invalid bridge name: $BRIDGE"

if contains_forbidden_path_delimiter "$IMAGE_FILE"; then
  die 'Image paths cannot contain commas or line breaks.'
fi

if [[ ! -f "$IMAGE_FILE" ]]; then
  die "Image file does not exist or is not a regular file: $IMAGE_FILE"
fi

if ! IMAGE_PATH="$(readlink -f -- "$IMAGE_FILE")"; then
  die "Cannot resolve image path: $IMAGE_FILE"
fi
if contains_forbidden_path_delimiter "$IMAGE_PATH"; then
  die 'Resolved image paths cannot contain commas or line breaks.'
fi

IMAGE_BASENAME=${IMAGE_PATH##*/}
DEFAULT_NAME=${IMAGE_BASENAME%.qcow2}
DEFAULT_NAME=${DEFAULT_NAME//[^a-zA-Z0-9-]/-}
TEMPLATE_NAME=${TEMPLATE_NAME:-$DEFAULT_NAME}
if (( ${#TEMPLATE_NAME} > 63 )) || [[ ! "$TEMPLATE_NAME" =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$ ]]; then
  die 'Use TEMPLATE_NAME to supply a hostname label of 1-63 letters, digits or internal hyphens.'
fi

if ! IMAGE_INFO="$(qemu-img info --output=json "$IMAGE_PATH")"; then
  die "qemu-img could not inspect the image: $IMAGE_PATH"
fi
if ! IMAGE_VALIDATION="$(printf '%s' "$IMAGE_INFO" | python3 -c '
import json
import sys
try:
    image = json.load(sys.stdin)
except json.JSONDecodeError as error:
    print(f"qemu-img returned invalid JSON: {error}")
    raise SystemExit(1)
format_name = image.get("format")
if format_name != "qcow2":
    print(f"image format is {format_name!r}; expected qcow2")
    raise SystemExit(1)
for key in ("backing-filename", "full-backing-filename"):
    if image.get(key):
        print(f"image has a backing file in {key}")
        raise SystemExit(1)
' 2>&1)"; then
  die "Image validation failed: ${IMAGE_VALIDATION:-unknown validation error}"
fi

if ! pvesh get /cluster/nextid --vmid "$VM_ID" >/dev/null; then
  die "VM ID $VM_ID is already allocated or unavailable in this cluster. No changes were made."
fi

if ! STORAGE_CONFIG="$(pvesh get "/storage/$STORAGE" --output-format json)"; then
  die "Cannot read storage configuration: $STORAGE"
fi
if ! STORAGE_VALIDATION="$(printf '%s' "$STORAGE_CONFIG" | python3 -c '
import json
import sys
try:
    storage = json.load(sys.stdin)
except json.JSONDecodeError as error:
    print(f"storage API returned invalid JSON: {error}")
    raise SystemExit(1)
content = storage.get("content", "")
if isinstance(content, str):
    content = [item.strip() for item in content.split(",")]
if not isinstance(content, list) or "images" not in content:
    print("storage does not support images content")
    raise SystemExit(1)
')"; then
  die "Storage validation failed for $STORAGE: ${STORAGE_VALIDATION:-unknown validation error}"
fi

NODE_NAME="$(hostname --short)"
if ! STORAGE_STATUS="$(pvesh get "/nodes/$NODE_NAME/storage/$STORAGE/status" --output-format json)"; then
  die "Cannot read storage status for $STORAGE on node $NODE_NAME"
fi
if ! STORAGE_STATUS_VALIDATION="$(printf '%s' "$STORAGE_STATUS" | python3 -c '
import json
import sys
try:
    status = json.load(sys.stdin)
except json.JSONDecodeError as error:
    print(f"storage status API returned invalid JSON: {error}")
    raise SystemExit(1)
if status.get("active") not in (1, True, "1", "true", "True", "yes"):
    print("storage is not active on this node")
    raise SystemExit(1)
')"; then
  die "Storage status validation failed for $STORAGE on $NODE_NAME: ${STORAGE_STATUS_VALIDATION:-unknown validation error}"
fi

if ! ip link show dev "$BRIDGE" >/dev/null 2>&1; then
  die "Bridge does not exist on this node: $BRIDGE"
fi

printf 'Creating VM %s for template %s...\n' "$VM_ID" "$TEMPLATE_NAME"
run qm create "$VM_ID" \
  --name "$TEMPLATE_NAME" \
  --machine q35 \
  --cpu host \
  --memory 1024 \
  --scsihw virtio-scsi-single \
  --scsi2 "$STORAGE:cloudinit" \
  --serial0 socket \
  --vga serial0 \
  --net0 "virtio,bridge=$BRIDGE" \
  --agent 1 \
  --ostype l26

printf 'Importing image disk...\n'
run qm set "$VM_ID" --scsi0 "$STORAGE:0,import-from=$IMAGE_PATH,discard=on,ssd=1"

printf 'Configuring boot disk and DHCP...\n'
run qm set "$VM_ID" --boot order=scsi0 --ipconfig0 ip=dhcp

printf 'Validating cloud-init configuration...\n'
run qm cloudinit dump "$VM_ID" user >/dev/null

printf 'Converting VM %s to a template...\n' "$VM_ID"
run qm template "$VM_ID"
printf 'Template %s (%s) was created successfully.\n' "$TEMPLATE_NAME" "$VM_ID"
