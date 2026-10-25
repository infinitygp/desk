#!/bin/bash
# Debian's system under any desktop, installed over SSH after cloud-init has made the users: runs
# as root before the desktop's install script.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
# Package lists and upgrades in the background would run at random moments of a test, after the
# clock jumps forward on restore; a test of them starts them by hand.
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer
apt-get update -q >/dev/null
apt-get purge -y -q unattended-upgrades >/dev/null
apt-get upgrade -y -q >/dev/null
# util-linux-extra: hwclock, through which qemu-guest-agent sets the clock (desk clock, restores).
# python3-gi with the Atspi typelib: the a11y helper. chromium: the browser every desktop image
# has (desk browser). NetworkManager, as on a desktop (its network settings and indicator need it).
apt-get install -y -q locales qemu-guest-agent util-linux-extra python3-gi gir1.2-atspi-2.0 at-spi2-core sudo \
    network-manager chromium >/dev/null
sed -i 's/^# *en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen
locale-gen >/dev/null
update-locale LANG=en_US.UTF-8
