"""Guest ports reachable from the host (desk forward 9090): an SSH tunnel per port, kept up by a
background process for as long as the run exists, so that a browser or a test on the host can use
a service in the guest. passt forwards only SSH into the guest, and only from the domain's start."""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from .config import PROJECT, RUNS, SSH_KEY, DeskError


def _dir(run) -> Path:
    return run.dir / "forward"


def _alive(pid: int) -> bool:
    try:
        return b"desk.forward" in Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False


def _free(port: int) -> bool:
    with socket.socket() as s:
        # Like ssh: a port whose last connections are in TIME_WAIT can be listened on again.
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def listing(run) -> list[dict]:
    out = []
    d = _dir(run)
    for f in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            info = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        out.append({**info, "alive": _alive(info["pid"])})
    return out


def lock(run):
    """Serialize tunnel creation and removal, independently of VM state changes."""
    from .runs import _flock

    return _flock(run.dir / ".forward.lock", f"another command changing forwards of run {run.id}", 60)


def start(run, port: int, local: int | None = None, *, exclusive: bool = False) -> int:
    """Forward the guest's port to the host's loopback; optionally refuse an existing tunnel."""
    with lock(run):
        return _start(run, port, local, exclusive=exclusive)


def _start(run, port: int, local: int | None, *, exclusive: bool) -> int:
    from . import runs
    from .virt import free_port

    for f in listing(run):
        if f["port"] == port and f["alive"]:
            if exclusive:
                raise DeskError(f"guest port {port} has a live forward already; the existing forward is left alone")
            if local is not None and local != f["local"]:
                raise DeskError(f"guest port {port} is forwarded to 127.0.0.1:{f['local']} already "
                                f"(desk forward --stop {port}, then again with :{local})")
            return f["local"]
    # A tunnel's ssh holds its port only while connected: while its guest reboots the port is
    # free, and another run must not take it.
    claimed = {f["local"]: r.id for r in runs.all_runs() for f in listing(r) if f["alive"]}
    if local is None:
        local = port if port not in claimed and _free(port) else free_port()
        while local in claimed:
            local = free_port()
    elif local in claimed:
        raise DeskError(f"port {local} on the host is the tunnel of run {claimed[local]}")
    elif not _free(local):
        raise DeskError(f"port {local} on the host is in use")
    d = _dir(run)
    d.mkdir(exist_ok=True)
    log_path = d / f"{port}.log"
    with open(log_path, "w") as log:
        p = subprocess.Popen([sys.executable, "-m", "desk.forward", run.id, str(port), str(local)],
                             cwd=PROJECT, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    (d / f"{port}.json").write_text(json.dumps({"port": port, "local": local, "pid": p.pid}))
    deadline = time.monotonic() + 15
    while not _listening(local):
        if p.poll() is not None or time.monotonic() > deadline:
            stop(run, port)
            tail = log_path.read_text(errors="replace").strip()[-300:]
            raise DeskError(f"forwarding guest port {port} failed" + (f": {tail}" if tail else ""))
        time.sleep(0.2)
    run.log_event(forward=port, local=local)
    return local


def stop(run, port: int | None = None, *, pid: int | None = None) -> list[int]:
    """Stop matching tunnels, optionally only the exact supervisor that a caller owns."""
    with lock(run):
        return _stop(run, port, pid=pid)


def _stop(run, port: int | None, *, pid: int | None) -> list[int]:
    stopped, held = [], []
    for f in listing(run):
        if port is not None and f["port"] != port:
            continue
        if pid is not None and f["pid"] != pid:
            continue
        (_dir(run) / f"{f['port']}.json").unlink(missing_ok=True)
        if f["alive"]:
            try:
                # The tunnel process leads a session of its own, with its ssh.
                os.killpg(f["pid"], signal.SIGTERM)
                held.append(f["local"])
            except ProcessLookupError:
                pass  # gone meanwhile
        stopped.append(f["port"])
    # Until the local ports are free again, so that forwarding them anew gets the same numbers.
    deadline = time.monotonic() + 5
    while not all(_free(local) for local in held) and time.monotonic() < deadline:
        time.sleep(0.1)
    return stopped


def _target(run) -> str | None:
    """Where in the guest the tunnel leads: its loopback, or on OpenWrt the LAN address, where
    router services listen (ttyd on @lan; uhttpd listens everywhere). None while the guest does
    not answer (rebooting)."""
    from . import guest, runs

    try:
        if runs.spec_for(run).family != "openwrt":
            return "127.0.0.1"
        r = guest.ssh_run(run.ssh_port, run.user, "echo $SSH_CONNECTION", capture=True, timeout=15)
        return r.stdout.decode().split()[2]
    except (DeskError, subprocess.TimeoutExpired, IndexError):
        return None


def _tunnel(run_id: str, port: int, local: int) -> None:
    """Keep the tunnel up while the run exists: SSH drops with every reboot or snapshot restore."""
    from . import runs

    child = None

    def end(*_):
        if child is not None and child.poll() is None:
            child.terminate()
        sys.exit(0)

    signal.signal(signal.SIGTERM, end)
    mine = RUNS / run_id / "forward" / f"{port}.json"

    def wanted() -> bool:
        # Until the run is gone or desk no longer counts this process as the port's tunnel.
        try:
            return json.loads(mine.read_text()).get("pid") == os.getpid()
        except (OSError, ValueError):
            return False

    deadline = time.monotonic() + 10
    while not mine.exists() and time.monotonic() < deadline:
        time.sleep(0.1)  # desk writes the file right after starting this process
    while wanted():
        run = runs.load(run_id)
        target = _target(run)
        if target is None:
            time.sleep(2)
            continue
        child = subprocess.Popen(["ssh", "-i", str(SSH_KEY), "-o", "StrictHostKeyChecking=no",
                                  "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
                                  "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=15",
                                  "-o", "ServerAliveCountMax=3", "-o", "ExitOnForwardFailure=yes", "-N",
                                  "-L", f"127.0.0.1:{local}:{target}:{port}", "-p", str(run.ssh_port),
                                  f"{run.user}@127.0.0.1"], stdin=subprocess.DEVNULL)
        child.wait()
        time.sleep(2)


if __name__ == "__main__":
    _tunnel(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]))
