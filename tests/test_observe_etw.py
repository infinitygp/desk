"""ETW through WPR and logman, with the guest in memory: the command lines desk builds, the trace
sessions it owns by name, and what it says of lost events. Whether WPR and logman take these
command lines on Windows 11 is for docs/observe/validation.md (WIN-ETW-*)."""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from desk import observe
from desk.observe import etw
from desk.observe.session import Store
from tests.observe_fake import FakeHost, FakeRun, FakeWindows


def tmpdir(test) -> Path:
    d = Path(tempfile.mkdtemp(prefix="desk-observe-"))
    test.addCleanup(shutil.rmtree, d, ignore_errors=True)
    return d


class Names(unittest.TestCase):
    def test_eventsource_guids_are_derived_as_dotnet_does(self):
        # Published GUIDs of EventSources that take theirs from their names.
        self.assertEqual(etw.eventsource_guid("Microsoft-Diagnostics-DiagnosticSource"),
                         "adb401e1-5296-51f8-c125-5fda75826144")
        self.assertEqual(etw.eventsource_guid("Microsoft-ApplicationInsights-Core"),
                         "74af9f20-af6a-5582-9382-f21f674fb271")
        self.assertEqual(etw.eventsource_guid("x"), etw.eventsource_guid("X"))

    def test_provider_arguments(self):
        self.assertEqual(etw.provider_args("Microsoft-Windows-Kernel-Process"), ["Microsoft-Windows-Kernel-Process"])
        self.assertEqual(etw.provider_args("Microsoft-Windows-Kernel-Process:0x10:4"),
                         ["Microsoft-Windows-Kernel-Process", "0x10", "0x4"])
        self.assertEqual(etw.provider_args("{22FB2CD6-0E7B-422B-A0C7-2FAD1FD0E716}:0xFF"),
                         ["{22FB2CD6-0E7B-422B-A0C7-2FAD1FD0E716}", "0xFF"])

    def test_session_names_are_desks_and_the_runs(self):
        s = Store(FakeRun(tmpdir(self), run_id="my-run.2")).new("etw", None, lambda s, o: None)
        self.assertEqual(etw.session_name(s), "desk_my_run_2_s1")

    def test_options_are_checked_before_anything_starts(self):
        from desk.config import DeskError

        for name, given, said in (("wpr", {"profile": "General Profile"}, "no WPR profile"),
                                  ("wpr", {"profile": "C:\\x.txt"}, "no WPR profile"),
                                  ("etw", {}, "needs providers"),
                                  ("etw", {"providers": "Kernel:zz"}, "NAME\\[:KEYWORDS")):
            with self.subTest(name=name, given=given), self.assertRaisesRegex(DeskError, said):
                observe.provider(name).options(given)
        self.assertEqual(observe.provider("wpr").options({"profile": "C:\\p\\x.wprp!Mine"})["profile"],
                         ["C:\\p\\x.wprp!Mine"])
        self.assertEqual(observe.provider("etw").options({"providers": "A:0x10:4,{22FB2CD6-0E7B-422B-A0C7-2FAD1FD0E716}"})
                         ["providers"], ["A:0x10:4", "{22FB2CD6-0E7B-422B-A0C7-2FAD1FD0E716}"])

    def test_lost_events_are_only_what_the_session_said(self):
        self.assertEqual(etw.lost({"EventsLost": 3, "Name": "x", "LogBuffersLost": 0}),
                         {"EventsLost": 3, "LogBuffersLost": 0})
        self.assertEqual(etw.lost(None), {})


class Base(unittest.TestCase):
    def setUp(self):
        self.run = FakeRun(tmpdir(self))
        self.guest = FakeWindows()
        self.host = FakeHost(self.run)

    def start(self, provider, **kw):
        return observe.start(self.run, provider, host=self.host, guest=self.guest, **kw)

    def stop(self, ref=None, **kw):
        return observe.stop(self.run, ref, host=self.host, guest=self.guest, **kw)


class Logman(Base):
    def test_a_named_session_with_its_providers_and_desks_marker(self):
        s = self.start("etw", options={"providers": ["Microsoft-Windows-Kernel-Process:0x10:4", "Other"]}, max_mb=64)
        name = "desk_r1_s1"
        start, update1, update2 = self.guest.execs("logman.exe")
        self.assertEqual(start, ["start", name, "-ets", "-o", "C:\\desk\\observe\\r1-s1\\trace.etl", "-p",
                                 "Microsoft-Windows-Kernel-Process", "0x10", "0x4", "-max", "64"])
        self.assertEqual(update1, ["update", "trace", name, "-ets", "-p", "Other"])
        self.assertEqual(update2[-1], "{" + etw.eventsource_guid(observe.MARKER_SOURCE) + "}")
        self.assertEqual(s.tool["name"], name)
        self.guest.etw[name]["EventsLost"] = 12
        s, problems = self.stop()
        self.assertEqual(problems, [])
        self.assertEqual(self.guest.execs("logman.exe")[-1], ["stop", name, "-ets"])
        self.assertEqual(s.stats["lost"], {"EventsLost": 12})
        self.assertIn("EventsLost 12", " ".join(observe.provider("etw").summary(s)))
        etl = s.artifacts[0]
        self.assertEqual((etl["name"], etl["verified"], etl["check"]["level"]), ("trace.etl", True, "size"))

    def test_without_markers_no_marker_provider(self):
        self.start("etw", options={"providers": ["A"], "markers": False})
        self.assertEqual(len(self.guest.execs("logman.exe")), 1)

    def test_lost_events_not_reported_are_not_measured_never_zero(self):
        self.start("etw", options={"providers": ["A"]})
        self.guest.etw["desk_r1_s1"] = {k: v for k, v in self.guest.etw["desk_r1_s1"].items() if k != "EventsLost"}
        s, _ = self.stop()
        self.assertNotIn("lost", s.stats)
        self.assertIn("not measured", " ".join(observe.provider("etw").summary(s)))

    def test_a_session_of_desks_name_left_in_the_guest_is_a_conflict(self):
        self.guest.etw["desk_r1_s1"] = {"EventsLost": 0, "_out": "x"}
        with self.assertRaisesRegex(observe.Conflict, "trace session named desk_r1_s1 runs already"):
            self.start("etw", options={"providers": ["A"]})

    def test_ended_by_its_watchdog_it_is_not_stopped_again(self):
        s = self.start("etw", options={"providers": ["A"]})
        self.guest.tools["logman.exe"](self.guest, ["stop", "desk_r1_s1", "-ets"])
        self.assertEqual(observe.listing(self.run, guest=self.guest)[0].state, "ended")
        n = len(self.guest.execs("logman.exe"))
        s, problems = self.stop()
        self.assertEqual(problems, [])
        self.assertEqual(len(self.guest.execs("logman.exe")), n)

    def test_tracerpt_on_request(self):
        self.start("etw", options={"providers": ["A"], "tracerpt": True})
        s, _ = self.stop()
        self.assertEqual([a["name"] for a in s.artifacts], ["trace.etl", "events.csv", "summary.txt", "report.xml"])
        self.assertEqual(self.guest.execs("tracerpt.exe")[0][:5],
                         ["C:\\desk\\observe\\r1-s1\\trace.etl", "-o", "C:\\desk\\observe\\r1-s1\\events.csv", "-of", "CSV"])
        self.assertIn("tracerpt read the trace", observe.provider("etw").summary(s))

    def test_a_start_logman_refuses_fails_with_its_code(self):
        self.guest.tools["logman.exe"] = lambda f, argv: {"rc": -2147024891, "stdout": "Access is denied."}
        with self.assertRaisesRegex(observe.ObserveError, r"logman.exe exit -2147024891 \(0x80070005\): Access is denied"):
            self.start("etw", options={"providers": ["A"]})
        self.assertEqual(Store(self.run).find("s1").state, "failed")

    def test_without_logman_or_elevation(self):
        self.guest.commands["logman.exe"] = None
        with self.assertRaisesRegex(observe.Missing, "logman.exe is not in the guest"):
            self.start("etw", options={"providers": ["A"]})
        self.guest.admin = False
        with self.assertRaises(observe.Privileges):
            self.start("etw", options={"providers": ["A"]})


class Wpr(Base):
    def test_profiles_under_an_instance_name_of_desks_own(self):
        s = self.start("wpr", options={"profile": ["GeneralProfile", "CPU.Light"]}, max_mb=100)
        self.assertEqual(self.guest.execs("wpr.exe")[0],
                         ["-start", "GeneralProfile", "-start", "CPU.Light", "-filemode", "-recordtempto",
                          "C:\\desk\\observe\\r1-s1\\wpr-temp", "-instancename", "desk_r1_s1"])
        self.assertEqual(observe.provider("wpr").watch_files(s), ["C:\\desk\\observe\\r1-s1\\wpr-temp"])
        s, problems = self.stop()
        self.assertEqual(problems, [])
        self.assertIn(["-stop", "C:\\desk\\observe\\r1-s1\\trace.etl", "-instancename", "desk_r1_s1"],
                      self.guest.execs("wpr.exe"))
        self.assertEqual(s.stats["lost"], {"WPR_initiated_desk_r1_s1.EventsLost": 7,
                                           "WPR_initiated_desk_r1_s1.BuffersLost": 0})
        # Its own instance is gone already: nothing to cancel.
        self.assertNotIn("-cancel", [a[0] for a in self.guest.execs("wpr.exe")])

    def test_skip_pdb_generation_on_request(self):
        s = self.start("wpr", options={"pdbgen": False})
        self.assertEqual(observe.provider("wpr").stop_commands(s)[0]["args"],
                         "-stop C:\\desk\\observe\\r1-s1\\trace.etl -instancename desk_r1_s1 -skipPdbGen")

    def test_a_trace_the_watchdog_wrote_is_not_stopped_again(self):
        s = self.start("wpr", duration=10)
        stop = observe.provider("wpr")._stop_args(s)
        self.guest.tools["wpr.exe"](self.guest, stop)
        self.guest.disk[s.watchdog["out"]] = b'{"state": "done", "reason": "duration", "stop": [{"rc": 0}]}'
        n = len(self.guest.execs("wpr.exe"))
        s, problems = self.stop()
        self.assertEqual(problems, [])
        self.assertEqual(self.guest.execs("wpr.exe")[n:], [])
        self.assertIn("time limit", " ".join(s.notes))

    def test_a_desk_killed_while_wpr_starts_leaves_a_recording_its_stop_cancels(self):
        tool = self.guest.tools["wpr.exe"]
        kept = {}

        def starting(f, argv):
            if argv[0] == "-start":
                kept.update(guest_dir=Store(self.run).load("s1").guest_dir)
            return tool(f, argv)

        self.guest.tools["wpr.exe"] = starting
        self.start("wpr")
        # What the store holds while WPR starts is what a kill -9 then leaves.
        self.assertEqual(kept["guest_dir"], "C:\\desk\\observe\\r1-s1")
        with Store(self.run).edit("s1") as s:
            s.state, s.pid, s.tool, s.watchdog = "starting", 999999, {}, None
        with mock.patch("desk.observe.pid_alive", return_value=False):
            listed = observe.listing(self.run, guest=self.guest)
        self.assertEqual(listed[0].state, "interrupted")
        s, problems = self.stop()
        self.assertIn(["-cancel", "-instancename", "desk_r1_s1"], self.guest.execs("wpr.exe"))
        self.assertNotIn("WPR_initiated_desk_r1_s1", self.guest.etw)
        self.assertFalse(s.complete)

    def test_a_second_wpr_of_the_run_is_refused(self):
        self.start("wpr")
        with self.assertRaisesRegex(observe.Conflict, "holds wpr"):
            self.start("wpr")

    def test_wpr_refusing_says_its_code(self):
        self.guest.tools["wpr.exe"] = lambda f, argv: {"rc": -984076288, "stdout": "There are currently active trace "
                                                       "sessions."}
        with self.assertRaisesRegex(observe.ObserveError, r"0xC5583000\): There are currently active trace sessions"):
            self.start("wpr")


if __name__ == "__main__":
    unittest.main()
