"""Client for the in-session agent on Windows guests (images/distros/windows-11/agent/agent.ps1).

SSH sessions on Windows cannot see the interactive desktop, so UI Automation queries and
`exec --gui` go to an agent started at logon inside that session, over a forwarded port.
"""

import json
import socket
import time

from .config import DeskError

# agent-loop.ps1 starts an agent that ended again within this many seconds (PowerShell's start and
# the C# it compiles).
RESTART_WAIT = 60


class AgentGone(DeskError):
    """Nothing listens on the agent's port, or it went away without an answer."""


def _query(req: dict) -> bool:
    """Asking again changes nothing in the guest."""
    return req.get("cmd") == "ping" or (req.get("cmd") == "uia" and req.get("op") in
                                        ("snapshot", "resolve"))


def request(run, timeout: float = 60, **req) -> dict:
    if not run.agent_port:
        raise DeskError(f"run {run.id} has no in-session agent port")
    try:
        return _request(run, timeout, req)
    except AgentGone:
        # An action may have been done before the agent ended: only a query is asked again.
        if not _query(req):
            raise
    deadline = time.monotonic() + RESTART_WAIT
    while True:
        try:
            _request(run, 5, {"cmd": "ping"})
            break
        except AgentGone:
            if time.monotonic() > deadline:
                raise
            time.sleep(1)
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
        raise DeskError(f"the in-session agent did not answer {req.get('cmd')!r} within {timeout:.0f}s; it may still "
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
    if isinstance(out, dict) and out.get("error"):
        raise DeskError(out["error"])
    return out
