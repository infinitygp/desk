#!/bin/bash
# Runs as root in the booted golden image of a GNOME image (Fedora, Arch) right before the warm
# state is saved, and after cold boots without one. This directory is in /tmp/desk-provision/files.
set -euo pipefail
FILES=/tmp/desk-provision/files
. /etc/os-release

install -d /usr/share/gnome-shell/extensions/desk-helper@desk.local
install -m 0644 "$FILES"/desk-helper@desk.local/* /usr/share/gnome-shell/extensions/desk-helper@desk.local/

# Pages' web console into the journal (desk browser console); also for a Firefox installed later.
case $ID in
    fedora) firefox=/usr/lib64/firefox ;;
    *) firefox=/usr/lib/firefox ;;
esac
install -D -m 0644 "$FILES"/desk-firefox.js "$firefox"/browser/defaults/preferences/desk-firefox.js
# The thrown object and stack of uncaught exceptions as well (desk.cfg, Firefox's autoconfig).
install -D -m 0644 "$FILES"/desk-autoconfig.js "$firefox"/defaults/pref/desk-autoconfig.js
install -D -m 0644 "$FILES"/desk.cfg "$firefox"/desk.cfg
# No first-run tabs (the privacy notice comes with telemetry), default browser question, offers
# to save passwords, studies or tips in the way of the page under test.
install -D -m 0644 "$FILES"/desk-firefox-policies.json /etc/firefox/policies/policies.json
# Chromium likewise: policies, and flags its launcher adds to every start (what each is for is in
# desk-chromium-flags). Fedora's wrapper reads them from chromium.conf, where the line is replaced
# when provisioning runs again; Arch's launcher from a file of their own.
install -D -m 0644 "$FILES"/desk-chromium-policies.json /etc/chromium/policies/managed/desk.json
if [ -f /etc/chromium/chromium.conf ]; then
    sed -i '/# desk-chromium-flags$/d' /etc/chromium/chromium.conf
    echo "CHROMIUM_FLAGS+=\" $(grep -v '^#' "$FILES"/desk-chromium-flags | xargs)\"  # desk-chromium-flags" \
        >> /etc/chromium/chromium.conf
else
    install -m 0644 "$FILES"/desk-chromium-flags /etc/chromium-flags.conf
fi

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
enabled-extensions=['desk-helper@desk.local']
CONF
dconf update

# The host's time zone and the keyboard layout desk types through (us, or pl: Polish
# (programmer's), the US layout plus AltGr letters), from desk. GNOME copied the layout into the
# user's settings at first login, so set it there as well.
timedatectl set-timezone "$DESK_TIMEZONE"
localectl set-x11-keymap "$DESK_LAYOUT"
as_tester() { runuser -u tester -- env XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus "$@"; }
as_tester gsettings set org.gnome.desktop.input-sources sources "[('xkb', '$DESK_LAYOUT')]"

# The accessibility tree (`desk a11y`). GTK4 exposes it regardless; Gecko, Chromium/Electron
# and Qt only when an assistive technology is announced (org.a11y.Status) or when forced
# through the environment. GNOME writes toolkit-accessibility=false into the user's settings
# at first login, so the system default alone is not enough.
as_tester gsettings set org.gnome.desktop.interface toolkit-accessibility true
install -d -o tester -g tester /home/tester/.config /home/tester/.config/environment.d
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

# Maintenance timers catch up after the clock jumps forward on restore, each after a random delay,
# so they ran at random moments of a test: fstrim, plocate's indexing, logrotate, dnf's metadata,
# the Arch keyring's sync over the network, shadow's checks. A test of one of them starts it by hand.
for t in dnf-makecache.timer fstrim.timer logrotate.timer plocate-updatedb.timer raid-check.timer \
         unbound-anchor.timer archlinux-keyring-wkd-sync.timer shadow.timer; do
    if systemctl cat "$t" >/dev/null 2>&1; then systemctl disable --now "$t"; fi
done

# GNOME Software's update banners and prompts pop up at random moments over the app under test.
# GNOME 50 starts it as a user service of every session (gnome-session-services.target) and on
# D-Bus activation; masking stops both. Stopped now too: this session already runs it.
systemctl --global mask gnome-software.service
as_tester systemctl --user daemon-reload
as_tester systemctl --user stop gnome-software.service || true
if pgrep -x gnome-software; then echo "gnome-software still runs" >&2; exit 1; fi

# `desk reboot` reboots even when apps with unsaved work block shutdown (and reports them);
# logind only lets root override that with a rule like this one, which systemd ships as an example.
cat > /etc/polkit-1/rules.d/10-desk-root-ignore-inhibitors.rules <<'RULE'
polkit.addRule(function(action, subject) {
    if (subject.user == "root" && /^org\.freedesktop\.login1\.(reboot|power-off)-ignore-inhibit$/.test(action.id))
        return polkit.Result.YES;
});
RULE
