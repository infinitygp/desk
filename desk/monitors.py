"""Monitors of a run: resolution, scaling and the arrangement of several displays, set in the
guest the way its own display settings would. Several displays sit side by side, left to right,
top-aligned, in the order of the virtio GPUs, which is how desk puts their pictures together
(display.screenshot)."""

import json
import time

from . import guest
from .config import DeskError, ImageSpec

# Windows takes these (percent); GNOME also fractional ones in between, as its settings offer.
WINDOWS_SCALES = (100, 125, 150, 175, 200, 225, 250, 300)


def check(spec: ImageSpec, displays: int = 1, resolution: tuple[int, int] | None = None,
          scale: int | None = None) -> None:
    """What can be refused without asking the guest, so `desk up` says it before it starts a VM."""
    if displays < 1:
        raise DeskError("--displays is 1 or more")
    if scale is not None and spec.family == "windows" and scale not in WINDOWS_SCALES:
        raise DeskError(f"Windows scales: {', '.join(map(str, WINDOWS_SCALES))} (percent)")
    if scale is not None and scale <= 0:
        raise DeskError("--scale is a percentage, e.g. 150")
    if (resolution or scale) and spec.family != "windows":
        spec.need_desktop("desk display")


def apply(run, spec: ImageSpec, resolution: tuple[int, int] | None = None, scale: int | None = None) -> None:
    if spec.family == "windows":
        _windows(run, spec, resolution, scale)
    else:
        spec.need_desktop("desk display")
        {"kde": _kde, "xfce": _xrandr, "cinnamon": _xrandr, "hyprland": _hyprland}.get(spec.desktop, _gnome)(
            run, resolution, scale)
    with run.edit() as r:
        if scale is not None:
            r.scale = scale
        if resolution is not None:
            r.resolution = list(resolution)


def _windows(run, spec: ImageSpec, resolution, scale) -> None:
    from . import winagent

    def arrange():
        if resolution or run.displays > 1:
            width, height = resolution or (0, 0)
            winagent.request(run, cmd="display", width=width, height=height)

    arrange()
    if scale is not None and scale != (run.scale or 100):
        check(spec, scale=scale)
        # One DPI for every monitor, which Windows applies when the user signs in again; desk signs
        # in again on Windows by rebooting.
        dpi = round(96 * scale / 100)
        r = guest.ssh_run(run.ssh_port, spec.root_user, spec.cmd(
            "$d = 'HKCU:\\Control Panel\\Desktop'; "
            f"Set-ItemProperty $d -Name LogPixels -Value {dpi} -Type DWord; "
            f"Set-ItemProperty $d -Name Win8DpiScaling -Value {0 if scale == 100 else 1} -Type DWord"),
            capture=True, timeout=60)
        if r.returncode != 0:
            # Not worth the reboot: the scale would stay as it is.
            raise DeskError(f"Windows did not take scale {scale} %: {r.stderr.decode(errors='replace').strip()[-300:]}")
        guest.reboot(run.ssh_port, spec)
        arrange()
        got = windows_dpi(run)
        if got != dpi:
            raise DeskError(f"Windows signed in again at {round(got * 100 / 96)} % instead of {scale} %")


def windows_dpi(run) -> int:
    """The DPI the desktop session runs at, asked of a program in it (not the registry, which
    only holds what applies at the next sign-in)."""
    from . import winagent

    script = ("Add-Type -Namespace Desk -Name Dpi -MemberDefinition '[DllImport(\"user32.dll\")] public static "
              "extern uint GetDpiForSystem(); [DllImport(\"user32.dll\")] public static extern bool "
              "SetProcessDpiAwarenessContext(IntPtr v);'; [void][Desk.Dpi]::SetProcessDpiAwarenessContext([IntPtr]-4); "
              "[Desk.Dpi]::GetDpiForSystem()")
    reply = winagent.request(run, timeout=90, cmd="run", file="powershell.exe", args=["-NoProfile", "-Command", script],
                             wait=True, timeout_s=60)
    try:
        return int((reply.get("stdout") or "").strip())
    except ValueError:
        raise DeskError(f"could not read the session's DPI: {(reply.get('stderr') or reply.get('stdout') or '').strip()[-200:]}") from None


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
    # gdctl gives a monitor without --mode its preferred mode: keep one desk display set earlier.
    resolution = resolution or (tuple(run.resolution) if run.resolution else None)
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


def _user(run, command: str, what: str) -> str:
    """`command` as the desktop user in the session, its output; what failed in `what`'s words."""
    r = guest.ssh_run(run.ssh_port, run.user, command, capture=True, timeout=60)
    if r.returncode != 0:
        raise DeskError(f"{what} failed: {(r.stderr or r.stdout).decode(errors='replace').strip()[-300:]}")
    return r.stdout.decode()


def _kde(run, resolution, scale) -> None:
    """Plasma, through kscreen-doctor; KWin, like mutter, reports logical positions."""
    resolution = resolution or (tuple(run.resolution) if run.resolution else None)
    env = ("env XDG_RUNTIME_DIR=/run/user/$(id -u) QT_QPA_PLATFORM=wayland "
           "$(systemctl --user show-environment | grep '^WAYLAND_DISPLAY=')")
    outputs = [o for o in json.loads(_user(run, f"{env} kscreen-doctor -j", "kscreen-doctor -j"))["outputs"]
               if o.get("connected")]
    if not outputs:
        raise DeskError("Plasma reports no monitors")
    name = outputs[0]["name"]
    args = []
    if resolution:
        modes = [m for m in outputs[0]["modes"] if (m["size"]["width"], m["size"]["height"]) == tuple(resolution)]
        if not modes:
            raise DeskError(f"{name} offers no {resolution[0]}x{resolution[1]} mode (desk up --resolution sets it)")
        args.append(f"output.{name}.mode.{modes[0]['id']}")
    want = (scale or run.scale or 100) / 100
    args.append(f"output.{name}.scale.{want:g}")
    _user(run, f"{env} kscreen-doctor {' '.join(args)}", "kscreen-doctor")

    def state():
        now = next(o for o in json.loads(_user(run, f"{env} kscreen-doctor -j", "kscreen-doctor -j"))["outputs"]
                   if o["name"] == name)
        size = next((m["size"] for m in now["modes"] if m["id"] == now.get("currentModeId")), None)
        return ((size["width"], size["height"]) if size else None), now.get("scale")

    # kscreen-doctor says nothing of a mode or scale it did not take; the run would then turn
    # every position into pixels with the wrong scale.
    size, scale_now = _settled(state, resolution, want)
    try:
        _check_taken(name, size, scale_now, resolution, want)
    except DeskError:
        # Back to what the run counts with.
        _user(run, f"{env} kscreen-doctor output.{name}.mode.{outputs[0]['currentModeId']} "
              f"output.{name}.scale.{outputs[0]['scale']:g}", "kscreen-doctor")
        raise


def _xrandr(run, resolution, scale) -> None:
    """Xfce and Cinnamon on X11: the mode through xrandr; X11 has no scaling of its own to set."""
    if scale not in (None, 100) or (run.scale or 100) != 100:
        raise DeskError("scaling is set on Wayland desktops and Windows; X11 desktops run at 100 %")
    resolution = resolution or (tuple(run.resolution) if run.resolution else None)
    if not resolution:
        return
    env = "env DISPLAY=:0 XAUTHORITY=$(systemctl --user show-environment | sed -n 's/^XAUTHORITY=//p')"
    output = next((line.split()[0] for line in _user(run, f"{env} xrandr", "xrandr").splitlines()
                   if " connected" in line), None)
    if output is None:
        raise DeskError("xrandr reports no monitors")
    _user(run, f"{env} xrandr --output {output} --mode {resolution[0]}x{resolution[1]}", "xrandr")
    time.sleep(1)


def _hyprland(run, resolution, scale) -> None:
    """Hyprland, through hyprctl: its monitor rule with the mode and the scale."""
    resolution = resolution or (tuple(run.resolution) if run.resolution else None)
    env = ("env XDG_RUNTIME_DIR=/run/user/$(id -u) HYPRLAND_INSTANCE_SIGNATURE="
           "$(systemctl --user show-environment | sed -n 's/^HYPRLAND_INSTANCE_SIGNATURE=//p')")
    monitors = json.loads(_user(run, f"{env} hyprctl -j monitors", "hyprctl monitors"))
    if not monitors:
        raise DeskError("Hyprland reports no monitors")
    m = monitors[0]
    mode = f"{resolution[0]}x{resolution[1]}" if resolution else f"{m['width']}x{m['height']}"
    want = (scale or run.scale or 100) / 100
    # A new rule waits for the next frame, which a still screen does not draw; the reload draws one.
    rule = f"{env} hyprctl keyword monitor {m['name']},{{}} && {env} hyprctl dispatch forcerendererreload"
    said = _user(run, rule.format(f"{mode},0x0,{want:g}"), "hyprctl keyword monitor")
    if said.split() != ["ok", "ok"]:
        raise DeskError(f"Hyprland refused the monitor rule: {said.strip()[-300:]}")

    def state():
        now = next(n for n in json.loads(_user(run, f"{env} hyprctl -j monitors", "hyprctl monitors"))
                   if n["name"] == m["name"])
        return (now["width"], now["height"]), now["scale"]

    # Hyprland takes a scale that does not divide the mode as one near it that does.
    size, scale_now = _settled(state, resolution, want)
    try:
        _check_taken(m["name"], size, scale_now, resolution, want)
    except DeskError:
        # Back to what the run counts with.
        _user(run, rule.format(f"{m['width']}x{m['height']},0x0,{m['scale']:g}"), "hyprctl keyword monitor")
        raise


def _settled(state, resolution, want: float, seconds: float = 3):
    """The mode and scale once they are what was asked, or what they are after `seconds`: the
    compositor applies them a moment after it answered."""
    deadline = time.monotonic() + seconds
    while True:
        size, scale = state()
        if ((not resolution or tuple(size or ()) == tuple(resolution)) and scale is not None
                and abs(scale - want) <= 0.005) or time.monotonic() > deadline:
            return size, scale
        time.sleep(0.5)


def _check_taken(output: str, size, scale, resolution, want: float) -> None:
    """Refuse a mode or scale the desktop did not take, which desk would otherwise count with."""
    if resolution and tuple(size or ()) != tuple(resolution):
        shown = f"{size[0]}x{size[1]}" if size else "no mode it reports"
        raise DeskError(f"{output} runs at {shown} instead of {resolution[0]}x{resolution[1]}")
    if scale is None or abs(scale - want) > 0.005:
        raise DeskError(f"{output} runs at scale {scale} instead of {want:g}: pick a scale the mode divides "
                        "evenly (desk display --scale)")
