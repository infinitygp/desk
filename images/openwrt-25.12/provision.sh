#!/bin/sh
# OpenWrt 25.12: run by desk as root with BusyBox ash (no bash on OpenWrt) at every warm-up.
set -e

apk update >/dev/null
# The guest agent (desk clock, exec --agent, the clock after a restore) and SFTP, which scp
# (desk push/pull, scenario artifacts) speaks.
apk add qemu-ga openssh-sftp-server >/dev/null

# The agent's port shows up under /dev/virtio-ports through hotplug, at the next boot only;
# link it now and start the agent.
for p in /sys/class/virtio-ports/*; do
    name=$(cat "$p/name" 2>/dev/null) || continue
    [ -n "$name" ] || continue
    mkdir -p /dev/virtio-ports
    ln -sf "/dev/${p##*/}" "/dev/virtio-ports/$name"
done
/etc/init.d/qemu-ga enable
/etc/init.d/qemu-ga restart

# The system log in a file as well, kept across reboots (logd's buffer is in memory), for the
# report; and the installed packages as provisioned, which the report compares with.
mkdir -p /desk
uci set system.@system[0].log_file='/desk/syslog'
uci set system.@system[0].log_size='1024'
# The host's time zone, from desk; with no zone files OpenWrt takes it as a POSIX rule.
uci set system.@system[0].zonename="$DESK_TIMEZONE"
uci set system.@system[0].timezone="$DESK_POSIX_TZ"
uci commit system
/etc/init.d/system reload
/etc/init.d/log restart
apk list -I 2>/dev/null | cut -d' ' -f1 | sort > /desk/packages
