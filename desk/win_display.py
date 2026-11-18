"""On-demand display configuration in an already logged-in Windows desktop session."""

import base64
import json
from pathlib import Path

from . import winagent
from .config import DeskError


class LiveUnavailable(DeskError):
    """The guest cannot change scale live and the request was for all monitors."""


# The C# travels in the child's environment, not in -EncodedCommand (32K command-line limit).
_SCRIPT = """
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try {
    Add-Type -TypeDefinition $env:DESK_DISPLAY_SOURCE
    $p = $env:DESK_DISPLAY_REQUEST | ConvertFrom-Json
    if ($null -ne $p.connected) {
        $rows = [WinDisplay]::Connection($p.displays, $p.active, $p.monitor, $p.connected, $p.width, $p.height)
    } else {
        $rows = [WinDisplay]::Change($p.count, $p.width, $p.height, $p.scale, $p.monitor)
    }
    ConvertTo-Json -InputObject @($rows) -Compress
} catch {
    $cause = $_.Exception.GetBaseException()
    # Windows PowerShell may put serialized progress on stderr even with text output.
    [Console]::WriteLine($cause.Message)
    if ($cause.GetType().FullName -eq 'WinDisplay+Unavailable') { exit 3 }
    exit 1
}
"""


def change(
    run, resolution: tuple[int, int] | None, scale: int | None, monitor: int | None
) -> list[dict[str, int]]:
    """Read all active monitors left to right after applying the requested settings.

    monitor is a compact 1-based active index, or None for all. No settings means read only.
    LiveUnavailable is exclusive to unsupported global scaling;
    transport errors, refusals and failed readbacks are ordinary DeskErrors.
    """
    count = run.displays - len(getattr(run, "disconnected", {}))
    if monitor is not None and not (type(monitor) is int and 1 <= monitor <= count):
        raise DeskError(f"active monitor index is 1 to {count}")
    if resolution is not None and (
        len(resolution) != 2
        or not all(type(v) is int and 0 < v < 2**31 for v in resolution)
    ):
        raise DeskError("resolution must have a positive width and height")
    if scale is not None and not (type(scale) is int and 0 < scale <= 2**31 - 1):
        raise DeskError("scale must be a positive percentage")
    width, height = resolution or (0, 0)
    params = dict(
        count=count, width=width, height=height, scale=scale or 0, monitor=monitor or 0
    )
    rows = _request(
        run,
        params,
        allow_unavailable=scale is not None and (monitor is None or run.displays == 1),
    )
    if (
        isinstance(rows, list)
        and len(rows) == count
        and all(
            isinstance(row, dict)
            and set(row) == {"width", "height", "scale"}
            and all(type(v) is int and v > 0 for v in row.values())
            for row in rows
        )
    ):
        return rows
    raise DeskError("Windows returned no usable per-monitor display configuration")


def change_connection(run, monitor: int, connected: bool) -> list[int]:
    """Use physical monitor numbers, including detached adapters. Never change host state."""
    if (
        type(monitor) is not int
        or not 2 <= monitor <= run.displays
        or type(connected) is not bool
    ):
        raise DeskError(
            "connection changes require a secondary monitor and a boolean state"
        )
    active = [i for i in range(1, run.displays + 1) if str(i) not in run.disconnected]
    if connected == (monitor in active):
        raise DeskError(
            f"monitor {monitor} is already {'connected' if connected else 'disconnected'}"
        )
    saved = run.disconnected.get(str(monitor), [0, 0])
    if connected and not (
        isinstance(saved, list)
        and len(saved) == 2
        and all(type(v) is int and 0 < v < 2**31 for v in saved)
    ):
        raise DeskError(f"no usable saved resolution for monitor {monitor}")
    size = _request(
        run,
        dict(
            displays=run.displays,
            active=active,
            monitor=monitor,
            connected=connected,
            width=saved[0],
            height=saved[1],
        ),
    )
    if (
        isinstance(size, list)
        and len(size) == 2
        and all(type(v) is int and v > 0 for v in size)
    ):
        return size
    raise DeskError(f"Windows returned no usable resolution for monitor {monitor}")


def _request(run, params, allow_unavailable=False):
    encoded = base64.b64encode(_SCRIPT.encode("utf-16-le")).decode()
    args = [
        "-NoProfile",
        "-NonInteractive",
        "-OutputFormat",
        "Text",
        "-EncodedCommand",
        encoded,
    ]
    env = {
        "DESK_DISPLAY_SOURCE": Path(__file__)
        .with_suffix(".cs")
        .read_text(encoding="utf-8"),
        "DESK_DISPLAY_REQUEST": json.dumps(params),
    }
    reply = winagent.request(
        run,
        timeout=90,
        cmd="run",
        file="powershell.exe",
        args=args,
        env=env,
        wait=True,
        timeout_s=60,
    )
    if reply.get("timeout"):
        raise DeskError("Windows display configuration timed out")
    if reply.get("code") != 0:
        why = " ".join(
            (reply.get("stdout") or reply.get("stderr") or "no result").split()
        )[-500:]
        error = (
            LiveUnavailable
            if reply.get("code") == 3 and allow_unavailable
            else DeskError
        )
        raise error(f"Windows display configuration failed: {why}")
    try:
        return json.loads(reply.get("stdout") or "")
    except (ValueError, TypeError) as e:
        raise DeskError("Windows returned no usable display configuration") from e
