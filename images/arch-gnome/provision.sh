#!/bin/bash
# Runs as root in the booted golden image right before the warm state is saved (and after
# cold boots). The shared GNOME files are in /tmp/desk-provision/files.
set -euo pipefail
FILES=/tmp/desk-provision/files

install -d /usr/share/gnome-shell/extensions/desk-helper@desk.local
install -m 0644 "$FILES"/desk-helper@desk.local/* /usr/share/gnome-shell/extensions/desk-helper@desk.local/

# Pages' web console into the journal (desk browser console); also for a Firefox installed later.
install -D -m 0644 "$FILES"/desk-firefox.js /usr/lib/firefox/browser/defaults/preferences/desk-firefox.js
# The thrown object and stack of uncaught exceptions as well (desk.cfg, Firefox's autoconfig).
install -D -m 0644 "$FILES"/desk-autoconfig.js /usr/lib/firefox/defaults/pref/desk-autoconfig.js
install -D -m 0644 "$FILES"/desk.cfg /usr/lib/firefox/desk.cfg

# Stable screen for screenshots: no lock, blanking, animations or blinking cursor, and no
# overview opened by a pointer passing the top left corner. GNOME Software is off (below),
# so the overview's search must not ask it.
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
enabled-extensions=['desk-helper@desk.local']
CONF
dconf update

# Polish (programmer's) layout = US plus AltGr letters, for `desk type`; and the a11y switches
# that Gecko, Chromium and Qt look for. GNOME copied defaults into the user's settings at first
# login, so set them there as well.
localectl set-x11-keymap pl
as_tester() { runuser -u tester -- env XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus "$@"; }
as_tester gsettings set org.gnome.desktop.input-sources sources "[('xkb', 'pl')]"
as_tester gsettings set org.gnome.desktop.interface toolkit-accessibility true
install -d -o tester -g tester /home/tester/.config /home/tester/.config/environment.d
cat > /home/tester/.config/environment.d/50-desk-a11y.conf <<'CONF'
GNOME_ACCESSIBILITY=1
QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1
QT_ACCESSIBILITY=1
CONF
chown -R tester:tester /home/tester/.config

# GNOME Software's update banners and prompts pop up at random moments over the app under test.
# GNOME 50 starts it as a user service of every session (gnome-session-services.target) and on
# D-Bus activation; masking stops both. Stopped now too: this session already runs it.
systemctl --global mask gnome-software.service
as_tester systemctl --user daemon-reload
as_tester systemctl --user stop gnome-software.service || true
if pgrep -x gnome-software; then echo "gnome-software still runs" >&2; exit 1; fi

# `desk reboot` reboots even when apps with unsaved work block shutdown (and reports them).
cat > /etc/polkit-1/rules.d/10-desk-root-ignore-inhibitors.rules <<'RULE'
polkit.addRule(function(action, subject) {
    if (subject.user == "root" && /^org\.freedesktop\.login1\.(reboot|power-off)-ignore-inhibit$/.test(action.id))
        return polkit.Result.YES;
});
RULE
