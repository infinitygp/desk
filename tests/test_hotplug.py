import os
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path

from desk import hotplug, hubs
from desk.config import DeskError
from tests.usbvfiod_fake import FakeHotplug


class Client(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.node = self.dir / "node"
        self.node.write_bytes(b"device-node")

    def serve(self, handler):
        server = FakeHotplug(self.dir, handler)
        self.addCleanup(server.close)
        return server

    def test_a_hub_is_a_framed_command_with_a_compact_json_payload(self):
        server = self.serve(lambda *a: "SuccessfulOperation")
        hotplug.attach_hub(server.path, hubs.parse_hub("5=067b:2586/4/hs"))
        body = b'{"path":"5","spec":{"vid":1659,"pid":9606,"ports":4,"speed":"high"}}'
        self.assertEqual(server.raw, [b"\x11" + struct.pack("<I", len(body)) + body])

    def test_the_tree_comes_back_as_the_controller_lists_it(self):
        tree = [{"path": "5", "speed": "high", "id": "virtual 1", "hub_ports": 4},
                {"path": "5.1", "speed": "full", "id": "external hid"}]
        server = self.serve(lambda *a: {"response": "SuccessfulOperation", "tree": tree})
        self.assertEqual(hotplug.tree(server.path), tree)
        self.assertEqual(server.seen[0][:2], (0x14, {}))

    def test_an_empty_tree_is_an_empty_list(self):
        server = self.serve(lambda *a: "SuccessfulOperation")
        self.assertEqual(hotplug.tree(server.path), [])

    def test_a_host_device_goes_with_its_file_descriptor_in_the_same_message(self):
        server = self.serve(lambda *a: "SuccessfulOperation")
        fd = os.open(self.node, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        hotplug.attach_host(server.path, 5, 12, fd, at="5.1.4")
        self.assertEqual(server.seen, [(0x10, {"path": "5.1.4", "bus": 5, "device": 12}, [b"device-node"])])

    def test_without_a_path_the_legacy_command_takes_the_first_free_root_port(self):
        server = self.serve(lambda *a: "SuccessfulOperation")
        fd = os.open(self.node, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        hotplug.attach_host(server.path, 5, 12, fd)
        self.assertEqual(server.seen, [(0x00, (5, 12), [b"device-node"])])
        self.assertEqual(server.raw, [bytes([0, 5, 12])])

    def test_a_refusal_names_the_response_and_gives_the_controllers_reason(self):
        server = self.serve(lambda *a: {"response": "PathOccupied", "message": "a device is attached at 5.1"})
        with self.assertRaisesRegex(DeskError, r"usbvfiod refused to detach 5\.1: PathOccupied "
                                               r"\(a device is attached at 5\.1\)") as caught:
            hotplug.detach_at(server.path, "5.1")
        self.assertEqual(caught.exception.response, "PathOccupied")

    def test_a_refusal_without_a_message_gets_the_explanation_of_desk(self):
        server = self.serve(lambda *a: "SpeedNotSupported")
        with self.assertRaisesRegex(DeskError, r"SpeedNotSupported.*high speed hubs on its way \(--hub PORTPATH=/hs\)"):
            hotplug.attach_hub(server.path, hubs.Hub((5, 1)))

    def test_the_legacy_reply_byte_is_the_index_of_the_response(self):
        for name, text in (("NoFreePort", "no free root port"), ("NoSuchDevice", "NoSuchDevice")):
            (self.dir / name).mkdir()
            server = FakeHotplug(self.dir / name, lambda *a, n=name: n)
            self.addCleanup(server.close)
            fd = os.open(self.node, os.O_RDONLY)
            self.addCleanup(os.close, fd)
            with self.subTest(name), self.assertRaisesRegex(DeskError, text):
                hotplug.attach_host(server.path, 5, 12, fd)

    def test_numbers_beyond_a_byte_are_refused_before_anything_is_sent(self):
        server = self.serve(lambda *a: "SuccessfulOperation")
        for bus, dev in ((0, 1), (1, 0), (256, 1), (1, 300)):
            with self.subTest((bus, dev)), self.assertRaisesRegex(DeskError, "below 256"):
                hotplug.attach_host(server.path, bus, dev, 0, at="5")
        self.assertEqual(server.seen, [])

    def test_a_missing_socket_says_the_server_is_gone(self):
        with self.assertRaisesRegex(DeskError, "nobody listens on .*usbvfiod of the run has ended"):
            hotplug.tree(self.dir / "none")

    def test_a_socket_nobody_serves_any_more_is_refused_the_same_way(self):
        path = self.dir / "dead"
        with socket.socket(socket.AF_UNIX) as s:
            s.bind(str(path))  # bound, never listening: connections are refused
        with self.assertRaisesRegex(DeskError, "nobody listens"):
            hotplug.tree(path)

    def test_a_server_that_hangs_up_without_a_reply_is_named(self):
        server = self.serve(lambda *a: None)
        with self.assertRaisesRegex(DeskError, "closed .* without a reply"):
            hotplug.tree(server.path)

    def test_a_server_that_does_not_answer_ends_at_the_limit_not_never(self):
        server = self.serve(lambda *a: "hang")
        t0 = time.monotonic()
        with self.assertRaisesRegex(DeskError, "did not answer .* within 0 s"):
            hotplug.tree(server.path, timeout=0.3)
        self.assertLess(time.monotonic() - t0, 3)

    def test_a_reply_that_trickles_in_does_not_get_more_time_for_it(self):
        path = self.dir / "slow"
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(path))
        listener.listen(1)
        self.addCleanup(listener.close)

        def drip():
            conn, _ = listener.accept()
            with conn:
                conn.recv(100)
                try:
                    for byte in struct.pack("<I", 1000):
                        conn.sendall(bytes([byte]))
                        time.sleep(0.2)
                except BrokenPipeError:
                    pass  # the client gave up, as it should

        threading.Thread(target=drip, daemon=True).start()
        t0 = time.monotonic()
        with self.assertRaisesRegex(DeskError, "did not answer"):
            hotplug.tree(path, timeout=0.5)
        self.assertLess(time.monotonic() - t0, 3)

    def test_a_reply_that_announces_a_megabyte_is_no_reply(self):
        path = self.dir / "big"
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(path))
        listener.listen(1)
        self.addCleanup(listener.close)

        def answer():
            conn, _ = listener.accept()
            with conn:
                conn.recv(100)
                conn.sendall(struct.pack("<I", 1 << 24))

        threading.Thread(target=answer, daemon=True).start()
        with self.assertRaisesRegex(DeskError, "announces 16777216 bytes"):
            hotplug.tree(path)

    def test_a_reply_that_is_no_json_is_an_error_not_a_crash(self):
        path = self.dir / "junk"
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(path))
        listener.listen(1)
        self.addCleanup(listener.close)

        def answer():
            conn, _ = listener.accept()
            with conn:
                conn.recv(100)
                conn.sendall(struct.pack("<I", 4) + b"nope")

        threading.Thread(target=answer, daemon=True).start()
        with self.assertRaisesRegex(DeskError, "talking to usbvfiod .* failed"):
            hotplug.tree(path)

    def test_a_path_too_long_for_a_unix_socket_is_an_error_of_desk(self):
        with self.assertRaisesRegex(DeskError, "talking to usbvfiod"):
            hotplug.tree("/" + "x" * 200)


if __name__ == "__main__":
    unittest.main()
