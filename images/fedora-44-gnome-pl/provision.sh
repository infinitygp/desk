#!/bin/bash
# Polish locale on top of fedora-44-gnome's provisioning; the relogin that follows applies it.
set -euo pipefail
dnf install -y -q glibc-langpack-pl langpacks-pl >/dev/null 2>&1
localectl set-locale LANG=pl_PL.UTF-8
# The session language comes from AccountsService, formats from the user's GNOME settings.
busctl call org.freedesktop.Accounts /org/freedesktop/Accounts/User1000 \
    org.freedesktop.Accounts.User SetLanguage s pl_PL.UTF-8
runuser -u tester -- env DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus \
    gsettings set org.gnome.system.locale region 'pl_PL.UTF-8'
# Folder names stay as installed; xdg-user-dirs would otherwise offer to rename them.
runuser -u tester -- sh -c 'echo en_US > ~/.config/user-dirs.locale'

# Firefox: Fedora's wrapper links the langpack into ~/.config/mozilla/extensions, which Firefox
# 150 does not pick up (seen on fc44, not investigated further), so a fresh profile stays in
# English. An enterprise policy makes the browser Polish the way a Polish user expects.
install -d /usr/lib64/firefox/distribution
cat > /usr/lib64/firefox/distribution/policies.json <<'JSON'
{
  "policies": {
    "RequestedLocales": ["pl"],
    "ExtensionSettings": {
      "langpack-pl@firefox.mozilla.org": {
        "installation_mode": "force_installed",
        "install_url": "file:///usr/lib64/firefox/langpacks/langpack-pl@firefox.mozilla.org.xpi"
      }
    }
  }
}
JSON
