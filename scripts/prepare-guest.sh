#!/bin/sh
# Only run inside the offline image through virt-customize --run.
set -eu
test -f /usr/local/share/cloud-image/required-packages.txt

install -d /etc/default/grub.d /etc/cloud/cloud.cfg.d \
  /etc/systemd/timesyncd.conf.d
cat > /etc/default/grub.d/99-cloud-image.cfg <<'EOF'
# Avoid probing other disks while customizing an offline image.
GRUB_DISABLE_OS_PROBER=true
EOF

# Reject unexpected layouts instead of silently accepting an ineffective sed.
test -f /etc/apt/sources.list.d/debian.sources
grep -q '^Types: deb' /etc/apt/sources.list.d/debian.sources
sed -i -E '/^Types:/ s/(^|[[:space:]])deb-src([[:space:]]|$)/ /g' \
  /etc/apt/sources.list.d/debian.sources

cat > /etc/cloud/cloud.cfg.d/99-cloud-image.cfg <<'EOF'
# Preserve the mirror lists supplied with this image.
apt:
  generate_mirrorlists: false
EOF
cat > /etc/systemd/timesyncd.conf.d/99-cloud-image.conf <<'EOF'
[Time]
NTP=time.apple.com time.windows.com
EOF
cat > /etc/apt/apt.conf.d/99-image-build <<'EOF'
Acquire::Retries "3";
APT::Update::Error-Mode "any";
EOF

# ttyS0 is already configured by Debian and matches PVE's serial0 device.
# Do not enable ttyS1 without also adding a second serial device in PVE.
