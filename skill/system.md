# desk: Monitors, extra disks, USB devices, the guest clock, system upgrades

Part of the desk skill ([SKILL.md](SKILL.md)); the commands and pitfalls there apply.

## Monitors: resolution, scaling, several displays

Runs start at 1280x800, 100 %, one monitor. To test layouts and DPI handling:

- `desk up IMAGE --resolution 1920x1080` (boots cold: the monitor offers that mode),
  `desk display --resolution WxH` switches among the modes it offers.
- `--scale 150` on `desk up` or `desk display`: Windows applies it by signing in again (a reboot,
  ~70 s; desk checks the session's DPI afterwards); GNOME at once, fractional scales too. Coordinates from `desk a11y` stay screen pixels.
- `desk up windows-11 --displays 2` (Windows only; boots cold, ~5 min): monitors side by side,
  left to right, top-aligned. Screenshots show them as one picture, and `desk a11y`, `desk click X Y`
  and `desk drag` use the same coordinates, across monitors too. On the primary monitor input
  comes from outside the guest as usual; on the others the in-session agent moves the pointer
  (Windows maps QEMU's tablet onto the primary monitor only), so elevated windows there do not
  get clicks. `desk display` prints the monitors.

## Extra disks

For backup targets, a second data disk or a disk to fill up: `desk disk add 10G --name backup`
(or `desk up IMAGE --disk 10G:backup`) plugs an empty, thin disk into the running guest, as a USB
disk would be. Linux sees it as `/dev/disk/by-id/virtio-backup`; on Windows it is the disk with
serial number `backup`, online but not initialized (`Initialize-Disk`, `New-Partition`,
`Format-Volume`). `desk disk rm NAME` unplugs and deletes it; the guest is not asked, so unmount
first unless pulling a disk out is what you test. `desk disk ls` lists them; snapshots include them.

## Host USB devices

For hardware the product talks to (a Stream Deck, a phone, a dongle, a USB stick): `desk usb ls`
lists the host's devices (VENDOR:PRODUCT, BUS.DEVICE, name, the run that has it), and
`desk usb add 0fd9:0084` (or `desk up IMAGE --usb 0fd9:0084`, in a scenario `usb = ["0fd9:0084"]`)
plugs it into the guest's USB controller as it is. The host does without it until `desk usb rm` or
`desk down`, so hand over only the device the user named: a phone given to the guest is gone from
the host's adb meanwhile. QEMU runs as the desk user, so the device node must be the user's to
open; otherwise `desk usb add` refuses and prints a udev rule as a fish command with sudo: give it
to the user to run. A device that re-enumerates (a firmware update, a phone switching USB mode)
gets a new device number on the host: `desk usb add` it again.

## Guest clock

For what depends on time (schedules, timers, expiry, retention): `desk clock +3d` (also `-2h30m`;
units s m h d w) or `desk clock '2026-12-24 23:59'` (guest local time: the host's time zone unless
its `config.toml` sets one) moves the guest's clock, which keeps running from there; `desk clock`
shows it. The guest's own time sync is
switched off (timedatectl / w32time) and desk keeps the offset across snapshot restores and
`desk reboot`. `desk clock reset` goes back to the host's time and turns time sync on again.
Timers that were due in the skipped time fire right after the jump, as after a suspend, but each
only once however many of its times were skipped. To let a daily job run for every day, move in
steps: `desk clock +3d --step 1d` jumps a day at a time, waits each time until what the timers
started has finished, and prints which units ran (in a scenario: `clock = "+3d"`, `step = "1d"`).

## Surviving a system upgrade

Whether a package still works after the user upgrades Fedora: install it on the older release's
image (`fedora-44-gnome`, `fedora-44-gnome-cockpit`), check it works, then upgrade like a user would and
check again (package still installed, services and timers still enabled, the UI still works):

```bash
desk exec --root -- 'dnf system-upgrade download -y --releasever=45'
desk reboot --command 'dnf offline reboot -y' --timeout 3600   # boots into the upgrade, then the new system
```

In a scenario: `root = "dnf system-upgrade download -y --releasever=45"` (with `timeout = 1800`),
then `reboot = "dnf offline reboot -y"` with `timeout = 3600`. After the reboot only the desktop
session is back: programs open before it have to be started again; on `fedora-44-gnome-cockpit` a
`prepare = true` step (or `desk prepare`) opens Firefox logged in to Cockpit again. The upgrade itself
takes about 10 minutes (download ~2 min, offline upgrade and boot ~7 min).
`fedora-45-gnome` is the next release itself (a beta until it is out).
