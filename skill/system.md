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
lists the host's devices (VENDOR:PRODUCT, BUS.DEVICE, port, name, the run that has it), and
`desk usb add 0fd9:0084` (or `desk up IMAGE --usb 0fd9:0084`, in a scenario `usb = ["0fd9:0084"]`)
plugs it into the guest's USB controller as it is. The host does without it until `desk usb rm` or
`desk down`, so hand over only the device the user named: a phone given to the guest is gone from
the host's adb meanwhile. QEMU runs as the desk user, so the device node must be the user's to
open; otherwise `desk usb add` refuses and prints a udev rule as a fish command with sudo: give it
to the user to run. A device that re-enumerates (a firmware update, a phone switching USB mode)
gets a new device number on the host: `desk usb add` it again. Its old entry stays in the guest
and holds a USB port; `desk usb ls` lists it as `gone`, and the next `desk usb add` in that run
takes it out. Name a device by what does not change with its number: `port:9-2.2` (the host port
it is plugged into, as `desk usb ls` shows) or `serial:SERIAL`; BUS.DEVICE is looked up each time.
Two devices of one model need one of these, and a name that fits several devices or none is
refused with the ones it fits. In a scenario, `usb_rm` and `usb_add` steps take a device out and
put it back between other steps (a hot-plug or reconnect test).

Host passthrough needs QEMU's `usb-host` backend (`qemu-hw-usb-host` on Arch,
`qemu-device-usb-host` on Fedora), not just a guest USB controller. `desk doctor` lists this
optional feature. `usb add` and `up --usb` refuse early with install hints if QEMU does not
advertise it or the capability probe fails; `up --usb` does so before starting a VM.
`usb ls`, `usb rm` and ordinary runs still work without it.

### Hubs and devices at port paths (usbvfiod)

Some products tell hardware by the hubs it sits behind (a base with modules). For those, and for
devices that an emulator serves instead of hardware (usbvfiod's external devices), a run can have
a second USB controller where hubs and devices stand at **port paths**:

```fish
desk up windows-11 --usbvfiod --name usb1           # boots cold (Windows 11: ~20 s, a warm start is ~1 s)
desk -r usb1 usb socket                             # the hotplug socket, for an emulator or usbvfiod's remote
desk -r usb1 usb add 0fd9:008f --at 5.1.4 --hub 5=067b:2586/4/hs --hub 5.1=067b:2586/4/hs
desk usb ls                                         # also the hubs and devices in each controller's tree
desk -r usb1 usb rm 0fd9:008f                       # or at:5.1 (a hub too: everything below it goes)
```

- A port path is the ports from the root port down: `5.1.4` is port 4 of the hub on port 1 of the
  hub on root port 5. Root ports 1-4 are USB 3, 5-8 USB 2; hubs stand on 5-8, at most five deep,
  up to 15 ports each. `--hub PATH=[VID:PID][/PORTS][/hs|/fs]` says what a hub on the way is;
  without it a missing hub is a 0409:55aa hub with 8 ports at high speed (a high speed device
  needs high speed hubs, and a high speed hub cannot stand below a full speed one). A hub that
  exists keeps what it is. Without `--at` the device takes the first free root port that fits its
  speed; `desk usb ls` shows where.
- The same rules as for any host device apply (give only the device the user named, the node must be
  the user's to open, `desk usb rm` or `desk down` gives it back). `usb add --at` on a run without
  the controller is refused, as is a snapshot of one with it (the controller is added to the
  running guest, so a restore would not have it).
- `desk ls` says "usbvfiod ended" when the server is gone: the guest has lost the controller, and
  QEMU can abort the VM with it. The run cannot be repaired; `desk down` it (the report has the
  server's log). Never kill the server (`usbvfiod`) of a run yourself while its VM runs.
- The server is `usbvfiod` from `$DESK_USBVFIOD` or `PATH`; `desk doctor` says whether it and QEMU's
  `vfio-user-pci` are there. In a scenario, `usbvfiod = true` at the top starts the run with the
  controller, and `usb_add` takes `at` and `hubs`, `usb_rm` takes `at:PORTPATH`.
- The run's directory has `usbvfiod/usbvfiod.log` and `usbvfiod/usb.pcap` (what the guest's driver
  sent to the devices); the report keeps both.

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
