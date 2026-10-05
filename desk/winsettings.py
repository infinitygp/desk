"""Windows settings that tests switch often and that otherwise mean clicking through the
Settings app: theme and taskbar. Written where Settings writes them (the user's registry), then
applied the way Windows applies them (a settings broadcast, or restarting Explorer)."""

import time

from . import display, guest
from .config import DeskError

PERSONALIZE = "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize"
ADVANCED = "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced"
STUCK_RECTS = "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\StuckRects3"


def _ps(run, spec, script: str) -> str:
    r = guest.ssh_run(run.ssh_port, spec.root_user, spec.cmd(script), capture=True, timeout=120)
    if r.returncode != 0:
        raise DeskError(r.stderr.decode(errors="replace").strip() or f"exit {r.returncode}")
    return r.stdout.decode(errors="replace").strip()


def _restart_explorer(run, spec) -> None:
    # Windows starts the shell again by itself; wait until the taskbar is back and still.
    _ps(run, spec, "Stop-Process -Name explorer -Force; Start-Sleep 2; "
                   "for ($i = 0; $i -lt 30 -and -not (Get-Process explorer -ErrorAction SilentlyContinue); $i++) "
                   "{ Start-Sleep 1 }")
    time.sleep(2)
    display.wait_stable(run.dom(), timeout=15)


def theme(run, spec, mode: str) -> None:
    light = {"light": 1, "dark": 0}[mode]
    _ps(run, spec, f"Set-ItemProperty '{PERSONALIZE}' -Name AppsUseLightTheme -Value {light} -Type DWord; "
                   f"Set-ItemProperty '{PERSONALIZE}' -Name SystemUsesLightTheme -Value {light} -Type DWord")
    # What Settings does next: tell every window the colour set changed (from the desktop session).
    from . import winagent

    winagent.request(run, cmd="settingchange", area="ImmersiveColorSet")
    display.wait_stable(run.dom(), timeout=10)


# TaskbarLocation, as Settings writes it (Personalization > Taskbar > Taskbar behaviors).
EDGES = {"left": 0, "top": 1, "right": 2, "bottom": 3}


def taskbar(run, spec, align: str | None = None, autohide: str | None = None, edge: str | None = None,
            size: str | None = None) -> None:
    script = []
    if edge:
        script.append(f"Set-ItemProperty '{ADVANCED}' -Name TaskbarLocation -Value {EDGES[edge]} -Type DWord")
    if size:
        script.append(f"Set-ItemProperty '{ADVANCED}' -Name TaskbarSize -Value {1 if size == 'small' else 0} -Type DWord")
    if align:
        script.append(f"Set-ItemProperty '{ADVANCED}' -Name TaskbarAl -Value {0 if align == 'left' else 1} -Type DWord")
    if autohide:
        # Byte 8 of StuckRects3's Settings: 3 hides the taskbar automatically, 2 keeps it.
        script.append(f"$v = (Get-ItemProperty '{STUCK_RECTS}').Settings; $v[8] = {3 if autohide == 'on' else 2}; "
                      f"Set-ItemProperty '{STUCK_RECTS}' -Name Settings -Value $v")
    if not script:
        raise DeskError("nothing to change (--edge, --size, --align, --autohide)")
    _ps(run, spec, "; ".join(script))
    _restart_explorer(run, spec)
