"""A run's screen for another program (desk view --socket PATH): a background process serves one
monitor as plain VNC (RFB, no authentication) on a unix socket only this user may open, for as
long as the run exists. It speaks to QEMU itself, so the run's VNC password stays with libvirt:
monitor 1 through libvirt's VNC display (a connection libvirt hands over without asking for the
password), the others through the sockets QEMU serves them on (virt.prepare_domain_xml).

Without --control the process passes on only what a picture needs (pixel format, encodings,
update requests) and drops keys, pointer, clipboard, desktop resizes and power requests, so a
watcher cannot get in the agent's way; a message it cannot size ends that watcher's connection.
With --control on a Windows guest's monitor 2 and on the pointer goes through the in-session
agent, as desk click's does there: QEMU would move the absolute tablet, which Windows maps onto
the primary monitor, so a click would land on monitor 1.
Every watcher connects to QEMU as a shared client, so none disconnects another."""

import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

from .config import PROJECT, RUNS, DeskError

HANDSHAKE = 10  # seconds a client and QEMU each get for the RFB handshake
UNIX_PATH_MAX = 107  # sun_path holds 108 bytes, the last a NUL


def _dir(run) -> Path:
    return run.dir / "view"


def _alive(pid: int) -> bool:
    try:
        return b"desk.view" in Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False


def _answers(path: Path) -> bool:
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(1)
        return s.connect_ex(str(path)) == 0


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
    from .runs import _flock

    return _flock(run.dir / ".view.lock", f"another command changing views of run {run.id}", 60)


def check_monitor(run, monitor: int) -> None:
    """Refuse a monitor the run does not have or whose picture QEMU does not serve."""
    if not 1 <= monitor <= run.displays:
        raise DeskError(
            f"run {run.id} has {run.displays} monitor{'s' if run.displays > 1 else ''}; "
            f"--monitor is 1 to {run.displays}"
        )
    if str(monitor) in getattr(run, "disconnected", {}):
        raise DeskError(f"monitor {monitor} is disconnected")
    if monitor > 1 and not (
        run.vnc_dir and (Path(run.vnc_dir) / f"{monitor}.sock").is_socket()
    ):
        raise DeskError(
            f"run {run.id} has no picture of monitor {monitor}: QEMU serves monitors 2 and on only for "
            "runs started by a desk that has them (desk up said why not, if it could not; desk up again)"
        )


def tablet_reaches(run, monitor: int) -> bool:
    """Whether QEMU's absolute tablet moves the pointer on this monitor; on Windows the agent does."""
    from . import runs

    return monitor == 1 or runs.spec_for(run).family != "windows"


def start(run, path: str, monitor: int = 1, control: bool = False) -> tuple[int, int]:
    """Serve the monitor on PATH; the size of its picture, seen through PATH."""
    from . import runs

    sock = Path(path).absolute()
    if len(str(sock).encode()) > UNIX_PATH_MAX:
        raise DeskError(
            f"{sock} is longer than a unix socket path may be ({UNIX_PATH_MAX} bytes); take a shorter one"
        )
    with lock(run):
        check_monitor(runs.load(run.id), monitor)
        for r in runs.all_runs():
            for v in listing(r):
                if v["socket"] != str(sock):
                    continue
                if not v["alive"]:
                    (_dir(r) / f"{v['pid']}.json").unlink(missing_ok=True)  # replaced by this one
                    continue
                if r.id == run.id and (v["monitor"], v["control"]) == (monitor, control):
                    return probe(sock)
                raise DeskError(f"{sock} shows monitor {v['monitor']} of run {r.id} already "
                                f"(desk -r {r.id} view --stop {sock} first)")
        if sock.is_socket():
            if _answers(sock):
                raise DeskError(f"{sock} is the socket of another program; take another path")
            sock.unlink()  # left by a process that is gone
        elif sock.exists() or sock.is_symlink():
            raise DeskError(f"{sock} exists and is not a socket; take another path")
        if not sock.parent.is_dir():
            raise DeskError(f"{sock.parent} is not a directory")
        d = _dir(run)
        d.mkdir(exist_ok=True)
        log_path = d / f"{sock.name}.log"
        with open(log_path, "a") as log:
            p = subprocess.Popen([sys.executable, "-m", "desk.view", run.id, str(monitor), "1" if control else "0",
                                  str(sock)], cwd=PROJECT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 start_new_session=True)
        (d / f"{p.pid}.json").write_text(json.dumps({"socket": str(sock), "monitor": monitor, "control": control,
                                                     "pid": p.pid}))
        deadline = time.monotonic() + 15
        while not (sock.is_socket() and _answers(sock)):
            if p.poll() is not None or time.monotonic() > deadline:
                _stop(run, None, pid=p.pid)
                tail = log_path.read_text(errors="replace").strip()[-300:]
                raise DeskError(f"serving monitor {monitor} of run {run.id} on {sock} failed" +
                                (f": {tail}" if tail else ""))
            time.sleep(0.1)
        try:
            # A look through the socket itself: QEMU answers on it with the picture's size.
            size = probe(sock)
        except DeskError:
            _stop(run, None, pid=p.pid)
            raise
    run.log_event(view=str(sock), monitor=monitor, control=control)
    return size


def stop(run, path: str | None = None) -> list[str]:
    with lock(run):
        return _stop(run, str(Path(path).absolute()) if path else None)


def _stop(run, path: str | None, *, pid: int | None = None) -> list[str]:
    stopped, held = [], []
    for v in listing(run):
        if (path is not None and v["socket"] != path) or (pid is not None and v["pid"] != pid):
            continue
        (_dir(run) / f"{v['pid']}.json").unlink(missing_ok=True)
        if v["alive"]:
            try:
                os.killpg(v["pid"], signal.SIGTERM)
                held.append(v["pid"])
            except ProcessLookupError:
                pass
        stopped.append(v["socket"])
    deadline = time.monotonic() + 5
    while any(_alive(p) for p in held) and time.monotonic() < deadline:
        time.sleep(0.05)
    for s in map(Path, stopped):
        # The process removes its socket as it ends; one that was killed outright leaves it.
        if s.is_socket() and not _answers(s):
            s.unlink(missing_ok=True)
    return stopped


# The RFB protocol (RFC 6143) and QEMU's additions to it.

def _exact(s: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed")
        buf += chunk
    return buf


def _skip(s: socket.socket, n: int) -> None:
    while n:
        n -= len(_exact(s, min(n, 65536)))


def _version(data: bytes) -> int:
    """The minor version a client speaks: 3, 7 or 8."""
    if len(data) != 12 or not data.startswith(b"RFB ") or data[7:8] != b"." or not data[4:11].replace(b".", b"").isdigit():
        raise ConnectionError(f"not an RFB client: {data!r}")
    major, minor = int(data[4:7]), int(data[8:11])
    return 8 if (major, minor) >= (3, 8) else 7 if (major, minor) == (3, 7) else 3


def _refuse(client: socket.socket, minor: int, reason: str) -> None:
    text = reason.encode()
    client.sendall((b"\0\0\0\0" if minor == 3 else b"\0") + struct.pack(">I", len(text)) + text)


def _greet(client: socket.socket, minor: int) -> None:
    """Security type None; then the client's ClientInit, whose shared flag QEMU never sees."""
    if minor == 3:
        client.sendall(struct.pack(">I", 1))
    else:
        client.sendall(b"\x01\x01")
        if _exact(client, 1) != b"\x01":
            raise ConnectionError("the client chose a security type it was not offered")
        if minor == 8:
            client.sendall(struct.pack(">I", 0))
    _exact(client, 1)


def handshake(s: socket.socket) -> bytes:
    """Log in to an RFB server that asks for no password, as a shared client; its ServerInit."""
    if not _exact(s, 12).startswith(b"RFB 003."):
        raise DeskError("the display does not speak RFB 3.x")
    s.sendall(b"RFB 003.008\n")
    types = _exact(s, _exact(s, 1)[0])
    if not types:
        raise DeskError("the display refused: " + _exact(s, struct.unpack(">I", _exact(s, 4))[0]).decode(errors="replace"))
    if 1 not in types:
        raise DeskError(f"the display asks for authentication (security types {list(types)})")
    s.sendall(b"\x01")
    if struct.unpack(">I", _exact(s, 4))[0] != 0:
        raise DeskError("the display refused the connection")
    s.sendall(b"\x01")
    head = _exact(s, 24)
    return head + _exact(s, struct.unpack(">I", head[20:24])[0])


def probe(path: Path) -> tuple[int, int]:
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(HANDSHAKE * 2)
        try:
            s.connect(str(path))
            init = handshake(s)
        except (OSError, ConnectionError) as e:
            raise DeskError(f"no picture on {path}: {e}") from None
    return struct.unpack(">HH", init[:4])


def _upstream(run_id: str, monitor: int) -> socket.socket:
    from . import runs

    run = runs.load(run_id)
    check_monitor(run, monitor)
    if monitor == 1:
        import libvirt

        try:
            fd = run.dom().openGraphicsFD(0, libvirt.VIR_DOMAIN_OPEN_GRAPHICS_SKIPAUTH)
        except libvirt.libvirtError as e:
            raise DeskError(f"run {run_id} has no VNC display: {e}") from None
        return socket.socket(fileno=fd)
    s = socket.socket(socket.AF_UNIX)
    try:
        s.connect(str(Path(run.vnc_dir) / f"{monitor}.sock"))
    except OSError as e:
        s.close()
        raise DeskError(f"monitor {monitor} of run {run_id} does not answer: {e}") from None
    return s


# Client messages of a fixed size: type -> bytes after the type byte.
_FIXED = {0: 19, 3: 9, 150: 9,  # SetPixelFormat, FramebufferUpdateRequest, EnableContinuousUpdates
          4: 7, 5: 5, 250: 3}  # KeyEvent, PointerEvent, xvp (power)
SHOWN = frozenset({0, 2, 3, 150})  # and SetEncodings (2)
ALL_BUT_POINTER = frozenset({0, 2, 3, 150, 4, 6, 250, 251, 255})


def _message(client: socket.socket) -> tuple[int, bytes]:
    """The next client message, whole; a type it cannot size ends the connection."""
    t = _exact(client, 1)[0]
    if t in _FIXED:
        return t, bytes([t]) + _exact(client, _FIXED[t])
    if t == 2:  # SetEncodings
        head = _exact(client, 3)
        return t, b"\x02" + head + _exact(client, 4 * struct.unpack(">H", head[1:])[0])
    if t == 6:  # ClientCutText; a negative length is the extended clipboard's
        head = _exact(client, 7)
        return t, b"\x06" + head + _exact(client, abs(struct.unpack(">i", head[3:])[0]))
    if t == 251:  # SetDesktopSize: it would resize the guest's monitor
        head = _exact(client, 7)
        return t, b"\xfb" + head + _exact(client, 16 * head[5])
    if t == 255:  # QEMU: extended key event, audio
        sub = _exact(client, 1)
        if sub == b"\x00":
            return t, b"\xff" + sub + _exact(client, 10)
        if sub == b"\x01":
            op = _exact(client, 2)
            if op in (b"\0\0", b"\0\x01"):
                return t, b"\xff" + sub + op
            if op == b"\0\x02":
                return t, b"\xff" + sub + op + _exact(client, 6)
        raise ConnectionError(f"QEMU message {sub[0]} unknown to the filter")
    raise ConnectionError(f"client message type {t} unknown to the filter")


def _filter(client: socket.socket, up: socket.socket, passed: frozenset = SHOWN, pointer=None) -> None:
    """Pass on the messages of the types given, drop the others. With a `pointer` (AgentPointer),
    pointer events go to it, and the rest of the input after them, in the order it came."""
    while True:
        t, msg = _message(client)
        if pointer is None:
            if t in passed:
                up.sendall(msg)
        elif t == 5:
            pointer.event(msg[1], *struct.unpack(">HH", msg[2:]))
        elif t in SHOWN:
            pointer.send(msg)
        elif t in passed:
            pointer.relay(msg)


class AgentPointer:
    """A viewer's pointer on a Windows guest's monitor 2 and on, through the in-session agent
    (SetCursorPos, mouse_event) in screen coordinates, as desk click moves it there. The agent
    serves one request at a time and a viewer sends motion by the hundred, so one thread sends
    them in order and only the newest position between two button changes, no closer together
    than the viewer sent them: Windows drops a right click held for 50 ms, as a backlog would send
    it. A press or release waits 100 ms after what came before it, as desk click's do.

    Keys and the rest of the viewer's input wait in the same line, so a key typed after a click
    reaches the window the click made active. A viewer that leaves, and a view that stops, let
    go of the buttons it held, or the agent's next move would drag."""

    BUTTONS = ((1, "left"), (2, "middle"), (4, "right"))
    WHEEL = ((8, 1), (16, -1))  # RFB's wheel up and down; the agent's delta is in notches
    BUTTON_GAP = 0.1

    def __init__(self, run, left: int, up: socket.socket):
        self.run, self.left, self.up = run, left, up
        self.up_lock = threading.Lock()  # two threads write to QEMU: the viewer's and this one's
        self.mask = 0
        self.pressed: set[str] = set()  # buttons the agent was told to press and not to let go
        self.queue: list[tuple[float, dict]] = []  # when the viewer sent it, what the agent gets
        self.ended = False
        self.cond = threading.Condition()
        self.worker = threading.Thread(target=self._work, daemon=True)
        _pointers.add(self)
        self.worker.start()

    def send(self, msg: bytes) -> None:
        with self.up_lock:
            self.up.sendall(msg)

    def relay(self, msg: bytes) -> None:
        """Input for QEMU, sent once what the viewer did before it has been done."""
        with self.cond:
            if not self.ended:
                self.queue.append((time.monotonic(), {"action": "relay", "msg": msg}))
                self.cond.notify()

    def event(self, mask: int, x: int, y: int) -> None:
        now = time.monotonic()
        with self.cond:
            if self.ended:
                return
            move = (now, {"action": "move", "x": self.left + x, "y": y})
            if self.queue and self.queue[-1][1]["action"] == "move":
                self.queue[-1] = move
            else:
                self.queue.append(move)
            for bit, name in self.BUTTONS:
                if (mask ^ self.mask) & bit:
                    self.queue.append((now, {"action": "down" if mask & bit else "up", "button": name}))
            for bit, delta in self.WHEEL:
                if mask & bit and not self.mask & bit:
                    self.queue.append((now, {"action": "wheel", "delta": delta}))
            self.mask = mask
            self.cond.notify()

    def end(self) -> None:
        """The viewer left: what it did is done, then the buttons it held are let go."""
        with self.cond:
            if not self.ended:
                for bit, name in self.BUTTONS:
                    if self.mask & bit:
                        self.queue.append((time.monotonic(), {"action": "up", "button": name}))
            self.mask = 0
            self.ended = True
            self.cond.notify()

    def let_go(self) -> None:
        """The view stops: what is still waiting is dropped, only the buttons are let go."""
        with self.cond:
            held = self.pressed | {name for bit, name in self.BUTTONS if self.mask & bit}
            self.queue = [(0.0, {"action": "up", "button": name}) for name in sorted(held)]
            self.mask = 0
            self.ended = True
            self.cond.notify()

    def _work(self) -> None:
        from . import winagent

        sent = came = 0.0  # when the last request went, and when its event had come
        while True:
            with self.cond:
                while not self.queue and not self.ended:
                    self.cond.wait()
                if not self.queue:
                    _pointers.discard(self)
                    return
                t, req = self.queue.pop(0)
            gap = max(t - came, self.BUTTON_GAP if req["action"] in ("down", "up") else 0)
            time.sleep(max(0.0, sent + gap - time.monotonic()))
            sent, came = time.monotonic(), t
            if req["action"] == "relay":
                try:
                    self.send(req["msg"])
                except OSError:
                    pass  # QEMU's connection is gone; the viewer's ends with it
                continue
            try:
                winagent.request(self.run, timeout=5, cmd="pointer", **req)
            except DeskError as e:
                print(f"[desk view] pointer {req['action']} through the agent: {e}", file=sys.stderr, flush=True)
                continue
            with self.cond:
                if req["action"] == "down":
                    self.pressed.add(req["button"])
                elif req["action"] == "up":
                    self.pressed.discard(req["button"])


_pointers: set[AgentPointer] = set()
LET_GO = 3  # seconds a stopping view gives the agent to let go of buttons; desk waits 5 for it


def _let_go(limit: float = LET_GO) -> None:
    """Let go of every button a viewer holds through the agent, within the limit."""
    pointers = list(_pointers)
    for p in pointers:
        p.let_go()
    deadline = time.monotonic() + limit
    for p in pointers:
        p.worker.join(max(0.0, deadline - time.monotonic()))


def _left_of(run, monitor: int) -> int:
    """Where the monitor starts in screen coordinates: the monitors sit side by side from the left."""
    from .display import screen_sizes

    before = sum(str(i) not in run.disconnected for i in range(1, monitor))
    return sum(w for w, _ in screen_sizes(run.dom())[:before])


def _pump(src: socket.socket, dst: socket.socket) -> None:
    while data := src.recv(65536):
        dst.sendall(data)


def _close(*socks: socket.socket) -> None:
    for s in socks:
        try:
            s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        s.close()


def _serve_client(client: socket.socket, run_id: str, monitor: int, control: bool, tablet: bool = True) -> None:
    up = agent = None
    try:
        client.settimeout(HANDSHAKE)
        client.sendall(b"RFB 003.008\n")
        minor = _version(_exact(client, 12))
        try:
            up = _upstream(run_id, monitor)
            up.settimeout(HANDSHAKE)
            init = handshake(up)
        except (DeskError, OSError, ConnectionError) as e:
            # The client shows why, rather than a connection that closed without a word.
            _refuse(client, minor, f"desk: monitor {monitor} of run {run_id}: {e}")
            raise
        _greet(client, minor)
        client.sendall(init)
        client.settimeout(None)
        up.settimeout(None)

        def down():
            try:
                _pump(up, client)
            except OSError:
                pass
            _close(client, up)

        threading.Thread(target=down, daemon=True).start()
        if control and tablet:
            _pump(client, up)
        elif control:
            from . import runs

            run = runs.load(run_id)
            agent = AgentPointer(run, _left_of(run, monitor), up)
            _filter(client, up, ALL_BUT_POINTER, agent)
        else:
            _filter(client, up, SHOWN)
    except (DeskError, OSError, ConnectionError) as e:
        print(f"[desk view] client of monitor {monitor}: {e}", file=sys.stderr, flush=True)
    finally:
        if agent:
            # What the viewer typed before it left still reaches QEMU, after its clicks.
            agent.end()
            agent.worker.join(LET_GO)
        _close(client, *([up] if up else []))


def _serve(run_id: str, monitor: int, control: bool, path: str) -> None:
    from . import runs

    mine = RUNS / run_id / "view" / f"{os.getpid()}.json"
    tablet = tablet_reaches(runs.load(run_id), monitor)
    os.umask(0o177)  # the socket: this user only
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(path)
    ino = os.stat(path).st_ino

    def end(*_):
        _finish(path, ino)

    signal.signal(signal.SIGTERM, end)
    srv.listen(16)
    srv.settimeout(1)

    def wanted() -> bool:
        # Until the run is gone or desk no longer counts this process as the socket's view.
        try:
            return json.loads(mine.read_text()).get("pid") == os.getpid()
        except (OSError, ValueError):
            return False

    deadline = time.monotonic() + 10
    while not mine.exists() and time.monotonic() < deadline:
        time.sleep(0.1)  # desk writes the file right after starting this process
    while wanted():
        try:
            client, _ = srv.accept()
        except TimeoutError:
            continue
        threading.Thread(target=_serve_client, args=(client, run_id, monitor, control, tablet),
                         daemon=True).start()
    end()


def _finish(path: str, ino: int) -> None:
    """End the view. Buttons held through the agent are let go first, as the guest stays; then it
    exits with every connection open, so each watcher sees its connection close."""
    _let_go()
    try:
        if os.stat(path).st_ino == ino:
            os.unlink(path)
    except OSError:
        pass
    os._exit(0)


if __name__ == "__main__":
    _serve(sys.argv[1], int(sys.argv[2]), sys.argv[3] == "1", sys.argv[4])
