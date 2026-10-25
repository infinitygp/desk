#!/bin/bash
# KDE Plasma (Wayland), which logs tester in by itself: runs as root over SSH on the installed
# system, after the distro's own install script, before it becomes the base image.
set -euo pipefail
. /etc/os-release
autologin() {
    # $1: the display manager's drop-in file.
    install -d "$(dirname "$1")"
    printf '[Autologin]\nUser=tester\nSession=plasma.desktop\n' > "$1"
}
case $ID in
    fedora)
        # The KDE edition's own set of packages without its first-run setup; kcalc and kwrite for
        # desk selftest.
        dnf install -y -q @kde-desktop-environment kcalc kwrite --exclude=plasma-setup >/dev/null
        systemctl set-default graphical.target
        autologin /etc/plasmalogin.conf.d/50-desk-autologin.conf
        ;;
    arch)
        pacman -S --noconfirm --needed plasma-meta konsole dolphin kcalc kwrite >/dev/null
        autologin /etc/plasmalogin.conf.d/50-desk-autologin.conf
        systemctl enable plasmalogin
        pacman -Scc --noconfirm >/dev/null
        ;;
    debian | ubuntu)
        export DEBIAN_FRONTEND=noninteractive
        apt-get install -y -q kde-plasma-desktop sddm kcalc kwrite >/dev/null
        autologin /etc/sddm.conf.d/50-desk-autologin.conf
        systemctl set-default graphical.target
        ;;
    *)
        echo "the kde block does not know how to install Plasma on $ID" >&2
        exit 1
        ;;
esac
