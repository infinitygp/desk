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
docs/observe/        desk observe: what was checked in a Windows 11 guest, the workload and its checker
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
`images/`. desk offers them with the shipped ones, but a shipped distro takes only the desktops it
lists: a desktop of the host's own goes with a distro of the host's own, and brings itself what
the shipped desktops get from `images/shared/desktop.sh`. A name is in one of the two places
only; a name desk cannot use (in both places, a desktop's or an addon's with a '-', a distro's
that with a '-' begins another's) fails only the images that use it, and `desk image blocks`
says why. A block moved from one to the other keeps its builds: fingerprints name files within
their root, as they do for a checkout and a release of the same commit. A desk before
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
  A command of several words is a program and its arguments: `guest.command` quotes each for
  PowerShell (`& 'prog' 'arg'`) and escapes the double quotes and trailing backslashes that
  Windows PowerShell 5.1 leaves as they are when it passes an argument with a space in double
  quotes; POSIX quoting (`shlex`) there turned `'a b'` into ` a b `.
- Keys go through the PS/2 keyboard. The install has a USB one as well, as this OVMF build ignores
  PS/2 at the CD prompt; QEMU queues 16 scancodes for it, which Windows takes one per poll, and a
  Windows busy for a moment (after a restore, Defender scanning) lost keys typed 40 ms apart,
  while PS/2, read by an interrupt each, kept up at 2 ms. Warm-ups and cold boots leave the USB one
  out of the domain (`virt.spare_usb_keyboards`). Unplugging it from a running guest instead lost
  keys too: Windows took the unplug seconds later, in the middle of typing. A warm state saved with
  it restores as it is, and `desk maintain` makes it again.
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
  theme, taskbar, transparency and text size where the Settings app writes them, applies contrast
  themes by opening their theme file in the session and the wallpaper with `SystemParametersInfo`
  there (from SSH it changes nothing), and reads every setting back. No accent colour: Settings
  derives a palette from it that nothing else computes.
- The in-session agent removes `PSExecutionPolicyPreference` from its environment after loading its
  own scripts: it runs with `-ExecutionPolicy Bypass`, which every program it starts would inherit,
  and a product's own scripts must meet Windows' default policy (Restricted) as on a user's machine.
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
  Host passthrough also needs QEMU's `usb-host` backend, distinct from the guest's controller:
  `desk doctor` lists it as optional, and `usb add` and `up --usb` check QEMU's advertised devices
  before attaching or starting a run, with package install hints if absent or the probe fails.
  Listing, removal and runs without host USB do not need it.
  A device that leaves the host (unplugged, or enumerated again under a new number) keeps its
  `<hostdev>` in the domain, and each entry holds one of the xHCI controller's 15 ports until
  `add` fails with "No free USB ports". `desk usb ls` shows such entries as `gone` with their run,
  and `desk usb add` takes them out of its run first, each with a line in the run's log. Only
  numbers that no device on the host has (`/sys/bus/usb/devices`, hubs included) count as gone,
  so a device that is there, even during a reset, is never taken from the guest.
  Besides VENDOR:PRODUCT and BUS.DEVICE, a device is named by its port (`port:9-2.2`, the name
  of its directory in `/sys/bus/usb/devices`) or its serial number (`serial:…`), which keep while
  the device number changes; desk looks the number up each time it needs one and refuses a name
  that fits several devices. The scenario steps `usb_add` and `usb_rm` do what `desk usb add|rm`
  does between other steps; `usb_add` waits up to its `timeout` for a device that is coming back.
- `desk up IMAGE --usbvfiod` (`desk/usbvfiod.py`, `desk/hotplug.py`, `desk/hubs.py`) gives a run a
  second USB controller where hubs and devices stand at port paths, for products that identify
  hardware by the hubs it sits behind (a base with modules) and for devices that another process
  emulates instead of hardware. The controller is served by usbvfiod, a
  private fork of a vfio-user server that emulates an xHCI controller for QEMU's `vfio-user-pci`
  device (`desk doctor` says whether the binary and that QEMU device are here; neither is
  required for anything else). A port path is the ports from the root port down:
  `5.1.4` is port 4 of the hub on port 1 of the hub on root port 5. Root ports 1-4 are USB 3 and
  5-8 USB 2; hubs, which are virtual USB 2.0 hubs of up to 15 ports, stand on 5-8, five deep at
  most. The libvirt-managed `qemu-xhci` controller stays, so the run's keyboard, tablet and every
  other `desk usb` use keep working beside it.
  - desk finds the binary in `$DESK_USBVFIOD`, else `usbvfiod` in `PATH` (the fork builds with
    `cargo install --locked --path CHECKOUT`); a missing one is refused before a VM is started.
    It runs in a session of its own in the run's directory (`usbvfiod/`: the vfio-user socket
    QEMU connects to, the hotplug socket, `usbvfiod.log` at debug level and `usb.pcap`, the
    traffic of the guest's driver), so it outlives the command that started it; its PID and
    those paths are in `run.json`, and `desk usb socket` prints the hotplug socket, for the
    fork's `remote` and for a process that serves emulated devices on it (the protocol and an
    example server: the fork's
    [External Devices](https://github.com/infinitygp/usbvfiod/blob/main/docs/users/external-devices.md)).
    It is started before the domain, and the controller is added with
    QMP `device_add` once the guest is ready, on the highest free PCIe root port (libvirt hands
    out the lowest, so extra disks added later do not meet it). It runs without
    `--reconnect`, so it ends with its QEMU even when desk does not get to stop it.
  - Two rules come from QEMU 11.1.3, which aborts the VM when a vfio-user device is removed with
    `device_del` and can abort it when the server dies: desk never removes the controller, and
    `runs.down` destroys the domain first and only then stops the server (by PID, after checking
    that the PID still is a usbvfiod of that run's socket; SIGTERM, SIGKILL after 5 s). A server that
    ended before is said so: `desk ls` shows "usbvfiod ended", every command that needs it names
    it and its log, `desk down` warns, and the report has the section described under Evidence.
  - The guest's RAM has to be shared memory, which usbvfiod maps for the controller's DMA
    (`--shared-memory`, implied). libvirt restores a saved state only into the memory backing it
    was saved with, and warm states are saved with anonymous memory (`unsupported configuration:
    Target memoryBacking source 'memfd' doesn't match source memoryBacking source 'none'`), so
    such a run boots cold: Windows 11 is ready in 20 s, a warm restore takes about 1 s. A
    second warm state per image, saved with shared memory, would remove that; it costs 10-25
    minutes of maintenance and a disk's worth of space per image, so nothing builds it unasked
    (Optional stays optional) and desk does not offer it yet.
  - `desk usb add DEVICE [--at PATH] [--hub PATH=[VID:PID][/PORTS][/hs|/fs]]…` opens the host
    device's node itself and hands it to the controller. With `--at` the hubs on the way that do not
    exist yet are made first, top down (by default 0409:55aa, 8 ports, high speed; `--hub` says
    what a hub is, and one that exists keeps what it is); a device the controller refuses takes
    the hubs made for it away again. Without `--at` the controller takes the first free root port
    that fits the device's speed, and the tree says which. `desk usb rm DEVICE` or `at:PORTPATH`
    (a hub too: everything below goes) detaches, `desk usb ls` adds the hubs and devices of every
    controller's tree. The tree is read from the controller each time and desk keeps no copy:
    other clients of the same socket make hubs of their own. A command is a connection with a limit of 10
    s (usbvfiod gives a client 5 s) and a lock of the run orders desk's own commands. The scenario
    steps `usb_add` (`at`, `hubs`) and `usb_rm` (`at:PORTPATH`) do the same between other steps,
    with `usbvfiod = true` at the top of the scenario.
  - Snapshots are refused: the controller is added to the running guest, so the domain definition
    a restore starts from does not hold it (a raw `virsh save` and `restore` gave a guest with
    the qemu-xhci controller only), and the save has already ended the server's connection.
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

A step's failure reads as a finding about the product, so whatever desk could not do or confirm
ends a scenario with exit 2 instead: lost SSH, an in-session agent that does not answer, an
accessibility tree that could not be read for as long as a step waited, a point off the screen, a
Windows setting that does not take (`config.NotDone`, from which these derive). ssh's own 255 is
told from a command's by what ssh prints last at `LogLevel=ERROR` and, when it prints nothing (a
closed connection is logged at INFO), by `exit 0` failing too; a connection the guest resets in
the handshake started nothing, so it is sent again.

A `host` step runs a program on the host between guest steps (a device emulator, a power switch,
a test server): as desk's user, from a list with no shell in between, from where `[build]` runs,
checked like an `exec` step (`timeout`, `rc`, `output`, `until`), with `DESK_RUN`, `DESK_RUN_DIR`
and `DESK_SSH_PORT` in its environment so it can aim at the run. Its line in the report and the
timeline is the command, its exit code and its time. `save = "NAME"` keeps its stdout, and
`{NAME}` in any later step becomes that value (an address to `usb_add`, a value an `exec` checks).
Loading refuses a `{NAME}` that no step before it saves, and a step run with `--from` past the one
that saves it fails as desk's. Only `{` and a name in capitals count, so `{guest:80}` and code in
braces stay as they are.

A guest without a desktop gets a browser machine (`browser = IMAGE`): a second run, of a Linux
desktop image, whose Firefox opens the guest's pages, so a11y, console errors, screenshots and the
report work there as they do on any desktop, with no browser to install on the host and no second
way of finding elements. `{guest:PORT}` in an address becomes a `desk forward` tunnel of the guest
under test, which the browser machine reaches at its default gateway, the address passt maps to
the host. Commands go to the guest under test, the rest to the browser machine, whose events and
screenshots are logged into the scenario's run (`scenario._Browser`); it is started after and
destroyed with that run, and its id in the run's log lets `--from` find it again.

`desk offline` and the offline step cut a guest off with its own firewall. passt, which connects
the guest, cannot filter what goes out (`--no-tcp` would take SSH along, and the `--outbound`
address that would keep only the host is not something libvirt passes on), and unplugging the
NIC (`domif-setlink`) drops SSH and the Windows agent too. So: an nft table of desk's on Linux
(`/etc/desk-offline.nft`, loaded again at boot by `desk-offline.service`), a chain in fw4's table
on OpenWrt (`/etc/nftables.d/99-desk-offline.nft`; fw4 keeps an unknown chain, emptied, when it
reloads, so `off` deletes it), a rule group in Windows Firewall whose block rules win over any allow
rule. Each refuses what goes out but DNS, the answers of connections into the guest (desk's SSH and
agent) and loopback, and is read back after the change.

Mouse steps send the machine's input like `desk click` does. A `press` keeps the button down for the
steps after it, and a `move` with it held glides as `drag` does, since toolkits start a
drag-and-drop only after motion with the button down; a button still held when the steps end or
one fails is let go, as every later move would otherwise be a drag. Builds: `[build.image.NAME]`
gives another image its own build over `[build]`, and a file that has such tables refuses an image
it has none for rather than copy another's packages; `--no-build` takes the newest artifacts as
they are and the report names them.

## Evidence

- `desk down` writes `reports/<run>/report.md`: failed units, unit failures from the journal since
  the start across reboots, SELinux denials, coredumps, journal warnings, package changes (dnf's
  UTC times made local) and the timeline with screenshots; `--diff` adds the files the run
  changed, from `virt-diff` against the image the run started from. What the guest could not be
  asked about says "NOT CHECKED" instead of counting nothing.
- A run with the usbvfiod controller says in "Found automatically" whether its server runs or has
  ended (and that the VM may have gone with it), how many warnings and errors its log has, and
  the controller's tree at the end; `usbvfiod.log` and the pcap of the guest's USB traffic (up to
  32 MiB) go into the report's directory.
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

## Observation

`desk observe` (`desk/observe/`, [skill/observe.md](skill/observe.md)) records inside a Windows
guest with Windows' own tools and the user's: Process Monitor (PML), ETW through WPR or logman
(ETL), pktmon (ETL, PCAPNG) and netsh trace (ETL), USBPcap (pcap), listings of pipes and sockets, and
debugger servers forwarded to the host. Providers build command lines and read answers; the
orchestration (`desk/observe/__init__.py`) keeps the session and its guarantees:

- A session belongs to its run (`runs/<id>/observe/`), with states starting, running, ended,
  interrupted, stopping, stopped and failed, and a lock per session so two commands never stop
  one capture twice. Guest folders and trace names carry the run and session
  (`C:\desk\observe\<run>-<sN>`, `desk_<run>_<sN>`).
- What runs in the guest goes through one helper, `helper.ps1`, pushed when its SHA-256 differs
  and run as a script block (no execution policy applies), over the same SSH as `desk exec`, with
  the time limit kept in the guest. Captures and watchdogs are started through WMI
  (`Win32_Process.Create`), outside the SSH session: Windows' OpenSSH ends every process of a
  session when it closes, and one WMI creates holds none of its handles and keeps the elevated
  token. Never through the in-session agent, which serves one request at a time. Processes are
  told apart by PID and creation time, as Windows reuses PIDs.
- Providers (`base.Provider`) say what they need (`probe_request`, `assess`), start, stop and
  name their files (`artifacts`, `convert`), and keep their guest state in `session.tool`; they
  never touch the store, the report or the run.
- desk stops only what it started: a ProcMon or packet capture it did not start makes `start`
  refuse, `procmon /Terminate` (which ends every instance) runs only while desk's is alone, WPR
  records under an instance name of desk's own and logman under a session name of desk's own, a
  packet capture is desk's when its trace session writes the session's folder (its
  LocalFilePath; netsh's session is named NetTrace-<hex> on Windows 11), and a tool a desk that
  was killed while starting never recorded is found by the session's folder on its command line.
- Stopping several sessions (`stop --all`, `observe_stop = true`, `desk down`) goes in two
  passes: every capture is stopped, then each is converted and copied, so no capture records
  desk taking the others' files. One that does not stop keeps its state and gets a watchdog for
  the time it has left, to be stopped again; the others are collected.
- pktmon writes segments (`--log-mode multi-file`, net1.etl, net2.etl...): its default circular
  file overwrote what did not fit, and one that went round kept no packet while pktmon said none
  were lost. Every segment is copied and turned into PCAPNG; one missing from the row makes the
  capture incomplete.
- Secrets (IDA's password) are options marked `secret`: never kept with the session, its report
  or the timeline (`--set password=` shows as hidden), sent to the guest on SSH's standard input
  (the helper's request is the guest command line every process listing shows) and handed to the
  tool in its environment. Without one desk makes one, kept in a 0600 file next to the session
  until the server stops.
- Every capture has a watchdog in the guest (`helper.ps1 -Watch`) that stops it at its duration
  (by a stopwatch, so `desk clock` does not move it) or its files' size, also when desk is gone,
  and writes why; `desk down`, gc and the end of a scenario stop and collect what is open.
- A capture counts as made only by its file: copied, compared by SHA-256 with the guest's, read
  as its format (`formats.py`: PML header and tables, pcap and pcapng record by record, ETL only
  as there and not empty). A file that is not whole stays on the host marked so, one that could
  not be copied stays in the guest, and the session is not complete; exit 2, desk's failure.
- Lost events are what the trace session counted before it stopped (ControlTrace's query:
  EventsLost, LogBuffersLost, RealTimeBuffersLost; the session's CIM class holds only its settings);
  where Windows does not answer, the report says they were not measured.
- Clocks: the guest's time is sampled through qemu-guest-agent at start, every mark and stop, so
  guest times are moved onto the host's, which the timeline, screenshots and video use (the
  helper's own clock, with its larger uncertainty said, when the agent does not answer). A mark
  writes a file the guest's file system sees (ProcMon, file I/O traces) and an event of the
  EventSource `Desk-Observe-Marker`, whose provider GUID follows from its name and which desk's
  logman sessions enable. Every helper answer says when its work began and how long it took in
  the guest; an IPC listing keeps when it was made and how long after its mark, and a mark that
  desk took long over says so, as one desk call took 4-5 s under ProcMon (SSH and PowerShell's
  start 3-4 s of it) and up to 80 s once.
- Reboots end captures (Windows' boot counter, BootId, differs: interrupted; the boot time
  would move with the clock desk sets after a warm restore); snapshots carry the sessions, so a
  restore says which captures went back to the snapshot's moment, which were never there, and
  brings a capture that was collected since back as a session of its own.

What was checked in a Windows 11 guest, how, and what is still open:
[docs/observe/validation.md](docs/observe/validation.md).

## Exit codes, locks, limits

- 0: done. 1: the check a command stands for failed (a scenario step, `selftest`, `find`,
  `wait`, `browser open`, `doctor`). 2: desk could not do what was asked; one line says why, also
  when a host tool, libvirt or the disk failed (`DESK_DEBUG=1` adds the traceback). 130:
  interrupted. `desk exec` passes on the guest command's code, 124 when it ran out of time
  (`push`/`pull` too), and 1 for a code an exit status cannot hold (Windows' -196608 or
  0xC0000135), which it prints in full: as its last byte -196608 would read as success.
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
| Single features | `python-pillow` (crops, several monitors), `qemu-hw-display-virtio-gpu-pci` (`--resolution`, `--displays`), `qemu-hw-usb-host` (`usb add`, `up --usb`), `guestfs-tools` (`desk diff`), `gstreamer` `gst-plugins-base` `gst-plugins-good` (`desk record`), `github-cli` (installing and `desk update` from a private repository) | `python3-pillow`, `qemu-device-display-virtio-gpu-pci`, `qemu-device-usb-host`, `guestfs-tools`, `gstreamer1` `gstreamer1-plugins-base` `gstreamer1-plugins-good`, `gh` |

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
