import unittest
from unittest import mock

from desk import display
from desk.config import DeskError
from desk.display import _abs, _qcode, key_plan


class KeyPlan(unittest.TestCase):
    def test_ascii(self):
        self.assertEqual(key_plan("aB1 "), [["a"], ["shift", "b"], ["1"], ["spc"]])
        self.assertEqual(key_plan("!_:\n"), [["shift", "1"], ["shift", "minus"], ["shift", "semicolon"], ["ret"]])

    def test_polish_on_pl_layout(self):
        self.assertEqual(key_plan("ąŻ", layout="pl"), [["alt_r", "a"], ["alt_r", "shift", "z"]])
        self.assertEqual(key_plan("ź", layout="pl"), [["alt_r", "x"]])

    def test_polish_needs_pl_layout(self):
        with self.assertRaises(DeskError):
            key_plan("ą")

    def test_unsupported(self):
        with self.assertRaises(DeskError):
            key_plan("ü", layout="pl")

    def test_aliases(self):
        self.assertEqual(_qcode("Enter"), "ret")
        self.assertEqual(_qcode("super"), "meta_l")
        self.assertEqual(_qcode("f5"), "f5")


class AbsoluteScaling(unittest.TestCase):
    def test_edges(self):
        self.assertEqual(_abs(0, 1280), 0)
        self.assertEqual(_abs(1279, 1280), 0x7FFF)

    def test_out_of_range(self):
        with self.assertRaises(DeskError):
            _abs(1280, 1280)


class NothingStaysPressed(unittest.TestCase):
    """What was pressed is released, whatever stops the command in between."""

    def setUp(self):
        self.sent = []
        for target, value in (("desk.display.qmp", lambda dom, execute, **a: self.sent.append(a["events"])),
                              ("desk.display.screen_sizes", lambda dom: [(100, 100)])):
            patch = mock.patch(target, value)
            patch.start()
            self.addCleanup(patch.stop)

    def last(self) -> dict:
        return self.sent[-1][-1]["data"]

    def test_key_released_when_interrupted(self):
        with mock.patch("desk.display.time.sleep", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            display.chord(None, ["alt_r", "a"])
        self.assertEqual([e["data"]["down"] for e in self.sent[-1]], [False, False])
        self.assertEqual(self.sent[-1][-1]["data"]["key"]["data"], "alt_r")

    def test_button_released_when_a_drag_leaves_the_screen(self):
        with mock.patch("desk.display.time.sleep"), self.assertRaises(DeskError):
            display.drag(None, 10, 10, 10, 500)
        self.assertEqual(self.last(), {"down": False, "button": "left"})

    def test_button_released_when_a_click_is_interrupted(self):
        with mock.patch("desk.display.time.sleep", side_effect=[None, KeyboardInterrupt]), \
                self.assertRaises(KeyboardInterrupt):
            display.click(None, 10, 10)
        self.assertEqual(self.last(), {"down": False, "button": "left"})


if __name__ == "__main__":
    unittest.main()
