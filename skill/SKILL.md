---
name: desk
description: Test desktop applications and system packages (rpm, Arch packages, Flatpak, AppImage, installers) end to end in a disposable VM with a real desktop session, judging the result from screenshots and the accessibility tree. Use when a change must be verified the way a user would see it on a real OS - installing a package, launching a GUI app, clicking through it, checking autostart, tray, dialogs, polkit prompts, reboots - rather than with unit tests or a browser alone.
---

# desk: disposable desktop VMs

`desk` (on PATH; `desk doctor` prints where its source and image definitions are, and where
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
- Cockpit plugins, also on phone-sized screens: [cockpit.md](cockpit.md)
- OpenWrt packages and LuCI apps: [openwrt.md](openwrt.md)
- Monitors (resolution, scaling, several displays), extra disks, host USB devices (hardware the
  product talks to), the guest clock (timers, expiry, retention), surviving a Fedora upgrade:
  [system.md](system.md)

## Loop

```bash
desk up fedora-44-gnome             # prints the run id; export DESK_RUN=<id> if several runs exist
desk exec --gui -- gnome-calculator # start an app in the session
desk wait --change                  # until its window has appeared and settled
desk a11y                           # focused window as text: @refs, roles, names, values, click points
desk click @e7                      # click an element from that listing (real mouse click)
desk click --name "Log in"          # or find it by name on the fly
desk type 'Zażółć gęślą jaźń'       # real key presses, Polish letters included
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
| `desk find TEXT [--role R] [--exact] [--all]` | Elements whose name, value or text matches, off-screen ones included; refs usable right away |
| `desk click (X Y \| @eN \| --name TEXT [--role R] [--exact]) [--button right] [--double]` | Click; an off-screen, covered or cut-off (by a scrolling area's edge) @ref or name is scrolled into view first, and the click goes where the element itself is hit, never onto what covers it; `--role` also takes ARIA names (`tab`, `checkbox`, `textbox`) |
| `desk action @eN [NAME]` | Invoke an AT-SPI action instead of clicking (element covered or off-screen) |
| `desk set-text @eN TEXT` | Replace an editable field's text as a user would: focus it, select all, type (or paste what the keyboard cannot type); web forms (React) see it; says when the field shows something else afterwards |
| `desk shot [--label L] [-o FILE] [--settle S] [--region X,Y,W,H \| --window NAME] [--zoom N]` | Screenshot once the screen is still (up to 3 s; `--settle 0` = now); `--region`/`--window` cut out a part, `--zoom 2` enlarges it to judge details |
| `desk move X Y`, `desk drag X1 Y1 X2 Y2`, `desk scroll X Y DY` | Pointer; `DY>0` scrolls down |
| `desk type TEXT [--anywhere]` | Type with real key presses: ASCII and Polish letters; text starting with a dash works. In a browser it refuses when no text field has the focus (the page would take the keys as shortcuts); `--anywhere` types anyway |
| `desk key COMBO...` | `super`, `enter`, `esc`, `tab`, `ctrl+alt+t`, `shift+f10`, `alt+f4`, `f5`, letters |
| `desk snapshot save NAME`, `desk snapshot restore NAME`, `desk snapshot list`, `desk snapshot delete NAME` | Save the running guest (~3-5 s) and go back to it (~1-3 s) |
| `desk up IMAGE --resolution WxH --scale N --displays N`, `desk display [--resolution WxH] [--scale N]` | Monitors: mode, scaling in percent, several displays ([system.md](system.md)) |
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
| `desk forward PORT[:LOCAL]`, `desk forward`, `desk forward --stop PORT\|all`, `desk up IMAGE --forward PORT` | Reach a guest port from the host (127.0.0.1, the same number when free, else printed) for as long as the run exists, across reboots and snapshot restores; a port already forwarded keeps its local port (`--stop` it first to move it) |
| `desk browser open URL\|/PATH` | Open an address in Firefox's current tab, as a link from another program would; a path is opened on the site shown now (`/podman`, `/system/services`); an address that differs from the page shown only after `#` loads that page afresh (Firefox loads no page for such a link from another program). Exits 1 and says what Firefox shows instead when the page did not load (server not found, connection refused, a certificate warning) |
| `desk browser viewport WxH\|reset` | Pages at WxH CSS pixels (a phone: `390x700`) through Firefox's responsive design mode; prints the `desk shot --region` of the page area |
| `desk browser console [--errors] [--all]` | Web console of every page since the run started (see Pitfalls) |
| `desk record start [--fps N]`, `desk record stop`, `desk up --record` | Record the screen as a WebM video, kept with the report |
| `desk usb ls`, `desk usb add VENDOR:PRODUCT\|BUS.DEVICE`, `desk usb rm ...`, `desk up IMAGE --usb ...` | Hand a host USB device to the guest ([system.md](system.md)) |
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
  `desk click @eN` re-reads the element's position, so moved elements are still hit.
- `click --name TEXT` (and scenario `click`) takes the element whose whole name is TEXT, ignoring
  case; only if there is none does a part of a name do, and then only when it is part of one name:
  "Save" next to "Save as..." clicks "Save", "Sav" is an error listing both.
- `desk a11y` lists what is visible and ends with "N more outside the visible area" when a page or
  list is scrolled; `desk find` searches those too. Long forms and dialogs at 1280x800 often hide
  their last fields and buttons that way.
- Toolkits report positions themselves. GTK4 places header bar buttons about 6 px up and left
  of where they are drawn (the click still lands inside them); if a click by ref misses a small
  element, check with a screenshot and click by pixels.
- Browsers build their accessibility tree lazily: if a page's content is missing from the first
  `desk a11y`, run it again.
- Web pages' console: `desk browser console` (`--errors`: console.error, uncaught exceptions and
  rejected promises only) prints what every page in Firefox logged since the run started, iframes
  included (Cockpit plugins run in one). Firefox's own messages are left out (`--all` shows them).
  An uncaught exception or rejected promise names the file and line it came from and, after
  `= `, the thrown value and its stack (`uncaught exception: Object = {"problem":"not-found"} (at
  load@https://localhost:9090/cockpit/@localhost/foo/index.js:2:41 < ...)`): the URL tells the
  plugin's code from Cockpit's. Reading only; to evaluate JavaScript open the devtools
  (`desk key ctrl+shift+k`) and type.
- Layouts that are still building move what `desk a11y` listed a moment ago; clicks by @ref or name
  look the position up again, but a click by pixels from an old listing can miss. Typing after a
  missed click is refused in a browser rather than sent to the page as shortcuts.
- `desk type` only knows ASCII and Polish letters; for anything else use `desk set-text`.
- In a GTK file chooser, `ctrl+l`, type the path, then click "Open": Enter may only accept the
  path completion.
- `pgrep -f`/`pkill -f PATTERN` in `desk exec --gui -- '...'` also matches the `systemd-run` that
  runs the snippet (and `pkill` kills it); write the pattern as `[g]st-launch` so it does not match
  itself. Plain `desk exec` and scenario `exec`/`root` steps hand the snippet over unseen.
- `dnf` prints its progress on stderr even with `-q`; add `>/dev/null 2>&1` and check the result with
  `rpm -q` / `rpm -V` instead of reading the transaction log.
- Web UIs on `https://localhost` with a self-signed certificate (a Cockpit installed by the test
  itself; `fedora-44-cockpit`'s Firefox trusts its own) show Firefox's warning on every new run:
  `desk click --name Advanced`, then `desk click --name Proceed` (it scrolls itself).

## Evidence report

`desk down` first writes `reports/<run>/report.md` under desk's home and prints its path: failed
units (now, and every unit failure since the start, before reboots too), SELinux denials, crashes,
journal warnings, package changes and Firefox console errors since the run started, plus a
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

`desk image list` shows them and which of them this host has built: a host keeps the images it
tests on, not all of them. When the image a test needs is not built, build that one
(`desk maintain --only NAME` builds, warms and selftests it; or `desk image build NAME`, then
`desk image warm NAME`; 10-25 minutes and 5-25 GB, so not the others along with it); a variant (`-pl`, `-cockpit`) only needs `desk image warm NAME` once its
base is there, and without a warm state a run still starts, cold, in ~20 s.
`fedora-44-gnome` is the default; `fedora-45-gnome` is the next
Fedora release (beta until it is out); `fedora-44-gnome-pl` is the same
system in Polish (UI, Firefox, formats) for testing Polish interfaces; `fedora-44-cockpit` has
Cockpit open in Firefox, logged in as tester with administrative access; `arch-gnome` is Arch Linux
(rolling, pacman, no SELinux) with the newest GNOME and libraries; `windows-11` is Windows 11
Enterprise (evaluation) for Windows builds and installers; `openwrt-25.12` is OpenWrt 25.12 without
a desktop, for OpenWrt packages and LuCI apps. Images are as old as their last build
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
several), copies the newest file matching each `artifacts` pattern (relative to the same place) to `/tmp/desk/` in the guest, starts a run of
`image`, executes the steps in order and stops at the first failure with a screenshot. Exit code 0
or 1, and 2 when a step lost SSH to the guest (a step that reboots it with a command says
`rc = 255`); the report (and the video with `record = true`) shows every step in its timeline.

```toml
image = "fedora-44-cockpit"
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
click = "Foo"                # by accessible name; role, exact (default true)
role = "link"
[[step]]
expect = "Nothing here yet"  # waits until shown (name, value or text); role, exact, timeout
[[step]]
shot = "empty-state"
```

Other steps: `type`, `gui = "firefox https://..."` (with `env = {NAME = "value"}`; on Windows the
string splits like a Windows command line, so `C:\path` stays as it is and paths with spaces
need quotes, or give a list), `expect_absent` (after a reload, first
`expect` something that is always there, or the check passes on a half-loaded page), `reboot = true`,
`wait = true` (screen still) or `wait = 2` (seconds), `until = true` on an `exec`/`root` step
(repeat it every 2 s until it gives the expected `rc`/`output`, `timeout` 120 s by default: a
service coming up; an `exec`/`root` step that runs out of its `timeout`, 600 s without `until`, is
stopped in the guest and fails as "still running"), `timeout` on a `click` (how long to wait for
the element, 60 s by default), `clock = "+1d"` (or `"reset"`),
`disk_add = "8G:data"`, `disk_rm = "data"` (top-level `disks = ["8G:data"]` plugs them in before
the first step, `usb = ["0fd9:0084"]` hands host USB devices over), `console_errors = 0` (at most that many new errors in Firefox's web console since
the start or the previous such step; end a UI scenario with one). In a scenario's report every
console message is tagged with the step running when it was logged (`[before the first step]` for
what the warm state and setup logged; a page logs a moment after the step that opened it, so look
at the step before as well), so no extra step is needed just to separate setup noise. Every step takes `name` for the
report, and `soft = true` for an independent check whose failure should not stop the steps after it.
`desk scenario check FILE` validates a file without starting anything: the image, every value's
type, and options a step would not read (`rc` on a `click`). When a scenario fails,
look at the failure screenshot and the report before changing the scenario: a failing step is
usually a real finding. A failed `exec`/`root` step prints the end of its stdout and stderr, where
the cause usually is. An element that did not appear (`expect`, `click`) comes with the focused
window, similar names on screen and, on GNOME, the overview or a notification banner over the
windows: a typo, the wrong window in front or a prompt of the system shows there without a
screenshot. `desk scenario run FILE --keep` writes the report and leaves the run running
(it prints the `DESK_RUN` to use) to go on by hand from where the scenario stopped; `desk down` it
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
  real machine (`desk exec --root` does not need it), 1280x800, en_US locale (pl_PL in the -pl
  variant), Polish (programmer's)
  keyboard layout = US plus AltGr letters,
  animations, screen lock and the hot corner off, session starts on the desktop rather than the
  overview. GNOME Software is masked, so no update banners or prompts cover the app under test
  and the overview's search does not list it; to test something about Software itself:
  `desk exec --root -- systemctl --global unmask gnome-software.service`, then `desk reboot`.
  Maintenance timers (fstrim, logrotate, plocate, dnf-makecache, raid-check, unbound-anchor; on
  Arch the keyring sync and shadow) are disabled, since after the clock catches up on restore they
  ran at random moments of a test; when the test is about one of them, `systemctl start` its
  service (the product's own timers are untouched, and `desk clock --step` runs them). Firefox
  starts without first-run tabs, the default browser question, offers to save passwords, studies
  or tips (`/etc/firefox/policies/policies.json`).
- Guest clocks are synced on restore; network goes through user-mode networking (outbound
  works, inbound only SSH; `desk forward PORT` makes a guest service reachable from the host).
- Several runs can exist at once; pass `-r ID` or set `DESK_RUN`. `desk ls` shows who holds each:
  the scenario, `desk up --label TEXT` (or `$DESK_LABEL`), or by default the coding agent and its
  directory ("claude @ cockpit-example", "(agent gone)" once it has exited). Leave other agents'
  runs alone. Only one run per image is restored from the warm state (~2 s); further ones boot
  cold (~25 s) from the same disk, so they start with the same files, only without the uptime.
  Each takes ~4 GB RAM; when the host is short of memory (too little available, or already
  swapping hard) `desk up` waits up to 10 min for other runs to end, then gives up; scenarios
  started in parallel wait in line that way instead of slowing each other down. Runs idle for 2 h
  are destroyed by the next `desk up` or `desk gc` (`DESK_TTL` seconds to change), their report
  written first.
- The image is a clean Workstation install: anything the product needs beyond its declared
  dependencies is missing there, exactly as on a user's machine. Report that rather than
  silently installing it; install it afterwards only to continue the test.
- `desk selftest <image>` checks the image and the harness end to end. `desk maintain` renews
  the images in use on the host, those with a warm state (rebuilds, selftests, switches builds), when it is run, by hand or
  from the weekly timer a host may have (`desk doctor` says). After changing an image definition
  in `images/<name>/` of the source repository, run `desk maintain --only <name>`: it rebuilds what
  changed and switches only after the selftest passes (`desk image build --force` /
  `desk image warm --force` switch without testing).
