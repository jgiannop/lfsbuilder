#!/bin/bash
# lfsbuilder-install: puts the running live system on a disk.
#
#   lfsbuilder-install DISK          asks you to type DISK again before erasing it
#   lfsbuilder-install --yes DISK    doesn't ask (for automatic installs)
#   lfsbuilder-install --auto        DISK from lfsbuilder.autoinstall=DISK on the
#                                    kernel command line, without asking
#
# The disk gets a GPT: a 1 MiB BIOS boot partition, a 512 MiB EFI system
# partition and the rest for the system (ext4). The live system is copied
# onto it, /etc/fstab is written with the partitions' UUIDs, and GRUB is
# installed for both BIOS and UEFI PCs (UEFI at the removable path, so no
# firmware settings are needed). Written by lfsbuilder.

set -euo pipefail

say() { printf '\n==> %s\n' "$*"; }
fail() { printf 'lfsbuilder-install: %s\n' "$*" >&2; exit 1; }
trap 'fail "stopped at line $LINENO: $BASH_COMMAND"' ERR

yes=0
if [ "${1:-}" = "--yes" ]; then yes=1; shift; fi
if [ "${1:-}" = "--auto" ]; then
    yes=1
    disk=$(tr ' ' '\n' < /proc/cmdline | sed -n 's/^lfsbuilder\.autoinstall=//p' | head -n1)
    [ -n "$disk" ] || fail "--auto, but no lfsbuilder.autoinstall=DISK on the kernel command line"
else
    disk="${1:-}"
fi
[ -n "$disk" ] || fail "which disk? e.g. lfsbuilder-install /dev/sda (lsblk lists them)"
[ "$(id -u)" = 0 ] || fail "run it as root (sudo lfsbuilder-install $disk)"
[ -b "$disk" ] || fail "$disk isn't a disk"
[ "$(lsblk -dno TYPE "$disk")" = disk ] || fail "$disk is a partition or something else; give the whole disk"

# Never the medium the live system runs from.
live=$(findmnt -no SOURCE /run/initramfs/live 2>/dev/null || true)
if [ -n "$live" ]; then
    live_disk=/dev/$(lsblk -no PKNAME "$live" 2>/dev/null | head -n1)
    [ "$live_disk" != "$disk" ] && [ "$live" != "$disk" ] || fail "$disk holds the live system you are running"
fi

if [ "$yes" = 0 ]; then
    lsblk "$disk"
    printf '\nEverything on %s will be erased. Type the disk name again to go on: ' "$disk"
    read -r again
    [ "$again" = "$disk" ] || fail "not the same name; nothing was changed"
fi

part() { case "$disk" in *[0-9]) echo "${disk}p$1" ;; *) echo "${disk}$1" ;; esac; }
bios=$(part 1); esp=$(part 2); root=$(part 3)
target=/mnt/lfsbuilder-target

say "Partitioning $disk"
wipefs -aq "$disk"
sgdisk --zap-all "$disk" >/dev/null
sgdisk -n1:0:+1M -t1:ef02 -c1:bios -n2:0:+512M -t2:ef00 -c2:efi -n3:0:0 -t3:8304 -c3:root "$disk" >/dev/null
partprobe "$disk" 2>/dev/null || true
udevadm settle
for p in "$bios" "$esp" "$root"; do
    for _ in $(seq 1 20); do [ -b "$p" ] && break; sleep 0.5; done
    [ -b "$p" ] || fail "$p didn't appear"
done

say "Making file systems"
mkfs.vfat -F 32 -n EFI "$esp" >/dev/null
mkfs.ext4 -q -F -L root "$root"

say "Copying the system (this takes a while)"
mkdir -p "$target"
mount "$root" "$target"
source=/run/rootfsbase
if [ ! -d "$source/etc" ]; then
    source=/mnt/lfsbuilder-squashfs
    mkdir -p "$source"
    mount -o ro,loop /run/initramfs/live/LiveOS/squashfs.img "$source"
fi
tar -C "$source" --one-file-system --xattrs --acls -cpf - . | tar -C "$target" --xattrs --acls -xpf -
mkdir -p "$target/boot/efi"
mount "$esp" "$target/boot/efi"

say "Writing /etc/fstab"
cat > "$target/etc/fstab" <<EOF
# Written by lfsbuilder-install
UUID=$(blkid -s UUID -o value "$root")  /          ext4  defaults    0 1
UUID=$(blkid -s UUID -o value "$esp")  /boot/efi  vfat  umask=0077  0 2
EOF
# The installed system makes its own machine id at first boot. "uninitialized" (systemd's first-boot
# mark), not no file: the root is mounted read-only first, and systemd then mounts a transient id over
# this file and writes it once / is writable; with no file, accounts-daemon (and with it GDM) fails.
if [ -e "$target/etc/machine-id" ]; then printf 'uninitialized\n' > "$target/etc/machine-id"; fi

say "Installing GRUB (BIOS and UEFI)"
# Slaves: unmounting them from the target never reaches the running system's own mounts.
for d in dev proc sys; do mount --rbind "/$d" "$target/$d"; mount --make-rslave "$target/$d"; done
chroot "$target" grub-install --target=i386-pc "$disk"
chroot "$target" grub-install --target=x86_64-efi --efi-directory=/boot/efi --removable --no-nvram
# The console= settings the live system was started with (a serial console, say) go to the installed one.
consoles=$(grep -o 'console=[^ ]*' /proc/cmdline | tr '\n' ' ' || true)
if [ -n "$consoles" ]; then
    sed -i "s|^#\?GRUB_CMDLINE_LINUX=.*|GRUB_CMDLINE_LINUX=\"${consoles% }\"|" "$target/etc/default/grub"
fi
# The menu says the distribution's name ("Argo" -> "Argo GNU/Linux"), from its os-release.
name=$(sed -n 's/^NAME="\(.*\)"$/\1/p' "$target/etc/os-release" | sed 's/ Linux$//')
if [ -n "$name" ]; then
    sed -i "s|^#\?GRUB_DISTRIBUTOR=.*|GRUB_DISTRIBUTOR=\"${name//[^A-Za-z0-9 ._+-]/}\"|" "$target/etc/default/grub"
fi
# The distribution's picture behind the menu (gfxterm is grub-mkconfig's default when it has a font).
if [ -f "$target/usr/share/argo/argo-grub-1024x768.png" ]; then
    cp "$target/usr/share/argo/argo-grub-1024x768.png" "$target/boot/grub/argo.png"
    sed -i 's|^#\?GRUB_BACKGROUND=.*|GRUB_BACKGROUND="/boot/grub/argo.png"|' "$target/etc/default/grub"
    # Gentoo's file names one of its themes (Gentoo's artwork), and a theme wins over the background.
    sed -i 's|^GRUB_THEME=|#GRUB_THEME=|' "$target/etc/default/grub"
fi
chroot "$target" grub-mkconfig -o /boot/grub/grub.cfg
# The automatic install, and the graphical installer's launcher and sudo rule, belong to the live system only.
rm -f "$target/etc/systemd/system/multi-user.target.wants/lfsbuilder-autoinstall.service" \
    "$target/usr/share/applications/argo-install.desktop" "$target/etc/sudoers.d/zz-argo-live-installer" "$target/usr/bin/argo-install"

say "Finishing"
sync
umount -R "$target"
[ "$source" = /run/rootfsbase ] || umount "$source"
say "Done: $disk holds the system. Take out the live medium and restart."
