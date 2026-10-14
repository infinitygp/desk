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


if __name__ == "__main__":
    unittest.main()
