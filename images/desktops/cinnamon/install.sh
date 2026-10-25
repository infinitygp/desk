#!/bin/bash
# Cinnamon on X11, which LightDM logs tester in to by itself: runs as root over SSH on the
# installed system, after the distro's own install script, before it becomes the base image. For
# desk: libwnck lists windows, xclip sets the clipboard, xrandr the screen's mode; GNOME's
# calculator and editor are what desk selftest drives.
set -euo pipefail
. /etc/os-release
case $ID in
    fedora)
        # The Cinnamon edition's own set of packages.
        dnf install -y -q @cinnamon-desktop-environment libwnck3 xclip xrandr gnome-calculator gnome-text-editor
        systemctl set-default graphical.target
        ;;
    arch)
        pacman -S --noconfirm --needed cinnamon lightdm lightdm-gtk-greeter libwnck3 xclip xorg-xrandr \
            gnome-calculator gnome-text-editor
        # Arch's LightDM logs in without a password only members of this group.
        groupadd -rf autologin
        gpasswd -a tester autologin >/dev/null
        systemctl enable lightdm
        pacman -Scc --noconfirm
        ;;
    debian | ubuntu)
        export DEBIAN_FRONTEND=noninteractive
        apt-get install -y -q cinnamon-desktop-environment lightdm gir1.2-wnck-3.0 xclip x11-xserver-utils \
            gnome-calculator gnome-text-editor
        systemctl set-default graphical.target
        ;;
    *)
        echo "the cinnamon block does not know how to install Cinnamon on $ID" >&2
        exit 1
        ;;
esac
install -d /etc/lightdm/lightdm.conf.d
printf '[Seat:*]\nautologin-user=tester\nautologin-session=cinnamon\n' > /etc/lightdm/lightdm.conf.d/50-desk-autologin.conf
