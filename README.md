# desk

`desk`: disposable desktop VMs that an agent drives through screenshots and real
mouse/keyboard input, for testing desktop apps and system packages end to end.

- Nested KVM, libvirt `qemu:///session` (no root), passt networking.
- Out of band: screenshots through libvirt, pointer and keyboard through QMP, so it works
  regardless of what runs in the guest (login screen, Wayland, polkit, crashes).
- In band: SSH as the desktop user, qemu-guest-agent as root.
- Accessibility tree (AT-SPI) with refs that map to screen pixels; a GNOME Shell extension in
  the image supplies window positions, which AT-SPI on Wayland does not know.
- Golden image -> provisioned disk -> warm state (saved RAM of a logged-in desktop) -> per-run
  qcow2 overlay.
  `desk up` restores in seconds, `desk down` discards everything.

Agent instructions: [skill/SKILL.md](skill/SKILL.md).

## Installing

A host that only uses desk needs no checkout: a release is one file, `desk.pyz`, which runs on
the system's `python3` with the distribution's libvirt bindings (see Host setup). In fish:

```fish
gh release download --repo infinitygp/desk --pattern desk.pyz --output ~/.local/bin/desk
chmod +x ~/.local/bin/desk
desk install      # the skill into ~/.agents/skills/desk (and ~/.claude/skills/desk), then desk doctor
desk update       # later: the newest release, skill included
```

On its first start a release unpacks itself into `~/.cache/desk/<build>/` and runs from there,
so the image definitions and the helpers it starts or copies into guests are ordinary files;
builds unpacked earlier go after a week unused. `desk --version` says which release runs and
from where.

To work on desk, clone the repository instead; `bin/desk install` links `bin/desk` into
`~/.local/bin` and the skill directory into `~/.agents/skills/desk`, so edits show at once, and
leaves a desk that is already there alone. A checkout and a release of the same commit see the
same image definitions, so switching between them rebuilds nothing.

## Layout

```text
bin/desk             entry point of a checkout (the system's python3 with the distribution's libvirt bindings)
scripts/             build.py makes desk.pyz, the release; pyz_main.py is its entry point
skill/               agent skill: SKILL.md, and a file per task it points to
desk/                harness code (guest_a11y.py runs inside the guest)
tests/               unit tests
images/<name>/       image.toml, kickstart, provision.sh
<home>/              base/, warm/, runs/, reports/, maintain/, cache/, logs/, keys/, ssh/  (see Host setup)
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

The images with a desktop on Linux have Chromium (`chromium` in `ks.cfg` and `user-data`), the
Fedora ones next to Firefox; Arch's GNOME comes without it, and `desk browser` says so.
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
its own. A request whose caller gave up waiting (its connection closed) is left undone when the
agent gets to it: the caller was told it failed. `agent-loop.ps1`, started at logon, starts it again when it ends and writes that to
`C:\desk\agent.log`, which the report lists among the failed units; desk asks a listing or lookup
again once the agent is back, but not an action, which may have been done. Commands are sent as
`powershell -EncodedCommand`, since Windows' command line mangles quotes.
The install brings WebView2 up to date with Microsoft's bootstrapper (signature checked), compiles
.NET's native images, and turns off what works in the background during tests and no test needs:
the updater of Edge and WebView2 (it went on with an update inside every run), SysMain, the search
indexer, telemetry and Delivery Optimization. Provisioning excludes from Defender, whose
real-time protection Tamper Protection keeps on, the folders where tests run and write programs
(`C:\Users`, the program folders, `C:\ProgramData`, Windows' `Temp` and `Installer`); AMSI blocks a
script that excludes the whole drive. Before the warm state is saved, `idle`
empties the standby list, the file cache that made up almost half of the saved memory (4.7 GB
before, 2.7 GB after).
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
bin/desk image warm fedora-44-gnome    # boot, provision, shut down, boot again, save the desktop
bin/desk image shot windows-11         # look at a graphical installer while it runs
bin/desk image list                    # state, build date, last selftest, licence, problems
```

Shared provisioning files live in `images/_shared/`. A variant (`base = "..."` in its
`image.toml`) inherits the base image's settings and adds provisioning scripts.

`prepare = "prepare.py"` in `image.toml` runs `prepare(steps)` on the host once the provisioned
session is up, before the warm state is saved (and after cold boots): what a user does before the
tests start, with real input (`desk/steps.py`: `gui`, `wait_for`, `click`, `type`, `key`).
`fedora-44-cockpit` logs in to Cockpit that way; Cockpit serves a certificate from a CA of the
image's own, which its Firefox and Chromium trust (Cockpit 368 keeps no CA of its own any more),
and the session has no idle timeout, since restoring the warm state moves the clock forward.

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

`desk usb add|rm|ls` (desk/usb.py) hands host USB devices to a run as `<hostdev type='usb'>` by
bus and device number, plugged into the guest's xHCI controller; QEMU runs as the user
(qemu:///session), so `desk usb add` checks the device node is the user's to open and otherwise
prints the udev rule that gives it to the kvm group.

`desk disk add|rm|ls` hot-plugs empty qcow2 disks (`runs/<id>/disks/NAME.qcow2`) as virtio-blk
into the spare PCIe root ports, serial = name; snapshots copy them along.
Firefox in the Linux images writes every page's console and uncaught errors to its output, so to
the journal (`images/_shared/gnome/desk-firefox.js`); `desk browser console` and the report read
them from there. Promises keep their rejection stack without the devtools open, and an autoconfig
script (`desk.cfg`, loaded into every content process) adds the thrown value and stack of each
uncaught exception as a `JavaScript error detail:` line, which desk joins to its error. Addresses
opened from outside (`desk browser open`, `firefox URL`) replace the current tab; one that
differs from the page shown only after `#` leaves Firefox with an empty tab that never loads, so
`desk browser open` goes through `about:blank` for it (quickly: Cockpit ends its session when none
of its pages is open for a few seconds). The Remote Agent
(WebDriver BiDi) is not used, as it marks the browser window; `desk browser viewport` types sizes
into the responsive design mode instead.

Chromium gets its policies and flags from `images/_shared/gnome/desk-chromium-*` (Fedora's
wrapper reads flags from `/etc/chromium/chromium.conf`, Arch's launcher from
`/etc/chromium-flags.conf`): `--password-store=basic`, since GDM logs in without a password and
leaves no unlocked keyring, so Chromium would ask for a new one's; `--force-renderer-accessibility`,
without which `desk a11y` saw only the frame of a Chromium just started; `--same-tab`, the
counterpart of Firefox's external links in the current tab; `--start-maximized`, as Firefox's
window is, for the height of a phone's page in the device mode; and `--enable-logging=stderr` for
the web console. Chromium logs every console call of a page at one level, so desk lists its messages
as `Chromium console:` whatever they are, counts only its uncaught exceptions (`Uncaught ...`) as
errors, and a `console_errors` step that meets one of the others fails with exit 2 rather than
pass without having looked. The browser `desk browser` drives is the one asked for (`--browser`),
else the one in the focused window, else Firefox. Chromium's error pages and certificate warning
have an address of their own (`chrome-error://`) and are told by their ids and classes; while it
waits for a server it keeps the page before, and its address bar (`OmniboxViewViews`) shows the
one it waits for. Its device mode belongs to DevTools: `desk browser viewport` opens them
(ctrl+shift+i), their device toolbar (ctrl+shift+m, only taken with the focus in DevTools), sets
the toolbar's zoom to 100 % (fitted to the window, the page is smaller on screen than the boxes
AT-SPI gives) and types the sizes, then reads them back and types them once more if they are
gone: the toolbar is drawn anew after the zoom or the window changes and empties a size typed
meanwhile. AT-SPI's box of the page is the part the window shows, rounded to a pixel either way.
Chromium reports window coordinates from its surface, shadow included, like GTK 3, and has no
EditableText interface: its fields count as editable by their state.

`desk clock` (`desk/clock.py`) sets the guest's time through qemu-guest-agent with the guest's
time sync off and keeps the offset in `run.json`, reapplied after snapshot restores and reboots
(tried for 20 s, as libvirt now and then sees the agent a moment late after a restore; a moved
clock that could not be put back is an error, not a warning).

## Clicking and typing

`desk click @ref|--name` asks the application, through AT-SPI hit tests, what is at the point it
is about to click: an element there that is not the target or inside it (a sticky footer, a
toast) counts as covering it, and another point of the target or scrolling is tried. Firefox does
not mark the buttons of sticky footers as showing; when the hit test reaches the footer around
them, they count as visible. A label of the target (AT-SPI's labelled-by) over it does not cover
it: a click on a label reaches its control, and PatternFly's selectable tiles lay one over the
whole card, radio button included. Firefox's hit test also answers with an element where a
scrolling container cuts it off (a table row reaching below Cockpit's main area), so the points
tried come inside every ancestor's box first, `desk a11y` lists such a point rather than the
middle, and an element with none there is scrolled into view first; a menu hanging out of its
container's box stays clickable where the hit test finds it. `desk a11y` marks covered elements.
`desk set-text` focuses the field, selects all and types (or pastes through the shell extension's
`SetClipboard`), since AT-SPI's text replacement leaves web pages' input events out. `desk type`
into a browser window whose focus is not in a text field is refused. Walks of the tree stop after
20 s.

`desk forward` keeps an SSH tunnel per guest port in a background process
(`python -m desk.forward`), restarted when SSH drops, stopped by `desk down`. ssh holds the host
port only while connected, so while a guest reboots no other run's `desk forward` takes it.

## Exit codes, locks, limits

- 0: done. 1: the check a command stands for failed (a scenario step, `selftest`, `find`,
  `wait`, `doctor`). 2: desk could not do what was asked; one line says why, also when a host tool,
  libvirt or the disk failed (`DESK_DEBUG=1` adds the traceback). 130: interrupted. `desk exec`
  passes on the guest command's code, 124 when it ran out of time (`push`/`pull` too).
- The state lock (`<home>/.lock`) is held while a domain is started or destroyed, not while a
  guest boots or a report is written, so a cold boot does not hold up other runs. A command that
  waits for a lock says who holds it and gives up after 15 minutes (an image's lock: 6 hours; the
  lock around a change of a run's `run.json`: a minute).
- What a command in the guest may take is enforced in the guest (`timeout`, a watchdog on
  BusyBox and Windows), for `desk exec`, `desk wait --until-exec` and scenario steps alike.

## Scenarios

`desk scenario run FILE` runs a repeatable end-to-end test described in TOML (format in
`skill/SKILL.md`): build on the host, copy the artifacts in, start a run, steps with the same real
input as the CLI (`desk/steps.py`), report with the steps in its timeline, exit code 0/1.
Projects keep theirs in `test/desk/`. `--from N` runs steps N.. in a run left by `--keep`, for
trying a fixed step again without the ones before it. A step that loses the guest (exit 2) says
whether its VM still runs and what the host had then (`runs.host_state`: free disk where the run
and libvirt's logs are, memory, swap, other runs): on 2026-10-20 a full `/home` ended QEMU in a
screendump, and the scenario saw only "Connection refused".

## Watching

- `desk view`: every run has a VNC display on 127.0.0.1 with a per-run password; the command
  prints the SSH tunnel to use from a workstation.

## Evidence

`desk down` writes `reports/<run>/report.md` (failed units, unit failures from the journal since
the start across reboots, SELinux denials, coredumps, journal warnings, package changes with dnf's
UTC times made local, timeline with screenshots); `--diff` adds the files the run changed,
from `virt-diff` against the image the run started from. `desk gc` removes reports older than
14 days, and writes the report of an idle run before destroying it, unless its owner comes back
meanwhile. Every `desk up` starts the same in the background (`logs/gc-idle.log`) and does it
itself only while it waits for memory, or when an idle run of its image may hold the warm state it
restores: another run's report and video can take minutes. One command at a time takes a run down; a second `desk down` of
it is refused. When the guest could not be asked, the report says "NOT CHECKED" instead of
counting nothing.

`desk record start|stop` (or `desk up --record`) takes screenshots at a steady rate (4 fps) in a
background process; unchanged frames are hard links, so a still screen costs nothing. Stopping
encodes them with GStreamer to WebM (VP9) with the wall-clock time in a bar below the picture,
and `desk down` copies the videos next to the report.

Provisioning (`provision.sh`, `files/`) runs in the warm-up's first boot, which then shuts down
cleanly: that disk (`provisioned.qcow2` in the warm build) is what the warm state boots before it
is saved, and what cold runs boot, so both start from the same files and cold runs skip
provisioning and the relogin (with no current warm build they boot the base disk and provision
it). It installs the shell extension, the Polish (programmer's) layout and a11y settings, and
masks GNOME Software, whose banners and prompts came up over the app under test at random.

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
| Single features | `python-pillow` (crops, several monitors), `qemu-hw-display-virtio-gpu-pci` (`--resolution`, `--displays`), `guestfs-tools` (`desk diff`), `gstreamer` `gst-plugins-base` `gst-plugins-good` (`desk record`), `github-cli` (installing and `desk update` of a release) | `python3-pillow`, `qemu-device-display-virtio-gpu-pci`, `guestfs-tools`, `gstreamer1` `gstreamer1-plugins-base` `gstreamer1-plugins-good`, `gh` |

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
