#!/bin/bash
# Xfce on X11, which LightDM logs tester in to by itself: runs as root over SSH on the installed
# system, after the distro's own install script, before it becomes the base image. For desk:
# libwnck lists windows, xclip sets the clipboard, xrandr the screen's mode, xset stops blanking;
# Mousepad (GTK 3) and GNOME's calculator (GTK 4) are what desk selftest drives.
set -euo pipefail
. /etc/os-release
case $ID in
    fedora)
        # The Xfce edition's own set of packages.
        dnf install -y -q @xfce-desktop-environment libwnck3 xclip xrandr xset mousepad gnome-calculator \
            >/dev/null
        systemctl set-default graphical.target
        ;;
    arch)
        pacman -S --noconfirm --needed xfce4 xfce4-goodies lightdm lightdm-gtk-greeter libwnck3 xclip \
            xorg-xrandr xorg-xset mousepad gnome-calculator >/dev/null
        # Arch's LightDM logs in without a password only members of this group.
        groupadd -rf autologin
        gpasswd -a tester autologin >/dev/null
        systemctl enable lightdm
        pacman -Scc --noconfirm >/dev/null
        ;;
    debian | ubuntu)
        export DEBIAN_FRONTEND=noninteractive
        apt-get install -y -q xfce4 xfce4-goodies lightdm gir1.2-wnck-3.0 xclip x11-xserver-utils mousepad \
            gnome-calculator >/dev/null
        systemctl set-default graphical.target
        ;;
    *)
        echo "the xfce block does not know how to install Xfce on $ID" >&2
        exit 1
        ;;
esac
# The panel as Xfce sets it up, instead of its question at the first login.
install -d -o tester -g tester /home/tester/.config /home/tester/.config/xfce4 /home/tester/.config/xfce4/xfconf \
    /home/tester/.config/xfce4/xfconf/xfce-perchannel-xml
install -o tester -g tester -m 0644 /etc/xdg/xfce4/panel/default.xml \
    /home/tester/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml
install -d /etc/lightdm/lightdm.conf.d
printf '[Seat:*]\nautologin-user=tester\nautologin-session=xfce\n' > /etc/lightdm/lightdm.conf.d/50-desk-autologin.conf
