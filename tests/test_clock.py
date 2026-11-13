import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

from desk import clock
from desk.config import DeskError


WARSAW = ZoneInfo("Europe/Warsaw")


class Parse(unittest.TestCase):
    def test_relative(self):
        self.assertEqual(clock.parse("+3d", 1000.0, WARSAW), 1000.0 + 3 * 86400)
        self.assertEqual(clock.parse("-2h30m", 100000.0, WARSAW), 100000.0 - 9000)
        self.assertEqual(clock.parse("+1w", 0.0, WARSAW), 7 * 86400)

    def test_absolute_is_guest_local_time(self):
        # 24 December: CET, UTC+1; in New York EST, UTC-5.
        self.assertEqual(clock.parse("2026-12-24 23:59", 0.0, WARSAW),
                         datetime.fromisoformat("2026-12-24T22:59:00+00:00").timestamp())
        self.assertEqual(clock.parse("2026-12-24 23:59", 0.0, ZoneInfo("America/New_York")),
                         datetime.fromisoformat("2026-12-25T04:59:00+00:00").timestamp())
        self.assertEqual(clock.parse("2026-07-01T12:00:00+00:00", 0.0, WARSAW),
                         datetime.fromisoformat("2026-07-01T12:00:00+00:00").timestamp())

    def test_bad(self):
        for bad in ("3d", "+3x", "+", "tomorrow", ""):
            with self.subTest(bad=bad), self.assertRaises(DeskError):
                clock.parse(bad, 0.0, WARSAW)

    def test_the_zone_is_the_runs(self):
        from unittest import mock

        from desk import runs

        run = runs.Run(id="r", image="fedora-44-gnome", domain="d", ssh_port=1, user="tester", created=0.0,
                       timezone="Asia/Kolkata", layout="pl")
        self.assertEqual(clock.zone(run), ZoneInfo("Asia/Kolkata"))
        # A run from before desk kept them: the image's.
        legacy = runs.Run(id="r", image="fedora-44-gnome", domain="d", ssh_port=1, user="tester", created=0.0)
        with mock.patch("desk.config.TIMEZONE", "America/New_York"), mock.patch("desk.config.LAYOUT", "us"):
            self.assertEqual(clock.zone(legacy), ZoneInfo("America/New_York"))
            self.assertEqual(runs.spec_for(run).layout, "pl")
            self.assertEqual(runs.spec_for(legacy).layout, "us")


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


class Drift(unittest.TestCase):
    def drift(self, guest_ahead: float, offset: float = 0.0) -> float:
        import time

        run = SimpleNamespace(dom=lambda: None, clock_offset=offset)
        with mock.patch("desk.guest.agent", lambda dom, execute: int((time.time() + offset + guest_ahead) * 1e9)):
            return clock.drift(run)

    def test_a_guest_behind_or_ahead(self):
        self.assertAlmostEqual(self.drift(-30), -30, delta=1)
        self.assertAlmostEqual(self.drift(30), 30, delta=1)

    def test_the_runs_offset_is_not_a_drift(self):
        self.assertAlmostEqual(self.drift(0, offset=86400), 0, delta=1)
        self.assertAlmostEqual(self.drift(-30, offset=-7200), -30, delta=1)

    def test_a_guest_that_does_not_answer_is_an_error(self):
        run = SimpleNamespace(dom=lambda: None, clock_offset=0.0)
        with mock.patch("desk.guest.agent", side_effect=DeskError("not responding")), \
                self.assertRaisesRegex(DeskError, "not responding"):
            clock.drift(run)


class DriftWarning(unittest.TestCase):
    def test_under_two_seconds_is_the_hosts_time(self):
        for seconds in (0, 1.99, -1.99):
            self.assertIsNone(clock.drift_warning(seconds))

    def test_two_seconds_and_more_is_said(self):
        self.assertIn("2 s ahead of the host's", clock.drift_warning(2))
        self.assertIn("2 s behind the host's", clock.drift_warning(-2.0))
        self.assertIn("31 s behind the host's", clock.drift_warning(-30.6))
        self.assertIn("are off by that much", clock.drift_warning(-30.6))


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
        shown = clock.describe(None, now=time.time() + 31 * 86400 - 0.05, tz=WARSAW)
        self.assertIn("host +31d 00:00:00", shown)
