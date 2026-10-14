"""Windows guests answer through the in-session agent; what desk makes of a refusal there."""

import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from desk import a11y, forward, monitors
from desk.config import DeskError, load_image

RUN = SimpleNamespace(id="t", agent_port=1, ssh_port=1, scale=None, displays=1, image="windows-11")


class Focus(unittest.TestCase):
    def focus(self, *replies):
        with mock.patch("desk.a11y.winagent.request", side_effect=list(replies)) as request:
            try:
                return a11y.focus(RUN, "Notepad")
            finally:
                self.calls = request.call_count

    def test_in_front(self):
        self.assertEqual(self.focus({"title": "Notepad", "focused": True}), "Notepad")

    def test_refused_once(self):
        self.assertEqual(self.focus({"title": "Notepad", "focused": False}, {"title": "Notepad", "focused": True}),
                         "Notepad")
        self.assertEqual(self.calls, 2)

    def test_kept_behind(self):
        with self.assertRaisesRegex(DeskError, "kept 'Notepad' behind"):
            self.focus({"title": "Notepad", "focused": False}, {"title": "Notepad", "focused": False})


class SetText(unittest.TestCase):
    SPEC = {"pid": 1, "path": [0], "role": "document", "name": "Text editor"}

    def replace(self, answer: dict, text: str, role: str = "document") -> str:
        with mock.patch("desk.a11y._helper", return_value=answer):
            return a11y.replace_text(RUN, {**self.SPEC, "role": role}, text)

    def test_the_field_holds_the_text(self):
        self.assertEqual(self.replace({"done": True, "text": "line one\rline two"}, "line one\nline two"), "")

    def test_the_field_holds_something_else(self):
        self.assertEqual(self.replace({"done": True, "text": "12345"}, "1234567890"),
                         "the field shows '12345' afterwards")

    def test_a_field_that_does_not_tell(self):
        self.assertEqual(self.replace({"done": True, "text": None}, "secret"), "")
        self.assertEqual(self.replace({"done": True, "text": "••••"}, "secret", role="password text"), "")


class Scale(unittest.TestCase):
    def test_a_registry_that_refuses_costs_no_reboot(self):
        refused = subprocess.CompletedProcess([], 1, b"", b"Set-ItemProperty : Requested registry access is not allowed.")
        with mock.patch("desk.monitors.guest.ssh_run", return_value=refused), \
                mock.patch("desk.monitors.guest.reboot") as reboot, mock.patch("desk.winagent.request"), \
                self.assertRaisesRegex(DeskError, "did not take scale 150 %.*registry access"):
            monitors._windows(RUN, load_image("windows-11"), None, 150)
        reboot.assert_not_called()

    def test_a_scale_that_did_not_stick(self):
        done = subprocess.CompletedProcess([], 0, b"", b"")
        with mock.patch("desk.monitors.guest.ssh_run", return_value=done), mock.patch("desk.monitors.guest.reboot"), \
                mock.patch("desk.winagent.request", return_value={"stdout": "96\r\n", "code": 0}), \
                self.assertRaisesRegex(DeskError, "at 100 % instead of 150 %"):
            monitors._windows(RUN, load_image("windows-11"), None, 150)

    def test_a_scale_that_stuck(self):
        done = subprocess.CompletedProcess([], 0, b"", b"")
        with mock.patch("desk.monitors.guest.ssh_run", return_value=done), mock.patch("desk.monitors.guest.reboot"), \
                mock.patch("desk.winagent.request", return_value={"stdout": "144\r\n", "code": 0}):
            monitors._windows(RUN, load_image("windows-11"), None, 150)


class Forward(unittest.TestCase):
    def test_another_local_port_for_a_forwarded_one_is_refused(self):
        live = [{"port": 22, "local": 40001, "pid": 1, "alive": True}]
        with mock.patch("desk.forward.listing", return_value=live):
            self.assertEqual(forward.start(RUN, 22), 40001)
            self.assertEqual(forward.start(RUN, 22, 40001), 40001)
            with self.assertRaisesRegex(DeskError, "forwarded to 127.0.0.1:40001 already"):
                forward.start(RUN, 22, 40002)


if __name__ == "__main__":
    unittest.main()
