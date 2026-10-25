#!/bin/bash
# What every Linux image with a desktop gets, whichever desktop it runs: runs as root in the
# booted golden image right before the warm state is saved (and after cold boots without one),
# before the desktop's own provisioning. Its files are in /tmp/desk-provision/files.
set -euo pipefail
FILES=/tmp/desk-provision/files
. /etc/os-release

# Pages' web console into the journal (desk browser console); also for a Firefox installed later.
case $ID in
    fedora) firefox=/usr/lib64/firefox ;;
    debian) firefox=/usr/lib/firefox-esr ;;
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
# when provisioning runs again; Debian's from /etc/chromium.d; Arch's launcher from a file of
# their own.
flags=$(grep -v '^#' "$FILES"/desk-chromium-flags | xargs)
install -D -m 0644 "$FILES"/desk-chromium-policies.json /etc/chromium/policies/managed/desk.json
if [ -f /etc/chromium/chromium.conf ]; then
    sed -i '/# desk-chromium-flags$/d' /etc/chromium/chromium.conf
    echo "CHROMIUM_FLAGS+=\" $flags\"  # desk-chromium-flags" >> /etc/chromium/chromium.conf
elif [ -d /etc/chromium.d ]; then
    echo "export CHROMIUM_FLAGS=\"\$CHROMIUM_FLAGS $flags\"" > /etc/chromium.d/desk
else
    install -m 0644 "$FILES"/desk-chromium-flags /etc/chromium-flags.conf
fi

# The host's time zone and the keyboard layout desk types through (us, or pl: Polish
# (programmer's), the US layout plus AltGr letters), from desk; desktops that keep a layout of
# their own set it there too.
timedatectl set-timezone "$DESK_TIMEZONE"
case $ID in
    # Their keyboard settings live in /etc/default/keyboard, which Ubuntu's localed refuses to
    # change for root over SSH ("Access denied").
    debian | ubuntu)
        [ -f /etc/default/keyboard ] || printf 'XKBMODEL="pc105"\nXKBLAYOUT="us"\nXKBVARIANT=""\nXKBOPTIONS=""\n' \
            > /etc/default/keyboard
        sed -i "s/^XKBLAYOUT=.*/XKBLAYOUT=\"$DESK_LAYOUT\"/" /etc/default/keyboard
        ;;
    *) localectl set-x11-keymap "$DESK_LAYOUT" ;;
esac

# The accessibility tree (`desk a11y`). GTK4 exposes it regardless; Gecko, Chromium/Electron
# and Qt only when an assistive technology is announced (org.a11y.Status) or when forced
# through the environment.
install -d -o tester -g tester /home/tester/.config /home/tester/.config/environment.d
cat > /home/tester/.config/environment.d/50-desk-a11y.conf <<'CONF'
GNOME_ACCESSIBILITY=1
QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1
QT_ACCESSIBILITY=1
CONF
chown -R tester:tester /home/tester/.config/environment.d

# What pops up over the app under test at login: update notifiers, welcome tours, usage and
# subscription prompts. A test of one of them starts it by hand.
install -d -o tester -g tester /home/tester/.config/autostart
for entry in update-notifier ubuntu-advantage-notification ubuntu-report-on-upgrade org.kde.discover.notifier \
             org.kde.plasma-welcome light-locker; do
    if [ -f "/etc/xdg/autostart/$entry.desktop" ]; then
        printf '[Desktop Entry]\nHidden=true\n' > "/home/tester/.config/autostart/$entry.desktop"
    fi
done
chown -R tester:tester /home/tester/.config/autostart

# No machine-check exceptions in a VM; its failed unit would show up in every health check.
if systemctl cat mcelog.service >/dev/null 2>&1; then
    systemctl disable --now mcelog.service
fi
systemctl reset-failed

# Maintenance timers catch up after the clock jumps forward on restore, each after a random delay,
# so they ran at random moments of a test: fstrim, plocate's and man-db's indexing, logrotate, the
# package lists of dnf and apt, the Arch keyring's sync over the network, shadow's checks. A test of
# one of them starts it by hand.
for t in dnf-makecache.timer fstrim.timer logrotate.timer plocate-updatedb.timer raid-check.timer \
         unbound-anchor.timer archlinux-keyring-wkd-sync.timer shadow.timer apt-daily.timer \
         apt-daily-upgrade.timer man-db.timer e2scrub_all.timer dpkg-db-backup.timer apt-listchanges.timer \
         motd-news.timer update-notifier-download.timer update-notifier-motd.timer apport-autoreport.timer \
         fwupd-refresh.timer ua-timer.timer xfs_scrub_all.timer; do
    if systemctl cat "$t" >/dev/null 2>&1; then systemctl disable --now "$t"; fi
done

# `desk reboot` reboots even when apps with unsaved work block shutdown (and reports them);
# logind only lets root override that with a rule like this one, which systemd ships as an example.
cat > /etc/polkit-1/rules.d/10-desk-root-ignore-inhibitors.rules <<'RULE'
polkit.addRule(function(action, subject) {
    if (subject.user == "root" && /^org\.freedesktop\.login1\.(reboot|power-off)-ignore-inhibit$/.test(action.id))
        return polkit.Result.YES;
});
RULE
