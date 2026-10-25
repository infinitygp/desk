#!/bin/bash
# GNOME's part of provisioning: runs as root in the booted golden image right before the warm
# state is saved, after what every desktop image gets (images/shared/desktop.sh). The files of
# both are in /tmp/desk-provision/files.
set -euo pipefail
FILES=/tmp/desk-provision/files

install -d /usr/share/gnome-shell/extensions/desk-helper@desk.local
install -m 0644 "$FILES"/desk-helper@desk.local/* /usr/share/gnome-shell/extensions/desk-helper@desk.local/

# Stable screen for screenshots: no lock, blanking, animations or blinking cursor (which would keep
# the screen from ever settling), and no overview opened by a pointer passing the top left corner.
# GNOME Software is off (below), so the overview's search must not ask it.
mkdir -p /etc/dconf/db/local.d /etc/dconf/profile
[ -f /etc/dconf/profile/user ] || printf 'user-db:user\nsystem-db:local\n' > /etc/dconf/profile/user
grep -q '^system-db:local$' /etc/dconf/profile/user || echo 'system-db:local' >> /etc/dconf/profile/user
cat > /etc/dconf/db/local.d/00-desk <<'CONF'
[org/gnome/desktop/session]
idle-delay=uint32 0

[org/gnome/desktop/screensaver]
lock-enabled=false
idle-activation-enabled=false

[org/gnome/desktop/lockdown]
disable-lock-screen=true

[org/gnome/desktop/interface]
enable-animations=false
cursor-blink=false
enable-hot-corners=false
toolkit-accessibility=true

[org/gnome/desktop/search-providers]
disabled=['org.gnome.Software.desktop']

[org/gnome/settings-daemon/plugins/power]
sleep-inactive-ac-type='nothing'
idle-dim=false

[org/gnome/software]
download-updates=false

[org/gnome/shell]
welcome-dialog-last-shown-version='999'
CONF
dconf update

# The keyboard layout desk types through, which GNOME copied into the user's settings at first
# login, and the a11y switch it wrote there as false (Gecko, Chromium and Qt look for it).
as_tester() { runuser -u tester -- env XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus "$@"; }
as_tester gsettings set org.gnome.desktop.input-sources sources "[('xkb', '$DESK_LAYOUT')]"
as_tester gsettings set org.gnome.desktop.interface toolkit-accessibility true

# The shell extension, added to the extensions the user has on, so the distribution's own (the
# Ubuntu dock) stay on.
as_tester python3 -c '
from gi.repository import Gio
s = Gio.Settings.new("org.gnome.shell")
on = s.get_strv("enabled-extensions")
if "desk-helper@desk.local" not in on:
    s.set_strv("enabled-extensions", on + ["desk-helper@desk.local"])
Gio.Settings.sync()'
# GNOME 51 dumps core now and then while the warm-up shuts down, and the unit started after the
# shell fails turns every extension off for good (disable-user-extensions), the desk helper too,
# so a warm state came up without it. The crash stays in the journal; the extensions stay on.
systemctl --global mask org.gnome.Shell-disable-extensions.service
as_tester gsettings set org.gnome.shell disable-user-extensions false

# GNOME Software's update banners and prompts pop up at random moments over the app under test.
# GNOME 50 starts it as a user service of every session (gnome-session-services.target) and on
# D-Bus activation, which masking stops; GNOME 48 (Debian) from an autostart entry, which the
# user's hidden copy stops. Stopped now too: this session already runs it.
systemctl --global mask gnome-software.service
if [ -f /etc/xdg/autostart/gnome-software-service.desktop ]; then
    install -d -o tester -g tester /home/tester/.config/autostart
    printf '[Desktop Entry]\nHidden=true\n' > /home/tester/.config/autostart/gnome-software-service.desktop
    chown tester:tester /home/tester/.config/autostart/gnome-software-service.desktop
fi
as_tester systemctl --user daemon-reload
as_tester systemctl --user stop gnome-software.service || true
pkill -x gnome-software && sleep 1 || true
if pgrep -x gnome-software; then echo "gnome-software still runs" >&2; exit 1; fi
