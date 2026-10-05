---
name: desk
description: Test desktop applications and system packages (rpm, Arch packages, Flatpak, AppImage, installers) end to end in a disposable VM with a real desktop session, judging the result from screenshots and the accessibility tree. Use when a change must be verified the way a user would see it on a real OS - installing a package, launching a GUI app, clicking through it, checking autostart, tray, dialogs, polkit prompts, reboots - rather than with unit tests or a browser alone.
---

# desk: disposable desktop VMs

`desk` (on PATH; source and image definitions in `~/work/github/infinitygp/desk`)
starts a throwaway VM from a golden image with a logged-in desktop in about two seconds, and
drives it like a person would: real mouse and keyboard input from outside the guest, the screen
as PNG, and the accessibility tree as text. Nothing in the guest can hide the screen or block
that input, so it works for any toolkit, login screens, installers, polkit dialogs, reboots and
crashes alike. Every run is a copy-on-write overlay; `desk down` throws all changes away.

## Loop

```bash
desk up fedora-44-gnome             # prints the run id; export DESK_RUN=<id> if several runs exist
desk exec --gui -- gnome-calculator # start an app in the session
desk wait --change                  # until its window has appeared and settled
desk a11y                           # focused window as text: @refs, roles, names, values, click points
desk click @e7                      # click an element from that listing (real mouse click)
desk click --name "Log in"          # or find it by name on the fly
desk type 'Zażółć gęślą jaźń'       # real key presses, Polish letters included
desk shot                           # -> /var/lib/vmlab/runs/<id>/shots/0001.png 1280x800; Read it
desk down                           # destroy the VM and its files (--keep to keep them)
```

Use `desk a11y` / `desk find` to know what is on screen and where, and to read values back.
Use screenshots to judge how it looks (layout, clipping, icons, colours, rendering) and whenever
the tree is empty or doubtful. Pixel coordinates are those of the screenshot, origin top-left.

## Commands

| Command | Purpose |
|---|---|
| `desk a11y [--app NAME] [--all] [--offscreen] [--max N]` | Accessibility tree of the focused window (`--all`: every window and the shell; `--offscreen`: also what is scrolled out of view) |
| `desk find TEXT [--role R] [--exact] [--all]` | Elements whose name, value or text matches, off-screen ones included; refs usable right away |
| `desk click (X Y \| @eN \| --name TEXT [--role R] [--exact]) [--button right] [--double]` | Click; an off-screen @ref or name is scrolled into view first |
| `desk action @eN [NAME]` | Invoke an AT-SPI action instead of clicking (element covered or off-screen) |
| `desk set-text @eN TEXT` | Replace an editable field's text through AT-SPI (characters `type` cannot produce) |
| `desk shot [--label L] [-o FILE] [--settle S]` | Screenshot once the screen is still (up to 3 s; `--settle 0` = now) |
| `desk move X Y`, `desk drag X1 Y1 X2 Y2`, `desk scroll X Y DY` | Pointer; `DY>0` scrolls down |
| `desk type TEXT` | Type with real key presses: ASCII and Polish letters |
| `desk key COMBO...` | `super`, `enter`, `esc`, `tab`, `ctrl+alt+t`, `shift+f10`, `alt+f4`, `f5`, letters |
| `desk snapshot save NAME`, `desk snapshot restore NAME`, `desk snapshot list`, `desk snapshot delete NAME` | Save the running guest (~3-5 s) and go back to it (~1-3 s) |
| `desk focus NAME` | Bring a window to the front by application or title (keys go to the focused window) |
| `desk scenario run FILE [--keep]`, `desk scenario check FILE` | Run a repeatable test from a TOML file (see below) |
| `desk wait [--change] [--timeout S]` | Until the screen is still; `--change` first waits for it to change at all |
| `desk reboot [--shot]` | Reboot and return when the new session is ready; reports apps that blocked shutdown |
| `desk exec [--root] -- CMD` | Run in the guest over SSH as the desktop user (`--root`: as root) |
| `desk exec --gui -- APP ARGS` | Start a program detached inside the graphical session |
| `desk exec --agent -- CMD` | Run as root through qemu-guest-agent; works without guest networking |
| `desk push LOCAL REMOTE`, `desk pull REMOTE LOCAL` | Copy files (scp) |
| `desk diff [--all]` | Files changed in the guest since the image it started from: added, changed, removed (~30 s) |
| `desk report [--diff]` | Write the evidence report now (see below) |
| `desk ls`, `desk down [ID] [--all] [--keep] [--diff] [--no-report]`, `desk gc` | List, destroy (writing the report first), clean up idle runs |
| `desk view` | Tell the user how to watch the run live (VNC through an SSH tunnel) |
| `desk record start [--fps N]`, `desk record stop`, `desk up --record` | Record the screen as a WebM video, kept with the report |

Action commands (`click`, `type`, `key`, `scroll`, `action`, ...) accept `--shot` to screenshot
once the screen settles. With one argument, `desk exec -- 'a && b | grep x'` goes through the
guest shell; `--root` and `--gui` apply to the whole snippet.

## Pitfalls

- A still screen is not proof that something happened: right after `desk exec --gui` nothing has
  changed yet, so plain `desk wait` returns at once. Use `desk wait --change`; when an app is slow,
  check `pgrep` in the guest before concluding it failed.
- Refs (`@e7`) come from the latest `desk a11y` or `desk find`; take a new one after the UI changes.
  `desk click @eN` re-reads the element's position, so moved elements are still hit.
- `desk a11y` lists what is visible and ends with "N more outside the visible area" when a page or
  list is scrolled; `desk find` searches those too. Long forms and dialogs at 1280x800 often hide
  their last fields and buttons that way.
- Toolkits report positions themselves. GTK4 places header bar buttons about 6 px up and left
  of where they are drawn (the click still lands inside them); if a click by ref misses a small
  element, check with a screenshot and click by pixels.
- Browsers build their accessibility tree lazily: if a page's content is missing from the first
  `desk a11y`, run it again.
- `desk type` only knows ASCII and Polish letters; for anything else use `desk set-text`.
- In a GTK file chooser, `ctrl+l`, type the path, then click "Open": Enter may only accept the
  path completion.
- `pkill -f PATTERN` inside `desk exec -- '...'` also matches the shell running that snippet and
  kills it; write the pattern as `[g]st-launch` so it does not match itself.
- `dnf` prints its progress on stderr even with `-q`; add `>/dev/null 2>&1` and check the result with
  `rpm -q` / `rpm -V` instead of reading the transaction log.
- Web UIs on `https://localhost` with a self-signed certificate (Cockpit) show Firefox's warning on
  every new run: `desk click --name Advanced`, then `desk click --name Proceed` (it scrolls itself).

## Evidence report

`desk down` first writes `/var/lib/vmlab/reports/<run>/report.md` and prints its path: failed
units, SELinux denials, crashes, journal warnings and package changes since the run started, plus a
timeline of every desk command with its exit code and the screenshots taken. `desk down --diff`
adds the files the run changed. Point the user to the report and summarise what it found; read
`journal.txt` / `avc.txt` next to it before blaming the product for a warning.

Record a video (`desk up --record`, or `desk record start` before the interesting part) when the
user will want to see the flow itself: a walkthrough of a feature, an installer, a bug that needs
several steps or depends on timing. `desk down` stops it and puts `video-N.webm` next to the report,
linked from the timeline; the wall-clock time below the picture matches the report. You cannot
watch the video yourself, so keep taking screenshots for your own judgement. Recording stops by
itself after 30 minutes (`--limit`).

## Images

`desk image list` shows them. `fedora-44-gnome` is the default; `fedora-44-gnome-pl` is the same
system in Polish (UI, Firefox, formats) for testing Polish interfaces; `fedora-44-cockpit` has
Cockpit open in Firefox, logged in as tester with administrative access (see below); `arch-gnome` is Arch Linux
(rolling, pacman, no SELinux) with the newest GNOME and libraries; `windows-11` is Windows 11
Enterprise (evaluation) for Windows builds and installers. Images are reinstalled automatically
(`desk maintain`, weekly); if `desk up` prints a warning about a failed maintenance or a licence
running out, pass it on to the user.

### Windows guests

- `desk exec -- CMD` runs PowerShell as `tester`, who is an administrator with a full token, so
  `--root` changes nothing; `desk exec --agent -- CMD` runs as SYSTEM (session 0, no desktop).
  Quoting is safe: commands reach PowerShell encoded.
- `desk exec --gui -- app.exe ARGS` starts the program on the desktop (through an agent in the
  user's session) and brings its window to the front, as starting it from the Start menu would.
- `desk a11y` / `find` / `click --name` use UI Automation; names are the ones Narrator would read
  (Calculator: "Seven", "Multiply by", "Equals"; result "Display is 42").
- Installers: `desk push setup.msi C:/Users/tester/Downloads/` then
  `desk exec -- 'Start-Process msiexec -ArgumentList "/i C:\Users\tester\Downloads\setup.msi /qn" -Wait'`,
  or run the installer UI with `exec --gui` and click through it. UAC prompts do not appear for
  `tester` over SSH, but do for programs started on the desktop: handle them like any dialog.
- Background helpers started with `exec --gui` (a test tone, a mock server) get a console window
  that covers the desktop, since Windows Terminal ignores `-WindowStyle Hidden`: start them as
  `desk exec --gui -- conhost.exe --headless powershell.exe -NoProfile -Command "..."`.
- Windows PowerShell 5.1 writes files in the ANSI code page by default: use
  `Set-Content -Encoding UTF8` / `Get-Content -Encoding UTF8` for text with Polish letters.
- If `desk a11y` says a window's contents are "not available through UI Automation", a popup of
  that app is in the way (Notepad's first-run tip does this): look at a screenshot, close it.
- Dialogs owned by elevated processes (UAC, the Windows Firewall "allow access" prompt) are not in
  the tree at all; they are on screen, so click them by pixels from a screenshot. Classic Win32
  installers (NSIS, Inno) are readable: buttons, fields and texts show up as usual.
- A clean Windows has no Visual C++ runtime. A program that needs `VCRUNTIME140.dll` dies before
  showing anything (exit code 0xC0000135, or a "System Error" dialog); check with
  `Start-Process <exe> -PassThru -Wait` and the exit code when something silently does not start.
- The report reads the event log (service failures, application errors and crashes, MSI
  installs); a "Network List Service terminated" entry comes from restoring the saved state, not
  from the app under test. `desk diff` works on NTFS too.

## Snapshots

When a test needs several attempts from the same point (a wizard, an installer's later pages,
a bug that takes setup to reach), `desk snapshot save NAME` there and `desk snapshot restore NAME`
before each attempt, instead of a new run and the setup all over again. Restoring puts back
memory, disk and screen exactly (programs opened later are gone) and syncs the clock. Each
snapshot keeps the guest's memory on disk (2.5-4 GB) until `desk snapshot delete` or `desk down`.

## Scenarios: repeatable tests

When a project has `test/desk/*.toml` (or the user wants a check that can be repeated), run it
with `desk scenario run FILE`: it builds on the host (`[build] run`, in the repository), copies the
newest file matching each `artifacts` pattern to `/tmp/desk/` in the guest, starts a run of
`image`, executes the steps in order and stops at the first failure with a screenshot. Exit code 0
or 1; the report (and the video with `record = true`) shows every step in its timeline.

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

Other steps: `type`, `gui = "firefox https://..."`, `expect_absent` (after a reload, first
`expect` something that is always there, or the check passes on a half-loaded page), `reboot = true`,
`wait = true` (screen still) or `wait = 2` (seconds). Every step takes `name` for the report.
`desk scenario check FILE` validates a file without starting anything. When a scenario fails,
look at the failure screenshot and the report before changing the scenario: a failing step is
usually a real finding.

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

### Cockpit plugins

Use `fedora-44-cockpit` to test what the plugin does: Firefox shows Cockpit at
`https://localhost:9090`, logged in with administrative access. Install the plugin's package with
`desk exec --root -- dnf install -y /tmp/foo.rpm`, then press F5 in Firefox: the new entry appears
in Cockpit's menu without logging in again. Test packaging and dependencies on `fedora-44-gnome`
instead, where Cockpit is not installed, as on a fresh Workstation: a plugin that depends only on
`cockpit-bridge` installs there without the web console a user needs to reach it.

## Judging screenshots

- Describe what is actually on the screen before concluding; check text, layout, clipping,
  icons, focus and error dialogs, not just that a window appeared.
- When something looks wrong, confirm it in the guest (logs, files, process list) before
  reporting it.
- Keep the shot paths of the key states; they are the evidence for the report. Use
  `desk down --keep` when the user should be able to look at them afterwards.

## Notes

- The guests run GNOME on Wayland with user `tester` (password `tester`) with
  ordinary sudo, so sudo, polkit and Cockpit's administrative access ask for that password as on a
  real machine (`desk exec --root` does not need it), 1280x800, en_US locale (pl_PL in the -pl
  variant), Polish (programmer's)
  keyboard layout = US plus AltGr letters,
  animations and screen lock off, session starts on the desktop rather than the overview.
- Guest clocks are synced on restore; network goes through user-mode networking (outbound
  works, inbound only SSH).
- Several runs can exist at once; pass `-r ID` or set `DESK_RUN`. Only one run per image is
  restored from the warm state (~2 s); further ones boot cold (~20 s). Each takes ~4 GB RAM;
  `desk up` refuses when the host is short of memory. Runs idle for 2 h are destroyed by the
  next `desk up` or `desk gc` (`DESK_TTL` seconds to change).
- The image is a clean Workstation install: anything the product needs beyond its declared
  dependencies is missing there, exactly as on a user's machine. Report that rather than
  silently installing it; install it afterwards only to continue the test.
- `desk selftest <image>` checks the image and the harness end to end. `desk maintain` keeps
  the images current (rebuilds, selftests, switches builds) and runs weekly from a timer. After
  changing an image definition in `images/<name>/` of the source repository, run
  `desk maintain --only <name>`: it rebuilds what changed and switches only after the selftest
  passes (`desk image build --force` / `desk image warm --force` switch without testing).
