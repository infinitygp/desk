#!/bin/bash
# Plasma's part of provisioning: runs as root in the booted golden image right before the warm
# state is saved, after the settings every desktop image gets (images/shared).
set -euo pipefail
as_tester() {
    runuser -u tester -- env XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus "$@"
}
kwrite() { as_tester kwriteconfig6 "$@"; }

# Stable screen for screenshots: no lock, blanking, dimming or suspend, no animations or blinking
# text cursor.
kwrite --file kscreenlockerrc --group Daemon --key Autolock false
kwrite --file kscreenlockerrc --group Daemon --key LockOnResume false
kwrite --file kscreenlockerrc --group Daemon --key Timeout 0
for profile in AC Battery LowBattery; do
    kwrite --file powerdevilrc --group "$profile" --group Display --key DimDisplayWhenIdle false
    kwrite --file powerdevilrc --group "$profile" --group Display --key TurnOffDisplayWhenIdle false
    kwrite --file powerdevilrc --group "$profile" --group SuspendAndShutdown --key AutoSuspendAction 0
done
kwrite --file kdeglobals --group KDE --key AnimationDurationFactor 0
kwrite --file kdeglobals --group KDE --key CursorBlinkRate 0
# KWrite opens new windows on its welcome page, where nothing typed lands in a document.
kwrite --file kwriterc --group General --key "Show welcome view for new window" false
# File indexing works in the background at random moments.
kwrite --file baloofilerc --group "Basic Settings" --key Indexing-Enabled false

# The keyboard layout desk types through (us, or pl: Polish (programmer's)), from desk.
kwrite --file kxkbrc --group Layout --key Use true
kwrite --file kxkbrc --group Layout --key LayoutList "$DESK_LAYOUT"
