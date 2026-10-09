#!/bin/bash
# Runs as root in the booted golden image right before the warm state is saved.
# images/<name>/files/ is available in /tmp/desk-provision/files.
set -euo pipefail
FILES=/tmp/desk-provision/files

install -d /usr/share/gnome-shell/extensions/desk-helper@desk.local
install -m 0644 "$FILES"/desk-helper@desk.local/* /usr/share/gnome-shell/extensions/desk-helper@desk.local/

# Pages' web console into the journal (desk browser console); also for a Firefox installed later.
install -D -m 0644 "$FILES"/desk-firefox.js /usr/lib64/firefox/browser/defaults/preferences/desk-firefox.js
# The thrown object and stack of uncaught exceptions as well (desk.cfg, Firefox's autoconfig).
install -D -m 0644 "$FILES"/desk-autoconfig.js /usr/lib64/firefox/defaults/pref/desk-autoconfig.js
install -D -m 0644 "$FILES"/desk.cfg /usr/lib64/firefox/desk.cfg

# A blinking text cursor would keep the screen from ever settling.
cat > /etc/dconf/db/local.d/01-desk-provision <<'CONF'
[org/gnome/desktop/interface]
cursor-blink=false

[org/gnome/shell]
enabled-extensions=['desk-helper@desk.local']
CONF
dconf update

# Polish (programmer's) is the US layout plus AltGr letters, so `desk type` can enter
# Polish text with real key presses while every US key stays where it was. GNOME copied
# the layout into the user's settings at first login, so set it there as well.
localectl set-x11-keymap pl
as_tester() { runuser -u tester -- env DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus "$@"; }
as_tester gsettings set org.gnome.desktop.input-sources sources "[('xkb', 'pl')]"

# The accessibility tree (`desk a11y`). GTK4 exposes it regardless; Gecko, Chromium/Electron
# and Qt only when an assistive technology is announced (org.a11y.Status) or when forced
# through the environment. GNOME writes toolkit-accessibility=false into the user's settings
# at first login, so the system default alone is not enough.
as_tester gsettings set org.gnome.desktop.interface toolkit-accessibility true
install -d /home/tester/.config/environment.d
cat > /home/tester/.config/environment.d/50-desk-a11y.conf <<'CONF'
GNOME_ACCESSIBILITY=1
QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1
QT_ACCESSIBILITY=1
CONF
chown -R tester:tester /home/tester/.config/environment.d

# No machine-check exceptions in a VM; its failed unit would show up in every health check.
if systemctl cat mcelog.service >/dev/null 2>&1; then
    systemctl disable --now mcelog.service
fi
systemctl reset-failed

# GNOME Software's update notifications pop up at random moments over the app under test.
install -d -o tester -g tester /home/tester/.config/autostart
printf '[Desktop Entry]\nType=Application\nName=GNOME Software\nHidden=true\n' \
    > /home/tester/.config/autostart/org.gnome.Software.desktop
chown tester:tester /home/tester/.config/autostart/org.gnome.Software.desktop
pkill -u tester -f '[g]nome-software --gapplication-service' || true

# `desk reboot` reboots even when apps with unsaved work block shutdown (and reports them);
# logind only lets root override that with this rule, shipped by systemd as an example.
install -m 0644 /usr/share/polkit-1/rules.d/10-systemd-logind-root-ignore-inhibitors.rules.example \
    /etc/polkit-1/rules.d/10-systemd-logind-root-ignore-inhibitors.rules
