#!/bin/bash
# Arch's system under any desktop, installed over SSH after cloud-init has made the users: runs as
# root before the desktop's install script.
set -euo pipefail
echo LANG=en_US.UTF-8 > /etc/locale.conf
sed -i 's/^#en_US.UTF-8/en_US.UTF-8/' /etc/locale.gen
locale-gen >/dev/null
# python-gobject: the a11y helper (AT-SPI through GObject introspection). chromium: the browser
# every desktop image has (desk browser). NetworkManager instead of the cloud image's
# systemd-networkd, as on a desktop (its network settings and indicator need it);
# systemd-resolved stays.
pacman -Syu --noconfirm --needed networkmanager python-gobject qemu-guest-agent sudo chromium >/dev/null
# The cloud image also holds the boot until NTP has synced (30 s with NetworkManager); a desktop
# does not, and the VM's clock comes from the host anyway.
systemctl disable systemd-networkd systemd-networkd-wait-online systemd-time-wait-sync
systemctl enable qemu-guest-agent NetworkManager
echo '%wheel ALL=(ALL:ALL) ALL' > /etc/sudoers.d/10-wheel
chmod 0440 /etc/sudoers.d/10-wheel
