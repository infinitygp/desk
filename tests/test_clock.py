import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

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


class GuestSince(unittest.TestCase):
    def since(self, offset: float, events: list[dict]) -> float:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "log.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
            return clock.guest_since(SimpleNamespace(dir=Path(tmp), since=10000.0, clock_offset=offset))

    def test_a_guest_set_back_is_asked_from_earlier(self):
        # Set back two hours, then reset: what it logged meanwhile is stamped before the start.
        self.assertEqual(self.since(0.0, [{"clock": "-2h", "offset": -7200}, {"clock": "reset"}]), 2800.0)
        self.assertEqual(self.since(-60.0, []), 9940.0)

    def test_a_guest_set_forward_is_asked_from_the_start(self):
        self.assertEqual(self.since(86400.0, [{"clock": "+1d", "offset": 86400}]), 10000.0)


class Duration(unittest.TestCase):
    def test_steps(self):
        self.assertEqual(clock.duration("1d"), 86400)
        self.assertEqual(clock.duration("1h30m"), 5400)
        for bad in ("+1d", "1x", "", "d"):
            with self.subTest(bad=bad), self.assertRaises(DeskError):
                clock.duration(bad)


class Describe(unittest.TestCase):
    def test_whole_days_stay_whole(self):
        import time

        # The guest's clock is read a moment before the host's.
        shown = clock.describe(None, now=time.time() + 31 * 86400 - 0.05)
        self.assertIn("host +31d 00:00:00", shown)
