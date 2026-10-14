"""Client for the in-session agent on Windows guests (images/_shared/windows/agent.ps1).

SSH sessions on Windows cannot see the interactive desktop, so UI Automation queries and
`exec --gui` go to an agent started at logon inside that session, over a forwarded port.
"""

import json
import socket

from .config import DeskError


def request(run, timeout: float = 60, **req) -> dict:
    if not run.agent_port:
        raise DeskError(f"run {run.id} has no in-session agent port")
    try:
        with socket.create_connection(("127.0.0.1", run.agent_port), timeout=timeout) as s:
            s.sendall(json.dumps(req).encode() + b"\n")
            s.shutdown(socket.SHUT_WR)
            chunks = []
            while chunk := s.recv(65536):
                chunks.append(chunk)
    except TimeoutError as e:
        # Connected or not, it is there but busy: one request at a time, and a large window's
        # tree or a program it waits for can take longer than this.
        raise DeskError(f"the in-session agent did not answer {req.get('cmd')!r} within {timeout:.0f}s; it may still "
                        "be working on it or on an earlier request") from e
    except OSError as e:
        raise DeskError(f"in-session agent not reachable ({e}); is the desktop session logged in?") from e
    raw = b"".join(chunks).decode("utf-8-sig")
    try:
        out = json.loads(raw) if raw else {"error": "the in-session agent closed the connection without an answer"}
    except ValueError:
        raise DeskError(f"the in-session agent's answer was cut off after {len(raw)} characters") from None
    if isinstance(out, dict) and out.get("error"):
        raise DeskError(out["error"])
    return out
