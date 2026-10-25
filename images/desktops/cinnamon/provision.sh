#!/bin/bash
# Cinnamon's part of provisioning: runs as root in the booted golden image right before the warm
# state is saved, after what every desktop image gets (images/shared/desktop.sh).
set -euo pipefail
as_tester() {
    runuser -u tester -- env XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus "$@"
}

# Stable screen for screenshots: no lock, blanking, effects or blinking text cursor.
as_tester gsettings set org.cinnamon.desktop.screensaver lock-enabled false
as_tester gsettings set org.cinnamon.desktop.screensaver idle-activation-enabled false
as_tester gsettings set org.cinnamon.desktop.session idle-delay 0
as_tester gsettings set org.cinnamon desktop-effects false
as_tester gsettings set org.cinnamon.desktop.interface cursor-blink false
as_tester gsettings set org.cinnamon.settings-daemon.plugins.power sleep-display-ac 0
# The keyboard layout desk types through: Cinnamon 6.6 keeps it like GNOME, 6.4 (Debian, Ubuntu)
# in libgnomekbd's settings.
if as_tester gsettings list-keys org.cinnamon.desktop.input-sources >/dev/null 2>&1; then
    as_tester gsettings set org.cinnamon.desktop.input-sources sources "[('xkb', '$DESK_LAYOUT')]"
else
    as_tester gsettings set org.gnome.libgnomekbd.keyboard layouts "['$DESK_LAYOUT']"
fi
