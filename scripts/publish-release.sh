#!/bin/bash
set -Eeuo pipefail

: "${GITHUB_REPOSITORY:?Missing repository}"
: "${GITHUB_SHA:?Missing build commit}"
if [[ ${GITHUB_REF:-} != refs/heads/main ||
      ! ${GITHUB_EVENT_NAME:-} =~ ^(push|workflow_dispatch)$ ]]; then
  echo 'Releases are only published from main on push or workflow_dispatch.' >&2
  exit 1
fi

OUTPUT_DIR=$(realpath "${1:-dist}")
WORK_DIR=$(mktemp -d)
trap 'rm -rf -- "$WORK_DIR"' EXIT
(cd "$OUTPUT_DIR" && sha256sum --check --strict SHA256SUMS)

# Read the date from the build, not from the time a release job is retried.
METADATA=$(python3 - "$OUTPUT_DIR/build-info.json" "$WORK_DIR/notes.md" <<'PY'
import datetime
import json
import os
from pathlib import Path
import re
import sys

info = json.loads(Path(sys.argv[1]).read_text())
sha = os.environ['GITHUB_SHA']
if not re.fullmatch('[0-9a-f]{40}', sha) or info['git_revision'] != sha:
    raise SystemExit('Artifact commit does not match the workflow commit')
version = str(info['debian_version'])
if version not in ('12', '13'):
    raise SystemExit('Unsupported Debian release in artifact metadata')
built = datetime.datetime.fromisoformat(info['built_at'])
if built.tzinfo is None:
    raise SystemExit('Build timestamp must include a timezone')
date = built.astimezone(datetime.timezone(datetime.timedelta(hours=8))).date().isoformat()
tag = f'debian-{version}-{date}-{sha[:12]}'
notes = f'''Custom Debian {version} genericcloud image for amd64.

- Build date: {date} (UTC+08:00).
- Source commit: `{sha}`.
- Image timezone: `{info['timezone']}`.
- Upstream image: {info['source_url']}.
- Upstream SHA512: `{info['source_sha512']}`.

Download all release assets, then verify them with `sha256sum -c SHA256SUMS`.
Use the qcow2 image and `pve-import-template.sh` to import a template into Proxmox VE.
The image passed offline checks. This CI run did not perform a boot test on a Proxmox VE node.
'''
Path(sys.argv[2]).write_text(notes)
print(tag)
print(version)
PY
)
mapfile -t FIELDS <<< "$METADATA"
TAG=${FIELDS[0]}
VERSION=${FIELDS[1]}
ASSETS=(
  "$OUTPUT_DIR/debian-$VERSION-genericcloud-amd64-custom.qcow2"
  "$OUTPUT_DIR/pve-import-template.sh"
  "$OUTPUT_DIR/SHA256SUMS"
  "$OUTPUT_DIR/build-info.json"
  "$OUTPUT_DIR/image-info.json"
  "$OUTPUT_DIR/packages.txt"
  "$OUTPUT_DIR/os-release"
  "$OUTPUT_DIR/source.sha512"
)
for asset in "${ASSETS[@]}"; do
  [[ -s "$asset" ]] || { echo "Missing or empty release asset: $asset" >&2; exit 1; }
done

if EXISTING=$(gh release view "$TAG" --repo "$GITHUB_REPOSITORY" \
  --json isDraft,targetCommitish 2> "$WORK_DIR/view-error.log"); then
  python3 - "$EXISTING" "$GITHUB_SHA" <<'PY'
import json
import sys
release = json.loads(sys.argv[1])
if not release['isDraft']:
    raise SystemExit('This tag is already published. Existing release assets will not be overwritten.')
if release['targetCommitish'] != sys.argv[2]:
    raise SystemExit('Existing draft targets a different commit; refusing to modify it')
PY
else
  # A non-404 lookup error will also make creation fail; do not hide that error.
  gh release create "$TAG" --repo "$GITHUB_REPOSITORY" \
    --target "$GITHUB_SHA" --draft \
    --title "$TAG" --notes-file "$WORK_DIR/notes.md"
fi

# Only drafts are modified. A failed upload leaves a retryable, unpublished draft.
gh release upload "$TAG" --repo "$GITHUB_REPOSITORY" --clobber "${ASSETS[@]}"
LATEST=false
if [[ "$VERSION" == 13 ]]; then LATEST=true; fi
gh release edit "$TAG" --repo "$GITHUB_REPOSITORY" \
  --draft=false --latest="$LATEST" --title "$TAG" --notes-file "$WORK_DIR/notes.md"
URL=$(gh release view "$TAG" --repo "$GITHUB_REPOSITORY" --json url --jq .url)
printf 'Published release: %s\n' "$URL"
if [[ -n ${GITHUB_STEP_SUMMARY:-} ]]; then
  printf '### Published image\n\n[%s](%s)\n' "$TAG" "$URL" >> "$GITHUB_STEP_SUMMARY"
fi
