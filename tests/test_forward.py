import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from desk import forward
from desk.config import DeskError


class Run:
    id = "t"

    def __init__(self, d):
        self.dir = Path(d)
        self.events = []

    def log_event(self, **event):
        self.events.append(event)


class Ports(unittest.TestCase):
    def test_free_and_listening(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen()
            port = s.getsockname()[1]
            self.assertFalse(forward._free(port))
            self.assertTrue(forward._listening(port))
        self.assertTrue(forward._free(port))
        self.assertFalse(forward._listening(port))


class Forwards(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.run_ = Run(tmp.name)
        (self.run_.dir / "forward").mkdir()

    def note(self, port: int, local: int, pid: int) -> None:
        (self.run_.dir / "forward" / f"{port}.json").write_text(json.dumps({"port": port, "local": local, "pid": pid}))

    def test_listing_tells_a_dead_tunnel_from_a_live_one(self):
        self.note(80, 8080, os.getpid())  # this process is alive, but no tunnel
        (self.run_.dir / "forward" / "broken.json").write_text("{")
        self.assertEqual(forward.listing(self.run_), [{"port": 80, "local": 8080, "pid": os.getpid(), "alive": False}])
        with mock.patch("desk.forward._alive", return_value=True):
            self.assertTrue(forward.listing(self.run_)[0]["alive"])

    def test_a_port_already_forwarded_keeps_its_local_port(self):
        self.note(80, 8080, 1)
        with mock.patch("desk.forward._alive", return_value=True):
            self.assertEqual(forward.start(self.run_, 80), 8080)

    def test_a_local_port_in_use_is_refused(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            with self.assertRaisesRegex(DeskError, "in use"):
                forward.start(self.run_, 80, s.getsockname()[1])

    def test_stop_forgets_dead_tunnels(self):
        self.note(80, 8080, 1)
        self.assertEqual(forward.stop(self.run_), [80])
        self.assertEqual(forward.listing(self.run_), [])


if __name__ == "__main__":
    unittest.main()
