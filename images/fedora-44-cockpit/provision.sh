#!/bin/bash
# Cockpit on top of fedora-44-gnome's provisioning; prepare.py then logs in to it in Firefox.
set -euo pipefail
dnf install -y -q cockpit >/dev/null 2>&1
# A CA of the image's own signs the certificate Cockpit serves (the last one in ws-certs.d), and
# Firefox trusts that CA: Cockpit 368 no longer keeps a CA next to its self-signed certificate.
certs=/etc/cockpit/ws-certs.d
install -d -m 0755 "$certs"
[ -s /etc/cockpit/desk-ca.pem ] || openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=desk test CA" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -keyout /etc/cockpit/desk-ca.key -out /etc/cockpit/desk-ca.pem 2>/dev/null
openssl req -newkey rsa:2048 -nodes -subj "/CN=localhost" -keyout "$certs/50-desk.key" -out /tmp/desk.csr 2>/dev/null
printf 'subjectAltName=DNS:localhost,IP:127.0.0.1,DNS:%s\nbasicConstraints=critical,CA:FALSE\nextendedKeyUsage=serverAuth\n' \
    "$(hostname)" > /tmp/desk-cert.ext
openssl x509 -req -in /tmp/desk.csr -CA /etc/cockpit/desk-ca.pem -CAkey /etc/cockpit/desk-ca.key -CAcreateserial \
    -days 3650 -extfile /tmp/desk-cert.ext -out "$certs/50-desk.cert" 2>/dev/null
chmod 0600 /etc/cockpit/desk-ca.key "$certs/50-desk.key"
rm -f /tmp/desk.csr /tmp/desk-cert.ext
# Restoring the warm state moves the clock forward by hours, which would end an idle session.
printf '[Session]\nIdleTimeout = 0\n' > /etc/cockpit/cockpit.conf
systemctl enable --now cockpit.socket
# A Cockpit already running (provisioning again after a cold boot) picks up the certificate.
systemctl try-restart cockpit.service

# Firefox trusts the CA above, so the login page opens without the certificate warning; the rest
# of its policies come from fedora-44-gnome's provisioning.
python3 - <<'PY'
import json
path = "/etc/firefox/policies/policies.json"
with open(path) as f:
    data = json.load(f)
data["policies"]["Certificates"] = {"Install": ["/etc/cockpit/desk-ca.pem"]}
with open(path, "w") as f:
    json.dump(data, f, indent=2)
PY
