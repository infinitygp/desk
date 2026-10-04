"""In-band access to the guest: SSH for everyday work, qemu-guest-agent when there is no network."""

import base64
import json
import shlex
import subprocess
import time

from .config import SSH_KEY, DeskError
from .virt import agent

_SSH_OPTS = [
    "-i", str(SSH_KEY),
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    "-o", "ConnectTimeout=5",
    "-o", "BatchMode=yes",
]


def ssh_argv(port: int, user: str, tty: bool = False) -> list[str]:
    return ["ssh", *_SSH_OPTS, *(["-t"] if tty else []), "-p", str(port), f"{user}@127.0.0.1"]


def remote_command(argv: list[str], root: bool = False, gui: bool = False) -> str:
    # A single argument is a shell snippet (`a && b | c`); wrap it whole so that
    # sudo or the session scope applies to all of it, not just the first command.
    cmd = shlex.join(argv) if len(argv) > 1 else argv[0]
    if gui:
        # Starts the program inside the logged-in graphical session and returns at once.
        cmd = f"systemd-run --user --collect --quiet -- bash -c {shlex.quote(cmd)}"
    if root:
        cmd = f"sudo -n bash -c {shlex.quote(cmd)}"
    return cmd


def ssh_run(port: int, user: str, command: str, *, capture: bool = False, input: bytes | None = None,
            timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*ssh_argv(port, user), command], input=input, capture_output=capture, timeout=timeout
    )


def wait_ssh(port: int, user: str, timeout: float = 180) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if ssh_run(port, user, "true", capture=True, timeout=15).returncode == 0:
                return
        except subprocess.TimeoutExpired:
            pass
        time.sleep(2)
    raise DeskError(f"SSH on port {port} not reachable after {timeout:.0f}s")


def wait_ready(port: int, user: str, ready: str, timeout: float = 300) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ssh_run(port, user, ready, capture=True, timeout=30).returncode == 0:
            return
        time.sleep(2)
    raise DeskError(f"guest not ready after {timeout:.0f}s: {ready}")


def scp(port: int, src: str, dst: str) -> int:
    return subprocess.run(["scp", *_SSH_OPTS, "-r", "-P", str(port), src, dst]).returncode


def agent_exec(dom, argv: list[str], timeout: float = 120) -> tuple[int, bytes, bytes]:
    res = agent(dom, "guest-exec", path=argv[0], arg=argv[1:], **{"capture-output": True})
    pid = res["pid"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = agent(dom, "guest-exec-status", pid=pid)
        if st.get("exited"):
            out = base64.b64decode(st.get("out-data", ""))
            err = base64.b64decode(st.get("err-data", ""))
            return st.get("exitcode", -1), out, err
        time.sleep(0.2)
    raise DeskError(f"guest-exec {argv[0]} still running after {timeout:.0f}s")


def agent_sync_time(dom) -> None:
    """After restoring a saved state the guest clock lags by however long the image sat on disk."""
    agent(dom, "guest-set-time", time=time.time_ns())


def boot_id(port: int, user: str) -> str:
    r = ssh_run(port, user, "cat /proc/sys/kernel/random/boot_id", capture=True, timeout=15)
    if r.returncode != 0:
        raise DeskError("cannot read the guest boot id")
    return r.stdout.decode().strip()


def shutdown_inhibitors(port: int, user: str) -> list[str]:
    r = ssh_run(port, user, "busctl --json=short call org.freedesktop.login1 /org/freedesktop/login1 "
                "org.freedesktop.login1.Manager ListInhibitors", capture=True, timeout=15)
    if r.returncode != 0:
        return []
    # a(ssssuu): what, who, why, mode, uid, pid
    return [f"{who}: {why}" for what, who, why, mode, _uid, _pid in json.loads(r.stdout)["data"][0]
            if mode == "block" and "shutdown" in what.split(":")]


def reboot(port: int, user: str, ready: str, timeout: float = 300) -> tuple[float, list[str]]:
    """Reboot and return once a new boot is ready, plus whatever was blocking shutdown.

    Apps with unsaved work make gnome-session block shutdown; a user would see a dialog.
    The harness reboots anyway and reports the inhibitors instead.
    """
    inhibitors = shutdown_inhibitors(port, user)
    before = boot_id(port, user)
    t0 = time.monotonic()
    try:
        r = ssh_run(port, user, "sudo -n systemctl reboot --check-inhibitors=no", capture=True, timeout=30)
        if r.returncode not in (0, 255):  # 255: the connection dropped as the guest went down
            raise DeskError(f"reboot refused: {r.stderr.decode(errors='replace').strip()}")
    except subprocess.TimeoutExpired:
        pass  # the session hung while the guest went down; the boot id check below decides
    deadline = t0 + timeout
    while time.monotonic() < deadline:
        time.sleep(2)
        try:
            if boot_id(port, user) != before:
                break
        except (DeskError, subprocess.TimeoutExpired):
            continue
    else:
        raise DeskError(f"guest did not come back within {timeout:.0f}s")
    wait_ready(port, user, ready, timeout=max(deadline - time.monotonic(), 30))
    return time.monotonic() - t0, inhibitors


def wait_session_gone(port: int, user: str, ready: str, timeout: float = 60) -> None:
    """After restarting the display manager, wait until the old session has actually ended."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ssh_run(port, user, ready, capture=True, timeout=30).returncode != 0:
            return
        time.sleep(1)
    raise DeskError(f"old session still running {timeout:.0f}s after relogin")
