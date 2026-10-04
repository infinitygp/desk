---
name: desk
description: Test desktop applications and system packages (rpm, deb, Flatpak, AppImage, installers) end to end in a disposable VM with a real desktop session, judging the result from screenshots. Use when a change must be verified the way a user would see it on a real OS - installing a package, launching a GUI app, clicking through it, checking autostart, tray, dialogs, polkit prompts, reboots - rather than with unit tests or a browser alone.
---

# desk: disposable desktop VMs

`desk` (on PATH; source and image definitions in `~/work/experiments/agent-visual-analysis`) starts a throwaway VM from a golden image with a
logged-in desktop in a few seconds, and drives it like a person would: it reads the
screen as PNG and sends real mouse and keyboard input from outside the guest. Nothing
in the guest can hide the screen or block that input, so it works for any toolkit,
login screens, installers, polkit dialogs, reboots and crashes alike. Every run is a
copy-on-write overlay; `desk down` throws all changes away.

## Loop

```bash
desk image list                     # golden images and whether they are warm
desk up fedora-44-gnome             # prints the run id; export DESK_RUN=<id> if several runs exist
desk shot                           # -> /var/lib/vmlab/runs/<id>/shots/0001.png 1280x800
# Read the PNG, decide, act:
desk click 799 737 --shot           # act, wait for the screen to settle, screenshot
desk type 'hello world' --shot
desk key ctrl+s --shot
desk down                           # destroy the VM and its files (--keep to keep them)
```

Always look at the screenshot (Read the printed path) before the next action. Coordinates
are pixels of that screenshot, origin top-left.

## Commands

| Command | Purpose |
|---|---|
| `desk shot [--label L] [-o FILE] [--settle S]` | Screenshot after the screen is still (default up to 3 s; `--settle 0` = now) |
| `desk click X Y [--button right] [--double]` | Click |
| `desk move X Y`, `desk drag X1 Y1 X2 Y2`, `desk scroll X Y DY` | Pointer; `DY>0` scrolls down |
| `desk type TEXT` | Type ASCII on the US layout; for other characters use the clipboard in the guest |
| `desk key COMBO...` | `super`, `enter`, `esc`, `tab`, `ctrl+alt+t`, `shift+f10`, `alt+f4`, letters, `f1`..`f12` |
| `desk wait [--change] [--ready] [--timeout S]` | Exit 0 when the screen stops changing; `--change` first waits for any change (use after launching an app), `--ready` first waits for SSH and the session (use after a reboot) |
| `desk exec [--root] -- CMD` | Run in the guest over SSH as the desktop user (`--root`: sudo) |
| `desk exec --gui -- APP ARGS` | Start a program detached inside the graphical session |
| `desk exec --agent -- CMD` | Run as root through qemu-guest-agent; works without guest networking |
| `desk push LOCAL REMOTE`, `desk pull REMOTE LOCAL` | Copy files (scp) |
| `desk ls`, `desk down [ID] [--all] [--keep]` | List and destroy runs |

Action commands accept `--shot` (and `--settle S`) to screenshot right after acting.
With one argument, `desk exec -- 'cmd && cmd | grep x'` goes through the guest shell; `--root`
and `--gui` apply to the whole snippet.

A still screen is not proof that something happened: right after `desk exec --gui` nothing has
changed yet, so plain `desk wait` returns at once. Use `desk wait --change`, and when an app
takes long to start, check `pgrep` in the guest before concluding it failed.

## Testing a package end to end

1. Build the package on the host as the project normally does.
2. `desk up <image>`, then `desk push ./dist/foo.rpm /tmp/`.
3. Install as a user would: `desk exec --root -- dnf install -y /tmp/foo.rpm`.
   Check scriptlets and files: `rpm -V foo`, `systemctl status ...`, `journalctl -b`.
4. Launch it the way a user would - through the GNOME overview (`desk key super`, type the
   name, `enter`) or `desk exec --gui -- foo` - and walk through the feature with screenshots.
5. Verify side effects in the guest with `desk exec`, not only on screen.
6. When relevant: upgrade from the previous version, remove (`dnf remove`), reboot
   (`desk exec --root -- systemctl reboot`, then `desk wait --ready` and `desk shot`) and check
   what remains.
7. `desk down`.

## Judging screenshots

- Describe what is actually on the screen before concluding; check text, layout, clipping,
  icons, focus and error dialogs, not just that a window appeared.
- When something looks wrong, confirm it in the guest (logs, files, process list) before
  reporting it.
- Keep the shot paths of the key states; they are the evidence for the report. Use
  `desk down --keep` when the user should be able to look at them afterwards.

## Notes

- The guest is Fedora 44 Workstation, GNOME on Wayland, user `tester`, passwordless sudo,
  1280x800, animations and screen lock off.
- Guest clocks are synced on restore; network goes through user-mode networking (outbound
  works, inbound only SSH).
- Several runs can exist at once; pass `-r ID` or set `DESK_RUN`. Only one run per image is
  restored from the warm state (~2 s); further ones boot cold (~20 s). Each takes ~4 GB RAM,
  so do not start more than two or three.
- The image is a clean Workstation install: anything the product needs beyond its declared
  dependencies is missing there, exactly as on a user's machine. Report that rather than
  silently installing it; install it afterwards only to continue the test.
- Images are built with `desk image build <name>` and `desk image warm <name>`; only rebuild
  when the image definition in `images/<name>/` of the source repository changes.
