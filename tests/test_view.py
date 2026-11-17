import io
import os
import shutil
import socket
import struct
import tempfile
import threading
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock

from desk import runs, view
from desk.config import DeskError

PIXEL_FORMAT = bytes([32, 24, 0, 1, 0, 255, 0, 255, 0, 255, 16, 8, 0, 0, 0, 0])
SERVER_INIT = struct.pack(">HH", 800, 600) + PIXEL_FORMAT + struct.pack(">I", 4) + b"QEMU"

SET_PIXEL_FORMAT = b"\x00\0\0\0" + PIXEL_FORMAT
SET_ENCODINGS = b"\x02\0" + struct.pack(">Hii", 2, 0, -258)
UPDATE_REQUEST = b"\x03\x01" + struct.pack(">HHHH", 0, 0, 800, 600)
CONTINUOUS = b"\x96\x01" + struct.pack(">HHHH", 0, 0, 800, 600)
KEY = b"\x04\x01\0\0" + struct.pack(">I", 0x61)
POINTER = b"\x05\x01" + struct.pack(">HH", 10, 20)
CUT_TEXT = b"\x06\0\0\0" + struct.pack(">I", 5) + b"hello"
EXTENDED_CUT_TEXT = b"\x06\0\0\0" + struct.pack(">i", -4) + b"\0\0\0\x01"
QEMU_KEY = b"\xff\x00" + struct.pack(">HII", 1, 0x61, 0x1e)
QEMU_AUDIO = b"\xff\x01\x00\x02" + b"\x03\x02" + struct.pack(">I", 44100)
DESKTOP_SIZE = b"\xfb\0" + struct.pack(">HHBB", 1024, 768, 1, 0) + bytes(16)
XVP = b"\xfa\0\x01\x04"


class FakeQemu:
    """An RFB server that asks for no password, like QEMU's VNC for desk; keeps what clients send."""

    def __init__(self):
        self.shared: list[int] = []
        self.received: list[bytes] = []
        self.done = threading.Event()

    def serve(self, s: socket.socket, frame: bytes = b"") -> None:
        try:
            s.sendall(b"RFB 003.008\n")
            view._exact(s, 12)
            s.sendall(b"\x01\x01")
            view._exact(s, 1)
            s.sendall(b"\0\0\0\0")
            self.shared.append(view._exact(s, 1)[0])
            s.sendall(SERVER_INIT + frame)
            got = b""
            while data := s.recv(4096):
                got += data
            self.received.append(got)
        except (OSError, ConnectionError):
            pass
        finally:
            s.close()
            self.done.set()


def client_hello(s: socket.socket, version: bytes = b"RFB 003.008\n", shared: int = 1) -> bytes:
    """A client's side of the handshake with the view; the ServerInit it gets."""
    assert view._exact(s, 12) == b"RFB 003.008\n"
    s.sendall(version)
    if version == b"RFB 003.003\n":
        assert struct.unpack(">I", view._exact(s, 4))[0] == 1
    else:
        assert view._exact(s, 2) == b"\x01\x01"
        s.sendall(b"\x01")
        if version == b"RFB 003.008\n":
            assert view._exact(s, 4) == b"\0\0\0\0"
    s.sendall(bytes([shared]))
    return view._exact(s, len(SERVER_INIT))


def recv_or_reset(s: socket.socket) -> bytes:
    try:
        return s.recv(1)
    except ConnectionResetError:  # closed with input of the client's unread
        return b""


class Proxy(unittest.TestCase):
    def setUp(self):
        quiet = mock.patch("sys.stderr", io.StringIO())  # what the view logs of each client
        quiet.start()
        self.addCleanup(quiet.stop)

    def connect(self, control: bool = False, frame: bytes = b"", tablet: bool = True, monitor: int = 1):
        qemu = FakeQemu()
        up_view, up_qemu = socket.socketpair()
        threading.Thread(target=qemu.serve, args=(up_qemu, frame), daemon=True).start()
        client, served = socket.socketpair()
        client.settimeout(5)
        # The client's thread asks for the upstream after this returns.
        patcher = mock.patch("desk.view._upstream", return_value=up_view)
        patcher.start()
        self.addCleanup(patcher.stop)
        t = threading.Thread(target=view._serve_client, args=(served, "r", monitor, control, tablet),
                             daemon=True)
        t.start()
        self.addCleanup(client.close)
        return qemu, client, t

    def test_view_only_passes_the_picture_and_drops_input(self):
        qemu, client, t = self.connect(frame=b"\x00\0\0\x00")
        self.assertEqual(client_hello(client, shared=0), SERVER_INIT)
        self.assertEqual(view._exact(client, 4), b"\x00\0\0\x00")  # what QEMU sends goes through
        client.sendall(SET_PIXEL_FORMAT + KEY + SET_ENCODINGS + POINTER + CUT_TEXT + EXTENDED_CUT_TEXT + QEMU_KEY
                       + QEMU_AUDIO + DESKTOP_SIZE + XVP + UPDATE_REQUEST + CONTINUOUS)
        client.shutdown(socket.SHUT_WR)
        t.join(5)
        qemu.done.wait(5)
        self.assertEqual(qemu.received, [SET_PIXEL_FORMAT + SET_ENCODINGS + UPDATE_REQUEST + CONTINUOUS])
        # A client that asks for the display alone does not disconnect the others.
        self.assertEqual(qemu.shared, [1])

    def test_control_passes_input(self):
        qemu, client, t = self.connect(control=True)
        client_hello(client)
        client.sendall(KEY + POINTER + UPDATE_REQUEST)
        client.shutdown(socket.SHUT_WR)
        t.join(5)
        qemu.done.wait(5)
        self.assertEqual(qemu.received, [KEY + POINTER + UPDATE_REQUEST])

    def test_control_of_a_monitor_the_tablet_does_not_reach_moves_the_pointer_through_the_agent(self):
        calls = []
        with mock.patch("desk.runs.load"), mock.patch("desk.view._left_of", return_value=1280), \
                mock.patch("desk.winagent.request", side_effect=lambda run, **req: calls.append(req)):
            qemu, client, t = self.connect(control=True, tablet=False, monitor=2)
            client_hello(client)
            client.sendall(KEY + POINTER + CUT_TEXT + QEMU_KEY + UPDATE_REQUEST)
            client.shutdown(socket.SHUT_WR)
            t.join(5)
            qemu.done.wait(5)
            deadline = time.monotonic() + 5
            while len(calls) < 3 and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertEqual(qemu.received, [KEY + CUT_TEXT + QEMU_KEY + UPDATE_REQUEST])
        # POINTER presses the left button at (10, 20) of monitor 2; the viewer left with it held.
        self.assertEqual(calls, [{"timeout": 5, "cmd": "pointer", "action": "move", "x": 1290, "y": 20},
                                 {"timeout": 5, "cmd": "pointer", "action": "down", "button": "left"},
                                 {"timeout": 5, "cmd": "pointer", "action": "up", "button": "left"}])

    def test_the_tablet_reaches_every_monitor_but_a_windows_guests_second(self):
        windows = runs.Run(id="w", image="windows-11", domain="d", ssh_port=1, user="u", created=0, displays=2)
        linux = runs.Run(id="l", image="fedora-44-gnome", domain="d", ssh_port=1, user="u", created=0, displays=2)
        self.assertEqual([view.tablet_reaches(r, m) for r in (windows, linux) for m in (1, 2)],
                         [True, False, True, True])

    def test_a_message_the_filter_cannot_size_ends_the_connection(self):
        qemu, client, t = self.connect()
        client_hello(client)
        client.sendall(b"\x07" + KEY)
        t.join(5)
        self.assertFalse(t.is_alive())
        qemu.done.wait(5)
        self.assertEqual(qemu.received, [b""])
        self.assertEqual(recv_or_reset(client), b"")

    def test_older_clients(self):
        for version in (b"RFB 003.003\n", b"RFB 003.007\n"):
            qemu, client, t = self.connect()
            self.assertEqual(client_hello(client, version), SERVER_INIT)

    def test_a_display_that_is_not_there_is_refused_with_the_reason(self):
        client, served = socket.socketpair()
        self.addCleanup(client.close)
        t = threading.Thread(target=view._serve_client, args=(served, "r", 1, False), daemon=True)
        with mock.patch("desk.view._upstream", side_effect=DeskError("run r has no VNC display")):
            t.start()
            client.settimeout(5)
            with self.assertRaisesRegex(DeskError, "refused: desk: monitor 1 of run r: run r has no VNC display"):
                view.handshake(client)
            t.join(5)

    def test_a_display_that_wants_a_password_is_not_logged_in_to(self):
        a, b = socket.socketpair()
        b.sendall(b"RFB 003.008\n\x01\x02")
        a.settimeout(5)
        with self.assertRaisesRegex(DeskError, "authentication"):
            view.handshake(a)
        a.close(), b.close()


class AgentPointerOrder(unittest.TestCase):
    def test_moves_between_button_changes_are_merged_and_held_buttons_let_go(self):
        calls, gate = [], threading.Event()

        def request(run, timeout, **req):
            gate.wait(5)  # the first move keeps the agent busy while the rest comes in
            calls.append((req["action"], req.get("x", req.get("button", req.get("delta"))), req.get("y")))

        with mock.patch("desk.winagent.request", side_effect=request):
            p = view.AgentPointer(mock.Mock(), 1280)
            p.event(0, 10, 20)
            deadline = time.monotonic() + 5
            while p.queue and time.monotonic() < deadline:
                time.sleep(0.01)
            for mask, x, y in ((0, 30, 40), (0, 35, 45), (1, 35, 45), (0, 35, 45), (8, 35, 45), (0, 35, 45),
                               (4, 50, 60)):
                p.event(mask, x, y)
            p.end()
            gate.set()
            p.worker.join(5)
        self.assertEqual(calls, [("move", 1290, 20), ("move", 1315, 45), ("down", "left", None),
                                 ("move", 1315, 45), ("up", "left", None), ("move", 1315, 45), ("wheel", 1, None),
                                 ("move", 1330, 60), ("down", "right", None), ("up", "right", None)])

    def test_the_agent_gets_a_click_held_as_long_as_the_viewer_held_it(self):
        sent = []
        with mock.patch("desk.winagent.request", side_effect=lambda run, timeout, **req: sent.append(
                (req["action"], time.monotonic()))):
            p = view.AgentPointer(mock.Mock(), 0)
            p.event(4, 5, 5)  # moved and pressed at once
            time.sleep(0.3)
            p.event(0, 5, 5)
            p.event(4, 5, 5)  # and a second click with no time between
            p.event(0, 5, 5)
            p.end()
            p.worker.join(5)
        actions = [a for a, _ in sent]
        self.assertEqual(actions, ["move", "down", "move", "up", "move", "down", "move", "up"])
        at = dict(enumerate(t for _, t in sent))
        self.assertGreaterEqual(at[1] - at[0], 0.1)  # a press 100 ms after the move
        self.assertGreaterEqual(at[3] - at[1], 0.29)  # held as long as the viewer held it
        self.assertGreaterEqual(at[7] - at[5], 0.1)  # held long enough for Windows to take it

    def test_an_agent_that_fails_costs_the_event_not_the_viewer(self):
        from desk.winagent import AgentGone

        with mock.patch("desk.winagent.request", side_effect=AgentGone("gone")), \
                mock.patch("sys.stderr", io.StringIO()) as err:
            p = view.AgentPointer(mock.Mock(), 0)
            p.event(1, 1, 1)
            p.end()
            p.worker.join(5)
        self.assertFalse(p.worker.is_alive())
        self.assertIn("pointer move through the agent: gone", err.getvalue())


class MonitorSockets(unittest.TestCase):
    """Where QEMU serves monitors 2 and on: the session's runtime directory, or libvirt's."""

    def env(self, **env):
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("XDG_RUNTIME_DIR", "XDG_CACHE_HOME"):
            if name not in env:
                os.environ.pop(name, None)

    def test_with_a_runtime_directory(self):
        tmp = short_dir(self)
        self.env(XDG_RUNTIME_DIR=str(tmp))
        d = runs.monitor_sockets("r1")
        self.assertEqual(d, str(tmp / "desk" / "r1"))
        self.assertEqual(Path(d).stat().st_mode & 0o777, 0o700)
        self.assertEqual((tmp / "desk").stat().st_mode & 0o777, 0o700)

    def test_without_one_libvirts_cache_directory(self):
        tmp = short_dir(self)
        (tmp / "libvirt" / "qemu").mkdir(parents=True)
        self.env(XDG_CACHE_HOME=str(tmp))
        self.assertEqual(runs.monitor_sockets("r1"), str(tmp / "libvirt" / "qemu" / "desk" / "r1"))

    def test_libvirts_directories_are_not_made_by_desk(self):
        tmp = short_dir(self)
        self.env(XDG_CACHE_HOME=str(tmp))
        with mock.patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(runs.monitor_sockets("r1"), "")
        self.assertIn("monitors 2 and on of run r1 cannot be watched", err.getvalue())
        self.assertFalse((tmp / "libvirt").exists())

    def test_a_path_too_long_for_a_socket(self):
        tmp = short_dir(self)
        long = tmp / ("x" * 100)
        long.mkdir()
        self.env(XDG_RUNTIME_DIR=str(long))
        with mock.patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(runs.monitor_sockets("r1"), "")
        self.assertIn("too long for a unix socket path", err.getvalue())


def short_dir(test) -> Path:
    # Unix socket paths are short; the test home under $TMPDIR may not be.
    d = tempfile.TemporaryDirectory(prefix="dv", dir="/tmp")
    test.addCleanup(d.cleanup)
    return Path(d.name)


class Process(unittest.TestCase):
    """desk view --socket with the real background process, against a fake monitor 2."""

    def setUp(self):
        # The view process outlives its Popen object, as it does in desk.
        warnings.simplefilter("ignore", ResourceWarning)
        self.tmp = short_dir(self)
        self.qemu = FakeQemu()
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(self.tmp / "2.sock"))
        listener.listen()
        self.addCleanup(listener.close)

        def accept():
            while True:
                try:
                    s, _ = listener.accept()
                except OSError:
                    return
                threading.Thread(target=self.qemu.serve, args=(s,), daemon=True).start()

        threading.Thread(target=accept, daemon=True).start()
        self.run_ = runs.Run(id="view1", image="fedora-44-gnome", domain="d", ssh_port=1, user="u", created=0,
                             displays=2, vnc_dir=str(self.tmp))
        self.run_.dir.mkdir(parents=True)
        self.run_.save()
        self.addCleanup(shutil.rmtree, self.run_.dir, ignore_errors=True)
        self.addCleanup(lambda: self.run_.dir.exists() and view.stop(self.run_))

    def test_serves_until_stopped_and_disconnects_its_watchers(self):
        path = self.tmp / "screen.sock"
        self.assertEqual(view.start(self.run_, str(path), monitor=2), (800, 600))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        # The same view again is the one there is; another one on its socket is refused.
        self.assertEqual(view.start(self.run_, str(path), monitor=2), (800, 600))
        self.assertEqual(len(view.listing(self.run_)), 1)
        with self.assertRaisesRegex(DeskError, "shows monitor 2 of run view1 already"):
            view.start(self.run_, str(path), monitor=2, control=True)
        watchers = []
        for _ in range(2):
            c = socket.socket(socket.AF_UNIX)
            c.settimeout(5)
            c.connect(str(path))
            self.addCleanup(c.close)
            self.assertEqual(client_hello(c, shared=0), SERVER_INIT)
            watchers.append(c)
        # One that was killed outright is replaced by the next on its socket.
        os.kill(view.listing(self.run_)[0]["pid"], 9)
        time.sleep(0.3)
        self.assertEqual(view.start(self.run_, str(path), monitor=2), (800, 600))
        self.assertEqual([v["alive"] for v in view.listing(self.run_)], [True])
        self.assertEqual(view.stop(self.run_, str(path)), [str(path)])
        self.assertFalse(path.exists())
        for c in watchers:
            self.assertEqual(recv_or_reset(c), b"")  # closed, not hanging
        self.assertEqual(view.listing(self.run_), [])

    def test_refusals(self):
        with self.assertRaisesRegex(DeskError, "--monitor is 1 to 2"):
            view.start(self.run_, str(self.tmp / "s"), monitor=3)
        with self.assertRaisesRegex(DeskError, "longer than a unix socket path"):
            view.start(self.run_, "/tmp/" + "x" * 120)
        (self.tmp / "file").write_text("")
        with self.assertRaisesRegex(DeskError, "not a socket"):
            view.start(self.run_, str(self.tmp / "file"), monitor=2)
        with self.assertRaisesRegex(DeskError, "another program"):
            view.start(self.run_, str(self.tmp / "2.sock"), monitor=2)
        os.unlink(self.tmp / "2.sock")
        with self.assertRaisesRegex(DeskError, "no picture of monitor 2"):
            view.start(self.run_, str(self.tmp / "s"), monitor=2)

    def test_a_run_that_goes_away_takes_its_view_along(self):
        path = self.tmp / "screen.sock"
        view.start(self.run_, str(path), monitor=2)
        pid = view.listing(self.run_)[0]["pid"]
        shutil.rmtree(self.run_.dir)
        deadline = time.monotonic() + 5
        while view._alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(view._alive(pid))
        self.assertFalse(path.exists())
