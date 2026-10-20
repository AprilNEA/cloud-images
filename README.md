# cloud-images

Build custom Debian Cloud Images for Proxmox VE.
The implementation follows [Sukka's blog](https://blog.skk.moe/post/proxmox-ve-customize-debian-cloud-image/).
The customization uses libguestfs and does not boot the target system.

## Build an image

Open `Build Custom Debian Cloud Image` in Actions.
Select `Run workflow`, then set the release and time zone.
The default release is Debian 13, and Debian 12 is also supported.
The default time zone is `Asia/Shanghai`, and the architecture is `amd64`.

Changes to build-related files trigger builds for pull requests and `main`.
README changes do not trigger image builds.
The workflow does not create a PVE virtual machine automatically.

After a successful run, download its Actions artifact.
You can also download individual files from a GitHub Release.
Actions artifacts are retained for 30 days, and failed-run logs for 7 days.
Release files are not deleted when Actions artifacts expire.

The build outputs include:

- `debian-*-genericcloud-amd64-custom.qcow2`: the customized image.
- `pve-import-template.sh`: the PVE import script.
- `SHA256SUMS`: checksums for the output files.
- `build-info.json`: source URL, source hash, release, time zone, and tool versions.
- `image-info.json`: disk format and size.
- `packages.txt`: versions of packages actually installed in the image.
- `os-release`: operating system version information from the image.
- `source.sha512`: checksum of the original Debian image.

## GitHub Release

Only successful builds on the `main` branch publish a Release.
Push and `workflow_dispatch` are supported; pull requests do not publish.
The Release contains 8 individual files from `dist/`.
You can download the image directly without first downloading a zip file.

Tags use the format `debian-<12-or-13>-YYYY-MM-DD-<12-character-commitSHA>`.
The date comes from `built_at` in `build-info.json`.
The date is always converted using UTC+08:00.
The tag points to the build's `GITHUB_SHA`.

The workflow first creates a draft Release.
It publishes the Release only after all files upload successfully.
If a tag has already been published, the workflow reports an error and does not replace its files.
A draft with the same tag can be retried, and Debian 12 does not replace Latest.

## Changes from the original workflow

| Before | Now |
| --- | --- |
| Debian 12 by default; any release could be entered | Debian 13 by default; only 12 and 13 are accepted |
| `ubuntu-latest` | Pinned to `ubuntu-24.04` |
| `checkout@v4` / `upload-artifact@v4` | v7, pinned to full commit SHAs |
| Modify the image directly after download | Verify the official SHA512 first, then modify a copy |
| Try to continue offline after customization fails | Stop immediately on failure and do not publish incomplete output |
| Run libguestfs with `sudo -E` | Run as a regular user with separate cache and temporary directories |
| Install dependencies repeatedly | Combine installation and explicitly install `isc-dhcp-client` |
| Append GRUB and NTP configuration | Write separate configuration fragments to avoid duplicate entries |
| Enable `ttyS1` without a corresponding device | Keep Debian's built-in `ttyS0` |
| `dnsutils` | Explicitly install the actual provider package, `bind9-dnsutils` |
| Upload the image and import script separately | Keep all outputs in the Actions artifact and provide individual Release files |
| Guess the PVE disk name and force qcow2 | Use `import-from` and let the storage select the format |

The workflow file is now `.github/workflows/debian.yml`.
If you copy files manually, delete the old `debain.yml` file.
Otherwise, GitHub runs both the new and old workflows.

The `genericcloud` base and package purposes from the blog are retained.
QEMU Guest Agent, SPICE Agent, and network tools are retained.
See `packages.txt` for the complete package list.
You can remove `spice-vdagent` when no graphical environment is needed.

The image keeps the official APT mirrors and does not include third-party mirrors.
The build disables `deb-src` and prevents cloud-init from rewriting the mirror list.
NTP still uses `time.apple.com` and `time.windows.com`.
These settings are in `scripts/prepare-guest.sh`.

After the build, cloud-init state and SSH host keys are cleaned.
Logs, the APT cache, and `machine-id` are also cleaned.
Compression still uses `virt-sparsify --compress`.
The artifact does not recompress the already compressed qcow2 image.

## Import into PVE

Download all Release files into the same directory on the PVE host.
You can also extract the Actions artifact into that directory.
Then verify all files and import the image:

```bash
sha256sum -c SHA256SUMS
bash pve-import-template.sh \
  ./debian-13-genericcloud-amd64-custom.qcow2 \
  9000 local-lvm vmbr0
```

Run the script as root.
The VMID must be unused, and the storage must support VM images.
The script creates a Q35 virtual machine and uses VirtIO SCSI.
The cloud-init disk uses `scsi2`, and the serial port uses `serial0`.
The script does not start the virtual machine while creating the template.

If import fails, the script leaves the partially created virtual machine in place.
Check the error first, then decide whether to delete it manually.
The script does not overwrite or delete existing virtual machines.

The default CPU type is `host`.
Before migration across heterogeneous clusters, select a CPU type supported by all hosts.
The target storage determines the disk format.
Do not require `local-lvm` to store qcow2 files.

## Create a virtual machine from the template

Run these commands on the PVE host.
First confirm that VMID `100` is unused.
Replace the SSH public key path with your own path.

```bash
qm clone 9000 100 --name debian-13-vm --full 1 --storage local-lvm
qm resize 100 scsi0 +7G
qm set 100 --ciuser debian --sshkeys /root/.ssh/id_ed25519.pub
qm set 100 --ipconfig0 ip=dhcp
qm start 100
```

The image uses an SSH public key by default and includes no password or private key.
After the first boot, check that cloud-init has completed.
Also check the network, disk expansion, and Guest Agent.

```bash
# Run inside the virtual machine
cloud-init status --wait
systemctl status qemu-guest-agent
lsblk

# Run on the PVE host
qm agent 100 ping
```

## Build and test locally

Use a separate build environment running Ubuntu 24.04.
Install the workflow dependencies, and ensure that the user can read the host kernel.
If `/dev/kvm` is available, the user also needs read and write access to that device.
Do not disable AppArmor on a production host for this purpose.

CI removes the optional `passt` package on its temporary build machine.
As a result, libguestfs uses QEMU's built-in SLIRP networking.
This avoids `passt` startup failures and does not change guest networking.

```bash
# Run as a regular user; do not add sudo.
DEBIAN_VERSION=13 TIMEZONE=Asia/Shanghai bash scripts/build-image.sh

# Static checks and simulated tests; PVE is not required.
for script in scripts/*.sh; do bash -n "$script"; done
shellcheck scripts/*.sh
python3 -m unittest discover -s tests -v
```

The default local output directory is `dist/`, and the log directory is `logs/`.
The output directory must be empty to prevent mixing in old outputs.
Set `OUTPUT_DIR` and `LOG_DIR` to use different directories.
`prepare-guest.sh` and `finalize-guest.sh` are only for libguestfs to call.
Do not run these two scripts directly on the host.

Simulated tests cover aborts on failure, verification failures, and import parameters.
The build also checks qcow2 format and image contents.
These checks do not replace a real PVE boot test.

## Version maintenance

Action versions were checked on 2026-10-21 (UTC+08:00).
The current pinned versions are `checkout` v7.0.1 and `upload-artifact` v7.0.2.
Dependabot checks for Action updates weekly and does not merge them automatically.
The configuration takes effect on GitHub only after it is merged into the default branch.

The source image uses Debian's `latest` URL.
Each build also updates packages.
Therefore, builds are traceable but are not guaranteed to be byte-for-byte reproducible.
Before release, save the complete output package, not only the workflow.
SHA512 and HTTPS are used to check download integrity.
The build does not currently perform additional offline signature verification for Debian.

References:

- [Original blog](https://blog.skk.moe/post/proxmox-ve-customize-debian-cloud-image/)
- [Debian 13 cloud image](https://cdimage.debian.org/images/cloud/trixie/latest/)
- [Proxmox Cloud-Init documentation](https://pve.proxmox.com/wiki/Cloud-Init_Support)
- [virt-customize manual](https://libguestfs.org/virt-customize.1.html)
- [Ubuntu 26.04 runner migration announcement](https://github.blog/changelog/2026-09-17-ubuntu-26-generally-available-and-latest-migration/)
- [checkout releases](https://github.com/actions/checkout/releases)
- [upload-artifact releases](https://github.com/actions/upload-artifact/releases)
