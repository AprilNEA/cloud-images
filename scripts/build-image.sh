#!/bin/bash
set -Eeuo pipefail

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
export DEBIAN_VERSION=${DEBIAN_VERSION:-13}
export TIMEZONE=${TIMEZONE:-Asia/Shanghai}
case "$DEBIAN_VERSION" in
  12) CODENAME=bookworm ;;
  13) CODENAME=trixie ;;
  *) echo 'Supported Debian versions: 12, 13' >&2; exit 1 ;;
esac
if [[ ! "$TIMEZONE" =~ ^[A-Za-z0-9_+/-]+$ || "$TIMEZONE" == *..* ||
      "$TIMEZONE" == /* || ! -f "/usr/share/zoneinfo/$TIMEZONE" ]]; then
  echo "Invalid IANA timezone: $TIMEZONE" >&2
  exit 1
fi
if (( EUID == 0 )); then
  echo 'Run this script as a regular user, not with sudo.' >&2
  exit 1
fi
for tool in curl sha512sum sha256sum qemu-img virt-customize virt-sparsify guestfish python3; do
  command -v "$tool" >/dev/null || { echo "Missing tool: $tool" >&2; exit 1; }
done

OUTPUT_DIR=${OUTPUT_DIR:-$ROOT_DIR/dist}
LOG_DIR=${LOG_DIR:-$ROOT_DIR/logs}
mkdir -p "$OUTPUT_DIR" "$LOG_DIR"
if [[ -n $(find "$OUTPUT_DIR" -mindepth 1 -maxdepth 1 -print -quit) ]]; then
  echo "Output directory must be empty: $OUTPUT_DIR" >&2
  exit 1
fi
OUTPUT_DIR=$(cd "$OUTPUT_DIR" && pwd)
LOG_DIR=$(cd "$LOG_DIR" && pwd)
export LIBGUESTFS_BACKEND=${LIBGUESTFS_BACKEND:-direct}
export LIBGUESTFS_CACHEDIR=${LIBGUESTFS_CACHEDIR:-${TMPDIR:-/tmp}/cloud-image-cache-$UID}
mkdir -p "$LIBGUESTFS_CACHEDIR"
WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/cloud-image.XXXXXX")
trap 'rm -rf -- "$WORK_DIR"' EXIT
exec > >(tee "$LOG_DIR/build.log") 2>&1
trap 'echo "Build failed at line $LINENO; no image bundle was published." >&2' ERR

IMAGE_NAME="debian-${DEBIAN_VERSION}-genericcloud-amd64.qcow2"
CUSTOM_NAME="debian-${DEBIAN_VERSION}-genericcloud-amd64-custom.qcow2"
export IMAGE_URL="https://cdimage.debian.org/images/cloud/${CODENAME}/latest/${IMAGE_NAME}"
BASE_URL=${IMAGE_URL%/*}
CURL=(curl --fail --location --silent --show-error --retry 3 --retry-delay 2 --proto '=https' --tlsv1.2)
"${CURL[@]}" "$BASE_URL/SHA512SUMS" -o "$WORK_DIR/SHA512SUMS"
"${CURL[@]}" "$IMAGE_URL" -o "$WORK_DIR/$IMAGE_NAME"
# Validate exactly this filename. Do not accept an empty checksum selection.
awk -v name="$IMAGE_NAME" '$2 == name || $2 == "*" name { print }' \
  "$WORK_DIR/SHA512SUMS" > "$WORK_DIR/source.sha512"
[[ $(wc -l < "$WORK_DIR/source.sha512") -eq 1 ]]
(cd "$WORK_DIR" && sha512sum --check --strict source.sha512)
export SOURCE_SHA512
SOURCE_SHA512=$(awk '{ print $1 }' "$WORK_DIR/source.sha512")
qemu-img check -q -f qcow2 "$WORK_DIR/$IMAGE_NAME"

mapfile -t PACKAGES < "$ROOT_DIR/packages.txt"
for package in "${PACKAGES[@]}"; do
  [[ "$package" =~ ^[a-z0-9][a-z0-9+.-]+$ ]] || { echo "Invalid package: $package" >&2; exit 1; }
done
(( ${#PACKAGES[@]} > 0 ))
PACKAGE_LIST=$(IFS=,; echo "${PACKAGES[*]}")

# Work on a copy. A failed customization is never retried on a half-built image.
cp --reflink=auto "$WORK_DIR/$IMAGE_NAME" "$WORK_DIR/working.qcow2"
virt-customize --format qcow2 -a "$WORK_DIR/working.qcow2" \
  --network --smp 2 --memsize 2048 --no-logfile \
  --timezone "$TIMEZONE" \
  --mkdir /usr/local/share/cloud-image \
  --upload "$ROOT_DIR/packages.txt:/usr/local/share/cloud-image/required-packages.txt" \
  --run "$ROOT_DIR/scripts/prepare-guest.sh" \
  --update --install "$PACKAGE_LIST" \
  --run "$ROOT_DIR/scripts/finalize-guest.sh"

mkdir "$WORK_DIR/bundle"
virt-sparsify --format qcow2 --convert qcow2 --compress \
  "$WORK_DIR/working.qcow2" "$WORK_DIR/bundle/$CUSTOM_NAME"
qemu-img check -q -f qcow2 "$WORK_DIR/bundle/$CUSTOM_NAME"
qemu-img info --output=json -f qcow2 "$WORK_DIR/bundle/$CUSTOM_NAME" \
  > "$WORK_DIR/bundle/image-info.json"

# Read-only inspection cannot regenerate the machine ID we just cleared.
CHECKS=$(guestfish --ro --format=qcow2 -a "$WORK_DIR/bundle/$CUSTOM_NAME" -i <<EOF
is-file /usr/sbin/qemu-ga
is-file /usr/bin/cloud-init
filesize /etc/machine-id
exists /var/lib/cloud/instance
EOF
)
[[ "$CHECKS" == $'true\ntrue\n0\nfalse' ]] || {
  printf 'Image verification failed:\n%s\n' "$CHECKS" >&2
  exit 1
}
guestfish --ro --format=qcow2 -a "$WORK_DIR/bundle/$CUSTOM_NAME" -i <<EOF
download /usr/local/share/cloud-image/packages.txt "$WORK_DIR/bundle/packages.txt"
download /etc/os-release "$WORK_DIR/bundle/os-release"
EOF
cp "$ROOT_DIR/scripts/pve-import-template.sh" "$WORK_DIR/bundle/"
cp "$WORK_DIR/source.sha512" "$WORK_DIR/bundle/source.sha512"
export BUILD_REVISION=${GITHUB_SHA:-$(git -C "$ROOT_DIR" rev-parse HEAD)}
python3 - "$WORK_DIR/bundle" <<'PY'
import datetime
import json
import os
import pathlib
import subprocess
import sys

bundle = pathlib.Path(sys.argv[1])
info = json.loads((bundle / 'image-info.json').read_text())
# Temporary paths are not useful after downloading the bundle.
info['filename'] = pathlib.Path(info['filename']).name
(bundle / 'image-info.json').write_text(json.dumps(info, indent=2) + '\n')
metadata = {
    'built_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'debian_version': os.environ['DEBIAN_VERSION'],
    'architecture': 'amd64',
    'timezone': os.environ['TIMEZONE'],
    'source_url': os.environ['IMAGE_URL'],
    'source_sha512': os.environ['SOURCE_SHA512'],
    'git_revision': os.environ['BUILD_REVISION'],
    'qemu_img': subprocess.check_output(['qemu-img', '--version'], text=True).splitlines()[0],
    'virt_customize': subprocess.check_output(['virt-customize', '--version'], text=True).strip(),
    'validation': 'offline; no PVE boot test',
}
(bundle / 'build-info.json').write_text(json.dumps(metadata, indent=2) + '\n')
PY
(cd "$WORK_DIR/bundle" && sha256sum -- ./* > SHA256SUMS)
# Only a fully verified bundle reaches the artifact directory.
mv "$WORK_DIR/bundle/"* "$OUTPUT_DIR/"
printf 'Verified image bundle: %s\n' "$OUTPUT_DIR"
