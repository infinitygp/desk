#!/bin/bash
# Runs as root in the booted golden image right before the warm snapshot is saved.
set -euo pipefail

# A blinking text cursor would keep the screen from ever settling.
cat > /etc/dconf/db/local.d/01-desk-provision <<'CONF'
[org/gnome/desktop/interface]
cursor-blink=false
CONF
dconf update

# No machine-check exceptions in a VM; its failed unit would show up in every health check.
systemctl disable --now mcelog.service
systemctl reset-failed
