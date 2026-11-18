"""Client for the in-session agent on Windows guests (images/distros/windows-11/agent/agent.ps1).

SSH sessions on Windows cannot see the interactive desktop, so UI Automation queries and
`exec --gui` go to an agent started at logon inside that session, over a forwarded port.
"""

import base64
import json
import secrets
import socket
import time

from .config import DeskError

# agent-loop.ps1 starts an agent that ended again within this many seconds (PowerShell's start and
# the C# it compiles).
RESTART_WAIT = 60
# What one ping that waits for the agent may take; a busy agent is pinged again until RESTART_WAIT.
PING_TIMEOUT = 5


class AgentGone(DeskError):
    """Nothing listens on the agent's port, or it went away without an answer."""


class AgentBusy(DeskError):
    """No answer within the time given: the agent is busy, or the answer was lost on the way."""


# Reuse the resolver that made this guest's listing. Run in a separate, windowless desktop
# process: even an existing warm image can do the check, and a hung provider can be killed.
_VISIBLE_POINT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
try {
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
. C:\desk\uia.ps1
Add-Type -Namespace Desk -Name Dpi -MemberDefinition @'
[DllImport("user32.dll")] public static extern IntPtr SetThreadDpiAwarenessContext(IntPtr value);
'@
if ([Desk.Dpi]::SetThreadDpiAwarenessContext([IntPtr](-4)) -eq [IntPtr]::Zero) { throw 'could not set DPI awareness' }
$q = $env:DESK_VISIBLE | ConvertFrom-Json
if ($q.spec.path[-1] -ge 100000) { throw 'cannot identify this MSAA-only item in a UI Automation hit test' }
$el = Find-Node $q.spec
$r = $el.Current.BoundingRectangle
function Test-Point {
    if ($el.Current.IsOffscreen -or $r.IsEmpty) { return @{point=$null} }
    $x0 = [Math]::Max(0, [Math]::Ceiling($r.Left)); $x1 = [Math]::Min($q.width, [Math]::Ceiling($r.Right))
    $y0 = [Math]::Max(0, [Math]::Ceiling($r.Top)); $y1 = [Math]::Min($q.height, [Math]::Ceiling($r.Bottom))
    if ($x0 -ge $x1 -or $y0 -ge $y1) { return @{point=$null} }
    $ancestors = @()
    for ($a = $RawWalker.GetParent($el); $null -ne $a; $a = $RawWalker.GetParent($a)) { $ancestors += $a }
    function Samples($lo, $hi) {
        @(0.5, 0.1, 0.9, 0, 1) | ForEach-Object { $lo + [Math]::Floor(($hi-$lo-1)*$_) } | Select-Object -Unique
    }
    foreach ($y in (Samples $y0 $y1)) {
        foreach ($x in (Samples $x0 $x1)) {
            $hit = $AE::FromPoint([Windows.Point]::new($x, $y))
            if ($null -eq $hit) { throw 'UI Automation returned no hit' }
            # The taskbar can return only its container. That proves neither that its child
            # was visible nor that another element covered it.
            if ($ancestors -contains $hit) { $inconclusive = $true; continue }
            if (-not $cover) { $cover = "$($hit.Current.ControlType.ProgrammaticName) $($hit.Current.Name)".Trim() }
            while ($null -ne $hit) {
                if ([Windows.Automation.Automation]::Compare($hit, $el)) { return @{point=@([int]$x, [int]$y)} }
                $hit = $RawWalker.GetParent($hit)
            }
        }
    }
    if ($inconclusive) { throw 'UI Automation could not distinguish the element from its container' }
    return @{point=$null; covered=$cover}
}
Test-Point | ConvertTo-Json -Compress
} catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }
"""


def visible_point(
    run, spec: dict, size: tuple[int, int]
) -> tuple[tuple[int, int] | None, str | None, bool]:
    """A point verified by a desktop hit test, never inferred from an element's rectangle."""
    encoded = base64.b64encode(_VISIBLE_POINT.encode("utf-16-le")).decode()
    reply = request(
        run,
        cmd="run",
        file="powershell.exe",
        wait=True,
        timeout_s=20,
        timeout=30,
        args=[
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-OutputFormat",
            "Text",
            "-EncodedCommand",
            encoded,
        ],
        env={
            "DESK_VISIBLE": json.dumps(
                {"spec": spec, "width": size[0], "height": size[1]}
            )
        },
    )
    if reply.get("timeout") or reply.get("code") != 0:
        why = (
            "timed out"
            if reply.get("timeout")
            else (reply.get("stderr") or "no result").strip()[:300]
        )
        raise DeskError(f"Windows visibility hit test failed: {why}")
    try:
        result = json.loads(reply.get("stdout") or "")
        point = result["point"]
        if point is None:
            return None, result.get("covered"), False
        if (
            isinstance(point, list)
            and len(point) == 2
            and all(
                type(v) is int and 0 <= v < n for v, n in zip(point, size, strict=True)
            )
        ):
            return (point[0], point[1]), None, False
    except (ValueError, KeyError, TypeError):
        pass
    raise DeskError("Windows visibility hit test returned no usable result")


def _query(req: dict) -> bool:
    """Asking again changes nothing in the guest."""
    return req.get("cmd") == "ping" or (req.get("cmd") == "uia" and req.get("op") in
                                        ("snapshot", "resolve"))


def request(run, timeout: float = 60, **req) -> dict:
    if not run.agent_port:
        raise DeskError(f"run {run.id} has no in-session agent port")
    if not _query(req):
        # An agent that has begun a request with this id does not begin it again, so an action
        # whose connection was lost can be sent again.
        req["id"] = secrets.token_hex(8)
    try:
        return _request(run, timeout, req)
    except (AgentGone, AgentBusy) as e:
        first = e
    # Lost on the way (a guest that still remembers an earlier connection from the same host port
    # resets this one), or the agent ended and agent-loop.ps1 starts it again: wait until it answers.
    deadline = time.monotonic() + RESTART_WAIT
    while True:
        try:
            pong = _request(run, PING_TIMEOUT, {"cmd": "ping"})
            break
        except (AgentGone, AgentBusy):
            if time.monotonic() > deadline:
                raise first
            time.sleep(1)
    if "id" in req and not pong.get("ids"):
        # An older agent knows no ids and may have done the action before the connection was lost.
        raise first
    return _request(run, timeout, req)


def _request(run, timeout: float, req: dict) -> dict:
    try:
        with socket.create_connection(("127.0.0.1", run.agent_port), timeout=timeout) as s:
            # Not half-closed, and said so ("open"): the agent takes a closed connection for a
            # caller that gave up while an earlier request held it, and leaves the request undone
            # (it was reported as failed). Older desks half-close, and their requests are done.
            s.sendall(json.dumps({**req, "open": True}).encode() + b"\n")
            chunks = []
            while chunk := s.recv(65536):
                chunks.append(chunk)
    except TimeoutError as e:
        # Connected or not, it is there but busy: one request at a time, and a large window's
        # tree or a program it waits for can take longer than this.
        raise AgentBusy(f"the in-session agent did not answer {req.get('cmd')!r} within {timeout:.0f}s; it may still "
                        "be working on it or on an earlier request") from e
    except OSError as e:
        raise AgentGone(f"in-session agent not reachable ({e}): it ended (C:\\desk\\agent.log says why; it is "
                        "started again within a minute) or the desktop session is not logged in yet") from e
    raw = b"".join(chunks).decode("utf-8-sig")
    if not raw:
        raise AgentGone("the in-session agent closed the connection without an answer")
    try:
        out = json.loads(raw)
    except ValueError:
        raise DeskError(f"the in-session agent's answer was cut off after {len(raw)} characters") from None
    if isinstance(out, dict) and out.get("begun"):
        # Sent again under its id while the program it waits for still runs: desk could not see its result.
        raise AgentGone(f"the in-session agent had started {req.get('file') or req.get('cmd')!r} when the connection "
                        "to it was lost; its result went with that connection, and it was not started again (it "
                        "is still running)")
    if isinstance(out, dict) and out.get("error"):
        raise DeskError(out["error"])
    return out
