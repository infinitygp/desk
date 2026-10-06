import unittest
from datetime import datetime

from desk import clock
from desk.config import DeskError


class Parse(unittest.TestCase):
    def test_relative(self):
        self.assertEqual(clock.parse("+3d", 1000.0), 1000.0 + 3 * 86400)
        self.assertEqual(clock.parse("-2h30m", 100000.0), 100000.0 - 9000)
        self.assertEqual(clock.parse("+1w", 0.0), 7 * 86400)

    def test_absolute_is_guest_local_time(self):
        # 24 December: CET, UTC+1.
        self.assertEqual(clock.parse("2026-12-24 23:59", 0.0),
                         datetime.fromisoformat("2026-12-24T22:59:00+00:00").timestamp())
        self.assertEqual(clock.parse("2026-07-01T12:00:00+00:00", 0.0),
                         datetime.fromisoformat("2026-07-01T12:00:00+00:00").timestamp())

    def test_bad(self):
        for bad in ("3d", "+3x", "+", "tomorrow", ""):
            with self.subTest(bad=bad), self.assertRaises(DeskError):
                clock.parse(bad, 0.0)
