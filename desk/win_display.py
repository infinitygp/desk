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
    $rows = [WinDisplay]::Change($p.count, $p.width, $p.height, $p.scale, $p.monitor)
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

    monitor is 1-based, or None for all. No settings means read only. The caller activates
    dormant outputs before calling. LiveUnavailable is exclusive to unsupported global scaling;
    transport errors, refusals and failed readbacks are ordinary DeskErrors.
    """
    if monitor is not None and not (
        type(monitor) is int and 1 <= monitor <= run.displays
    ):
        raise DeskError(f"--monitor is 1 to {run.displays}")
    if resolution is not None and (
        len(resolution) != 2
        or not all(type(v) is int and 0 < v < 2**31 for v in resolution)
    ):
        raise DeskError("resolution must have a positive width and height")
    if scale is not None and not (type(scale) is int and 0 < scale <= 2**31 - 1):
        raise DeskError("scale must be a positive percentage")
    width, height = resolution or (0, 0)
    encoded = base64.b64encode(_SCRIPT.encode("utf-16-le")).decode()
    args = [
        "-NoProfile",
        "-NonInteractive",
        "-OutputFormat",
        "Text",
        "-EncodedCommand",
        encoded,
    ]
    params = dict(
        count=run.displays,
        width=width,
        height=height,
        scale=scale or 0,
        monitor=monitor or 0,
    )
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
        global_change = monitor is None or run.displays == 1
        error = (
            LiveUnavailable
            if reply.get("code") == 3 and scale is not None and global_change
            else DeskError
        )
        raise error(f"Windows display configuration failed: {why}")
    try:
        rows = json.loads(reply.get("stdout") or "")
        if (
            isinstance(rows, list)
            and len(rows) == run.displays
            and all(
                isinstance(row, dict)
                and set(row) == {"width", "height", "scale"}
                and all(type(v) is int and v > 0 for v in row.values())
                for row in rows
            )
        ):
            return rows
    except (ValueError, TypeError):
        pass
    raise DeskError("Windows returned no usable per-monitor display configuration")
