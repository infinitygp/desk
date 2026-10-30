"""Local IPC endpoints of the guest, listed at the session's start, at every mark and at its stop:
named pipes by name, TCP connections and listeners and UDP endpoints with the process that owns
each (Get-NetTCPConnection, Get-NetUDPEndpoint), so the report says which appeared and went
while the product ran, and which belong to the processes followed.

What it does not see: what goes through them. No message content is read from a pipe or a socket
by these listings, and nothing here intercepts IPC. A pipe's server process is not in the listing
either: Windows tells it (GetNamedPipeServerProcessId) only to a client connected to the pipe.
`desk observe pipe NAME` is that client, one connection on request, which a server that takes a
single client sees like any other; ALPC, RPC endpoints, shared memory and window messages are not
listed at all."""

import base64
import json
import time
from pathlib import Path

from .base import ArtifactSpec, Option, Provider
from .guestops import ObserveError
from .session import Session


def _name(v: str) -> str | None:
    return None if 0 < len(v) <= 256 else "a pipe name of 1 to 256 characters"


class Ipc(Provider):
    name = "ipc"
    title = "local IPC endpoints"
    kind = "snapshot"
    needs_admin = False
    OPTIONS = {
        "pipes": Option(bool, True, "list named pipes"),
        "tcp": Option(bool, True, "list TCP connections and listeners with their processes"),
        "udp": Option(bool, False, "list UDP endpoints with their processes"),
        "match": Option(str, "", "only pipes whose name contains this (case-insensitive)"),
    }
    notes = ("names of pipes and the endpoints of sockets with their owning processes, at start, marks and stop; "
             "nothing of the data sent through them, and nothing that opened and closed between two listings",
             "a pipe's server process only through desk observe pipe NAME, which connects to it")

    def probe_request(self, opts: dict, host) -> dict:
        return {}

    def _dir(self, session: Session) -> Path:
        return Path(session.tool["host_dir"])

    def take(self, guest, session: Session, label: str) -> dict:
        o = session.options
        listing = guest.call("ipc", limit=120, pipes=o["pipes"], tcp=o["tcp"], udp=o["udp"])
        names = {p["pid"]: p.get("name") for p in guest.processes()}
        match = o["match"].lower()
        snap = {"t": time.time(), "label": label,
                "pipes": sorted(p for p in listing.get("pipes") or [] if match in p.lower()),
                "tcp": [{**c, "process": names.get(c["pid"])} for c in listing.get("tcp") or []],
                "udp": [{**c, "process": names.get(c["pid"])} for c in listing.get("udp") or []]}
        if listing.get("pipes_note"):
            snap["pipes_note"] = listing["pipes_note"]
        n = len(session.tool.setdefault("snapshots", [])) + 1
        name = f"ipc-{n:03d}-{label.replace(' ', '-')}.json"
        d = self._dir(session)
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(json.dumps(snap, indent=1))
        session.tool["snapshots"].append({"file": name, "label": label, "t": snap["t"], "pipes": len(snap["pipes"]),
                                          "tcp": len(snap["tcp"]), "udp": len(snap["udp"])})
        return snap

    def start(self, guest, session: Session, host) -> None:
        self.take(guest, session, "start")

    def on_mark(self, guest, session: Session, marker: dict) -> None:
        self.take(guest, session, f"mark-{marker['seq']}")

    def stop(self, guest, session: Session, host, force: bool = False) -> None:
        self.take(guest, session, "stop")

    def artifacts(self, session: Session) -> list[ArtifactSpec]:
        return [ArtifactSpec(s["file"], None, "json", description=f"listing at {s['label']}")
                for s in session.tool.get("snapshots") or []]

    def cleanup(self, guest, session: Session, host=None) -> None:
        pass

    def summary(self, session: Session) -> list[str]:
        snaps = session.tool.get("snapshots") or []
        if len(snaps) < 2:
            return []
        d = self._dir(session)
        try:
            first, last = (json.loads((d / s["file"]).read_text()) for s in (snaps[0], snaps[-1]))
        except (OSError, ValueError):
            return ["the listings could not be read back"]
        lines = []
        came, went = sorted(set(last["pipes"]) - set(first["pipes"])), sorted(set(first["pipes"]) - set(last["pipes"]))
        if came or went:
            lines.append("pipes from start to stop: " + ", ".join([f"+{p}" for p in came[:20]] +
                                                                   [f"-{p}" for p in went[:20]]))
        listen = {(c["local"], c["lport"], c.get("process")) for c in last["tcp"] if c["state"] == "Listen"} - \
            {(c["local"], c["lport"], c.get("process")) for c in first["tcp"] if c["state"] == "Listen"}
        if listen:
            lines.append("new TCP listeners: " + ", ".join(f"{a}:{p} ({n})" for a, p, n in sorted(listen, key=str)[:20]))
        followed = {p["pid"] for p in session.processes.values()}
        if followed:
            mine = sorted({f"{c['local']}:{c['lport']}->{c['remote']}:{c['rport']} {c['state']} ({c.get('process')})"
                           for s in (first, last) for c in s["tcp"] if c["pid"] in followed})
            if mine:
                lines.append("TCP endpoints of the processes followed: " + "; ".join(mine[:20]))
        return lines


def probe_pipe(guest, name: str, send: bytes = b"", read: int = 0, timeout: float = 5) -> dict:
    """Connect to a named pipe as a client: its server's PID, and what it answers to `send`."""
    why = _name(name)
    if why:
        raise ObserveError(why)
    if not 0 < timeout <= 60:
        raise ObserveError("--timeout is 1 to 60 seconds")
    r = guest.call("pipeprobe", limit=timeout + 60, name=name, timeout_ms=int(timeout * 1000),
                   send_b64=base64.b64encode(send).decode() if send else None, read_bytes=read)
    if r.get("received_b64") is not None:
        r["received"] = base64.b64decode(r["received_b64"])
    return r
