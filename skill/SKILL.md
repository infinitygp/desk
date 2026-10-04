---
name: desk
description: Test desktop applications and system packages (rpm, deb, Flatpak, AppImage, installers) end to end in a disposable VM with a real desktop session, judging the result from screenshots and the accessibility tree. Use when a change must be verified the way a user would see it on a real OS - installing a package, launching a GUI app, clicking through it, checking autostart, tray, dialogs, polkit prompts, reboots - rather than with unit tests or a browser alone.
---

# desk: disposable desktop VMs

`desk` (on PATH; source and image definitions in `~/work/experiments/agent-visual-analysis`)
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
| `desk a11y [--app NAME] [--all] [--max N]` | Accessibility tree of the focused window (`--all`: every window and the shell) |
| `desk find TEXT [--role R] [--exact] [--all]` | Elements whose name, value or text matches; prints refs usable right away |
| `desk click (X Y \| @eN \| --name TEXT [--role R] [--exact]) [--button right] [--double]` | Click |
| `desk action @eN [NAME]` | Invoke an AT-SPI action instead of clicking (element covered or off-screen) |
| `desk set-text @eN TEXT` | Replace an editable field's text through AT-SPI (characters `type` cannot produce) |
| `desk shot [--label L] [-o FILE] [--settle S]` | Screenshot once the screen is still (up to 3 s; `--settle 0` = now) |
| `desk move X Y`, `desk drag X1 Y1 X2 Y2`, `desk scroll X Y DY` | Pointer; `DY>0` scrolls down |
| `desk type TEXT` | Type with real key presses: ASCII and Polish letters |
| `desk key COMBO...` | `super`, `enter`, `esc`, `tab`, `ctrl+alt+t`, `shift+f10`, `alt+f4`, `f5`, letters |
| `desk wait [--change] [--timeout S]` | Until the screen is still; `--change` first waits for it to change at all |
| `desk reboot [--shot]` | Reboot and return when the new session is ready; reports apps that blocked shutdown |
| `desk exec [--root] -- CMD` | Run in the guest over SSH as the desktop user (`--root`: sudo) |
| `desk exec --gui -- APP ARGS` | Start a program detached inside the graphical session |
| `desk exec --agent -- CMD` | Run as root through qemu-guest-agent; works without guest networking |
| `desk push LOCAL REMOTE`, `desk pull REMOTE LOCAL` | Copy files (scp) |
| `desk ls`, `desk down [ID] [--all] [--keep]`, `desk gc` | List, destroy, clean up idle runs |

Action commands (`click`, `type`, `key`, `scroll`, `action`, ...) accept `--shot` to screenshot
once the screen settles. With one argument, `desk exec -- 'a && b | grep x'` goes through the
guest shell; `--root` and `--gui` apply to the whole snippet.

## Pitfalls

- A still screen is not proof that something happened: right after `desk exec --gui` nothing has
  changed yet, so plain `desk wait` returns at once. Use `desk wait --change`; when an app is slow,
  check `pgrep` in the guest before concluding it failed.
- Refs (`@e7`) come from the latest `desk a11y` or `desk find`; take a new one after the UI changes.
  `desk click @eN` re-reads the element's position, so moved elements are still hit.
- Toolkits report positions themselves; GTK4 is sometimes off by a few pixels inside padded
  containers. If a click by ref misses, check with a screenshot and click by pixels.
- Browsers build their accessibility tree lazily: if a page's content is missing from the first
  `desk a11y`, run it again.
- `desk type` only knows ASCII and Polish letters; for anything else use `desk set-text`.

## Testing a package end to end

1. Build the package on the host as the project normally does.
2. `desk up <image>`, then `desk push ./dist/foo.rpm /tmp/`.
3. Install as a user would: `desk exec --root -- dnf install -y /tmp/foo.rpm`.
   Check scriptlets and files: `rpm -V foo`, `systemctl status ...`, `journalctl -b`.
4. Launch it the way a user would - from the GNOME overview (`desk key super`, type the name,
   `enter`) or `desk exec --gui -- foo` - and walk through the feature with `desk a11y`, clicks and
   screenshots.
5. Verify side effects in the guest with `desk exec`, not only on screen.
6. When relevant: upgrade from the previous version, remove (`dnf remove`), `desk reboot` and
   check what remains or comes back (autostart, enabled units).
7. `desk down`.

## Judging screenshots

- Describe what is actually on the screen before concluding; check text, layout, clipping,
  icons, focus and error dialogs, not just that a window appeared.
- When something looks wrong, confirm it in the guest (logs, files, process list) before
  reporting it.
- Keep the shot paths of the key states; they are the evidence for the report. Use
  `desk down --keep` when the user should be able to look at them afterwards.

## Notes

- The guest is Fedora 44 Workstation, GNOME on Wayland, user `tester` (password `tester`),
  passwordless sudo, 1280x800, Polish (programmer's) keyboard layout = US plus AltGr letters,
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
- `desk selftest <image>` checks the image and the harness end to end. Images are built with
  `desk image build <name>` and `desk image warm <name>`; only rebuild when the image
  definition in `images/<name>/` of the source repository changes.
