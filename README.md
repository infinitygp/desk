# desk

`desk`: disposable desktop VMs that an agent drives through screenshots and real
mouse/keyboard input, for testing desktop apps and system packages end to end.

- Nested KVM, libvirt `qemu:///session` (no root), passt networking.
- Out of band: screenshots through libvirt, pointer and keyboard through QMP, so it works
  regardless of what runs in the guest (login screen, Wayland, polkit, crashes).
- In band: SSH as the desktop user, qemu-guest-agent as root.
- Accessibility tree (AT-SPI) with refs that map to screen pixels; the desktop supplies window
  positions, which AT-SPI on Wayland does not know (Desktops).
- Golden image -> provisioned disk -> warm state (saved RAM of a logged-in desktop) -> per-run
  qcow2 overlay.
  `desk up` restores in seconds, `desk down` discards everything.

Agent instructions: [skill/SKILL.md](skill/SKILL.md).

## Installing

A host that only uses desk needs no checkout: a release is one file, `desk.pyz`, which runs on
the system's `python3` with the distribution's libvirt bindings (see Host setup). In fish:

```fish
curl --create-dirs -fLo ~/.local/bin/desk https://github.com/infinitygp/desk/releases/latest/download/desk.pyz
chmod +x ~/.local/bin/desk
desk install      # the skill into ~/.agents/skills/desk (and ~/.claude/skills/desk), then desk doctor
desk update       # later: the newest release, skill included
```

`desk update` downloads over HTTPS as well, and only when that fails (a private repository, a fork
of it) with `gh`, logged in: the first line would then be
`gh release download --repo OWNER/desk --pattern desk.pyz --output ~/.local/bin/desk`.

On its first start a release unpacks itself into `~/.cache/desk/<build>/` and runs from there,
so the blocks and the helpers it starts or copies into guests are ordinary files;
builds unpacked earlier go after a week unused. `desk --version` says which release runs and
from where.

To work on desk, clone the repository instead; `bin/desk install` links `bin/desk` into
`~/.local/bin` and the skill directory into `~/.agents/skills/desk`, so edits show at once, and
leaves a desk that is already there alone. A checkout and a release of the same commit see the
same blocks, so switching between them rebuilds nothing.

## Layout

```text
bin/desk             entry point of a checkout (the system's python3 with the distribution's libvirt bindings)
scripts/             build.py makes desk.pyz, the release; pyz_main.py is its entry point
skill/               agent skill: SKILL.md, and a file per task it points to
desk/                harness code (guest_a11y.py runs inside the guest)
tests/               unit tests
images/              blocks images are made of: distros/, desktops/, addons/; shared/ what desktops share
<home>/              base/, warm/, runs/, reports/, maintain/, cache/, logs/, keys/, ssh/  (see Host setup)
```

## Images

An image is made of blocks: a distro (how it is installed and how desk talks to it), the desktop
it runs and addons on top. A host defines the images it tests on with `desk image add`, which
writes `~/.config/desk/images/<name>.toml`; none is required, and nothing builds one unasked. The
name is the blocks (`fedora-44-gnome`, `fedora-44-gnome-cockpit`), so a scenario's `image` means
the same on every host, and `desk up` of an image the host has not defined prints the
`desk image add` that defines it.

| Block | Kind | Install, or what it adds | Notes |
|---|---|---|---|
| `fedora-44` | distro | kickstart over the network, the system without a desktop | |
| `fedora-45` | distro | kickstart, the beta's installer tree, the release's packages | next release; follows it once it is out |
| `arch` | distro | official cloud image: users by cloud-init (NoCloud seed), the rest by `install.sh` | rolling; no Secure Boot, no Firefox |
| `debian-13` | distro | official cloud image (generic: the genericcloud one's kernel drives no display), likewise | Firefox ESR |
| `ubuntu-26.04` | distro | official cloud image, likewise | Firefox from Mozilla's repository; no Chromium (Ubuntu has only its snap) |
| `windows-11` | distro | evaluation ISO + `autounattend.xml` on a second CD | comes with its desktop; 90-day evaluation, reinstalled by `desk maintain` |
| `openwrt-25.12` | distro | the release's disk image (x86-64 ext4 combined, EFI), grown and preconfigured offline | no desktop: BusyBox ash, procd, apk, LuCI |
| `gnome` | desktop | the Workstation's packages (Fedora), the gnome group (Arch), gnome-core (Debian), ubuntu-desktop-minimal (Ubuntu), automatic login; shell extension, settings for steady screenshots | |
| `kde` | desktop | Plasma on Wayland, logged in by Plasma Login Manager (Fedora, Arch) or SDDM (Debian, Ubuntu) | |
| `xfce` | desktop | Xfce on X11, logged in by LightDM | |
| `cinnamon` | desktop | Cinnamon on X11, logged in by LightDM | |
| `hyprland` | desktop | Hyprland (Wayland, tiling), started by greetd through uwsm | not on Fedora 44, which has no Hyprland; Debian's from trixie-backports |
| `cockpit` | addon | Cockpit, Firefox logged in with administrative access | Fedora 44 |

The Linux images with a desktop have Chromium (not Ubuntu's) and Firefox (not Arch's).

```bash
bin/desk image blocks                              # the distros, desktops and addons there are
bin/desk image add fedora-44 gnome                 # defines fedora-44-gnome on this host
bin/desk image add fedora-44 gnome --addon cockpit # fedora-44-gnome-cockpit
bin/desk image set fedora-44-gnome --memory 8G     # sizes (--disk 60G is a new install)
bin/desk image show fedora-44-gnome-cockpit        # the settings its blocks add up to
bin/desk maintain --only fedora-44-gnome           # build, warm and selftest it
bin/desk image build fedora-44-gnome               # by hand: unattended install, ~10 min, needs network
bin/desk image warm fedora-44-gnome                # boot, provision, shut down, boot again, save the desktop
bin/desk image shot windows-11                     # look at a graphical installer while it runs
bin/desk image list                                # state, build date, last selftest, licence, problems
bin/desk image rm fedora-44-gnome-cockpit          # the definition; its builds stay until deleted by hand
```

### Definitions

A block is a directory with a `block.toml` of `ImageSpec` fields (`desk/config.py`), next to the
files it names relative to itself (kickstart or `user-data`, `provision.sh`, `prepare.py`); the
first sentence of its first comment is what `desk image blocks` says of it. An image layers its
distro, its desktop and its addons in that order:

- A setting of a later block wins; `install`, `provision`, `files` and `unattend_files` add up,
  and only one block brings a `prepare`.
- A distro lists the desktops it was tried with (`desktops`); one without the list comes with its
  desktop (Windows) or has none (OpenWrt). An addon may list the distros it works on (`distros`).
  `desk image add` refuses other combinations.
- The image's own file names its blocks and sets what names no file: sizes, `layout`,
  `timezone`, `rebuild_days`. `desk image add` and `desk image set` write it whole, without
  comments; it may be edited by hand as well.

The settings that shape a run:

- `installer` (`kickstart`, `cloudimg`, `disk`, `windows`) and `family` (`linux`, `windows`,
  `openwrt`): how the image is installed and how desk talks to the guest.
- `desktop`: the session desk drives, set to the desktop block's name (`windows` on Windows).
  Without one the screen is a text console, and commands that need a session refuse.
- `layout` and `timezone`: the guest's keyboard layout, which `desk type` types through (`us`, or
  `pl`: Polish (programmer's), US plus AltGr letters, so Polish letters are real key presses too;
  any other layout moves keys desk cannot know of), and its time zone, in which `desk clock` and
  the report read guest times. Both come from `config.toml` or the host unless the image sets them
  (Host setup), and provisioning applies them, so a change is a new warm state; a run keeps the
  ones its guest was provisioned with in `run.json`.
- `sound`: an emulated sound card without host output (`windows-11`: `ich9`), so programs get an
  audio device to play to.
- `secure_boot = false` for a boot loader that is not signed for UEFI Secure Boot (Arch).
- `memory_mib`, `vcpus`: the size of the VM. The warm-up and every run set them on the installed
  domain, so a change is a new warm state, not a new install; `desk up --memory/--cpus` gives one
  run other sizes, booting cold when they differ from the warm state's.
- `install`: scripts run as root over SSH on the installed system before it becomes the base
  image, the distro's (Arch's system around any desktop) and then the desktop's (its packages and
  the automatic login); a change is a new install. The installers put down only the system
  without a desktop (Fedora's kickstart; cloud-init makes the users), so a script that fails says
  why at once instead of leaving the install to time out.
- `rebuild_days`, `expiry`, `install_marker`: see Builds and maintenance.

An image whose addons only add to the warm state (provisioning, files, a prepare script) is a
variant: it runs on the install of the image without them, which `desk image add` defines too when
it is missing, and has a warm state of its own. What that image's file sets comes between the
desktop and the addons, so the variant's addons and its own file win over it; an install setting
(`disk_gb`) is set there only.

`prepare = "prepare.py"` runs `prepare(steps)` on the host once the provisioned session is up,
before the warm state is saved (and after cold boots): what a user does before the tests start,
with real input (`desk/steps.py`: `gui`, `wait_for`, `click`, `type`, `key`). The `cockpit` addon
logs in to Cockpit that way. Its Cockpit serves a certificate from a CA of the image's own, which
Firefox and Chromium trust, and its session has no idle timeout, since restoring the warm state
moves the clock forward.

Blocks only one host needs (its language, its hardware) are kept on that host, in
`~/.config/desk/distros/`, `desktops/` and `addons/` (`$XDG_CONFIG_HOME`), laid out like
`images/`. desk offers them with the shipped ones. A name is in one of the two places only, and a
block moved from one to the other keeps its builds: fingerprints name files within their root,
as they do for a checkout and a release of the same commit. A desk before
blocks kept a directory per image in `~/.config/desk/images/`; desk refuses those until their
files are in an addon and the image is added again, and `desk image list` shows builds left without
a definition. Their builds are installed again once by `desk maintain`, as fingerprints name files
by the block they are in now.

### Provisioning

Provisioning (`provision`, `files`) runs in the warm-up's first boot, which then shuts down
cleanly. That disk (`provisioned.qcow2` in the warm build) is what the warm state boots before it
is saved and what cold runs boot, so both start from the same files and cold runs skip
provisioning and the relogin; with no current warm build they boot the base disk and provision it.
Every desktop image first gets `images/shared/desktop.sh`: the browsers set up for desk, the
time zone and keyboard layout, the accessibility switches of Gecko, Chromium and Qt, quiet
maintenance timers and the polkit rule `desk reboot` needs. Then its desktop's own script: on
GNOME (`images/desktops/gnome/provision.sh`) the shell extension, settings for a still screen, and
GNOME Software masked, whose banners and prompts would cover the app under test.
Every script has `DESK_TIMEZONE` (an IANA name), `DESK_POSIX_TZ` (its POSIX rule, from the host's
zone file, for OpenWrt) and `DESK_LAYOUT` in its environment; the installs themselves are UTC and
US.

### Desktops

Window positions, the clipboard and the screen's mode come from the desktop, each in its own way,
in `desk/guest_a11y.py` (`shell_windows`, `focus`, `clipboard`) and `desk/monitors.py`; which one
an image runs is `desktop` (the desktop block's name). `desk image blocks` lists the desktops each
distro was tried with.

| Desktop | Windows on the screen | Clipboard (`desk set-text`) | Screen (`desk display`) | Logs in | selftest drives |
|---|---|---|---|---|---|
| `gnome` | the shell extension `desk-helper` | the extension | `gdctl` | GDM | GNOME's calculator and text editor |
| `kde` | a KWin script loaded for each listing, which calls the helper back over D-Bus | Klipper | `kscreen-doctor` | Plasma Login Manager, SDDM | KCalc, KWrite |
| `xfce`, `cinnamon` | libwnck, with GTK's shadow (`_GTK_FRAME_EXTENTS`, read through libX11) | `xclip` | `xrandr` (no scaling on X11) | LightDM | GNOME's calculator; Mousepad, GNOME's editor |
| `hyprland` | `hyprctl clients` (the focus history stands for the stacking order) | `wl-copy` | `hyprctl keyword monitor` | greetd, through uwsm | GNOME's calculator and text editor |

- KWin tells an XWayland window from a Wayland one only by its class (`KWin::X11Window`), and
  places windows by their client area, which leaves out the decoration KWin draws.
- KWrite names its text after the document, which changes with what is typed; `desk set-text`
  reads the focused field at its place back.
- On X11 AT-SPI's screen coordinates are right for GTK 3 and Qt but not GTK 4, which counts from
  its window there too; so every toolkit is placed as on Wayland, from the window X knows (GTK's
  shadow included) and the frame GTK draws inside it.
- Hyprland 0.56 writes a Lua configuration of its own when it finds none, which `hyprctl keyword`
  and `dispatch` do not take; its block writes `hyprland.conf` before the first start.
- `desk display` on Plasma and Hyprland reads the mode and scale back: one the desktop did not take
  (Hyprland takes a scale that does not divide the mode as one near it) is refused and the one
  before put back. Hyprland applies a monitor rule only with its next frame, which desk has it draw.
- greetd starts the automatic session once per boot (`/run/greetd.run`, which the relogin removes),
  on the terminal its unit keeps getty off: Debian's and Ubuntu's tty7. On tty1 there, getty hung
  the session up within a second.

### Browsers on Linux

`desk browser` drives the browser asked for (`--browser`), else the one in the focused window,
else Firefox.

Firefox:

- Writes every page's console and uncaught errors to its output, so to the journal
  (`images/shared/desktop/desk-firefox.js`), where `desk browser console` and the report read
  them.
  Promises keep their rejection stack without the devtools open, and an autoconfig script
  (`desk.cfg`, loaded into every content process) adds the thrown value and stack of each uncaught
  exception as a `JavaScript error detail:` line, which desk joins to its error.
- Addresses opened from outside (`desk browser open`, `firefox URL`) replace the current tab. One
  that differs from the page shown only after `#` would leave an empty tab that never loads, so
  `desk browser open` goes through `about:blank` for it, quickly: Cockpit ends its session when
  none of its pages is open for a few seconds.
- Its policies (`desk-firefox-policies.json`) leave out first-run pages, prompts and Mozilla's
  Terms of Use dialog, which Mozilla's own builds (Ubuntu's Firefox) show over the first page.
- The Remote Agent (WebDriver BiDi) is not used, as it marks the browser window;
  `desk browser viewport` types the sizes into the responsive design mode.

Chromium gets its policies and flags from `images/shared/desktop/desk-chromium-*` (Fedora's
wrapper reads flags from `/etc/chromium/chromium.conf`, Debian's from `/etc/chromium.d/`, Arch's
launcher from `/etc/chromium-flags.conf`):

- `--password-store=basic`: GDM logs in without a password and leaves no unlocked keyring, for
  which Chromium would ask a new password;
- `--force-renderer-accessibility`: without it the accessibility tree of a Chromium just started
  holds only its frame;
- `--same-tab`: external links in the current tab, as in Firefox;
- `--start-maximized`, as Firefox's window is, for the height of a phone's page in the device mode;
- `--enable-logging=stderr`: the web console.

What else differs from Firefox:

- Every console call of a page is logged at one level, so desk lists the messages as
  `Chromium console:` and counts only uncaught exceptions (`Uncaught ...`) as errors; a
  `console_errors` step that meets one of the others fails with exit 2 rather than pass unlooked.
- Error pages and the certificate warning (`chrome-error://`) are told by their ids and classes;
  while it waits for a server, Chromium keeps the page before and its address bar
  (`OmniboxViewViews`) shows the address it waits for.
- The device mode belongs to DevTools: `desk browser viewport` opens them (ctrl+shift+i) and their
  device toolbar (ctrl+shift+m), sets its zoom to 100 % (fitted to the window, the page is smaller
  on screen than AT-SPI's boxes) and types the sizes, again if the toolbar, redrawn after a
  change, emptied them.
- Window coordinates come from the surface, shadow included, as in GTK 3, and fields count as
  editable by their state (no EditableText interface).

### Windows

- The ISO, the virtio-win ISO and the Win32-OpenSSH MSI are downloaded into `cache/` when missing
  and checked against the SHA-256 pinned in its `block.toml`.
- No TPM (LabConfig bypass); a virtio disk with the viostor driver loaded by Setup (QEMU 10.2's
  AHCI emulation crashes under Windows); an e1000e NIC (inbox driver); OpenSSH from the MSI with
  PowerShell as the shell; virtio-win guest tools for the display driver and qemu-guest-agent.
- Commands are sent as `powershell -EncodedCommand`, since Windows' command line mangles quotes.
- The install brings WebView2 up to date with Microsoft's bootstrapper (signature checked),
  compiles .NET's native images, and turns off what works in the background during tests and no
  test needs: the updater of Edge and WebView2, SysMain, the search indexer, telemetry and Delivery
  Optimization.
- Provisioning excludes the folders where tests run and write programs from Defender, whose
  real-time protection Tamper Protection keeps on: `C:\Users`, `C:\desk`, the program folders,
  `C:\ProgramData`, Windows' `Temp` and `Installer` (AMSI blocks a script that excludes the whole
  drive). It opts in to early feature updates and enables the taskbar position and size features
  (59213768, 61090762) with ViVeTool v0.3.4 (GPL-3.0, downloaded and checked against a pinned
  SHA-256), so `desk win taskbar --edge/--size` work on every build; `desk/winsettings.py` writes
  theme and taskbar settings where the Settings app writes them.
- Notepad's first-run teaching tip is marked closed in its settings: UI Automation gets nothing
  from its window while the tip is open.
- Provisioning turns the IANA time zone into Windows' id with ICU's `ucal_getWindowsTimeZoneID`
  (`icu.dll`, part of Windows), and the layout into the en-US input method (00000409 or 00000415).
- Before the warm state is saved, `idle` empties the standby list, the file cache that would make
  up almost half of the saved memory.

UI Automation and programs on the desktop, which SSH sessions cannot see, go through an agent in
the logged-in session (`images/distros/windows-11/agent/agent.ps1`, its port forwarded like SSH):

- It serves one request at a time, so nothing may hold it: a connection that sends nothing is
  dropped after 5 s, a tree walk ends after 20 s, and a program that is waited for
  (`exec --gui --wait`) is waited for on a thread of its own.
- A request whose caller gave up waiting (its connection closed) is left undone when the agent gets
  to it: the caller was told it failed.
- `agent-loop.ps1`, started at logon, starts the agent again when it ends and writes that to
  `C:\desk\agent.log`, which the report lists among the failed units. desk asks a listing or a
  lookup again once the agent is back, but not an action, which may have been done.

### OpenWrt

`installer = "disk"`, `family = "openwrt"`, `desktop = false`:

- The install checks the image from `disk_image_url` against the `sha256sums` next to it, grows its
  last partition and ext4 to `disk_gb` (parted, guestfish) and writes `disk_files` onto it: the SSH
  key, a uci-defaults script (LAN as a DHCP client on passt, so SSH and LuCI come in through the
  LAN zone; root's password) and an `rc.local` that powers the first boot
  off, which ends the install (~30 s).
- Provisioning (`sh -s`, no bash) adds qemu-ga and the SFTP server (scp), keeps the system log
  in `/desk/syslog` and sets the time zone as a POSIX rule (OpenWrt has no zone files).
- Commands that need a desktop refuse; screenshots show the text console. `desk exec`'s time
  limit is a `setsid` watchdog (BusyBox has no `timeout`), and `desk forward` leads to the LAN
  address, where router services listen.

## Builds and maintenance

Every install and every warm state is a build of its own, `base/<image>.<version>/` and
`warm/<image>.<version>/` (the version is the UTC time it began), with `base/<image>` and
`warm/<image>` symlinks to the current ones. Runs pin the builds they start from, so a new build
never disturbs a running or kept run; `desk gc` deletes builds nothing uses any more. Builds of an
image whose definition is gone stay until they are deleted by hand.

`desk maintain` reinstalls an image family (a base image and its variants) when

- the base is a day short of `rebuild_days` old (`fedora-44-gnome`, `arch-gnome`: 14 days;
  `fedora-45-gnome`: 7),
- its install definition changed, or
- its licence (`expiry`, the Windows evaluation) has less than 14 days left,

and re-warms the images whose definition changed. New builds are selftested next to the current
ones and become current only when every member of the family passes; otherwise the current builds
stay, and `desk up` and `desk image list` show the failure. Reports and screenshots of failures go
to `<home>/maintain/<date>/`; an install that hangs leaves `logs/build-<image>-stuck.png` and its
serial console in `logs/build-<image>.log`. An install counts as stuck after 3 hours, and is kept
only if it powered off by itself and, where the image names an `install_marker` (Windows:
`firstboot.ps1` reports on the serial console), said that its setup succeeded.

Maintenance is an offer, not a duty: it looks after the images with a warm state, the ones in use
on the host, and builds no other unless `--only NAME` asks for it. It runs when somebody runs it or
weekly from the optional timer (12 hours at most); without the timer, images are as old as their
last build. `desk up` warns three weeks before a licence runs out and refuses an expired image.

```bash
bin/desk maintain --dry-run         # what is due
bin/desk maintain --only windows-11 # one image family, now
bin/desk maintain --install-timer   # optional: weekly, Monday 03:00 (systemd user timer, needs lingering)
systemctl --user list-timers desk-maintain.timer
journalctl --user -u desk-maintain  # output of the last runs
```

## Runs

### Monitors

`desk up --resolution/--scale/--displays` and `desk display` (`desk/monitors.py`):

- Each monitor is a virtio GPU of its own, as Windows' viogpudo drives one head per GPU and QEMU
  leaves further heads disconnected without a display UI; this needs QEMU's `virtio-gpu-pci`.
- Screens are put side by side into one screenshot; pointer events go to the console of the monitor
  under the point, on Windows through the agent for all but the primary one, which is all its
  absolute tablet reaches.
- Windows scales by DPI after signing in again; GNOME scales through `gdctl` in the logical layout,
  and desk turns the logical positions toolkits report into screen pixels.
- Several monitors are refused on GNOME guests: mutter never updates a second GPU's monitor
  without 3D acceleration.

### Snapshots, disks, USB devices, clock

- `desk snapshot save|restore|list|delete` works like the warm state, for a running run: the
  guest's memory is saved (the VM stops for a few seconds), disk and firmware variables are reflink
  copies, and the run carries on restored from the saved memory. Restoring destroys the domain,
  puts the copies back and restores the memory. Snapshots live in `runs/<id>/snapshots/`.
- `desk disk add|rm|ls` hot-plugs empty qcow2 disks (`runs/<id>/disks/NAME.qcow2`) as virtio-blk
  into the spare PCIe root ports, serial = name; snapshots copy them along.
- `desk usb add|rm|ls` (`desk/usb.py`) hands host USB devices to a run as `<hostdev type='usb'>`
  by bus and device number, plugged into the guest's xHCI controller. QEMU runs as the user
  (`qemu:///session`), so `desk usb add` checks that the device node is the user's to open and
  otherwise prints the udev rule that gives it to the kvm group.
- `desk clock` (`desk/clock.py`) sets the guest's time through qemu-guest-agent with the guest's
  time sync off and keeps the offset in `run.json`, put back after snapshot restores and reboots
  (tried for 20 s, as the agent may answer a moment late after a restore). A moved clock that
  could not be put back is an error, not a warning.

### Clicking and typing

`desk click @ref|--name` asks the application, through AT-SPI hit tests, what is at the point it
is about to click. An element there that is neither the target nor inside it (a sticky footer, a
toast) covers it, and another point of the target or scrolling is tried; `desk a11y` marks covered
elements.

- Firefox does not mark the buttons of sticky footers as showing; when the hit test reaches the
  footer around them, they count as visible.
- A label of the target (AT-SPI's labelled-by) over it does not cover it: a click on a label
  reaches its control, and PatternFly's selectable tiles lay one over the whole card, radio button
  included.
- Firefox's hit test also answers with an element where a scrolling container cuts it off (a table
  row reaching below Cockpit's main area). So the points tried come inside every ancestor's box
  first, `desk a11y` lists such a point rather than the middle, and an element with none there is
  scrolled into view first; a menu hanging out of its container's box stays clickable where the
  hit test finds it.

`desk set-text` focuses the field, selects all and types (or pastes through the shell extension's
`SetClipboard`), since AT-SPI's text replacement leaves web pages' input events out. `desk type`
into a browser window whose focus is not in a text field is refused. Walks of the tree stop after
20 s.

### Forwarding and watching

- `desk forward` keeps an SSH tunnel per guest port in a background process
  (`python -m desk.forward`), restarted when SSH drops and stopped by `desk down`. ssh holds the
  host port only while connected, so while a guest reboots no other run's `desk forward` takes it.
- `desk view`: every run has a VNC display on 127.0.0.1 with a per-run password; the command
  prints the SSH tunnel to use from a workstation.

## Scenarios

`desk scenario run FILE` runs a repeatable end-to-end test described in TOML (format in
`skill/SKILL.md`): build on the host, copy the artifacts in, start a run, steps with the same real
input as the CLI (`desk/steps.py`), report with the steps in its timeline, exit code 0/1. Projects
keep theirs in `test/desk/`. `--from N` runs steps N.. in a run left by `--keep`, to try a fixed
step again without the ones before it. A step that loses the guest (exit 2) says whether its VM
still runs and what the host had then (`runs.host_state`: free disk where the run and libvirt's
logs are, memory, swap, other runs), since a full disk that ends QEMU shows in the step only as
"Connection refused".

## Evidence

- `desk down` writes `reports/<run>/report.md`: failed units, unit failures from the journal since
  the start across reboots, SELinux denials, coredumps, journal warnings, package changes (dnf's
  UTC times made local) and the timeline with screenshots; `--diff` adds the files the run
  changed, from `virt-diff` against the image the run started from. What the guest could not be
  asked about says "NOT CHECKED" instead of counting nothing.
- One command at a time takes a run down; a second `desk down` of it is refused.
- `desk gc` removes reports older than 14 days and takes down runs idle for `DESK_TTL` seconds
  (2 hours), writing their report first unless the owner comes back meanwhile. Every `desk up`
  starts the same in the background (`logs/gc-idle.log`) and does it itself only while it waits for
  memory, or when an idle run of its image may hold the warm state it restores: another run's
  report and video can take minutes.
- `desk record start|stop` (or `desk up --record`) takes screenshots at a steady rate (4 fps,
  `--fps` 1-10) in a background process; unchanged frames are hard links, so a still screen costs
  nothing. Stopping encodes them with GStreamer to WebM (VP9) with the wall-clock time in a bar
  below the picture, and `desk down` copies the videos next to the report. A recording stops by
  itself after 30 minutes (`--limit`) or 4 GiB.

## Exit codes, locks, limits

- 0: done. 1: the check a command stands for failed (a scenario step, `selftest`, `find`,
  `wait`, `browser open`, `doctor`). 2: desk could not do what was asked; one line says why, also
  when a host tool, libvirt or the disk failed (`DESK_DEBUG=1` adds the traceback). 130:
  interrupted. `desk exec` passes on the guest command's code, 124 when it ran out of time
  (`push`/`pull` too).
- The state lock (`<home>/.lock`) is held while a domain is started or destroyed, not while a
  guest boots or a report is written, so a cold boot does not hold up other runs. A command that
  waits for a lock says who holds it and gives up after 15 minutes (an image's lock: 6 hours; the
  lock around a change of a run's `run.json`: a minute).
- What a command in the guest may take is enforced in the guest (`timeout`, a watchdog on
  BusyBox and Windows), for `desk exec`, `desk wait --until-exec` and scenario steps alike.

## Checks

```bash
/usr/bin/python3 -m unittest discover -s tests -t .   # unit tests, no VM
bin/desk selftest fedora-44-gnome --reboot            # end to end in a real run, ~1 min
```

Which checks a change needs: `AGENTS.md`.

## Host setup

Arch Linux and Fedora, and what is built on them (CachyOS, EndeavourOS, ...). `bin/desk doctor`
checks the host and prints the one command that installs what is missing; it needs only Python's
libvirt bindings to run.

| | Arch | Fedora |
|---|---|---|
| To start runs | `libvirt` `libvirt-python` `qemu-system-x86` `qemu-hw-display-virtio-vga` `qemu-img` `edk2-ovmf` `passt` `openssh` | `libvirt-daemon-driver-qemu` `python3-libvirt` `qemu-system-x86-core` `qemu-device-display-virtio-vga` `qemu-img` `edk2-ovmf` `passt` `openssh-clients` |
| To build images | `virt-install` `openssh`; cloud images (`arch`, `debian-13`, `ubuntu-26.04`): `curl` `openssl`; `openwrt-*`: `curl` `gzip` `gptfdisk` `parted` `libguestfs`; `windows-11`: `curl` `7zip` `libisoburn` | `virt-install` `openssh`; cloud images (`arch`, `debian-13`, `ubuntu-26.04`): `curl` `openssl`; `openwrt-*`: `curl` `gzip` `gdisk` `parted` `libguestfs`; `windows-11`: `curl` `7zip` `xorriso` |
| Single features | `python-pillow` (crops, several monitors), `qemu-hw-display-virtio-gpu-pci` (`--resolution`, `--displays`), `guestfs-tools` (`desk diff`), `gstreamer` `gst-plugins-base` `gst-plugins-good` (`desk record`), `github-cli` (installing and `desk update` from a private repository) | `python3-pillow`, `qemu-device-display-virtio-gpu-pci`, `guestfs-tools`, `gstreamer1` `gstreamer1-plugins-base` `gstreamer1-plugins-good`, `gh` |

The user needs `/dev/kvm` (the `kvm` group where it is not open to everybody); VMs run under
`qemu:///session`, so nothing runs as root.

What is the host's own lives outside the repository: images, runs and reports in
`~/.local/share/desk` unless `~/.config/desk/config.toml` says otherwise (`$DESK_HOME` and
`$DESK_URI` win over the file), the guests' time zone and keyboard layout, the host's images in
`~/.config/desk/images/` and its own blocks beside them (Images). `desk doctor` shows the time zone and layout guests
get and where they come from.

```toml
# ~/.config/desk/config.toml
home = "/srv/desk"           # a file system with reflinks (btrfs, XFS) and room: 5-25 GiB an image
uri = "qemu:///session"      # the default
timezone = "Europe/Berlin"   # default: the host's (where /etc/localtime links), else UTC
layout = "pl"                # us (the default) or pl
```

Snapshots, warm-ups and `desk diff` copy disks as reflinks; on a file system without them every
copy is a full one and `desk diff` does not work (`desk doctor` says which it is). With SELinux
enforcing (Fedora) the default home works as it is: libvirt labels what its VMs use.

## Licence

MIT, see [LICENSE](LICENSE). Tools the images download (ViVeTool, Win32-OpenSSH, virtio-win)
keep their own licences and are not part of this repository.
