"""Monitors of a run: resolution, scaling and the arrangement of several displays, set in the
guest the way its own display settings would. Several displays sit side by side, left to right,
top-aligned, in the order of the virtio GPUs, which is how desk puts their pictures together
(display.screenshot)."""

import time

from . import guest
from .config import DeskError, ImageSpec

# Windows takes these (percent); GNOME also fractional ones in between, as its settings offer.
WINDOWS_SCALES = (100, 125, 150, 175, 200, 225, 250, 300)


def apply(run, spec: ImageSpec, resolution: tuple[int, int] | None = None, scale: int | None = None) -> None:
    if spec.family == "windows":
        _windows(run, spec, resolution, scale)
    else:
        _gnome(run, resolution, scale)
    if scale is not None:
        run.scale = scale
    if resolution is not None:
        run.resolution = list(resolution)
    run.save()


def _windows(run, spec: ImageSpec, resolution, scale) -> None:
    from . import winagent

    def arrange():
        if resolution or run.displays > 1:
            width, height = resolution or (0, 0)
            winagent.request(run, cmd="display", width=width, height=height)

    arrange()
    if scale is not None and scale != (run.scale or 100):
        if scale not in WINDOWS_SCALES:
            raise DeskError(f"Windows scales: {', '.join(map(str, WINDOWS_SCALES))} (percent)")
        # One DPI for every monitor, which Windows applies when the user signs in again; desk signs
        # in again on Windows by rebooting.
        dpi = round(96 * scale / 100)
        guest.ssh_run(run.ssh_port, spec.root_user, spec.cmd(
            "$d = 'HKCU:\\Control Panel\\Desktop'; "
            f"Set-ItemProperty $d -Name LogPixels -Value {dpi} -Type DWord; "
            f"Set-ItemProperty $d -Name Win8DpiScaling -Value {0 if scale == 100 else 1} -Type DWord"),
            capture=True, timeout=60)
        guest.reboot(run.ssh_port, spec)
        arrange()


def _gnome_output(run, what: str) -> str:
    r = guest.ssh_run(run.ssh_port, run.user, f"gdctl show {what}", capture=True, timeout=30)
    if r.returncode != 0:
        raise DeskError(f"gdctl show failed: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout.decode()


def _gnome_connectors(run) -> list[str]:
    """Connected monitors in the order of the virtio GPUs (Virtual-1, Virtual-2, ...)."""
    names = [line.strip("│├└─ ").split()[1] for line in _gnome_output(run, "").splitlines()
             if line.strip("│├└─ ").startswith("Monitor ")]
    return sorted(names, key=lambda n: int(n.rsplit("-", 1)[-1]) if n.rsplit("-", 1)[-1].isdigit() else 0)


def _gnome_mode(run, connector: str, resolution: tuple[int, int]) -> str:
    want = f"{resolution[0]}x{resolution[1]}@"
    current = None
    for line in _gnome_output(run, "--modes").splitlines():
        text = line.strip("│├└─ ")
        if text.startswith("Monitor "):
            current = text.split()[1]
        elif current == connector and text.startswith(want):
            return text.split()[0]
    raise DeskError(f"{connector} offers no {resolution[0]}x{resolution[1]} mode (desk up --resolution sets it)")


def _gnome(run, resolution, scale) -> None:
    connectors = _gnome_connectors(run)
    if not connectors:
        raise DeskError("GNOME reports no monitors")
    # Logical layout: fractional scales work, and windows report positions in logical pixels,
    # which desk turns into screen pixels with the run's scale (a11y._helper).
    args = ["gdctl", "set", "--layout-mode", "logical"]
    for i, connector in enumerate(connectors):
        args += ["--logical-monitor", "--monitor", connector]
        if resolution:
            args += ["--mode", _gnome_mode(run, connector, resolution)]
        args += ["--scale", f"{(scale or run.scale or 100) / 100:g}"]
        args += ["--primary"] if i == 0 else ["--right-of", connectors[i - 1]]
    r = guest.ssh_run(run.ssh_port, run.user, " ".join(args), capture=True, timeout=60)
    if r.returncode != 0:
        raise DeskError(f"gdctl refused the configuration: {r.stderr.decode(errors='replace').strip()}")
    time.sleep(1)
