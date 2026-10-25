#!/bin/bash
# Xfce's part of provisioning: runs as root in the booted golden image right before the warm state
# is saved, after what every desktop image gets (images/shared/desktop.sh).
set -euo pipefail
as_tester() {
    runuser -u tester -- env XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus "$@"
}
set_xfconf() { as_tester xfconf-query -c "$1" -p "$2" -n -t "$3" -s "$4"; }

# Stable screen for screenshots: no locker, blanking or power saving, no blinking text cursor.
set_xfconf xfce4-screensaver /saver/enabled bool false
set_xfconf xfce4-screensaver /lock/enabled bool false
set_xfconf xfce4-power-manager /xfce4-power-manager/dpms-enabled bool false
set_xfconf xfce4-power-manager /xfce4-power-manager/blank-on-ac int 0
set_xfconf xfce4-power-manager /xfce4-power-manager/lock-screen-suspend-hibernate bool false
set_xfconf xsettings /Net/CursorBlink bool false
# X's own blanking, which Xfce's settings do not reach before the power manager starts.
cat > /etc/xdg/autostart/desk-no-blanking.desktop <<'ENTRY'
[Desktop Entry]
Type=Application
Name=desk: no screen blanking
Exec=xset s off -dpms
ENTRY

# The keyboard layout desk types through: Xfce leaves the X server's alone unless told otherwise.
set_xfconf keyboard-layout /Default/XkbDisable bool false
set_xfconf keyboard-layout /Default/XkbLayout string "$DESK_LAYOUT"
