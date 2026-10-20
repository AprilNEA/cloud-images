#!/bin/sh
# Only run inside the offline image through virt-customize --run.
set -eu
test -f /usr/local/share/cloud-image/required-packages.txt

apt-get -y autoremove --purge
apt-get clean
update-grub

# Missing packages must fail the build, even when apt reports success.
while IFS= read -r package; do
  [ -n "$package" ] || continue
  [ "$(dpkg-query -W -f='${Status}' "$package")" = 'install ok installed' ]
done < /usr/local/share/cloud-image/required-packages.txt
command -v cloud-init
command -v qemu-ga
systemctl is-enabled systemd-timesyncd.service

dpkg-query -W -f='${binary:Package}\t${Version}\n' \
  > /usr/local/share/cloud-image/packages.txt

# This image has never booted. Remove any state created during customization.
cloud-init clean --logs --seed
find /etc/ssh -maxdepth 1 -type f -name 'ssh_host_*' -delete
find /var/log -type f -exec truncate -s 0 {} +
find /var/lib/apt/lists /var/cache/apt -mindepth 1 -delete
rm -f /etc/apt/apt.conf.d/99-image-build /var/lib/systemd/random-seed
rm -f /var/lib/dbus/machine-id
ln -s /etc/machine-id /var/lib/dbus/machine-id
truncate -s 0 /etc/machine-id
