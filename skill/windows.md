# desk: Windows guests

Part of the desk skill ([SKILL.md](SKILL.md)); the commands and pitfalls there apply.

## Windows guests

- `desk exec -- CMD` runs PowerShell as `tester`, who is an administrator with a full token, so
  `--root` changes nothing; `desk exec --agent -- CMD` runs as SYSTEM (session 0, no desktop).
  Quoting is safe: commands reach PowerShell encoded.
- Open Win32 menus (context menus, a window's system menu) show up as a focused window "menu"
  whose `menu item`s, disabled ones marked, `desk click --name ... --role "menu item"` clicks;
  they come from MSAA, as UI Automation does not see them.
- The desktop is a window too: `desk a11y --all` lists its icons as `list item`s under
  "Program Manager" with their positions (read through MSAA, since UI Automation shows the icon
  view empty), so `desk click --name "Recycle Bin" --role "list item"`, `desk drag` an icon and
  `desk find NAME --all --role "list item"` to read where it ended up all work. The wallpaper is
  the fixed Windows 11 default (no Spotlight), so screenshots of the desktop are comparable.
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
- What only the desktop session sees (FindWindow, UI Automation, the session's environment):
  `desk exec --gui --wait -- powershell.exe -NoProfile -Command "..."` returns the output and exit
  code, with no console window; other desk commands (a11y, clicks) work while it runs. UTF-8 output comes through as it is (Rust, or PowerShell after
  `[Console]::OutputEncoding = [Text.Encoding]::UTF8`); console tools' OEM code page is converted.
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
- Settings tests switch often, without clicking through the Settings app:
  `desk win theme dark|light` (apps and system, applied at once) and
  `desk win taskbar [--edge bottom|top|left|right] [--size default|small] [--align left|center]
  [--autohide on|off]` (restarts Explorer, ~5 s). Auto-hide only works with the taskbar at the bottom.
- Windows enables some features for a share of machines only (Controlled Feature Rollout). The
  image opts in to "Get the latest updates as soon as they're available" and forces on, with
  ViVeTool: taskbar position (59213768) and taskbar size (61090762). Everything else is as a
  fresh install of that build has it; if the app's behaviour depends on a feature outside that
  list, say so in the report instead of assuming users see the same.
- The report reads the event log (service failures, application errors and crashes, MSI
  installs); a "Network List Service terminated" entry comes from restoring the saved state, not
  from the app under test. `desk diff` works on NTFS too.
