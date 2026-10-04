import unittest

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


if __name__ == "__main__":
    unittest.main()
