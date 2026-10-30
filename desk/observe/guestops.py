"""How observation reaches a Windows guest: the helper's operations (helper.ps1, pushed when the
guest's copy differs), copies to and from it, and its clock. `SshGuest` goes the way every desk
command does (SSH as the run's administrator, the time limit kept in the guest); `LocalGuest`
runs the same operations on the Windows machine it runs on, for checks on Windows CI without a VM.

Nothing here imports libvirt at import time: providers and this module also run on Windows."""

import base64
import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path, PureWindowsPath

from ..config import DeskError

ROOT = r"C:\desk\observe"
HELPER = Path(__file__).with_name("helper.ps1")
GUEST_HELPER = ROOT + r"\helper.ps1"
REPLY = "@@desk-observe@@"
MISSING = "@@desk-observe-helper-missing@@"
# Extra seconds a helper operation gets besides the tool it runs: PowerShell's start, WMI.
OVERHEAD = 60


class ObserveError(DeskError):
    """desk could not observe as asked (a tool missing or failing, a trace that is not whole): its
    failure, exit 2, never a finding about the product under test."""


def helper_sha256() -> str:
    return hashlib.sha256(HELPER.read_bytes()).hexdigest()


def win(*parts: str) -> str:
    """A guest path from parts, with backslashes."""
    return str(PureWindowsPath(*parts))


def scp_path(path: str) -> str:
    """A guest path as scp to Windows' OpenSSH takes it: C:/desk/..."""
    return path.replace("\\", "/")


def request_script(op: str, params: dict, digest: str | None = None, secret: bool = False) -> str:
    """The PowerShell that runs one helper operation, or says the helper must be pushed first. A
    secret comes on its standard input: this script is the guest process's command line, which
    every process listing and Process Monitor log shows."""
    payload = base64.b64encode(json.dumps({"op": op, **params}).encode()).decode()
    return ((f"$deskSecret = [Console]::In.ReadLine()\n" if secret else "")
            + f"$h = '{GUEST_HELPER}'\n"
            f"if (-not (Test-Path -LiteralPath $h) -or (Get-FileHash -LiteralPath $h -Algorithm SHA256).Hash "
            f"-ne '{digest or helper_sha256()}') {{ New-Item -ItemType Directory -Force -Path '{ROOT}' | Out-Null; "
            f"'{MISSING}'; exit 0 }}\n"
            f"& ([ScriptBlock]::Create([IO.File]::ReadAllText($h))) -Request '{payload}'"
            + (" -Secret $deskSecret" if secret else "") + "\n")


def parse_reply(op: str, rc: int, out: str, err: str):
    """The operation's result; ObserveError with what the guest said when there is none."""
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith(REPLY):
            try:
                reply = json.loads(line[len(REPLY):])
            except ValueError:
                raise ObserveError(f"the guest's answer to {op} is cut off ({len(line)} characters)") from None
            if not reply.get("ok"):
                raise ObserveError(f"in the guest: {reply.get('error') or 'failed without a reason'}")
            result = reply.get("result")
            if isinstance(result, dict):
                for k in ("now_ms", "start_ms", "op_ms"):
                    if reply.get(k) is not None:
                        result.setdefault(f"_{k}", reply[k])
            return result
    said = (err.strip() or out.strip())[-400:]
    raise ObserveError(f"the observation helper gave no answer to {op} (exit {rc})" + (f": {said}" if said else ""))


def items(result) -> list:
    return list((result or {}).get("items") or [])


class Guest:
    """One Windows guest as observation sees it. Subclasses run PowerShell and copy files."""

    root = ROOT

    def _powershell(self, script: str, limit: float, stdin: bytes | None = None) -> tuple[int, str, str]:
        raise NotImplementedError

    def push(self, local: Path, remote: str, timeout: float = 600) -> None:
        raise NotImplementedError

    def pull(self, remote: str, local: Path, timeout: float = 600) -> None:
        raise NotImplementedError

    def clock(self) -> dict:
        """A sample of host and guest time: where the guest's clock stands against the host's."""
        before = time.time()
        now = self.call("boot", limit=60).get("_now_ms")
        after = time.time()
        if now is None:
            raise ObserveError("the guest did not say its time")
        return {"host": (before + after) / 2, "guest": now / 1000, "uncertainty": (after - before) / 2,
                "via": "helper"}

    def call(self, op: str, limit: float = 60, secret: str | None = None, **params):
        stdin = (secret + "\n").encode() if secret is not None else None
        for attempt in (1, 2):
            rc, out, err = self._powershell(request_script(op, params, secret=stdin is not None), limit, stdin)
            if MISSING in out:
                if attempt == 2:
                    raise ObserveError("the observation helper does not reach the guest intact (copied, but its "
                                       "SHA-256 differs)")
                self.push(HELPER, GUEST_HELPER)
                continue
            return parse_reply(op, rc, out, err)

    # The operations, as the providers use them.

    def exec(self, exe: str, args: list[str], timeout: float, encoding: str | None = None,
             cwd: str | None = None) -> dict:
        """Run a program to its end within `timeout` seconds (ended with what it started after)."""
        return self.call("exec", limit=timeout + OVERHEAD, exe=exe, args=subprocess.list2cmdline(args),
                         timeout=timeout, encoding=encoding, cwd=cwd)

    def launch(self, exe: str, args: list[str], cwd: str | None = None,
               secret_env: tuple[str, str] | None = None) -> dict:
        """Start a program detached from the SSH session, with none of its handles (so neither SSH
        nor this call waits for it); its identity (pid, creation time). Its output is not kept.
        `secret_env` (NAME, VALUE) is added to its environment, never to a command line."""
        line = subprocess.list2cmdline(args)
        if secret_env:
            return self.call("launch", secret=secret_env[1], exe=exe, args=line or None, cwd=cwd,
                             secret_env=secret_env[0])
        return self.call("launch", exe=exe, args=line or None, cwd=cwd)

    def alive(self, procs: list[dict]) -> list[dict]:
        if not procs:
            return []
        return items(self.call("alive", procs=[{"pid": p["pid"], "created": p["created"]} for p in procs]))

    def stop_process(self, proc: dict, tree: bool = False) -> dict:
        return self.call("stopproc", pid=proc["pid"], created=proc["created"], tree=tree)

    def wait_gone(self, procs: list[dict], timeout: float) -> bool:
        if not procs:
            return True
        return bool(self.call("waitgone", limit=timeout + OVERHEAD, timeout=timeout,
                              procs=[{"pid": p["pid"], "created": p["created"]} for p in procs]).get("gone"))

    def processes(self) -> list[dict]:
        return items(self.call("procs"))

    def files(self, paths: list[str], hash: bool = False, limit: float = 60) -> dict:
        result = self.call("files", limit=limit, paths=paths, hash=hash)
        return {k: v for k, v in result.items() if not k.startswith("_")}

    def mkdir(self, *paths: str) -> None:
        self.call("mkdir", paths=list(paths))

    def remove(self, *paths: str) -> list[str]:
        return list(self.call("remove", paths=list(paths)).get("removed") or [])

    def probe(self, **what) -> dict:
        return self.call("probe", limit=120, **what)

    def boot(self) -> str:
        return str(self.call("boot").get("boot"))


class SshGuest(Guest):
    """A run's guest over SSH, as its administrator (`desk exec --root`)."""

    def __init__(self, run, spec):
        self.run, self.spec = run, spec

    def _powershell(self, script: str, limit: float, stdin: bytes | None = None) -> tuple[int, str, str]:
        from .. import guest

        try:
            r = guest.ssh_run(self.run.ssh_port, self.spec.root_user, guest.command(self.spec, [script], limit=limit),
                              capture=True, input=stdin, timeout=limit + 30)
        except subprocess.TimeoutExpired:
            raise ObserveError(f"the guest did not answer within {limit + 30:.0f}s") from None
        guest.connected(r, "observation")
        return r.returncode, r.stdout.decode(errors="replace"), r.stderr.decode(errors="replace")

    def _copy(self, src: str, dst: str, timeout: float, what: str) -> None:
        from .. import guest

        rc = guest.scp(self.run.ssh_port, src, dst, timeout=timeout)
        if rc == 124:
            raise ObserveError(f"{what}: still copying after {timeout:.0f}s, stopped")
        if rc != 0:
            raise ObserveError(f"{what}: scp exit {rc}")

    def push(self, local: Path, remote: str, timeout: float = 600) -> None:
        self._copy(str(local), f"{self.spec.root_user}@127.0.0.1:{scp_path(remote)}", timeout,
                   f"copying {Path(local).name} to the guest")

    def pull(self, remote: str, local: Path, timeout: float = 600) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        self._copy(f"{self.spec.root_user}@127.0.0.1:{scp_path(remote)}", str(local), timeout,
                   f"copying {PureWindowsPath(remote).name} from the guest")

    def clock(self) -> dict:
        # qemu-guest-agent answers in milliseconds where SSH and PowerShell take a second.
        from ..virt import agent

        try:
            dom = self.run.dom()
            before = time.time()
            ns = agent(dom, "guest-get-time")
            after = time.time()
            return {"host": (before + after) / 2, "guest": ns / 1e9, "uncertainty": (after - before) / 2,
                    "via": "qemu-guest-agent"}
        except DeskError:
            return super().clock()


class LocalGuest(Guest):
    """This Windows machine itself (Windows CI): the same helper operations, no SSH, no VM."""

    def __init__(self, spec):
        self.spec = spec

    def _powershell(self, script: str, limit: float, stdin: bytes | None = None) -> tuple[int, str, str]:
        try:
            r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", self.spec.cmd(script)],
                               capture_output=True, input=stdin, timeout=limit + 30)
        except subprocess.TimeoutExpired:
            raise ObserveError(f"PowerShell did not finish within {limit + 30:.0f}s") from None
        return r.returncode, r.stdout.decode(errors="replace"), r.stderr.decode(errors="replace")

    def push(self, local: Path, remote: str, timeout: float = 600) -> None:
        Path(remote).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(local, remote)

    def pull(self, remote: str, local: Path, timeout: float = 600) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(remote, local)
