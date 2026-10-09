"""The guest's clock, moved for tests of what depends on time (schedules, expiry, retention):
`desk clock set +3d`. Time keeps running from there; the guest's own time sync is off until
`desk clock reset`, and desk keeps the offset across restores and reboots."""

import re
import shlex
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
    elif spec.family == "openwrt":
        command = ("/etc/init.d/sysntpd enable && /etc/init.d/sysntpd restart" if on
                   else "/etc/init.d/sysntpd stop; /etc/init.d/sysntpd disable")
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
    target = parse(value, time.time() + run.clock_offset)
    _sync(run, False)
    _jump(run, target)
    run.log_event(clock=value, offset=round(run.clock_offset))
    return target


def _jump(run, target: float) -> None:
    run.clock_offset = target - time.time()
    run.save()
    apply(run)


def duration(value: str) -> float:
    m = re.fullmatch(r"(?:\d+[smhdw])+", value.strip())
    if not m:
        raise DeskError(f"a step is like 1d, 6h or 1h30m (got {value!r})")
    return sum(int(n) * UNITS[u] for n, u in re.findall(r"(\d+)([smhdw])", value))


# What timers start, for the system and the desktop user: the units they activate (and pending
# jobs) that are still running, and those that started or finished after a journal cursor (not a
# time: the journal holds entries from wherever the clock was before). Windows: scheduled tasks
# running now.
_TIMED = r"""
scopes() { echo ""; echo "--user -M {user}@"; }
activated() {
    scopes | while read -r s; do
        systemctl $s list-timers --all --no-legend --plain </dev/null 2>/dev/null | awk '{print $NF}'
    done | sort -u
}
"""
LINUX_BUSY = _TIMED + r"""
scopes | while read -r s; do
    systemctl $s list-jobs --no-legend </dev/null 2>/dev/null | awk '{print "job " $2}'
    for u in $(systemctl $s list-timers --all --no-legend --plain </dev/null 2>/dev/null | awk '{print $NF}'); do
        case $(systemctl $s is-active "$u" </dev/null 2>/dev/null) in
            active|activating|deactivating|reloading) echo "$u";;
        esac
    done
done
"""
LINUX_STARTED = _TIMED + r"""
journalctl _COMM=systemd --after-cursor "$C" -o cat --no-pager </dev/null 2>/dev/null \
    | sed -n 's/^\(Started\|Finished\) \([^ ]*\) - .*/\2/p' | sort -u | grep -Fx -f <(activated)
"""
WINDOWS_TASKS = "Get-ScheduledTask | Where-Object State -eq Running | ForEach-Object { $_.TaskPath + $_.TaskName }"


def _lines(run, spec, script: str, cursor: str = "") -> set[str]:
    if spec.family == "windows":
        r = guest.ssh_run(run.ssh_port, spec.root_user, spec.cmd(script), capture=True, timeout=60)
    else:
        r = guest.ssh_run(run.ssh_port, spec.root_user, f"C={shlex.quote(cursor)} bash -s",
                          input=script.replace("{user}", run.user).encode(), capture=True, timeout=60)
    return {ln.strip() for ln in r.stdout.decode(errors="replace").splitlines() if ln.strip()}


def _cursor(run, spec) -> str:
    r = guest.ssh_run(run.ssh_port, spec.root_user, "journalctl -n 0 --show-cursor -q", capture=True, timeout=60)
    out = r.stdout.decode(errors="replace").strip()
    if not out.startswith("-- cursor: "):
        raise DeskError(f"cannot read the guest's journal position: {out or r.stderr.decode(errors='replace')}")
    return out.removeprefix("-- cursor: ")


def _busy(run, spec) -> set[str]:
    if spec.family == "openwrt":
        return set()  # cron has no notion of a job still running
    return _lines(run, spec, WINDOWS_TASKS if spec.family == "windows" else LINUX_BUSY)


def _settle(run, spec, before: set[str], timeout: float) -> None:
    """Until what the jump set off has finished: no new jobs or running tasks twice in a row."""
    deadline = time.monotonic() + timeout
    quiet = 0
    while quiet < 2:
        time.sleep(1)
        quiet = quiet + 1 if not (_busy(run, spec) - before) else 0
        if time.monotonic() > deadline:
            raise DeskError(f"still busy {timeout:.0f}s after the clock moved: {sorted(_busy(run, spec) - before)[:5]}")


def advance(run, value: str, step: str, timeout: float = 600):
    """Move the clock forward in steps, letting every timer due in a step run before the next:
    a single jump fires each timer once, however many of its times it skipped. Yields the guest
    time after each step and the units that started."""
    spec = load_image(run.image)
    current = time.time() + run.clock_offset
    target, size = parse(value, current), duration(step)
    if target <= current:
        raise DeskError("--step moves the clock forward only")
    _sync(run, False)
    before = _busy(run, spec)
    t = current
    while t < target:
        t = min(t + size, target)
        timers = spec.family == "linux"
        cursor = _cursor(run, spec) if timers else ""
        _jump(run, t)
        _settle(run, spec, before, timeout)
        started = sorted(_lines(run, spec, LINUX_STARTED, cursor)) if timers else []
        yield t, started
    run.log_event(clock=value, clock_step=step, offset=round(run.clock_offset))


def reset(run) -> None:
    run.clock_offset = 0.0
    run.save()
    apply(run)
    _sync(run, True)
    run.log_event(clock="reset")


def describe(run, now: float | None = None) -> str:
    t = guest_time(run) if now is None else now
    host = time.time()
    shown = datetime.fromtimestamp(t, GUEST_TZ)
    off = t - host
    if abs(off) < 2:
        return f"{shown:%Y-%m-%d %H:%M:%S %Z} (as the host)"
    # The guest's clock was read a moment before the host's: +31d is 2678399.98 s ahead.
    sign, off = ("+" if off > 0 else "-"), round(abs(off))
    d, rest = divmod(off, 86400)
    note = ""
    here = datetime.fromtimestamp(host, GUEST_TZ)
    if shown.utcoffset() != here.utcoffset():
        # Offsets count real time; across a change of summer time the wall clock shows an hour more or less.
        note = f"; {shown:%Z} there, {here:%Z} now, so the wall clock differs by {_wall(shown, here)}"
    return f"{shown:%Y-%m-%d %H:%M:%S %Z} (host {sign}{d}d {rest // 3600:02d}:{rest % 3600 // 60:02d}:{rest % 60:02d}{note})"


def _wall(a: datetime, b: datetime) -> str:
    diff = round((a.replace(tzinfo=None) - b.replace(tzinfo=None)).total_seconds())
    sign, diff = ("+" if diff >= 0 else "-"), abs(diff)
    d, rest = divmod(diff, 86400)
    return f"{sign}{d}d {rest // 3600:02d}:{rest % 3600 // 60:02d}:{rest % 60:02d}"
