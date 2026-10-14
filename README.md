# desk

`desk`: disposable desktop VMs that an agent drives through screenshots and real
mouse/keyboard input, for testing desktop apps and system packages end to end.

- Nested KVM, libvirt `qemu:///session` (no root), passt networking.
- Out of band: screenshots through libvirt, pointer and keyboard through QMP, so it works
  regardless of what runs in the guest (login screen, Wayland, polkit, crashes).
- In band: SSH as the desktop user, qemu-guest-agent as root.
- Accessibility tree (AT-SPI) with refs that map to screen pixels; a GNOME Shell extension in
  the image supplies window positions, which AT-SPI on Wayland does not know.
- Golden image -> warm state (saved RAM of a logged-in desktop) -> per-run qcow2 overlay.
  `desk up` restores in seconds, `desk down` discards everything.

Agent instructions: [skill/SKILL.md](skill/SKILL.md), linked as `~/.agents/skills/desk` and
`~/.claude/skills/desk`; `bin/desk` is linked into `~/.local/bin`.

## Layout

```text
bin/desk             entry point (the system's python3 with the distribution's libvirt bindings)
skill/SKILL.md       agent skill
desk/                harness code (guest_a11y.py runs inside the guest)
tests/               unit tests
images/<name>/       image.toml, kickstart, provision.sh
<home>/              base/, warm/, runs/, reports/, maintain/, cache/, logs/, keys/  (see Host setup)
```

## Images

A host keeps the images it tests on: none is required, and nothing builds them unasked.
`desk image list` and `desk doctor` show which are there.

| Image | Install | Notes |
|---|---|---|
| `fedora-44-gnome` | kickstart over the network | default |
| `fedora-45-gnome` | kickstart, the beta's installer tree, the release's packages | next release; follows it once it is out |
| `fedora-44-gnome-pl` | variant of `fedora-44-gnome` | Polish locale; shares the base disk, own warm state |
| `fedora-44-cockpit` | variant of `fedora-44-gnome` | Cockpit, Firefox logged in with administrative access |
| `arch-gnome` | official cloud image, GNOME installed by cloud-init (NoCloud seed) | rolling; no Secure Boot |
| `windows-11` | evaluation ISO + autounattend.xml on a second CD | 90-day evaluation, reinstalled by `desk maintain` |
| `openwrt-25.12` | the release's disk image (x86-64 ext4 combined, EFI), grown and preconfigured offline | no desktop: BusyBox ash, procd, apk, LuCI |

Windows specifics: the ISO, the virtio-win ISO and the Win32-OpenSSH MSI are downloaded into
`cache/` when missing and checked against the SHA-256 pinned in `image.toml`;
no TPM (LabConfig bypass), virtio disk with the viostor driver loaded by Setup
(QEMU 10.2's AHCI emulation crashes under Windows), e1000e NIC (inbox driver),
OpenSSH from the Win32-OpenSSH MSI with PowerShell as the shell, virtio-win guest tools for the
display driver and qemu-guest-agent, and an agent in the logged-in session
(`images/_shared/windows/agent.ps1`, port forwarded like SSH) for UI Automation and starting
programs on the desktop, which SSH sessions cannot see. The agent serves one request at a time,
so nothing may hold it: a connection that sends nothing is dropped after 5 s, a tree walk ends
after 20 s, and a program that is waited for (`exec --gui --wait`) is waited for on a thread of
its own. Commands are sent as
`powershell -EncodedCommand`, since Windows' command line mangles quotes.
Provisioning opts in to early feature updates and enables the taskbar position and size
features (59213768, 61090762) with ViVeTool v0.3.4 (GPL-3.0, downloaded and checked against a
pinned SHA-256), so `desk win taskbar --edge/--size` work on every build; `desk/winsettings.py`
writes theme and taskbar settings where the Settings app writes them.

OpenWrt specifics (`installer = "disk"`, `family = "openwrt"`, `desktop = false`): the image
from `disk_image_url` is checked against the `sha256sums` next to it, its last partition and ext4
grown to `disk_gb` (parted, guestfish) and `disk_files` written onto it: the SSH key, a
uci-defaults script (LAN as a DHCP client on passt, so SSH and LuCI come in through the LAN zone;
the Warsaw time zone; root's password) and an `rc.local` that powers the first boot off, which ends
the install (~30 s). Provisioning (`sh -s`, no bash) adds qemu-ga and the SFTP server (scp) and keeps
the system log in `/desk/syslog`. Without a desktop, commands that need one refuse
(`ImageSpec.need_desktop`); screenshots show the text console. `desk exec`'s time limit is a
`setsid` watchdog (BusyBox has no `timeout`), and `desk forward` leads to the LAN address, where
router services listen.

```bash
bin/desk image build fedora-44-gnome   # unattended install, ~10 min, needs network
bin/desk image warm fedora-44-gnome    # boot, provision, save the running desktop
bin/desk image shot windows-11         # look at a graphical installer while it runs
bin/desk image list                    # state, build date, last selftest, licence, problems
```

Shared provisioning files live in `images/_shared/`. A variant (`base = "..."` in its
`image.toml`) inherits the base image's settings and adds provisioning scripts.

`prepare = "prepare.py"` in `image.toml` runs `prepare(steps)` on the host once the provisioned
session is up, before the warm state is saved (and after cold boots): what a user does before the
tests start, with real input (`desk/steps.py`: `gui`, `wait_for`, `click`, `type`, `key`).
`fedora-44-cockpit` logs in to Cockpit that way; Cockpit serves a certificate from a CA of the
image's own, which its Firefox trusts (Cockpit 368 keeps no CA of its own any more), and the
session has no idle timeout, since restoring the warm state moves the clock forward.

## Builds and maintenance

Every install and every warm state is a build of its own: `base/<image>.<version>/` and
`warm/<image>.<version>/`, with `base/<image>` and `warm/<image>` symlinks to the current ones.
Runs pin the builds they start from, so a new build never disturbs a running or kept run;
`desk gc` deletes builds nothing uses any more.

`desk maintain` reinstalls an image family (a base image and its variants) when the base is
`rebuild_days` old (Fedora and Arch: 14), when its install definition changed or when its licence
(`expiry`, Windows evaluation) has less than 14 days left, and re-warms images whose definition
changed. New builds are selftested next to the current ones and become current only when every
member of the family passes; otherwise the current builds stay, and `desk up` and
`desk image list` show the failure. Reports and screenshots of failures go to
`<home>/maintain/<date>/` (an install that hangs: `logs/build-<image>-stuck.png`, with its serial
console in `logs/build-<image>.log`). An install may take 3 hours before it counts as stuck; the
whole run 12 hours. An install is kept only if it powered off by itself and, where the image
names an `install_marker` (Windows: `firstboot.ps1` reports on the serial console), said that its
setup succeeded.

Maintenance is an offer, not a duty. It looks after the images in use on the host, which are
those with a warm state, and installs or warms no other: `desk maintain --only NAME` is how an
image (or a variant's warm state) that is not there yet gets built and tested in one go. And it
runs when somebody runs it: a host that should renew its images by itself gets the weekly
timer; one without it has images as old as their last build.

Builds of images removed from `images/` are not deleted automatically.

```bash
bin/desk maintain --dry-run         # what is due
bin/desk maintain --only windows-11 # one image family, now
bin/desk maintain --install-timer   # optional: weekly, Monday 03:00 (systemd user timer, needs lingering)
systemctl --user list-timers desk-maintain.timer
journalctl --user -u desk-maintain  # output of the last runs
```

`desk up` warns three weeks before a licence runs out and refuses an expired image.

## Monitors

`desk up --resolution/--scale/--displays` and `desk display` (desk/monitors.py). Each monitor is
a virtio GPU of its own (Windows' viogpudo drives one head per GPU, and QEMU leaves further heads
of a GPU disconnected without a display UI); this needs QEMU's `virtio-gpu-pci` module. Screens
are put side by side into one screenshot, pointer events go to the console of the monitor under
the point, and on Windows, whose absolute tablet only reaches the primary monitor, operations on
other monitors go through the agent. Windows scales by DPI after signing in again; GNOME scales
through `gdctl` in the logical layout, and desk turns the logical positions toolkits report into
screen pixels. Several monitors are refused on GNOME guests: mutter never updates a second GPU's
monitor without 3D acceleration.

## Snapshots

`desk snapshot save|restore|list|delete` works like the warm state, for a running run: the
guest's memory is saved (the VM stops for a few seconds), disk and firmware variables are reflink
copies, and the run carries on restored from the saved memory. Restoring destroys the domain,
puts the copies back and restores the memory. Snapshots live in `runs/<id>/snapshots/`.

`desk disk add|rm|ls` hot-plugs empty qcow2 disks (`runs/<id>/disks/NAME.qcow2`) as virtio-blk
into the spare PCIe root ports, serial = name; snapshots copy them along.
Firefox in the Linux images writes every page's console and uncaught errors to its output, so to
the journal (`images/_shared/gnome/desk-firefox.js`); `desk browser console` and the report read
them from there. Promises keep their rejection stack without the devtools open, and an autoconfig
script (`desk.cfg`, loaded into every content process) adds the thrown value and stack of each
uncaught exception as a `JavaScript error detail:` line, which desk joins to its error. Addresses
opened from outside (`desk browser open`, `firefox URL`) replace the current tab. The Remote Agent
(WebDriver BiDi) is not used, as it marks the browser window; `desk browser viewport` types sizes
into the responsive design mode instead.

`desk clock` (`desk/clock.py`) sets the guest's time through qemu-guest-agent with the guest's
time sync off and keeps the offset in `run.json`, reapplied after snapshot restores and reboots
(tried for 20 s, as libvirt now and then sees the agent a moment late after a restore; a moved
clock that could not be put back is an error, not a warning).

## Clicking and typing

`desk click @ref|--name` asks the application, through AT-SPI hit tests, what is at the point it
is about to click: an element there that is not the target or inside it (a sticky footer, a
toast) counts as covering it, and another point of the target or scrolling is tried. Firefox does
not mark the buttons of sticky footers as showing; when the hit test reaches the footer around
them, they count as visible. `desk a11y` marks covered elements. `desk set-text` focuses the
field, selects all and types (or pastes through the shell extension's `SetClipboard`), since
AT-SPI's text replacement leaves web pages' input events out. `desk type` into a browser window
whose focus is not in a text field is refused. Walks of the tree stop after 20 s.

`desk forward` keeps an SSH tunnel per guest port in a background process
(`python -m desk.forward`), restarted when SSH drops, stopped by `desk down`.

## Exit codes, locks, limits

- 0: done. 1: the check a command stands for failed (a scenario step, `selftest`, `find`,
  `wait`, `doctor`). 2: desk could not do what was asked; one line says why, also when a host tool,
  libvirt or the disk failed (`DESK_DEBUG=1` adds the traceback). 130: interrupted. `desk exec`
  passes on the guest command's code, 124 when it ran out of time (`push`/`pull` too).
- The state lock (`<home>/.lock`) is held while a domain is started or destroyed, not while a
  guest boots or a report is written, so a cold boot does not hold up other runs. A command that
  waits for a lock says who holds it and gives up after 15 minutes (an image's lock: 6 hours).
- What a command in the guest may take is enforced in the guest (`timeout`, a watchdog on
  BusyBox and Windows), for `desk exec`, `desk wait --until-exec` and scenario steps alike.

## Scenarios

`desk scenario run FILE` runs a repeatable end-to-end test described in TOML (format in
`skill/SKILL.md`): build on the host, copy the artifacts in, start a run, steps with the same real
input as the CLI (`desk/steps.py`), report with the steps in its timeline, exit code 0/1.
Projects keep theirs in `test/desk/`.

## Watching

- `desk view`: every run has a VNC display on 127.0.0.1 with a per-run password; the command
  prints the SSH tunnel to use from a workstation.

## Evidence

`desk down` writes `reports/<run>/report.md` (failed units, unit failures from the journal since
the start across reboots, SELinux denials, coredumps, journal warnings, package changes with dnf's
UTC times made local, timeline with screenshots); `--diff` adds the files the run changed,
from `virt-diff` against the image the run started from. Reports are pruned after 14 days.
`desk gc` (and so every `desk up`) writes the report of an idle run before destroying it, unless
its owner comes back meanwhile. One command at a time takes a run down; a second `desk down` of
it is refused. When the guest could not be asked, the report says "NOT CHECKED" instead of
counting nothing.

`desk record start|stop` (or `desk up --record`) takes screenshots at a steady rate (4 fps) in a
background process; unchanged frames are hard links, so a still screen costs nothing. Stopping
encodes them with GStreamer to WebM (VP9) with the wall-clock time in a bar below the picture,
and `desk down` copies the videos next to the report.

Provisioning (`provision.sh`, `files/`) runs before the warm state is saved and after cold
boots; it installs the shell extension, the Polish (programmer's) layout and a11y settings.

## Checks

```bash
/usr/bin/python3 -m unittest discover -s tests -t .   # unit tests, no VM
bin/desk selftest fedora-44-gnome --reboot            # end to end in a real run, ~1 min
```

## Host setup

Arch Linux and Fedora, and what is built on them (CachyOS, EndeavourOS, ...). `bin/desk doctor`
checks the host and prints the one command that installs what is missing; it needs only Python's
libvirt bindings to run.

| | Arch | Fedora |
|---|---|---|
| To start runs | `libvirt` `libvirt-python` `qemu-system-x86` `qemu-hw-display-virtio-vga` `qemu-img` `edk2-ovmf` `passt` `openssh` | `libvirt-daemon-driver-qemu` `python3-libvirt` `qemu-system-x86-core` `qemu-device-display-virtio-vga` `qemu-img` `edk2-ovmf` `passt` `openssh-clients` |
| To build images | `virt-install` `curl`; `arch-gnome`: `openssl`; `openwrt-*`: `gptfdisk` `parted` `libguestfs`; `windows-11`: `7zip` `libisoburn` | `virt-install` `curl`; `arch-gnome`: `openssl`; `openwrt-*`: `gdisk` `parted` `libguestfs`; `windows-11`: `7zip` `xorriso` |
| Single features | `python-pillow` (crops, several monitors), `qemu-hw-display-virtio-gpu-pci` (`--resolution`, `--displays`), `guestfs-tools` (`desk diff`), `gstreamer` `gst-plugins-base` `gst-plugins-good` (`desk record`) | `python3-pillow`, `qemu-device-display-virtio-gpu-pci`, `guestfs-tools`, `gstreamer1` `gstreamer1-plugins-base` `gstreamer1-plugins-good` |

The user needs `/dev/kvm` (the `kvm` group where it is not open to everybody); VMs run under
`qemu:///session`, so nothing runs as root.

Where images, runs and reports live is the host's own business, outside the repository:
`~/.local/share/desk` unless `~/.config/desk/config.toml` says otherwise, or `$DESK_HOME`.

```toml
# ~/.config/desk/config.toml
home = "/srv/desk"           # a file system with reflinks (btrfs, XFS) and room: 5-25 GiB an image
uri = "qemu:///session"      # the default
```

Snapshots, warm-ups and `desk diff` copy disks as reflinks; on a file system without them every
copy is a full one and `desk diff` does not work (`desk doctor` says which it is). With SELinux
enforcing (Fedora) the default home works as it is: libvirt labels what its VMs use.
