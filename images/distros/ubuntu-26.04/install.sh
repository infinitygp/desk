#!/bin/bash
# Ubuntu's system under any desktop, installed over SSH after cloud-init has made the users: runs
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
# python3-gi with the Atspi typelib: the a11y helper. NetworkManager, as on a desktop (its network
# settings and indicator need it). Ubuntu has Chromium only as a snap, so no Chromium.
apt-get install -y -q qemu-guest-agent util-linux-extra python3-gi gir1.2-atspi-2.0 at-spi2-core network-manager >/dev/null
# Crash reports would pop up over the app under test; a test of a crash reads the journal.
apt-get purge -y -q apport >/dev/null
