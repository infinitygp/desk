#!/bin/bash
# GNOME, which logs tester in by itself: runs as root over SSH on the installed system, after the
# distro's own install script, before it becomes the base image (desk image build).
set -euo pipefail
. /etc/os-release
case $ID in
    fedora)
        # The Workstation's own set of packages, without its first-run tour.
        dnf install -y -q @workstation-product-environment --exclude=gnome-initial-setup,gnome-tour >/dev/null
        systemctl set-default graphical.target
        ;;
    arch)
        # The gnome group has no gnome-initial-setup, so the session starts on the desktop.
        pacman -S --noconfirm --needed gnome >/dev/null
        systemctl enable gdm
        pacman -Scc --noconfirm >/dev/null
        ;;
    *)
        echo "the gnome block does not know how to install GNOME on $ID" >&2
        exit 1
        ;;
esac
printf '[daemon]\nAutomaticLoginEnable=True\nAutomaticLogin=tester\n' > /etc/gdm/custom.conf
