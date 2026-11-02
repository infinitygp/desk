---
name: desk
description: Test desktop applications and system packages (rpm, Arch packages, Flatpak, AppImage, installers) end to end in a disposable VM with a real desktop session, judging the result from screenshots and the accessibility tree. Use when a change must be verified the way a user would see it on a real OS - installing a package, launching a GUI app, clicking through it, checking autostart, tray, dialogs, polkit prompts, reboots - rather than with unit tests or a browser alone.
---

# desk: disposable desktop VMs

`desk` (on PATH; `desk doctor` prints where its source and this host's images are, and where
runs and reports are kept on this host)
starts a throwaway VM from a golden image with a logged-in desktop in about two seconds, and
drives it like a person would: real mouse and keyboard input from outside the guest, the screen
as PNG, and the accessibility tree as text. Nothing in the guest can hide the screen or block
that input, so it works for any toolkit, login screens, installers, polkit dialogs, reboots and
crashes alike. Every run is a copy-on-write overlay; `desk down` throws all changes away.

Checks that need no desktop and no real boot (an rpm installing with its scriptlets, a CLI, a
systemd unit) run faster in a container: `podman run --rm -v "$PWD:/src:Z" fedora:44 dnf install
-y /src/foo.rpm`, with `--systemd=always` for units. desk is for what a user meets on a real OS:
the desktop, a boot, SELinux, polkit, reboots.

Read the file for your task before you start:

- Windows guests (`windows-11`): [windows.md](windows.md)
- What a Windows program does inside (files, registry, processes, ETW, packets, USB, pipes) and
  debugger servers in the guest: [observe.md](observe.md)
- Cockpit plugins, also on phone-sized screens: [cockpit.md](cockpit.md)
- OpenWrt packages and LuCI apps: [openwrt.md](openwrt.md)
- Monitors (resolution, scaling, several displays), extra disks, host USB devices (hardware the
  product talks to), the guest clock (timers, expiry, retention), surviving a Fedora upgrade:
  [system.md](system.md)

## Loop

```bash
desk up fedora-44-gnome             # prints the run id; desk -r <id> ... when several runs exist
desk exec --gui -- gnome-calculator # start an app in the session
desk wait --change                  # until its window has appeared and settled
desk a11y                           # focused window as text: @refs, roles, names, values, click points
desk click @e7                      # click an element from that listing (real mouse click)
desk click --name "Log in"          # or find it by name on the fly
desk type 'hello world'             # real key presses (Polish letters too on the pl layout)
desk shot                           # prints the PNG's path and size (.../shots/0001.png 1280x800); Read it
desk down                           # destroy the VM and its files (--keep to keep them)
```

Use `desk a11y` / `desk find` to know what is on screen and where, and to read values back.
Use screenshots to judge how it looks (layout, clipping, icons, colours, rendering) and whenever
the tree is empty or doubtful. Pixel coordinates are those of the screenshot, origin top-left.

## Commands

| Command | Purpose |
|---|---|
| `desk a11y [--app NAME] [--all] [--offscreen] [--max N]` | Accessibility tree of the focused window (`--all`: every window and the shell; `--offscreen`: also what is scrolled out of view); `[covered] under X` marks what a sticky bar, toast or popup lies over (the element's own label over it does not count: clicking it is clicking the element) |
| `desk find TEXT [--role R] [--exact] [--all]` | Elements whose name, value or text matches (a text element's first 10,000 characters, a whole log in a `<pre>`; listings show 160), off-screen ones included; refs usable right away |
| `desk click (X Y \| @eN \| --name TEXT [--role R] [--exact]) [--button right] [--double]` | Click; an off-screen, covered or cut-off (by a scrolling area's edge) @ref or name is scrolled into view first, and the click goes where the element itself is hit, never onto what covers it; `--role` also takes ARIA names (`tab`, `checkbox`, `textbox`) |
| `desk action @eN [NAME]` | Invoke an AT-SPI action instead of clicking (element covered or off-screen) |
| `desk set-text @eN TEXT` | Replace an editable field's text as a user would: focus it, select all, type (or paste what the keyboard cannot type); web forms (React) see it; says when the field shows something else afterwards |
| `desk shot [--label L] [-o FILE] [--settle S] [--region X,Y,W,H \| --window NAME] [--zoom N]` | Screenshot once the screen is still (up to 3 s; `--settle 0` = now); `--region`/`--window` cut out a part, `--zoom 2` enlarges it to judge details |
| `desk move X Y`, `desk drag X1 Y1 X2 Y2`, `desk scroll X Y DY` | Pointer; `DY>0` scrolls down |
| `desk type TEXT [--anywhere]` | Type with real key presses: ASCII, and Polish letters where the guest's layout is pl (`desk doctor` names the one new guests get); text starting with a dash works. In a browser it refuses when no text field has the focus (the page would take the keys as shortcuts); `--anywhere` types anyway |
| `desk key COMBO...` | `super`, `enter`, `esc`, `tab`, `ctrl+alt+t`, `shift+f10`, `alt+f4`, `f5`, letters |
| `desk snapshot save NAME`, `desk snapshot restore NAME`, `desk snapshot list`, `desk snapshot delete NAME` | Save the running guest (~3-5 s) and go back to it (~1-3 s) |
| `desk up IMAGE --resolution WxH --scale N --displays N`, `desk display [--resolution WxH] [--scale N]` | Monitors: mode, scaling in percent, several displays ([system.md](system.md)) |
| `desk up IMAGE --memory 8G --cpus N` | Memory and vCPUs of this run instead of the image's (4 GB and 4 on the desktop images, 6 GB on Windows); other than its warm state's, the run boots cold (~25 s) |
| `desk focus NAME` | Bring a window to the front by application or title (keys go to the focused window); on Windows it fails when Windows keeps the window behind the one in front |
| `desk scenario run FILE [--keep]`, `desk scenario check FILE` | Run a repeatable test from a TOML file (see below) |
| `desk wait [--change] [--timeout S]` | Until the screen is still; `--change` first waits for it to change at all |
| `desk wait --until-exec CMD [--root] [--timeout S]` | Run CMD in the guest every 2 s until it exits 0 (a service up, a file written; default 120 s), instead of a polling loop |
| `desk reboot [--shot] [--prepare]` | Reboot and return when the new session is ready; reports apps that blocked shutdown; `--prepare` then redoes the image's preparation (Cockpit logged in again) |
| `desk exec [--root] [--timeout S] -- CMD` | Run in the guest over SSH as the desktop user (`--root`: as root); stopped in the guest after 600 s by default (exit 124; `--timeout 0`: no limit) |
| `desk exec --gui -- APP ARGS` | Start a program detached inside the graphical session |
| `desk exec --gui --wait -- CMD` | Run inside the session and return its output and exit code: what only the desktop session sees (windows, FindWindow, UI Automation, the session's environment) |
| `desk exec --env NAME=VALUE ...` | Environment for the command (with or without `--gui`, repeatable) |
| `desk exec --agent -- CMD` | Run as root through qemu-guest-agent; works without guest networking |
| `desk push [--root] LOCAL REMOTE`, `desk pull [--root] REMOTE LOCAL` | Copy files (scp) as the desktop user; `--root` for `/root`, `/etc` and the like; stopped after 600 s (exit 124; `--timeout S`, 0: no limit) |
| `desk diff [--all]` | Files changed in the guest since the image it started from: added, changed, removed (~30 s) |
| `desk report [--diff]` | Write the evidence report now (see below) |
| `desk ls`, `desk down [ID] [--all] [--keep] [--diff] [--no-report]`, `desk gc` | List (with who holds each run), destroy (writing the report first), clean up idle runs |
| `desk view` | Tell the user how to watch the run live (VNC through an SSH tunnel) |
| `desk forward PORT[:LOCAL]`, `desk forward`, `desk forward --stop PORT\|all`, `desk up IMAGE --forward PORT` | Reach a guest port from the host (127.0.0.1, the same number when free and not another run's tunnel, else printed) for as long as the run exists, across reboots and snapshot restores; a port already forwarded keeps its local port (`--stop` it first to move it) |
| `desk browser open URL\|/PATH [--browser chromium]` | Open an address in the current tab of the browser in the focused window (Firefox when no browser is; `--browser firefox\|chromium` picks one and starts it), as a link from another program would; a path is opened on the site shown now (`/podman`, `/system/services`); an address that differs from the page shown only after `#` loads that page afresh (the browsers load no page for such a link from another program). Exits 1 and says what the browser shows instead when the page did not load (server not found, connection refused, a certificate warning, a server that does not answer) |
| `desk browser viewport WxH\|reset [--browser chromium]` | Pages at WxH CSS pixels (a phone: `390x700`) through Firefox's responsive design mode or Chromium's device mode (DevTools docked in its window, the page at 100 %); prints the `desk shot --region` of the page area |
| `desk browser console [--errors] [--all]` | Web console of every page since the run started (see Pitfalls) |
| `desk record start [--fps N]`, `desk record stop`, `desk up --record` | Record the screen as a WebM video, kept with the report |
| `desk usb ls`, `desk usb add VENDOR:PRODUCT\|BUS.DEVICE`, `desk usb rm ...`, `desk up IMAGE --usb ...` | Hand a host USB device to the guest ([system.md](system.md)) |
| `desk observe start PROVIDER`, `desk observe mark TEXT`, `desk observe stop`, `desk observe providers` | Record what a program does inside a Windows guest with its own tools (Process Monitor, ETW, pktmon, USBPcap, IPC listings, debugger servers); files copied, hashed and checked ([observe.md](observe.md)) |
| `desk doctor` | What this host has: where desk keeps its files, which host tools are missing (with the command that installs them), which images are built |

Action commands (`click`, `type`, `key`, `scroll`, `action`, ...) accept `--shot` to screenshot
once the screen settles. With one argument, `desk exec -- 'a && b | grep x'` goes through the
guest shell; `--root` and `--gui` apply to the whole snippet.

## Pitfalls

- Exit code 1 means the check failed: a scenario step, `desk find` with no match, `desk wait` on a
  screen that keeps changing, a failing selftest. Exit code 2 means desk itself could not do what
  was asked (no such run, a host tool or libvirt failing, the guest unreachable); its one line
  says why. A 2 is never a finding about the product under test: fix the cause or report it as
  the harness's, and do not count the test as failed.
- Your own shell tool may stop a command after about 10 minutes, whatever `desk exec --timeout`
  says. For longer guest work (an upgrade, a big build, a slow backup) run the desk command in
  the background with your tool's background option and wait for it to finish, or start it
  detached in the guest (`desk exec --root -- 'systemd-run --unit=job CMD'`) and poll
  `systemctl is-active job` with short `desk exec` calls.
- A still screen is not proof that something happened: right after `desk exec --gui` nothing has
  changed yet, so plain `desk wait` returns at once. Use `desk wait --change`; when an app is slow,
  check `pgrep` in the guest before concluding it failed.
- Refs (`@e7`) come from the latest `desk a11y` or `desk find`; take a new one after the UI changes.
  `desk click @eN` re-reads the element's position, so moved elements are still hit; one that moved
  while the window has others of its role and name (two "Delete" buttons) is an error: list again.
- `click --name TEXT` (and scenario `click`) takes the element whose whole name is TEXT, written
  as given before one that differs in case only ("registry" before "Registry"); only if there is
  none does a part of a name do, and then only when it is part of one name: "Save" next to
  "Save as..." clicks "Save", "Sav" is an error listing both. Several elements of that very name
  (a "Delete" in each row) give a warning that lists where they are; the first is clicked, `--role`
  or an @ref from `desk find` picks another.
- `desk a11y` lists what is visible and ends with "N more outside the visible area" when a page or
  list is scrolled; `desk find` searches those too. Long forms and dialogs at 1280x800 often hide
  their last fields and buttons that way.
- Toolkits report positions themselves. GTK4 places header bar buttons about 6 px up and left
  of where they are drawn (the click still lands inside them); if a click by ref misses a small
  element, check with a screenshot and click by pixels.
- Browsers build their accessibility tree lazily: if a page's content is missing from the first
  `desk a11y`, run it again.
- Web pages' console: `desk browser console` (`--errors`: console.error, uncaught exceptions and
  rejected promises only) prints what every page in Firefox and Chromium logged since the run
  started, iframes included (Cockpit plugins run in one). The browsers' own messages are left out
  (`--all` shows them). In Firefox an uncaught exception or rejected promise names the file and
  line it came from and, after `= `, the thrown value and its stack (`uncaught exception: Object =
  {"problem":"not-found"} (at load@https://localhost:9090/cockpit/@localhost/foo/index.js:2:41 <
  ...)`): the URL tells the plugin's code from Cockpit's. Chromium logs console.error at the level
  of console.log, so its messages are listed as `Chromium console: "..." (URL, line N)` whatever
  they are, and `--errors` has only its uncaught exceptions and rejected promises (`Uncaught
  TypeError: ...`, without the thrown value or stack) and says how many others could be errors;
  of a message of several lines Chromium's log keeps the first. Reading only; to evaluate
  JavaScript open the devtools (Firefox `desk key ctrl+shift+k`, Chromium `ctrl+shift+j`) and type.
- Layouts that are still building move what `desk a11y` listed a moment ago; clicks by @ref or name
  look the position up again, but a click by pixels from an old listing can miss. Typing after a
  missed click is refused in a browser rather than sent to the page as shortcuts.
- `desk type` only knows ASCII, and Polish letters on the pl layout; for anything else use
  `desk set-text`.
- In a GTK file chooser, `ctrl+l`, type the path, then click "Open": Enter may only accept the
  path completion.
- `pgrep -f`/`pkill -f PATTERN` in `desk exec --gui -- '...'` also matches the `systemd-run` that
  runs the snippet (and `pkill` kills it); write the pattern as `[g]st-launch` so it does not match
  itself. Plain `desk exec` and scenario `exec`/`root` steps hand the snippet over unseen.
- `dnf` prints its progress on stderr even with `-q`; add `>/dev/null 2>&1` and check the result with
  `rpm -q` / `rpm -V` instead of reading the transaction log.
- Web UIs on `https://localhost` with a self-signed certificate (a Cockpit installed by the test
  itself; `fedora-44-gnome-cockpit`'s browsers trust its own) show the browser's warning on every new
  run: `desk click --name Advanced`, then `desk click --name Proceed` (it scrolls itself).

## Evidence report

`desk down` first writes `reports/<run>/report.md` under desk's home and prints its path: failed
units (now, and every unit failure since the start, before reboots too), SELinux denials, crashes,
journal warnings, package changes and web console errors since the run started, plus a
timeline of every desk command with its exit code and the screenshots taken. `desk down --diff`
adds the files the run changed. Without `--keep`, `desk down` deletes the run's directory (disk,
snapshots, extra disks); screenshots and logs stay in the report. Point the user to the report
and summarise what it found; read `journal.txt` / `avc.txt` next to it before blaming the product
for a warning. A report that says "NOT CHECKED" could not ask the guest (it was unreachable):
that is no clean bill, so say that the guest's logs were not looked at.

Record a video (`desk up --record`, or `desk record start` before the interesting part) when the
user will want to see the flow itself: a walkthrough of a feature, an installer, a bug that needs
several steps or depends on timing. `desk down` stops it and puts `video-N.webm` next to the report,
linked from the timeline; the wall-clock time below the picture matches the report. You cannot
watch the video yourself, so keep taking screenshots for your own judgement. Recording stops by
itself after 30 minutes (`--limit`).

## Images

An image is made of blocks, and its name says which: a distro, the desktop it runs, addons on top
(`fedora-44-gnome-cockpit` is Fedora 44 with GNOME and Cockpit). `desk image list` shows the
images this host has defined and which of them it has built: a host keeps the images it tests on,
not all of them. When `desk up` says an image is not defined here, it prints the
`desk image add ...` that defines it; run that, then build that one image (`desk maintain --only
NAME` builds, warms and selftests it; 10-25 minutes and 5-25 GB, so not the others along with
it). An image with an addon runs on the install of the one without (`-cockpit` on
`fedora-44-gnome`) and only needs its own warm state once that is built; without a warm state a
run still starts, cold, in ~20 s. `desk image blocks` lists what images can be made of,
`desk image show NAME` the settings an image ends up with.

`fedora-44-gnome` is the default; `fedora-45-gnome` is the next Fedora release (beta until it is
out); `fedora-44-gnome-cockpit` has Cockpit open in Firefox, logged in as tester with
administrative access (its Chromium trusts Cockpit's certificate too, logged out); `arch-gnome` is
Arch Linux (rolling, pacman, no SELinux) with the newest GNOME and libraries; `debian-13-gnome` is
Debian 13 (apt, GNOME 48, Firefox ESR); `ubuntu-26.04-gnome` is Ubuntu 26.04 LTS with Ubuntu's own
desktop (Firefox from Mozilla's repository, no Chromium); `fedora-44-kde`, `arch-kde`,
`debian-13-kde` and `ubuntu-26.04-kde` run KDE Plasma (KCalc, KWrite, Qt apps); the same four
distros come with Xfce and Cinnamon too, on X11 (`fedora-44-xfce`, `ubuntu-26.04-cinnamon`; no
`--scale` there); `arch-hyprland`,
`debian-13-hyprland` and `ubuntu-26.04-hyprland` run Hyprland, tiling, with GNOME's calculator and
editor (no Fedora: it has no Hyprland); `windows-11` is
Windows 11 Enterprise (evaluation) for Windows builds and installers; `openwrt-25.12` is OpenWrt
25.12 without a desktop, for OpenWrt packages and LuCI apps. Images are as old as their last build
(`desk image list` shows the date) unless the host renews them with `desk maintain`, by hand or
from its weekly timer; if `desk up` prints a warning about a failed maintenance or a licence
running out, pass it on to the user.

## Snapshots

When a test needs several attempts from the same point (a wizard, an installer's later pages,
a bug that takes setup to reach), `desk snapshot save NAME` there and `desk snapshot restore NAME`
before each attempt, instead of a new run and the setup all over again. Restoring puts back
memory, disk and screen exactly (programs opened later are gone) and syncs the clock. Each
snapshot keeps the guest's memory on disk (2.5-4 GB) until `desk snapshot delete` or `desk down`.

## Scenarios: repeatable tests

When a project has `test/desk/*.toml` (or the user wants a check that can be repeated), run it
with `desk scenario run FILE`: it builds on the host (`[build] run`, at the top of the repository,
or in `dir`, relative to the scenario file: `dir = "../.."` for one project of a repository of
several), copies the newest file matching each `artifacts` pattern (relative to the same place)
to `/tmp/desk/` in the guest, starts a run of `image`, executes the steps in order and stops at
the first failure with a screenshot. Exit code 0 or 1, and 2 when a step lost SSH to the guest (a
step that reboots it with a command says `rc = 255`; a connection the guest resets in the
handshake is tried again first), could not reach a Windows guest's in-session agent or could not
read the accessibility tree for as long as it waited (`expect`, `expect_absent`, `click`); after a
lost SSH the message says whether the VM still runs and what the host had (free disk, memory, swap, other
runs), as a full disk ends a VM; the report (and the video with `record = true`) shows every step
in its timeline.

```toml
image = "fedora-44-gnome-cockpit"
record = true
[build]
run = "make rpm"
artifacts = ["cockpit-foo-*.noarch.rpm"]
[[step]]
root = "dnf install -y /tmp/desk/cockpit-foo-*.noarch.rpm"   # also: exec (as tester); rc, output
[[step]]
focus = "Firefox"            # say which window keys go to; never rely on what happens to be in front
[[step]]
key = "f5"                   # one combo or a list
[[step]]
click = "Foo"                # by accessible name in the focused window; role, exact (default true), button
role = "link"
[[step]]
expect = "Nothing here yet"  # waits until shown (name, value or text); role, exact, timeout, window, state
[[step]]
shot = "empty-state"
```

Other steps: `type`, the machine's mouse at screen pixels read from a screenshot (`click = [x, y]`
with `button` and `double = true`, which a click by name takes too; `move = [x, y]` to hover;
`drag = [x1, y1, x2, y2]` with the left button, moved as a hand would; `scroll = [x, y, notches]`,
positive down; a point off the screen fails the step as desk's, exit 2, with nothing clicked;
`move`, `drag = ["from name", "to name"]`, `press` and `release` also take elements by name, with
`role`, `exact`, `timeout`, `window` as a click: `press = "Media control"` keeps the button down
(`button` for another) for the steps that follow, so `expect`, `shot` and `key = "escape"` see the
drag in progress, a `move` with it held glides there for drag-and-drop, and `release = true` lets
go where the pointer is, or after gliding to a name or [x, y]; a button still held when the steps
end or one fails is let go),
`set_text = "Repository URL"` with `text = "/srv/repo"` (`role`, `exact`,
`timeout`, `window` as on a `click`; `text = ""` empties it: the field's text replaced as `desk
set-text` does, in a web page, a GTK/Qt application or a Windows one, and read back: a field that
shows something else afterwards, a read-only one, fails the step and says what it shows),
`gui = "firefox https://..."` (with `env = {NAME = "value"}`; on Windows the
string splits like a Windows command line, so `C:\path` stays as it is and paths with spaces
need quotes, or give a list; it starts the program in the desktop session and waits until the
screen changes; with `rc` or `output` it waits for the program to end instead and checks it like an
`exec` step, as `exec --gui --wait`: what only the session has, its display for `wl-paste` or
`xdotool`, UI Automation on Windows, an installer whose end matters (`gui = ["msiexec.exe", "/i",
'C:\Users\tester\desk\app.msi', "/qn"]` with `rc = 0`), with `timeout` (600 s; stopped in the
guest after it) and `until`), `expect_absent` (after a reload, first
`expect` something that is always there, or the check passes on a half-loaded page), `reboot = true`,
`wait = true` (screen still) or `wait = 2` (seconds), `until = true` on an `exec`/`root` step
(repeat it every 2 s until it gives the expected `rc`/`output`, `timeout` 120 s by default: a
service coming up; an `exec`/`root` step that runs out of its `timeout`, 600 s without `until`, is
stopped in the guest and fails as "still running"), `timeout` on a `click` (how long to wait for
the element, 60 s by default), `button = "right"` (or `"middle"`) on a `click`, `state =
"focused"` on an `expect` (or `checked`, `selected`, `expanded`, `pressed`, `editable`, `active`,
`disabled`, or a list of them: the element must have them all; of several elements with the name, the
one that has them, so `expect = "Device"` with `state = "focused"` asks which element has the
keyboard focus; one that is there without them fails and says which states it has), `window =
"TEXT"` on a `click`, `expect` or `expect_absent` (look for the element in the windows whose title or application contains TEXT instead of
the focused one: the taskbar, `window = "explorer"` on Windows, where it has no title, or a tray's
flyout, `"System tray overflow window."`, which stays open after a choice in a tray menu and whose
arrow reads `Show Hidden Icons Hide` while it is open; GNOME's top bar, its indicators (tray icons)
and their menus are `window = "gnome-shell"`, on every step in such a menu too, as a menu of the
shell leaves the focus with the window that has it; the click is the machine's mouse, so what it opens comes
to the front as it does for a user, and on Windows a menu it opens is the focused window of the next step),
`clock = "+1d"` (or `"reset"`),
`open = "/example#/tasks"` (`desk browser open`: a path on the site shown or a whole address; fails
when the page did not load; `timeout`, 30 s), `viewport = "390x700"` (or `"reset"`; `desk browser
viewport`; both take `browser = "chromium"` or `"firefox"`, else the browser in the focused window),
`disk_add = "8G:data"`, `disk_rm = "data"` (top-level `disks = ["8G:data"]` plugs them in
before the first step, `usb = ["0fd9:0084"]` hands host USB devices over), `console_errors = 0` (at
most that many new errors in the web console since the start or the previous such step; end a UI
scenario with one. Chromium's messages may be console.error, which its log does not tell: any
new one makes the step fail with exit 2, desk's failure, not the product's). In a scenario's
report every console message is tagged with the step running when it was logged (`[before the
first step]` for what the warm state and setup logged; a page logs a moment after the step that
opened it, so look at the step before as well), so no extra step is needed just to separate setup
noise. `[[observe]]` tables start
captures before the first step, `observe_start`, `observe_stop` and `mark` steps drive them
([observe.md](observe.md)). Every step takes `name` for the
report, and `soft = true` for an independent check whose failure should not stop the steps after it.
`desk scenario check FILE` validates a file without starting anything: the image, every value's
type, and options a step would not read (`rc` on a `click`). When a scenario fails,
look at the failure screenshot and the report before changing the scenario: a failing step is
usually a real finding. A failed `exec`/`root` step prints the end of its stdout and stderr, where
the cause usually is. An element that did not appear (`expect`, `click`) comes with the focused
window, similar names on screen and, on GNOME, the overview or a notification banner over the
windows: a typo, the wrong window in front or a prompt of the system shows there without a
screenshot. `desk scenario run FILE --keep` writes the report and leaves the run running
(it prints the `desk -r ID` to use) to go on by hand from where the scenario stopped; `desk down` it
afterwards (left alone, it is destroyed after 2 h idle). While writing a scenario, fix the failed
step in the file and try it again in that run with `desk -r ID scenario run FILE --from N`: steps
N.. only, no build and no new run, seconds instead of the whole scenario; the run stays, and its
report (`desk down`) counts each step's last attempt. Finish with one whole run from the start,
which is what the scenario has to pass. `scenario check` and `run` warn about fixed waits
(`wait = 2`, `sleep` in a command): they fail on a busy host and waste time on an idle one, so
`expect` what should appear, or repeat a check with `until = true`. A `type` step refuses, like `desk type`,
when a browser's focus is not in a text field; `anywhere = true` types anyway (page shortcuts).
If the run itself cannot start, `desk` exits 2 and names a screenshot of what the screen showed
then (`runs/<id>/failed-start.png`).

## Testing a package end to end

1. Build the package on the host as the project normally does.
2. `desk up <image>`, then `desk push ./dist/foo.rpm /tmp/`.
3. Install as a user would: `desk exec --root -- dnf install -y /tmp/foo.rpm`.
   Check scriptlets and files: `rpm -V foo`, `systemctl status ...`, `journalctl -b`.
4. Launch it the way a user would - from the GNOME overview (`desk key super`, type the name,
   `enter`) or `desk exec --gui -- foo` - and walk through the feature with `desk a11y`, clicks and
   screenshots.
5. Verify side effects in the guest with `desk exec`, not only on screen.
6. When relevant: upgrade from the previous version, remove (`dnf remove` / `pacman -R`),
   `desk reboot` and check what remains or comes back (autostart, enabled units, timers);
   `desk diff` shows exactly which files the package left behind.
7. `desk down --diff` and hand over the report.

## Judging screenshots

- Describe what is actually on the screen before concluding; check text, layout, clipping,
  icons, focus and error dialogs, not just that a window appeared.
- When something looks wrong, confirm it in the guest (logs, files, process list) before
  reporting it.
- Keep the shot paths of the key states; they are the evidence for the report. Use
  `desk down --keep` when the user should be able to look at them afterwards.

## Notes

- The desktop guests run GNOME on Wayland with user `tester` (password `tester`) with
  ordinary sudo, so sudo, polkit and Cockpit's administrative access ask for that password as on a
  real machine (`desk exec --root` does not need it), 1280x800, en_US locale, the host's time zone,
  the US keyboard layout (Polish (programmer's), US plus AltGr letters, where the host's
  `config.toml` says `layout = "pl"`),
  animations, screen lock and the hot corner off, session starts on the desktop rather than the
  overview. GNOME Software is masked, so no update banners or prompts cover the app under test
  and the overview's search does not list it; to test something about Software itself:
  `desk exec --root -- systemctl --global unmask gnome-software.service`, then `desk reboot`.
  Maintenance timers (fstrim, logrotate, plocate, dnf-makecache, raid-check, unbound-anchor; on
  Arch the keyring sync and shadow) are disabled, since after the clock catches up on restore they
  ran at random moments of a test; when the test is about one of them, `systemctl start` its
  service (the product's own timers are untouched, and `desk clock --step` runs them). Firefox
  starts without first-run tabs, the default browser question, offers to save passwords, studies
  or tips (`/etc/firefox/policies/policies.json`); Chromium, next to it in the Linux desktop
  images, without its first-run dialog, a keyring password prompt, the default browser question,
  offers to save passwords or to sign in (`/etc/chromium/policies/managed/desk.json`, and its flags
  in `/etc/chromium/chromium.conf`, on Arch `/etc/chromium-flags.conf`). In both an address opened
  from another program replaces the page in the current tab.
- Guest clocks are synced on restore; network goes through user-mode networking (outbound
  works, inbound only SSH; `desk forward PORT` makes a guest service reachable from the host).
- Several runs can exist at once; pass `-r ID` (an exported `DESK_RUN` points every later command
  of the shell at one run, also those meant for another). `desk ls` shows who holds each:
  the scenario, `desk up --label TEXT` (or `$DESK_LABEL`), or by default the coding agent and its
  directory ("claude @ cockpit-example", "(agent gone)" once it has exited). Leave other agents'
  runs alone. Only one run per image is restored from the warm state (~2 s); further ones boot
  cold (~25 s) from the same disk, so they start with the same files, only without the uptime.
  Each takes ~4 GB RAM (`--memory`); when the host is short of memory (too little available, or already
  swapping hard) `desk up` waits up to 10 min for other runs to end, then gives up; scenarios
  started in parallel wait in line that way instead of slowing each other down. Runs idle for 2 h
  are destroyed by the next `desk up` or `desk gc` (`DESK_TTL` seconds to change), their report
  written first.
- The image is a clean Workstation install: anything the product needs beyond its declared
  dependencies is missing there, exactly as on a user's machine. Report that rather than
  silently installing it; install it afterwards only to continue the test.
- `desk selftest <image>` checks the image and the harness end to end. `desk maintain` renews
  the images in use on the host, those with a warm state (rebuilds, selftests, switches builds), when it is run, by hand or
  from the weekly timer a host may have (`desk doctor` says). After changing a block in
  `images/` of the source repository, run `desk maintain --only <name>` for an image that uses it:
  it rebuilds what changed and switches only after the selftest passes (`desk image build --force`
  / `desk image warm --force` switch without testing).
