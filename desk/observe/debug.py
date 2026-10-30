"""Debugger servers in the guest, reachable from the host's 127.0.0.1 only: desk starts the
server, waits until it listens, forwards its port like `desk forward` (an SSH tunnel to the
guest's loopback, bound to 127.0.0.1 on the host) and checks the forward answers. The debugger
itself runs on the host and is the user's; desk debugs nothing and ships no debugger.

- `dbgsrv`: the process server of Debugging Tools for Windows (WinDbg, cdb, kd connect with
  `-premote tcp:server=127.0.0.1,port=LOCAL`), started as `dbgsrv.exe -t tcp:port=PORT`. The
  tools come with the Windows SDK; desk looks where it installs them, or takes `exe`.
- `ida`: IDA's remote debugging server for Windows (win64_remote*.exe, shipped with IDA, under
  IDA's licence): the host's copy is set up with `desk observe setup ida FILE`, started as
  `SERVER -i127.0.0.1 -pPORT` with its password in its environment (IDA_DBGSRV_PASSWD), not on a
  command line every process listing shows. Without one given desk makes one, kept on the host
  in a file only its user reads, next to the session.

The guest's own network is user-mode (passt): nothing outside the host reaches a port in it
unless desk forwards it, and desk forwards to the host's 127.0.0.1 only."""

import os
import secrets
from pathlib import Path

from .base import Capability, Conflict, Missing, Option, Provider
from .guestops import items, win
from .session import Session

SDK_DEBUGGERS = r"C:\Program Files (x86)\Windows Kits\10\Debuggers\x64"
LISTEN_WAIT = 30


def _port(v: int) -> str | None:
    return None if 1024 <= v < 65536 else "a port from 1024 to 65535"


class _Server(Provider):
    kind = "endpoint"
    default_port = 0

    def resource_of(self, session: Session) -> str | None:
        return f"port:{session.options['port']}"

    def exe(self, session_or_opts) -> str:
        raise NotImplementedError

    def args(self, session: Session) -> list[str]:
        raise NotImplementedError

    def probe_request(self, opts: dict, host) -> dict:
        return {"files": [self.exe(opts)]}

    def assess(self, probe: dict, opts: dict, host) -> Capability:
        cap = super().assess(probe, opts, host)
        info = (probe.get("files") or {}).get(self.exe(opts)) or {}
        cap.version = info.get("version") or ""
        if not info.get("exists") and not self.setup_files:
            cap.problems.append(f"{self.exe(opts)} is not in the guest ({self.where})")
        elif not info.get("exists") and not host.setup(self.name):
            cap.problems.append(f"no {self.title} set up on this host (desk observe setup {self.name} FILE; "
                                f"{self.setup_from})")
        cap.available = not cap.problems
        return cap

    def secret_env(self, session: Session) -> tuple[str, str] | None:
        """(NAME, VALUE) for the server's environment."""
        return None

    def start(self, guest, session: Session, host) -> None:
        port = session.options["port"]
        taken = items(guest.call("listening", port=port))
        if taken:
            raise Conflict(f"guest port {port} is in use already (pid {taken[0]['pid']}); choose another with "
                           "--set port=N")
        if self.setup_files and host.setup(self.name):
            self.deploy(guest, session, host)
        exe = self.exe(session.options)
        secret = self.secret_env(session)
        proc = guest.launch(exe, self.args(session), secret_env=secret) if secret else guest.launch(exe, self.args(session))
        if proc.get("exited"):
            raise Missing(f"{exe} ended at once (try it by hand with desk exec to see why)")
        session.tool.update(exe=exe, port=port, procs=[{**proc, "name": exe.rsplit("\\", 1)[-1]}])
        import time

        deadline = time.monotonic() + LISTEN_WAIT
        while True:
            listening = items(guest.call("listening", port=port))
            if listening:
                break
            alive = guest.alive(session.tool["procs"])
            if not alive or not alive[0].get("alive"):
                raise Missing(f"{exe} ended before it listened on port {port} (try it by hand with desk exec to see why)")
            if time.monotonic() > deadline:
                raise Missing(f"{exe} runs but does not listen on port {port} after {LISTEN_WAIT}s")
            time.sleep(1)
        owners = {x["pid"] for x in listening}
        if proc["pid"] not in owners:
            raise Conflict(f"port {port} is listened on by pid {sorted(owners)}, not by the server desk started")
        session.tool["listening"] = sorted({x["address"] for x in listening})
        local = host.forward(port)
        session.tool.update(local_port=local, forwarded=True)
        if not host.reachable(local):
            raise Missing(f"the forward to guest port {port} (127.0.0.1:{local}) does not answer")
        session.stats["endpoint"] = f"127.0.0.1:{local}"

    def stop(self, guest, session: Session, host, force: bool = False) -> None:
        self.cleanup(guest, session, host)

    def cleanup(self, guest, session: Session, host=None) -> None:
        for p in session.tool.get("procs") or []:
            guest.stop_process(p, tree=True)
        if session.tool.get("forwarded") and host is not None:
            host.unforward(session.tool["port"])
            session.tool["forwarded"] = False
        if session.tool.get("password_file"):
            # The server is gone: its password opens nothing any more.
            Path(session.tool["password_file"]).unlink(missing_ok=True)

    def summary(self, session: Session) -> list[str]:
        if not session.tool.get("local_port"):
            return []
        return [f"{self.title} listened on guest port {session.tool['port']} "
                f"({', '.join(session.tool.get('listening') or [])}), forwarded to 127.0.0.1:{session.tool['local_port']}"]

    def connect_hint(self, session: Session) -> str:
        raise NotImplementedError


class DbgSrv(_Server):
    name = "dbgsrv"
    title = "the Debugging Tools for Windows process server (dbgsrv)"
    where = "install the Debugging Tools for Windows from the Windows SDK in the guest, or give --set exe=..."
    OPTIONS = {
        "port": Option(int, 5005, "guest port the server listens on", check=_port),
        "exe": Option(str, SDK_DEBUGGERS + r"\dbgsrv.exe", "where dbgsrv.exe is in the guest"),
    }
    notes = ("a process server: WinDbg, cdb or kd on the host attach to and start processes in the guest through "
             "it; desk only starts it and forwards its port to 127.0.0.1",)

    def exe(self, opts) -> str:
        return opts["exe"]

    def args(self, session: Session) -> list[str]:
        return ["-t", f"tcp:port={session.options['port']}"]

    def connect_hint(self, session: Session) -> str:
        return f"windbg -premote tcp:server=127.0.0.1,port={session.tool['local_port']}"


class IdaServer(_Server):
    name = "ida"
    title = "IDA's Windows remote debugging server"
    where = ""
    setup_files = ("win64_remote.exe", "win64_remote64.exe", "win32_remote.exe")
    setup_from = "the server from IDA's dbgsrv folder, under IDA's licence (desk does not ship it)"
    licence = ""
    OPTIONS = {
        "port": Option(int, 23946, "guest port the server listens on", check=_port),
        "password": Option(str, "", "password the server asks of IDA (default: one desk makes and shows at start)",
                           secret=True),
        "server": Option(str, "win64_remote.exe", "which of the files set up to start (IDA 8 and later ship "
                         "win64_remote.exe, IDA 7 win64_remote64.exe)",
                         choices=("win64_remote.exe", "win64_remote64.exe", "win32_remote.exe")),
    }
    notes = ("IDA on the host connects to the forwarded port as Remote Windows debugger; desk only starts the "
             "server, bound to the guest's loopback, and forwards its port to 127.0.0.1",)

    def exe(self, opts) -> str:
        return win(r"C:\desk\observe\tools\ida", opts["server"])

    def assess(self, probe: dict, opts: dict, host) -> Capability:
        cap = super().assess(probe, opts, host)
        setup = host.setup(self.name)
        have = [f["name"] for f in (setup or {}).get("files") or []]
        if setup and opts["server"] not in have:
            cap.problems.append(f"{opts['server']} is not among the files set up ({', '.join(have)}): "
                                f"--set server={have[0] if have else '...'}")
            cap.available = False
        return cap

    def args(self, session: Session) -> list[str]:
        return ["-i127.0.0.1", f"-p{session.options['port']}"]

    def secret_env(self, session: Session) -> tuple[str, str]:
        password = (getattr(session, "secrets", None) or {}).get("password") or secrets.token_urlsafe(18)
        f = Path(session.tool["host_dir"]).with_suffix(".secret")
        f.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as out:
            out.write(password + "\n")
        session.tool["password_file"] = str(f)
        return "IDA_DBGSRV_PASSWD", password

    def summary(self, session: Session) -> list[str]:
        return super().summary(session) + (["password protected: " + ("the one given" if session.options["password"]
                                                                       else "desk's own, shown at start")]
                                           if session.tool.get("password_file") else [])

    def connect_hint(self, session: Session) -> str:
        return (f"IDA: Debugger > Process options: host 127.0.0.1, port {session.tool['local_port']}, password in "
                f"{session.tool.get('password_file')}")
