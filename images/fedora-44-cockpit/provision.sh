#!/bin/bash
# Cockpit on top of fedora-44-gnome's provisioning; prepare.py then logs in to it in Firefox.
set -euo pipefail
dnf install -y -q cockpit >/dev/null 2>&1
/usr/libexec/cockpit-certificate-ensure
# Restoring the warm state moves the clock forward by hours, which would end an idle session.
printf '[Session]\nIdleTimeout = 0\n' > /etc/cockpit/cockpit.conf
systemctl enable --now cockpit.socket

# Firefox trusts Cockpit's own CA, so the login page opens without the certificate warning, and
# no first-run pages (the privacy notice comes with telemetry), offers to save the password or
# other tabs get in the way of the page under test.
install -d /etc/firefox/policies
cat > /etc/firefox/policies/policies.json <<'JSON'
{
  "policies": {
    "Certificates": {"Install": ["/etc/cockpit/ws-certs.d/0-self-signed-ca.pem"]},
    "OverrideFirstRunPage": "",
    "OverridePostUpdatePage": "",
    "DontCheckDefaultBrowser": true,
    "DisableTelemetry": true,
    "OfferToSaveLogins": false,
    "UserMessaging": {"SkipOnboarding": true, "MoreFromMozilla": false, "FeatureRecommendations": false},
    "Preferences": {"browser.sessionstore.resume_from_crash": {"Value": false, "Status": "default"}}
  }
}
JSON
