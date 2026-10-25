#!/bin/bash
# GNOME, which logs tester in by itself: runs as root over SSH on the installed system, after the
# distro's own install script, before it becomes the base image (desk image build).
set -euo pipefail
. /etc/os-release
gdm=/etc/gdm/custom.conf
case $ID in
    fedora)
        # The Workstation's own set of packages, without its first-run tour.
        dnf install -y -q @workstation-product-environment --exclude=gnome-initial-setup,gnome-tour
        systemctl set-default graphical.target
        ;;
    arch)
        # The gnome group has no gnome-initial-setup, so the session starts on the desktop.
        pacman -S --noconfirm --needed gnome
        systemctl enable gdm
        pacman -Scc --noconfirm
        ;;
    debian)
        export DEBIAN_FRONTEND=noninteractive
        # GNOME's core as Debian's GNOME task puts it down, and its Firefox, without the office suite.
        apt-get install -y -q gnome-core firefox-esr
        systemctl set-default graphical.target
        gdm=/etc/gdm3/daemon.conf
        ;;
    ubuntu)
        export DEBIAN_FRONTEND=noninteractive
        # Firefox from Mozilla's own repository: Ubuntu has it only as a snap, which desk cannot set
        # up (and which brings no Chromium either).
        install -d -m 0755 /etc/apt/keyrings
        curl -fsSL --proto =https https://packages.mozilla.org/apt/repo-signing-key.gpg \
            -o /etc/apt/keyrings/packages.mozilla.org.asc
        echo "deb [signed-by=/etc/apt/keyrings/packages.mozilla.org.asc] https://packages.mozilla.org/apt mozilla main" \
            > /etc/apt/sources.list.d/mozilla.list
        printf 'Package: *\nPin: origin packages.mozilla.org\nPin-Priority: 1000\n' > /etc/apt/preferences.d/mozilla
        apt-get update -q
        # Ubuntu's own desktop, as its installer puts the minimal one down.
        apt-get install -y -q ubuntu-desktop-minimal firefox
        systemctl set-default graphical.target
        gdm=/etc/gdm3/custom.conf
        ;;
    *)
        echo "the gnome block does not know how to install GNOME on $ID" >&2
        exit 1
        ;;
esac
printf '[daemon]\nAutomaticLoginEnable=True\nAutomaticLogin=tester\n' > "$gdm"
# No first-login assistant where the distribution has one, nor Ubuntu's welcome to its release.
install -d -o tester -g tester /home/tester/.config /home/tester/.config/gnome-initial-setup
echo yes > /home/tester/.config/gnome-initial-setup-done
touch "/home/tester/.config/gnome-initial-setup/upgrade-${VERSION_ID:-0}-done"
chown -R tester:tester /home/tester/.config
