"""Windows settings that tests switch often and that otherwise mean clicking through the
Settings app: theme, taskbar, transparency, contrast themes, text size and wallpaper. Written
where Settings writes them (the user's registry), applied the way Windows applies them (a
settings broadcast, a theme file, restarting Explorer), then read back from the guest."""

import argparse
import base64
import time

from . import display, guest
from .config import DeskError, NotDone

PERSONALIZE = "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize"
ADVANCED = "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced"
STUCK_RECTS = "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\StuckRects3"
ACCESSIBILITY = "HKCU:\\Software\\Microsoft\\Accessibility"
HIGH_CONTRAST = "HKCU:\\Control Panel\\Accessibility\\HighContrast"
DESKTOP = "HKCU:\\Control Panel\\Desktop"
# Windows 11's contrast themes (Settings > Accessibility > Contrast themes) and their files, whose
# own names are the older ones (High Contrast #1, White, #2, Black).
CONTRAST = {"aquatic": "hc1", "desert": "hcwhite", "dusk": "hc2", "night-sky": "hcblack"}
THEMES_KEY = "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Themes"
THEMES = "C:\\Windows\\Resources\\Themes"
CONTRAST_THEMES = "C:\\Windows\\Resources\\Ease of Access Themes"


class NotApplied(NotDone):
    """Windows does not have the setting afterwards: desk could not make it."""


def _ps(run, spec, script: str) -> str:
    r = guest.ssh_run(run.ssh_port, spec.root_user, spec.cmd(script), capture=True, timeout=120)
    if r.returncode != 0:
        raise DeskError(r.stderr.decode(errors="replace").strip() or f"exit {r.returncode}")
    return r.stdout.decode(errors="replace").strip()


def _read(run, spec, key: str, name: str) -> str:
    return _ps(run, spec, f"(Get-ItemProperty '{key}' -ErrorAction SilentlyContinue).'{name}'")


def _expect(what: str, got, want) -> None:
    if str(got).strip().lower() != str(want).strip().lower():
        raise NotApplied(f"{what} did not take: Windows has {str(got).strip() or 'nothing'} instead of {want}")


def _broadcast(run, area: str) -> None:
    """What Settings does after a change: tell every window (from the desktop session)."""
    from . import winagent

    winagent.request(run, cmd="settingchange", area=area)
    display.wait_stable(run.dom(), timeout=10)


def _in_session(run, script: str, timeout: float = 60) -> None:
    """PowerShell in the desktop session: what applies to the user's desktop (the wallpaper) does
    nothing from an SSH session."""
    from . import winagent

    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    reply = winagent.request(run, timeout=timeout + 30, cmd="run", file="powershell.exe",
                             args=["-NoProfile", "-EncodedCommand", encoded], wait=True, timeout_s=timeout)
    if reply.get("timeout") or reply.get("code") != 0:
        said = (reply.get("stderr") or reply.get("stdout") or "").strip()[-300:]
        raise DeskError(f"PowerShell in the session {'ran out of time' if reply.get('timeout') else 'failed'}"
                        + (f": {said}" if said else ""))


def _restart_explorer(run, spec) -> None:
    # Windows starts the shell again by itself; wait until the taskbar is back and still.
    _ps(run, spec, "Stop-Process -Name explorer -Force -ErrorAction SilentlyContinue; Start-Sleep 2; "
                   "for ($i = 0; $i -lt 30 -and -not (Get-Process explorer -ErrorAction SilentlyContinue); $i++) "
                   "{ Start-Sleep 1 }; "
                   "if (-not (Get-Process explorer -ErrorAction SilentlyContinue)) "
                   "{ Write-Error 'Explorer (the taskbar and desktop) did not start again within 30 s' }")
    time.sleep(2)
    display.wait_stable(run.dom(), timeout=15)


def theme(run, spec, mode: str, only: str | None = None) -> str:
    light = {"light": 1, "dark": 0}[mode]
    names = {"apps": "AppsUseLightTheme", "system": "SystemUsesLightTheme"}
    parts = [only] if only else list(names)
    _ps(run, spec, "; ".join(f"Set-ItemProperty '{PERSONALIZE}' -Name {names[p]} -Value {light} -Type DWord"
                             for p in parts))
    _broadcast(run, "ImmersiveColorSet")
    for p in parts:
        _expect(f"{mode} mode for {p}", _read(run, spec, PERSONALIZE, names[p]), light)
    return f"{mode} mode for {' and '.join(parts)}"


# TaskbarLocation, as Settings writes it (Personalization > Taskbar > Taskbar behaviors).
EDGES = {"left": 0, "top": 1, "right": 2, "bottom": 3}


def taskbar(run, spec, align: str | None = None, autohide: str | None = None, edge: str | None = None,
            size: str | None = None) -> str:
    script, wanted = [], []
    if edge:
        script.append(f"Set-ItemProperty '{ADVANCED}' -Name TaskbarLocation -Value {EDGES[edge]} -Type DWord")
        wanted.append(("taskbar edge", "TaskbarLocation", EDGES[edge]))
    if size:
        value = 1 if size == "small" else 0
        script.append(f"Set-ItemProperty '{ADVANCED}' -Name TaskbarSize -Value {value} -Type DWord")
        wanted.append(("taskbar size", "TaskbarSize", value))
    if align:
        value = 0 if align == "left" else 1
        script.append(f"Set-ItemProperty '{ADVANCED}' -Name TaskbarAl -Value {value} -Type DWord")
        wanted.append(("taskbar alignment", "TaskbarAl", value))
    if autohide:
        # Byte 8 of StuckRects3's Settings: 3 hides the taskbar automatically, 2 keeps it.
        script.append(f"$v = (Get-ItemProperty '{STUCK_RECTS}').Settings; $v[8] = {3 if autohide == 'on' else 2}; "
                      f"Set-ItemProperty '{STUCK_RECTS}' -Name Settings -Value $v")
    if not script:
        raise DeskError("nothing to change (--edge, --size, --align, --autohide)")
    _ps(run, spec, "; ".join(script))
    _restart_explorer(run, spec)
    for what, name, value in wanted:
        _expect(what, _read(run, spec, ADVANCED, name), value)
    if autohide:
        _expect("taskbar auto-hide", _ps(run, spec, f"(Get-ItemProperty '{STUCK_RECTS}').Settings[8]"),
                3 if autohide == "on" else 2)
    return "taskbar " + ", ".join(f"{k} {v}" for k, v in (("edge", edge), ("size", size), ("align", align),
                                                             ("auto-hide", autohide)) if v)


def transparency(run, spec, state: str) -> str:
    value = 1 if state == "on" else 0
    _ps(run, spec, f"Set-ItemProperty '{PERSONALIZE}' -Name EnableTransparency -Value {value} -Type DWord")
    _broadcast(run, "ImmersiveColorSet")
    _expect("transparency effects", _read(run, spec, PERSONALIZE, "EnableTransparency"), value)
    return f"transparency effects {state}"


def text_size(run, spec, percent: int) -> str:
    # 100 % is no value at all, as Settings leaves it.
    _ps(run, spec, f"Remove-ItemProperty '{ACCESSIBILITY}' -Name TextScaleFactor -ErrorAction SilentlyContinue"
        if percent == 100 else
        f"Set-ItemProperty '{ACCESSIBILITY}' -Name TextScaleFactor -Value {percent} -Type DWord")
    _broadcast(run, "WindowMetrics")
    _expect("text size", _read(run, spec, ACCESSIBILITY, "TextScaleFactor") or 100, percent)
    # Seen in a guest: a Notepad started afterwards is larger, the one already open is not.
    return f"text size {percent} % (for programs started from now on; running ones may keep their size)"


def contrast(run, spec, name: str) -> str:
    """A contrast theme, or `off`: the Windows (light) theme, with its wallpaper. Applied by opening
    the theme file in the session, as double-clicking it does, which also opens Settings; that
    window is closed."""
    from . import winagent

    path = f"{THEMES}\\aero.theme" if name == "off" else f"{CONTRAST_THEMES}\\{CONTRAST[name]}.theme"
    if _ps(run, spec, f"Test-Path '{path}'") != "True":
        raise DeskError(f"this Windows has no {path}")
    winagent.request(run, cmd="run", file=path)
    want, file = name != "off", path.rsplit("\\", 1)[1]
    deadline = time.monotonic() + 30
    while True:
        flags = _read(run, spec, HIGH_CONTRAST, "Flags")
        on = flags.isdigit() and bool(int(flags) & 1)   # HCF_HIGHCONTRASTON
        current = _read(run, spec, THEMES_KEY, "CurrentTheme")
        if (on == want and current.lower().endswith(file.lower())) or time.monotonic() > deadline:
            break
        time.sleep(1)
    # Settings opens after the theme is applied, seconds later: closed once it is there.
    _ps(run, spec, "for ($i = 0; $i -lt 15; $i++) { $p = Get-Process SystemSettings -ErrorAction SilentlyContinue; "
                   "if ($p) { Start-Sleep 1; $p | Stop-Process -Force; break }; Start-Sleep 1 }")
    display.wait_stable(run.dom(), timeout=15)
    _expect("contrast", "on" if on else "off", "on" if want else "off")
    _expect("theme", current.rsplit("\\", 1)[-1], file)
    return f"contrast theme {name}"


def wallpaper(run, spec, path: str) -> str:
    if _ps(run, spec, f"Test-Path -PathType Leaf '{path}'") != "True":
        raise DeskError(f"no file {path} in the guest")
    # SPI_SETDESKWALLPAPER, saved and announced (SPIF_UPDATEINIFILE | SPIF_SENDCHANGE) as Settings does.
    _in_session(run, "Add-Type -Namespace W -Name U -MemberDefinition '[DllImport(\"user32.dll\", "
                     "CharSet=CharSet.Unicode)] public static extern bool SystemParametersInfoW(uint a, uint u, "
                     "string p, uint f);'; if (-not [W.U]::SystemParametersInfoW(0x14, 0, "
                     f"'{path}', 3)) {{ exit 1 }}")
    display.wait_stable(run.dom(), timeout=10)
    _expect("wallpaper", _read(run, spec, DESKTOP, "WallPaper"), path)
    return f"wallpaper {path}"


def _percent(text: str) -> int:
    n = int(text)
    if not 100 <= n <= 225:
        raise ValueError(text)
    return n


def add_commands(sub, extra=lambda p: None) -> None:
    """The settings, for `desk win` and for a scenario's win step (the same words)."""
    p = sub.add_parser("theme", help="light or dark mode for Windows and apps, or only one of them")
    p.add_argument("mode", choices=["light", "dark"])
    p.add_argument("--only", choices=["apps", "system"], help="apps, or system (the taskbar and Start)")
    extra(p)
    p = sub.add_parser("taskbar", help="taskbar edge, size, alignment and auto-hide (restarts Explorer)")
    p.add_argument("--edge", choices=["bottom", "top", "left", "right"])
    p.add_argument("--size", choices=["default", "small"])
    p.add_argument("--align", choices=["left", "center"])
    p.add_argument("--autohide", choices=["on", "off"])
    extra(p)
    p = sub.add_parser("transparency", help="transparency effects on or off")
    p.add_argument("state", choices=["on", "off"])
    extra(p)
    p = sub.add_parser("contrast", help="a contrast theme, or off (the Windows light theme)")
    p.add_argument("theme", choices=[*CONTRAST, "off"])
    extra(p)
    p = sub.add_parser("text-size", help="text size, 100-225 %% (Accessibility > Text size)")
    p.add_argument("percent", type=_percent)
    extra(p)
    p = sub.add_parser("wallpaper", help="the desktop background, an image file in the guest")
    p.add_argument("path")
    extra(p)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise DeskError(f"win: {message}")


def parse(argv: list[str]) -> argparse.Namespace:
    """A scenario's win step, as `desk win` would read the same words."""
    ap = _Parser(prog="win", add_help=False)
    add_commands(ap.add_subparsers(dest="win_cmd", required=True, parser_class=_Parser))
    return ap.parse_args(argv)


def apply(run, spec, args: argparse.Namespace) -> str:
    """Make the setting and read it back; what Windows has now."""
    if spec.family != "windows":
        raise DeskError("desk win is for Windows guests")
    if args.win_cmd == "theme":
        return theme(run, spec, args.mode, args.only)
    if args.win_cmd == "taskbar":
        return taskbar(run, spec, align=args.align, autohide=args.autohide, edge=args.edge, size=args.size)
    if args.win_cmd == "transparency":
        return transparency(run, spec, args.state)
    if args.win_cmd == "contrast":
        return contrast(run, spec, args.theme)
    if args.win_cmd == "text-size":
        return text_size(run, spec, args.percent)
    return wallpaper(run, spec, args.path)
