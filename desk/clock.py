"""The guest's clock, moved for tests of what depends on time (schedules, expiry, retention):
`desk clock set +3d`. Time keeps running from there; the guest's own time sync is off until
`desk clock reset`, and desk keeps the offset across restores and reboots."""

import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import guest
from .config import DeskError, load_image

# Every image is installed in this time zone; times given without a zone are the guest's local time.
GUEST_TZ = ZoneInfo("Europe/Warsaw")
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
RELATIVE = re.compile(r"([+-])((?:\d+[smhdw])+)")


def parse(value: str, now: float) -> float:
    """`+3d`, `-2h30m` (from the guest's current time) or `2026-12-24 23:59` (guest local time)."""
    m = RELATIVE.fullmatch(value.strip())
    if m:
        delta = sum(int(n) * UNITS[u] for n, u in re.findall(r"(\d+)([smhdw])", m.group(2)))
        return now + (delta if m.group(1) == "+" else -delta)
    try:
        when = datetime.fromisoformat(value.strip())
    except ValueError:
        raise DeskError(f"time is +3d, -2h30m (s m h d w) or 2026-12-24 23:59 (got {value!r})") from None
    if when.tzinfo is None:
        when = when.replace(tzinfo=GUEST_TZ)
    return when.timestamp()


def _sync(run, on: bool) -> None:
    spec = load_image(run.image)
    if spec.family == "windows":
        script = ("Set-Service w32time -StartupType Manual; Start-Service w32time; w32tm /resync /force | Out-Null"
                  if on else "Stop-Service w32time -Force; Set-Service w32time -StartupType Disabled")
        command = spec.cmd(script)
    else:
        command = f"timedatectl set-ntp {'true' if on else 'false'}"
    r = guest.ssh_run(run.ssh_port, spec.root_user, command, capture=True, timeout=60)
    if r.returncode != 0:
        raise DeskError(f"cannot switch the guest's time sync: {r.stderr.decode(errors='replace').strip()}")


def guest_time(run) -> float:
    return guest.agent(run.dom(), "guest-get-time") / 1e9


def apply(run) -> None:
    """Host time plus the run's offset into the guest (after a restore or a reboot)."""
    guest.agent(run.dom(), "guest-set-time", time=time.time_ns() + int(run.clock_offset * 1e9))


def set_time(run, value: str) -> float:
    now = time.time()
    current = now + run.clock_offset
    target = parse(value, current)
    _sync(run, False)
    run.clock_offset = target - now
    run.save()
    apply(run)
    run.log_event(clock=value, offset=round(run.clock_offset))
    return target


def reset(run) -> None:
    run.clock_offset = 0.0
    run.save()
    apply(run)
    _sync(run, True)
    run.log_event(clock="reset")


def describe(run) -> str:
    t = guest_time(run)
    shown = datetime.fromtimestamp(t, GUEST_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    off = t - time.time()
    if abs(off) < 2:
        return f"{shown} (as the host)"
    sign, off = ("+" if off > 0 else "-"), abs(off)
    d, rest = divmod(int(off), 86400)
    return f"{shown} (host {sign}{d}d {rest // 3600:02d}:{rest % 3600 // 60:02d}:{rest % 60:02d})"
