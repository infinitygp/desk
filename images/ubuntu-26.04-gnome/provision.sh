#!/bin/bash
# Runs as root in the booted golden image right before the warm state is saved (and after
# cold boots). The shared GNOME files are in /tmp/desk-provision/files.
set -euo pipefail
FILES=/tmp/desk-provision/files
export DEBIAN_FRONTEND=noninteractive

# Background package jobs would hold the dpkg lock while a test installs its .deb, and update
# prompts would cover the screen.
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service 2>/dev/null || true
systemctl stop apt-daily.service apt-daily-upgrade.service 2>/dev/null || true
while pgrep -x 'apt|apt-get|dpkg|unattended-upgr' >/dev/null; do sleep 2; done
snap refresh --hold=forever >/dev/null 2>&1 || true

# The a11y helper uses AT-SPI through GObject introspection; qemu-guest-agent's guest-set-time
# (clock sync after `desk up` restores a saved state) needs hwclock from util-linux-extra.
apt-get install -y -q gir1.2-atspi-2.0 python3-gi util-linux-extra >/dev/null

install -d /usr/share/gnome-shell/extensions/desk-helper@desk.local
install -m 0644 "$FILES"/desk-helper@desk.local/* /usr/share/gnome-shell/extensions/desk-helper@desk.local/

# Stable screen for screenshots: no lock, blanking, animations or blinking cursor.
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
toolkit-accessibility=true

[org/gnome/settings-daemon/plugins/power]
sleep-inactive-ac-type='nothing'
idle-dim=false

[org/gnome/shell]
welcome-dialog-last-shown-version='999'
enabled-extensions=['desk-helper@desk.local']
CONF
dconf update

# Polish (programmer's) layout = US plus AltGr letters, for `desk type`; and the a11y switches
# that Gecko, Chromium and Qt look for. GNOME copied defaults into the user's settings at first
# login, so set them there as well.
# localed refuses this on 26.04 ("Access denied" even for root); the session layout below is what counts.
localectl set-x11-keymap pl 2>/dev/null || true
as_tester() { runuser -u tester -- env DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus "$@"; }
as_tester gsettings set org.gnome.desktop.input-sources sources "[('xkb', 'pl')]"
as_tester gsettings set org.gnome.desktop.interface toolkit-accessibility true
install -d -o tester -g tester /home/tester/.config /home/tester/.config/environment.d
cat > /home/tester/.config/environment.d/50-desk-a11y.conf <<'CONF'
GNOME_ACCESSIBILITY=1
QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1
QT_ACCESSIBILITY=1
CONF
# No welcome wizard: Ubuntu runs gnome-initial-setup at first login and again, in
# --upgrade-user mode, whenever gnome-initial-setup-done exists without the per-release marker
# (gnome-initial-setup-upgrade-login.service).
. /etc/os-release
install -d -o tester -g tester /home/tester/.config/gnome-initial-setup
echo yes > /home/tester/.config/gnome-initial-setup-done
touch "/home/tester/.config/gnome-initial-setup/upgrade-${VERSION_ID}-done"
# Update and Ubuntu Pro notifications pop up at random moments over the app under test.
install -d /home/tester/.config/autostart
for app in update-notifier ubuntu-advantage-notification ubuntu-report-on-upgrade; do
    printf '[Desktop Entry]\nType=Application\nName=%s\nHidden=true\n' "$app" \
        > "/home/tester/.config/autostart/$app.desktop"
done
chown -R tester:tester /home/tester/.config
pkill -x update-notifier 2>/dev/null || true

# `desk reboot` reboots even when apps with unsaved work block shutdown (and reports them).
cat > /etc/polkit-1/rules.d/10-desk-root-ignore-inhibitors.rules <<'RULE'
polkit.addRule(function(action, subject) {
    if (subject.user == "root" && /^org\.freedesktop\.login1\.(reboot|power-off)-ignore-inhibit$/.test(action.id))
        return polkit.Result.YES;
});
RULE
