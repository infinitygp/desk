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
    except OSError as e:
        raise DeskError(f"in-session agent not reachable ({e}); is the desktop session logged in?") from e
    raw = b"".join(chunks).decode("utf-8-sig")
    out = json.loads(raw) if raw else {"error": "empty reply"}
    if isinstance(out, dict) and out.get("error"):
        raise DeskError(out["error"])
    return out
