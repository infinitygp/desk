import unittest

from desk.cli import _text_args


class TextArgs(unittest.TestCase):
    def test_text_starting_with_a_dash_is_text(self):
        self.assertEqual(_text_args(["type", "---"]), ["type", "--", "---"])
        self.assertEqual(_text_args(["-r", "abc", "type", "--shot", "-v"]), ["-r", "abc", "type", "--shot", "--", "-v"])
        self.assertEqual(_text_args(["type", "--settle", "2", "-x"]), ["type", "--settle", "2", "--", "-x"])
        self.assertEqual(_text_args(["set-text", "@e3", "-5"]), ["set-text", "@e3", "--", "-5"])

    def test_everything_else_untouched(self):
        for argv in (["type", "--", "-a"], ["type", "abc"], ["click", "--name", "-x"], ["type", "--anywhere", "-"],
                     ["type", "-"]):
            with self.subTest(argv=argv):
                self.assertEqual(_text_args(argv), argv)
