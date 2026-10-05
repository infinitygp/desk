import unittest
from unittest import mock

from desk import display


class Run:
    agent_port = 47011


class Pointer(unittest.TestCase):
    def pointer(self, sizes, run=None, points=()):
        with mock.patch.object(display, "screen_sizes", return_value=sizes):
            return display._Pointer(object(), run, points)

    def test_point_goes_to_the_console_of_its_monitor(self):
        p = self.pointer([(1280, 800), (1920, 1080)])
        with mock.patch.object(display, "qmp") as qmp:
            p.move(1280 + 960, 540)
        kwargs = qmp.call_args.kwargs
        self.assertEqual((kwargs["device"], kwargs["head"]), ("video1", 0))
        self.assertEqual([e["data"]["value"] for e in kwargs["events"]],
                         [display._abs(960, 1920), display._abs(540, 1080)])

    def test_single_monitor_events_name_no_console(self):
        p = self.pointer([(1280, 800)])
        with mock.patch.object(display, "qmp") as qmp:
            p.move(10, 10)
        self.assertNotIn("device", qmp.call_args.kwargs)

    def test_below_a_shorter_monitor_is_refused(self):
        p = self.pointer([(1920, 1080), (1280, 800)])
        with self.assertRaises(display.DeskError):
            p.move(1920 + 10, 900)

    def test_windows_secondary_monitor_uses_the_agent(self):
        self.assertIsNotNone(self.pointer([(1280, 800), (1280, 800)], Run(), [(1500, 300)]).agent)
        self.assertIsNone(self.pointer([(1280, 800), (1280, 800)], Run(), [(500, 300)]).agent)
        self.assertIsNone(self.pointer([(1280, 800)], Run(), [(500, 300)]).agent)


if __name__ == "__main__":
    unittest.main()
