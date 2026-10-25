#!/bin/bash
# Hyprland, started for tester by greetd through uwsm (which brings graphical-session.target up):
# runs as root over SSH on the installed system, after the distro's own install script, before it
# becomes the base image. GNOME's calculator and editor are the applications desk selftest drives.
set -euo pipefail
. /etc/os-release
case $ID in
    arch)
        pacman -S --noconfirm --needed hyprland uwsm greetd xdg-desktop-portal-hyprland kitty wl-clipboard \
            gnome-calculator gnome-text-editor
        pacman -Scc --noconfirm
        ;;
    debian | ubuntu)
        export DEBIAN_FRONTEND=noninteractive
        if [ "$ID" = debian ]; then
            # Hyprland, uwsm and greetd come with trixie-backports only.
            echo "deb http://deb.debian.org/debian ${VERSION_CODENAME}-backports main" \
                > /etc/apt/sources.list.d/backports.list
            apt-get update -q
            from=(-t "${VERSION_CODENAME}-backports")
        else
            from=()
        fi
        apt-get install -y -q "${from[@]}" hyprland uwsm greetd
        apt-get install -y -q xdg-desktop-portal-hyprland kitty wl-clipboard gnome-calculator gnome-text-editor
        ;;
    *)
        echo "the hyprland block does not know how to install Hyprland on $ID" >&2
        exit 1
        ;;
esac
# A configuration of desk's from the first start on, which provisioning completes: without one,
# Hyprland writes one of its own (Lua from 0.56 on) and shows a warning over the screen.
install -d -o tester -g tester /home/tester/.config /home/tester/.config/hypr
printf 'misc {\n    disable_hyprland_logo = true\n}\n' > /home/tester/.config/hypr/hyprland.conf
chown tester:tester /home/tester/.config/hypr/hyprland.conf
# greetd starts the session once per boot, without asking: the automatic login. On the terminal
# its unit keeps getty off (Debian's tty7, Arch's tty1): a getty starting on the session's
# terminal hangs it up, and the session ends within a second.
greeter=greeter
id _greetd >/dev/null 2>&1 && greeter=_greetd
vt=$(systemctl cat greetd.service | sed -n 's/^Conflicts=.*getty@tty\([0-9]*\)\.service.*/\1/p' | head -n1)
install -d /etc/greetd
cat > /etc/greetd/config.toml <<CONF
[terminal]
vt = ${vt:-1}

[default_session]
command = "agreety --cmd /bin/sh"
user = "$greeter"

[initial_session]
command = "uwsm start -- hyprland.desktop"
user = "tester"
CONF
systemctl enable greetd
systemctl set-default graphical.target
