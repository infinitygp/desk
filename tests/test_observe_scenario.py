"""Observation in scenarios, reports, `desk down` and snapshots, with the guest in memory."""

import json
import shutil
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from desk import evidence, observe, scenario, snapshot
from desk.config import DeskError, load_image
from desk.observe import report
from desk.observe.session import Store
from tests.observe_fake import FakeHost, FakeRun, FakeWindows, with_procmon
from tests.test_scenario import write

PROCMON = "C:\\pm\\Procmon64.exe"


def tmpdir(test) -> Path:
    d = Path(tempfile.mkdtemp(prefix="desk-observe-"))
    test.addCleanup(shutil.rmtree, d, ignore_errors=True)
    return d


def guest_with_procmon() -> FakeWindows:
    g = FakeWindows()
    with_procmon(g)
    g.registry["HKCU:\\Software\\Sysinternals\\Process Monitor\\EulaAccepted"] = 1
    g.disk[PROCMON] = b"MZ"
    return g


SCENARIO = f'''
image = "windows-11"
[[observe]]
provider = "procmon"
label = "app"
duration = 300
max_mb = 200
process = "app.exe"
options = {{exe = '{PROCMON}', csv = true}}
[[observe]]
provider = "procmon"
label = "later"
start = "step"
options = {{exe = '{PROCMON}'}}
[[step]]
gui = "app.exe"
[[step]]
mark = "saved the file"
[[step]]
observe_stop = "app"
[[step]]
observe_start = "later"
[[step]]
observe_stop = true
'''


class Load(unittest.TestCase):
    def test_observations_and_their_steps_are_read(self):
        path = write(SCENARIO)
        data = scenario.load(path)
        obs = scenario.observations(path, data)
        self.assertEqual(list(obs), ["app", "later"])
        self.assertEqual(scenario.describe(data["step"][1]), ("mark", "saved the file"))
        self.assertEqual(scenario.describe(data["step"][4]), ("observe_stop", ""))

    def test_mistakes_are_refused_before_anything_starts(self):
        base = 'image = "windows-11"\n'
        for text, said in (
            ('[[observe]]\nprovider = "strace"', "provider is one of"),
            ('observe = "procmon"', "are \\[\\[observe\\]\\] tables"),
            ('[[observe]]\nprovider = "procmon"\nfilter = "x"', "unknown keys \\['filter'\\]"),
            ('[[observe]]\nprovider = "procmon"\nlabel = "a b"', "label is letters"),
            ('[[observe]]\nprovider = "procmon"\n[[observe]]\nprovider = "procmon"', "label 'procmon' twice"),
            ('[[observe]]\nprovider = "procmon"\nstart = "now"', "start is"),
            ('[[observe]]\nprovider = "procmon"\nduration = 0', "duration takes a number above 0"),
            ('[[observe]]\nprovider = "procmon"\noptions = {cvs = true}', "does not take cvs"),
            ('[[observe]]\nprovider = "procmon"\noptions = "csv"', "options is a table"),
            ('[[observe]]\nprovider = "procmon"\nstart = "step"', "no observe_start step names it"),
            ('[[observe]]\nprovider = "procmon"\n[[step]]\nobserve_stop = "app"', "names no \\[\\[observe\\]\\] label"),
            ('[[step]]\nmark = "x"', "mark needs an \\[\\[observe\\]\\] session"),
            ('[[observe]]\nprovider = "procmon"\n[[step]]\nobserve_stop = false', "observe_stop takes true"),
            ('[[observe]]\nprovider = "procmon"\n[[step]]\nmark = "x"\ntimeout = 3', "mark does not use timeout"),
        ):
            with self.subTest(text=text), self.assertRaisesRegex(DeskError, said):
                scenario.load(write(base + text))

    def test_a_scenario_without_observations_is_as_before(self):
        data = scenario.load(write('image = "fedora-44-gnome"\n[[step]]\nexec = "true"'))
        self.assertEqual(scenario.observations(Path("x"), data), {})
        scenario.check_observations(data, load_image("fedora-44-gnome"))

    def test_a_provider_for_another_family_is_refused_for_the_image(self):
        data = scenario.load(write('image = "fedora-44-gnome"\n[[observe]]\nprovider = "procmon"'))
        with self.assertRaisesRegex(DeskError, "observes windows guests, and fedora-44-gnome is a linux one"):
            scenario.check_observations(data, load_image("fedora-44-gnome"))


class Steps(unittest.TestCase):
    def setUp(self):
        self.run = FakeRun(tmpdir(self))
        self.guest = guest_with_procmon()
        for target, value in (("desk.observe.guest_for", lambda run, spec=None: self.guest),
                              ("desk.observe.Host", FakeHost)):
            p = mock.patch(target, value)
            p.start()
            self.addCleanup(p.stop)
        self.path = write(SCENARIO)
        self.data = scenario.load(self.path)
        self.steps = mock.Mock(observations=scenario.observations(self.path, self.data))

    def step(self, n: int) -> str:
        return scenario._step(self.run, load_image("windows-11"), self.steps, self.data["step"][n - 1])

    def test_start_mark_and_stop_as_steps(self):
        s = scenario._observe_start(self.run, "app", self.steps.observations["app"])
        self.assertEqual((s.label, s.limits["duration"], s.limits["max_bytes"], s.target),
                         ("app", 300, 200 << 20, {"image": "app.exe"}))
        self.assertTrue(s.options["csv"])
        self.assertEqual(self.step(2), "mark 1 in s1")
        self.assertEqual(self.step(3), "s1 (app): procmon.pml, procmon.csv")
        self.assertEqual(self.step(4), "session s2 (procmon)")
        self.assertEqual(self.step(5), "s2 (later): procmon.pml")
        self.assertEqual(self.step(5), "no session open")

    def test_an_observation_that_fails_is_desks_failure_not_the_products(self):
        scenario._observe_start(self.run, "app", self.steps.observations["app"])
        del self.guest.disk["C:\\desk\\observe\\r1-s1\\procmon.pml"]
        self.guest.tools["procmon64.exe"] = lambda f, argv: {"rc": 0}
        for p in self.guest.running("Procmon64.exe"):
            p["alive"] = False
        with self.assertRaisesRegex(observe.ObserveError, "not complete: procmon.pml was not made"):
            self.step(3)
        with self.assertRaisesRegex(observe.ObserveError, "no running observation session"):
            self.step(2)

    def test_going_on_in_a_run_from_step_1_starts_what_starts_before_it_and_from_later_does_not(self):
        self.run.image = "windows-11"
        with mock.patch("desk.scenario._run_steps", return_value=True) as steps:
            scenario.continue_scenario(self.path, self.run, 3)
            self.assertEqual(Store(self.run).all(), [])
            scenario.continue_scenario(self.path, self.run, 1)
        self.assertEqual([(s.label, s.state) for s in Store(self.run).all()], [("app", "running")])
        self.assertEqual(steps.call_count, 2)

    def test_the_steps_stop_at_it_with_exit_2(self):
        run = mock.Mock()
        with mock.patch("desk.scenario._step", side_effect=observe.ObserveError("trace not complete")), \
                mock.patch("desk.scenario.display"), self.assertRaises(observe.ObserveError):
            scenario._run_steps(run, mock.Mock(), {"step": [{"mark": "x", "soft": True}, {"exec": "true"}]})
        self.assertEqual(len([c for c in run.log_event.call_args_list if c.kwargs.get("ok") is False]), 1)


class Report(unittest.TestCase):
    def setUp(self):
        self.run = FakeRun(tmpdir(self))
        self.guest = guest_with_procmon()
        self.host = FakeHost(self.run)

    def test_the_section_lists_sessions_marks_and_kept_files(self):
        self.run.log_event(record="start", fps=4)
        observe.start(self.run, "procmon", options={"exe": PROCMON}, host=self.host, guest=self.guest)
        self.run.log_event(shot=str(self.run.dir / "shots" / "0001.png"))
        time.sleep(0.01)
        observe.mark(self.run, "clicked Save", guest=self.guest)
        observe.stop(self.run, host=self.host, guest=self.guest)
        self.run.log_event(record="stop", video="video-1.webm", seconds=60)
        out = tmpdir(self)
        md = "\n".join(report.section(self.run, out, self.run.events))
        self.assertIn("### s1: procmon (Process Monitor)", md)
        self.assertIn("- state: stopped, complete", md)
        self.assertIn("mark 1 'clicked Save'", md)
        self.assertIn("[video-1.webm](video-1.webm) at 00:0", md)
        self.assertIn("after shots/0001.png", md)
        self.assertIn("[procmon.pml](observe/s1/procmon.pml): verified", md)
        self.assertIn("guest clock +2.5", md)
        kept = out / "observe" / "s1" / "procmon.pml"
        self.assertTrue(kept.exists())
        data = json.loads((out / "observe.json").read_text())
        s = data["sessions"][0]
        self.assertEqual((data["schema"], s["id"], s["complete"]), (1, "s1", True))
        self.assertEqual(s["artifacts"][0]["report_path"], "observe/s1/procmon.pml")
        self.assertEqual(s["markers"][0]["video"], "video-1.webm")
        self.assertEqual(s["markers"][0]["shot_before"], "0001.png")
        self.assertNotIn("desk's mark took", md)
        # A mark the guest was slow to take says how long desk waited around it.
        with Store(self.run).edit("s1") as cur:
            cur.markers[0]["host_window"] = [cur.markers[0]["host"] - 16, cur.markers[0]["host"] + 16]
        self.assertIn("desk's mark took 32 s", "\n".join(report.section(self.run, tmpdir(self), self.run.events)))

    def test_no_sessions_no_section(self):
        self.assertEqual(report.section(self.run, tmpdir(self), []), [])

    def test_a_session_that_is_not_complete_says_why(self):
        observe.start(self.run, "procmon", options={"exe": PROCMON}, host=self.host, guest=self.guest)
        self.run.live = False
        observe.stop(self.run, host=self.host, guest=self.guest)
        md = "\n".join(report.section(self.run, tmpdir(self), self.run.events))
        self.assertIn("NOT complete: interrupted: the VM is no longer running", md)

    def test_the_timeline_names_observation_events(self):
        self.assertEqual(report.timeline({"observe": "start", "session": "s1", "provider": "wpr"}),
                         "observation s1 started (wpr)")
        self.assertEqual(report.timeline({"observe": "stop", "session": "s1", "provider": "wpr", "complete": False}),
                         "observation s1 stopped (wpr): NOT complete")
        self.assertEqual(report.timeline({"observe": "mark", "seq": 2, "text": "x"}), "mark 2: x")
        self.assertEqual(report.timeline({"observe": "interrupted", "session": "s1", "detail": "rebooted"}),
                         "observation s1 interrupted: rebooted")
        self.assertIsNone(report.timeline({"shot": "x"}))

    def test_the_whole_report_has_the_section_and_the_timeline_entries(self):
        observe.start(self.run, "procmon", options={"exe": PROCMON}, host=self.host, guest=self.guest)
        observe.mark(self.run, "here", guest=self.guest)
        snap = tmpdir(self)
        observe.snapshot_saved(self.run, snap)
        observe.stop(self.run, host=self.host, guest=self.guest)
        observe.snapshot_restored(self.run, snap, "mid")
        observe.stop(self.run, "s2", host=self.host, guest=self.guest)
        (self.run.dir / "log.jsonl").write_text("".join(json.dumps(e) + "\n" for e in self.run.events))
        self.run.ready = 0.0
        self.run.live = False
        with mock.patch.object(evidence, "REPORTS", tmpdir(self)):
            out = evidence.collect(self.run)
        md = (out / "report.md").read_text()
        self.assertLess(md.index("## Observation"), md.index("## Timeline"))
        self.assertIn("observation s1 started (procmon)", md)
        self.assertIn("mark 1: here", md)
        self.assertIn("snapshot mid: snapshot mid brought the capture of s1 back", md)
        self.assertTrue((out / "observe.json").exists())


class Teardown(unittest.TestCase):
    """desk down, gc and the end of a scenario collect what is open while the guest is there."""

    def finish(self, **kw):
        run = FakeRun(tmpdir(self))

        @contextmanager
        def lock():
            yield

        with mock.patch("desk.observe.collect_all", return_value=["observation s1 (procmon): not made"]) as collect, \
                mock.patch("desk.record.stop"), mock.patch("desk.record.save_videos", return_value=[]), \
                mock.patch("desk.evidence.collect", return_value=None), mock.patch("desk.runs.state_lock", lock), \
                mock.patch("desk.runs.down"), mock.patch("sys.stderr") as err:
            evidence.finish(run, **kw)
        return collect, err

    def test_open_sessions_are_collected_before_the_run_goes(self):
        collect, err = self.finish()
        collect.assert_called_once()
        self.assertIn("not made", "".join(str(c) for c in err.write.call_args_list))

    def test_a_kept_scenario_run_keeps_its_captures_running(self):
        collect, _ = self.finish(running=True)
        collect.assert_not_called()


class SnapshotHooks(unittest.TestCase):
    def setUp(self):
        resume = mock.patch("desk.snapshot._resume")
        resume.start()
        self.addCleanup(resume.stop)

    def test_sessions_travel_with_snapshots(self):
        from tests.test_snapshot import Run

        run = Run(tmpdir(self))
        run.id = "r1"
        store = Store(run)
        s = store.new("procmon", None, lambda s, o: None)
        with store.edit(s.id) as cur:
            cur.state = "running"
        snapshot.save(run, "mid")
        self.assertTrue((run.dir / "snapshots" / "mid" / "observe-sessions" / "s1.json").exists())
        snapshot.save(run, "mid", force=True)
        with store.edit(s.id) as cur:
            cur.state = "running"
        later = store.new("procmon", "later", lambda s, o: None)
        with store.edit(later.id) as cur:
            cur.state = "running"
        with mock.patch("sys.stderr"):
            snapshot.restore(run, "mid")
        self.assertEqual([x.state for x in store.all()], ["running", "interrupted"])


if __name__ == "__main__":
    unittest.main()
