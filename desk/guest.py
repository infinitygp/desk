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
    # passt keeps the TCP connection up even when the guest hangs; this notices within 2 min.
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=4",
    "-o", "BatchMode=yes",
]


def ssh_argv(port: int, user: str, tty: bool = False) -> list[str]:
    return ["ssh", *_SSH_OPTS, *(["-t"] if tty else []), "-p", str(port), f"{user}@127.0.0.1"]


def remote_command(argv: list[str], gui: bool = False, wait: bool = False,
                   env: dict[str, str] | None = None, limit: float | None = None, shell: str = "bash") -> str:
    # A single argument is a shell snippet (`a && b | c`); wrap it whole so that the
    # session scope applies to all of it, not just the first command.
    cmd = shlex.join(argv) if len(argv) > 1 else argv[0]
    setenv = "".join(f" -E {shlex.quote(f'{k}={v}')}" for k, v in (env or {}).items())
    if env and not gui:
        cmd = "env " + " ".join(shlex.quote(f"{k}={v}") for k, v in env.items()) + f" {shell} -c " + shlex.quote(cmd)
    if limit and not gui and shell != "bash":
        # BusyBox without `timeout` (OpenWrt): the command in a session of its own, which a
        # watchdog (in another one, holding no output open) stops whole; stdin kept on fd 3
        # first, as a background job reads /dev/null.
        watchdog = 'sleep "$1"; kill -TERM -"$2"; sleep 5; kill -KILL -"$2"'
        cmd = (f"exec 3<&0; setsid sh -c {shlex.quote(cmd)} <&3 3<&- & p=$!; exec 3<&-; "
               f"setsid sh -c {shlex.quote(watchdog)} _ {max(1, round(limit))} $p </dev/null >/dev/null 2>&1 & w=$!; "
               "wait $p; rc=$?; kill -TERM -$w $w 2>/dev/null; exit $rc")
    elif limit and not gui:
        # Stopped in the guest too: closing SSH would leave a hung command running there. The
        # command reaches its shell through the environment, not as an argument: `pgrep -f X`
        # would otherwise find `timeout ... bash -c 'pgrep -f X'` and count it.
        cmd = (f"DESK_CMD={shlex.quote(cmd)} timeout -k 5 {limit:.0f} bash -c "
               + shlex.quote('c=$DESK_CMD; unset DESK_CMD; eval "$c"'))
    if limit and gui:
        setenv += f" -p RuntimeMaxSec={limit:.0f}"
    if gui:
        # Starts the program inside the logged-in graphical session and returns at once. The
        # language comes from the shell's own environment: the systemd user manager can outlive
        # a relogin and still carry the locale it started with.
        cmd = ("shell=$(pgrep -u \"$(id -u)\" -x 'gnome-shell|plasmashell' | head -n1); "
               "lang=$( [ -n \"$shell\" ] && tr '\\0' '\\n' < /proc/$shell/environ "
               "| grep -E '^(LANG|LANGUAGE|LC_[A-Z]+)=' | sed 's/^/-E /'); "
               f"systemd-run --user --collect --quiet{' --wait --pipe' if wait else ''} $lang{setenv} "
               f"-- bash -c {shlex.quote(cmd)}")
    return cmd


def command(spec, argv: list[str], env: dict[str, str] | None = None, limit: float | None = None) -> str:
    """`argv` (one element: a shell snippet) as it is sent over SSH to this kind of guest, stopped
    in the guest after `limit` seconds: closing the SSH connection would leave it running there."""
    if spec.family != "windows":
        return remote_command(argv, env=env, limit=limit, shell=spec.shell)
    script = "".join(f"$env:{k} = '{v.replace(chr(39), chr(39) * 2)}'; " for k, v in (env or {}).items())
    script += remote_command(argv)
    if limit:
        # Processes outlive a closed SSH session on Windows, so a watchdog in the guest ends the
        # command and everything it started.
        script = (f"$deskWatch = Start-Process powershell.exe -WindowStyle Hidden -PassThru -ArgumentList "
                  f"'-NoProfile', '-Command', \"Start-Sleep {limit:.0f}; taskkill.exe /T /F /PID $PID\"; "
                  f"try {{ {script}\n}} finally {{ Stop-Process -Id $deskWatch.Id -Force -ErrorAction Ignore }}")
    return spec.cmd(script)


def ssh_run(port: int, user: str, command: str, *, capture: bool = False, input: bytes | None = None,
            timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*ssh_argv(port, user), command], input=input, capture_output=capture, timeout=timeout
    )


def connected(r: subprocess.CompletedProcess, what: str) -> subprocess.CompletedProcess:
    """ssh exits 255 when it did not reach the guest or lost it: then there is no answer, which
    callers must not read as an empty one (no failed units, no console errors)."""
    if r.returncode == 255:
        why = (r.stderr or b"").decode(errors="replace").strip()[-300:]
        raise DeskError(f"{what}: no SSH connection to the guest" + (f" ({why})" if why else ""))
    return r


def _why(r: subprocess.CompletedProcess | None) -> str:
    """What the last attempt of a wait said: a refused key, a refused connection and a guest that
    does not answer are different problems."""
    if r is None:
        return "the guest accepted the connection but did not answer"
    said = (r.stderr or b"").decode(errors="replace").strip().splitlines()
    return said[-1][-200:] if said else f"exit {r.returncode}"


def wait_ssh(port: int, user: str, timeout: float = 180) -> None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            # `exit 0` means the same in POSIX shells and in PowerShell (Windows guests).
            last = ssh_run(port, user, "exit 0", capture=True, timeout=15)
            if last.returncode == 0:
                return
        except subprocess.TimeoutExpired:
            last = None
        time.sleep(2)
    raise DeskError(f"SSH on port {port} not reachable after {timeout:.0f}s: {_why(last)}")


def wait_ready(port: int, user: str, ready: str, timeout: float = 300) -> None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        # While a guest reboots, passt still accepts the connection and SSH then hangs waiting for
        # the banner; a timeout there just means "not yet".
        try:
            last = ssh_run(port, user, ready, capture=True, timeout=20)
            if last.returncode == 0:
                return
        except subprocess.TimeoutExpired:
            last = None
        time.sleep(2)
    # 255 is ssh's own failure; anything else is the check answering "not yet".
    why = _why(last) if last is None or last.returncode == 255 else f"the check still exits {last.returncode}"
    raise DeskError(f"guest not ready after {timeout:.0f}s ({why}): {ready[:200]}")


# Long enough for a disk image over the loopback; a copy that hangs must not hang its caller.
COPY_TIMEOUT = 600.0


def scp(port: int, src: str, dst: str, timeout: float | None = COPY_TIMEOUT) -> int:
    """scp's exit code; 124 when it was stopped after `timeout` seconds (None: no limit)."""
    try:
        return subprocess.run(["scp", *_SSH_OPTS, "-r", "-P", str(port), src, dst], timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        return 124


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


# After a restore libvirt now and then takes a moment to see the guest agent's channel as
# connected again (measured: one restore in ten, well under a second).
AGENT_WAIT = 20.0


def agent_set_time(dom, offset: float = 0.0, wait: float = AGENT_WAIT) -> None:
    """The host's time plus `offset` seconds into the guest. After restoring a saved state its
    clock lags by however long the image sat on disk. Tried again for `wait` seconds while the
    agent does not answer."""
    deadline = time.monotonic() + wait
    while True:
        try:
            agent(dom, "guest-set-time", time=time.time_ns() + int(offset * 1e9))
            return
        except DeskError as e:
            # Not a guest that refuses the time, or a domain that is gone: those stay as they are.
            away = any(sign in str(e) for sign in ("not responding", "not connected", "not available"))
            if not away or time.monotonic() >= deadline:
                raise
            time.sleep(0.5)


def boot_id(port: int, user: str, command: str = "cat /proc/sys/kernel/random/boot_id") -> str:
    r = ssh_run(port, user, command, capture=True, timeout=15)
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


def reboot(port: int, spec, timeout: float = 300, command: str | None = None) -> tuple[float, list[str]]:
    """Reboot and return once a new boot is ready, plus whatever was blocking shutdown.

    Apps with unsaved work make gnome-session block shutdown; a user would see a dialog.
    The harness reboots anyway and reports the inhibitors instead. `command` reboots another
    way (`dnf offline reboot -y` into a system upgrade); boots without SSH on the way are waited out.
    """
    user, ready = spec.user, spec.cmd(spec.ready)
    inhibitors = shutdown_inhibitors(port, user) if spec.family == "linux" else []
    before = boot_id(port, user, spec.cmd(spec.boot_id))
    t0 = time.monotonic()
    try:
        # No stdin: a command that asks first (dnf offline reboot without -y) fails at once
        # instead of waiting for an answer while the boot id check below waits for a reboot.
        r = ssh_run(port, spec.root_user, spec.cmd(command or spec.reboot), capture=True, timeout=30, input=b"")
        if r.returncode not in (0, 255):  # 255: the connection dropped as the guest went down
            raise DeskError(f"reboot refused: {r.stderr.decode(errors='replace').strip()}")
    except subprocess.TimeoutExpired:
        pass  # the session hung while the guest went down; the boot id check below decides
    deadline = t0 + timeout
    while time.monotonic() < deadline:
        time.sleep(2)
        try:
            if boot_id(port, user, spec.cmd(spec.boot_id)) != before:
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
        try:
            if ssh_run(port, user, ready, capture=True, timeout=20).returncode != 0:
                return
        except subprocess.TimeoutExpired:
            return  # SSH no longer answering: the session (and machine) is going down
        time.sleep(1)
    raise DeskError(f"old session still running {timeout:.0f}s after relogin")
