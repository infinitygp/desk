"""Whether a guest is calm, measured from the host as the CPU time of the domain (QEMU's process:
every vCPU thread, and the device emulation done for the guest), so it needs nothing from the
guest. A restored or freshly booted Windows works in the background for minutes (updates,
indexing, .NET's compiler), and a test started meanwhile shares the CPU with it."""

import subprocess
import time

import libvirt

from . import guest
from .config import DeskError

# Seconds the guest is sampled at.
STEP = 3
# The stretch of time a guest must stay calm for, and over which its busiest moment is measured.
WINDOW = 15
# vCPUs a calm guest stays under on average, and in its busiest WINDOW.
AVERAGE = 1.0
PEAK = 2.0
# Processes named when the guest is not calm.
TOP = 5

# Windows: the CPU seconds of every process the session may ask (the others raise), one per line; a
# svchost under the names of its services, as "svchost" says nothing about what worked.
WINDOWS_PROCESSES = r"""
$svc = @{}; Get-CimInstance Win32_Service -Filter 'ProcessId > 0' | ForEach-Object { $svc[[int]$_.ProcessId] += @($_.Name) }
foreach ($p in Get-Process) {
    $name = if ($p.Name -eq 'svchost' -and $svc[$p.Id]) { $svc[$p.Id] -join '+' } else { $p.Name }
    try { "$name`t$($p.TotalProcessorTime.TotalSeconds.ToString([cultureinfo]::InvariantCulture))" } catch {}
}
"""


def cpu_seconds(dom) -> float:
    try:
        return dom.info()[4] / 1e9
    except libvirt.libvirtError as e:
        raise DeskError(f"cannot read the guest's CPU time: {e}") from None


def _sample(dom) -> tuple[float, float]:
    return time.monotonic(), cpu_seconds(dom)


def _rate(start: tuple[float, float], end: tuple[float, float]) -> float:
    return (end[1] - start[1]) / (end[0] - start[0])


def _window(points: list[tuple[float, float]]) -> float | None:
    """vCPUs over the WINDOW seconds up to the last sample; None while there is less than that."""
    old = [p for p in points[:-1] if p[0] <= points[-1][0] - WINDOW]
    return _rate(old[-1], points[-1]) if old else None


def watch(dom, seconds: float) -> tuple[float, float]:
    """The guest's vCPUs on average over `seconds` and in its busiest WINDOW."""
    points = [_sample(dom)]
    while points[-1][0] - points[0][0] < seconds:
        time.sleep(STEP)
        points.append(_sample(dom))
    average = _rate(points[0], points[-1])
    windows = [v for i in range(len(points)) if (v := _window(points[:i + 1])) is not None]
    return average, max(windows, default=average)


def wait(dom, limit: float) -> float | None:
    """Until the last WINDOW seconds averaged under AVERAGE vCPUs: None then; after `limit` seconds
    those of the last WINDOW. At least one whole WINDOW is measured."""
    points = [_sample(dom)]
    while True:
        time.sleep(STEP)
        points.append(_sample(dom))
        vcpus = _window(points)
        if vcpus is not None:
            if vcpus < AVERAGE:
                return None
            if points[-1][0] - points[0][0] >= limit:
                return vcpus


def processes(run, spec) -> dict[str, float]:
    """CPU seconds the guest's processes have used so far, summed per name."""
    if spec.family == "windows":
        command = spec.cmd(WINDOWS_PROCESSES)
    elif spec.family == "linux":
        command = "ps -eo comm=,cputimes="
    else:
        raise DeskError(f"the processes of a {spec.family} guest cannot be listed")
    try:
        r = guest.ssh_run(run.ssh_port, spec.root_user, command, capture=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise DeskError("the guest did not list its processes within 60 s") from None
    guest.connected(r, "asking the guest which processes work")
    if r.returncode != 0:
        raise DeskError(f"the guest could not list its processes: {r.stderr.decode(errors='replace').strip()[-300:]}")
    used: dict[str, float] = {}
    for line in r.stdout.decode(errors="replace").splitlines():
        # The name may hold spaces (Windows' "Memory Compression", a thread name on Linux).
        name, _, seconds = line.strip().rpartition("\t" if spec.family == "windows" else " ")
        try:
            used[name.strip()] = used.get(name.strip(), 0.0) + float(seconds)
        except ValueError:
            pass  # a line that is no listing entry (a warning of the shell)
    if not used:
        raise DeskError("the guest's list of processes held nothing readable")
    return used


def busiest(before: dict[str, float], after: dict[str, float]) -> str:
    """The processes that used the most CPU time between two listings: `TiWorker 9.1 s, MsMpEng 6.5 s`."""
    gain = {name: seconds - before.get(name, 0.0) for name, seconds in after.items()}
    top = sorted((name for name in gain if gain[name] > 0), key=lambda name: -gain[name])[:TOP]
    return ", ".join(f"{name} {gain[name]:.1f} s" for name in top) or "no process used CPU time"
