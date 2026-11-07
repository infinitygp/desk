import os
import subprocess
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from desk.config import DeskError
from desk.evidence import (
    format_changes,
    guest_problems,
    local_dnf_times,
    parse_virt_diff,
    unit_failures,
)

# Trimmed real virt-diff output (Fedora guest, warm image vs run overlay).
SAMPLE = """\
+ d 0755         12 /etc/desktest
+ - 0644          3 /etc/desktest/a.conf
- d 0755       4490 /etc
+ d 0755       4506 /etc
# changed: st_size
= - 0644          0 /etc/.updated
@@ -0,0 +1,4 @@
+# This file was created by systemd-update-done.
@@ End of diff @@
- - 0644        120 /etc/hosts
+ - 0644        140 /etc/hosts
- - 0644         50 /etc/removed.conf
+ d 0755          0 /var/lib/dnf
+ - 0644       1000 /var/log/dnf5.log
+ - 0644         10 /home/tester/.cache/x
+ - 0755      99000 /usr/bin/htop
"""


class ParseVirtDiff(unittest.TestCase):
    def test_buckets_and_noise(self):
        ch = parse_virt_diff(SAMPLE)
        self.assertEqual(ch.added, ["/etc/desktest/", "/etc/desktest/a.conf", "/usr/bin/htop"])
        self.assertEqual(ch.changed, ["/etc/hosts"])
        self.assertEqual(ch.removed, ["/etc/removed.conf"])
        # /etc/.updated, /var/lib/dnf, /var/log/..., ~/.cache are routine
        self.assertEqual(ch.hidden, 4)

    def test_all_paths(self):
        ch = parse_virt_diff(SAMPLE, all_paths=True)
        self.assertIn("/var/log/dnf5.log", ch.added)
        self.assertIn("/etc/.updated", ch.changed)
        self.assertNotIn("/etc", ch.changed, "a directory whose listing changed is not a change by itself")

    def test_format(self):
        out = format_changes(parse_virt_diff(SAMPLE)).splitlines()
        self.assertIn("+ /usr/bin/htop", out)
        self.assertIn("~ /etc/hosts", out)
        self.assertIn("- /etc/removed.conf", out)
        self.assertTrue(out[-1].startswith("3 added, 1 changed, 1 removed; 4 routine paths hidden"))


class WindowsNoise(unittest.TestCase):
    def test_routine_windows_paths_hidden(self):
        sample = "\n".join([
            "+ - 0644         10 /DeskTest/hello.txt",
            "+ - 0644         10 /Windows/Prefetch/SVCHOST.EXE-09F4AEA4.pf",
            "+ - 0644         10 /Windows/System32/config/SECURITY.LOG1",
            "+ - 0644         10 /Users/tester/AppData/Local/Microsoft/OneDrive/logs/x.aodl",
            "+ - 0644         10 /Windows/System32/config/SOFTWARE",
        ])
        ch = parse_virt_diff(sample)
        self.assertEqual(ch.added, ["/DeskTest/hello.txt", "/Windows/System32/config/SOFTWARE"])
        self.assertEqual(ch.hidden, 3)


class GuestProblems(unittest.TestCase):
    RUN = SimpleNamespace(image="fedora-44-gnome", ssh_port=1, since=0.0, clock_offset=0.0,
                          dir=Path("/nonexistent"))

    def collect(self, returncode: int, stdout: bytes, stderr: bytes = b"") -> dict:
        answer = subprocess.CompletedProcess([], returncode, stdout, stderr)
        with mock.patch("desk.evidence.guest.ssh_run", return_value=answer):
            return guest_problems(self.RUN)

    def test_sections(self):
        self.assertEqual(self.collect(0, b"=== failed\n=== journal\nline one\nline two\n"),
                         {"failed": "", "journal": "line one\nline two"})

    def test_a_lost_connection_is_not_a_clean_guest(self):
        with self.assertRaisesRegex(DeskError, "no SSH connection.*Connection refused"):
            self.collect(255, b"", b"ssh: connect to host 127.0.0.1 port 1: Connection refused")
        # Cut off half way: what came through would read as "nothing else was wrong".
        with self.assertRaisesRegex(DeskError, "no SSH connection"):
            self.collect(255, b"=== failed\n")

    def test_no_output_is_not_a_clean_guest(self):
        with self.assertRaisesRegex(DeskError, "gave nothing.*bash: not found"):
            self.collect(127, b"", b"bash: not found")


if __name__ == "__main__":
    unittest.main()


class Report(unittest.TestCase):
    def test_clock_steps_are_not_scenario_steps(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock

        from desk import evidence

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            events = [
                {"t": 100.0, "scenario": "/x/retention.toml", "steps": 2},
                {"t": 101.0, "step": 1, "action": "clock +3d", "ok": True, "detail": "", "seconds": 9.0},
                # Written by desk clock --step before its step size had a key of its own.
                {"t": 101.0, "clock": "+3d", "step": "1d", "offset": 259200},
                {"t": 102.0, "clock": "+1d", "clock_step": "6h", "offset": 345600},
                {"t": 103.0, "step": 2, "action": "expect done", "ok": True, "detail": "", "seconds": 1.0},
            ]
            (run_dir / "log.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
            run = mock.Mock(id="r1", dir=run_dir, image="fedora-44-gnome", created=99.0, ready=0.0, usbvfiod={})
            run.live_domain.return_value = None
            with mock.patch.object(evidence, "REPORTS", Path(tmp) / "reports"):
                md = (evidence.collect(run) / "report.md").read_text()
        self.assertIn("2 of 2 steps passed", md)
        self.assertIn("clock +3d in steps of 1d", md)
        self.assertIn("clock +1d in steps of 6h", md)

    def test_scenario_continued_with_from_counts_each_steps_last_attempt(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock

        from desk import evidence

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            events = [
                {"t": 100.0, "scenario": "/x/a.toml", "steps": 3},
                {"t": 101.0, "step": 1, "action": "gui app", "ok": True, "detail": "", "seconds": 1.0},
                {"t": 102.0, "step": 2, "action": "click Sav", "ok": False, "detail": "element 'Sav' did not appear",
                 "seconds": 1.0},
                # desk scenario run a.toml --from 2, after fixing the step
                {"t": 110.0, "scenario": "/x/a.toml", "steps": 3, "first": 2},
                {"t": 111.0, "step": 2, "action": "click Save", "ok": True, "detail": "", "seconds": 1.0},
                {"t": 112.0, "step": 3, "action": "expect Saved", "ok": True, "detail": "", "seconds": 1.0},
            ]
            (run_dir / "log.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
            run = mock.Mock(id="r1", dir=run_dir, image="fedora-44-gnome", created=99.0, ready=0.0, usbvfiod={})
            run.live_domain.return_value = None
            with mock.patch.object(evidence, "REPORTS", Path(tmp) / "reports"):
                md = (evidence.collect(run) / "report.md").read_text()
        self.assertIn("result: passed, 3 of 3 steps passed", md)
        self.assertNotIn("failed at step", md)
        self.assertIn("step 2 FAILED", md)  # the timeline keeps the first attempt
        self.assertNotIn("- build:", md)

    def test_a_build_skipped_with_no_build_is_in_the_report(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock

        from desk import evidence

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            said = "skipped (--no-build); artifacts: target/release/app (2026-11-02 01:00)"
            events = [{"t": 100.0, "scenario": "/x/a.toml", "steps": 1, "build": said},
                      {"t": 101.0, "step": 1, "action": "gui app", "ok": True, "detail": "", "seconds": 1.0},
                      {"t": 110.0, "scenario": "/x/a.toml", "steps": 1, "first": 1}]  # then --from 1
            (run_dir / "log.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
            run = mock.Mock(id="r1", dir=run_dir, image="fedora-44-gnome", created=99.0, ready=0.0, usbvfiod={})
            run.live_domain.return_value = None
            with mock.patch.object(evidence, "REPORTS", Path(tmp) / "reports"):
                md = (evidence.collect(run) / "report.md").read_text()
        self.assertIn(f"- build: {said}", md)


class DiffFailure(unittest.TestCase):
    def test_a_diff_that_fails_leaves_the_rest_of_the_report(self):
        import tempfile

        from desk import evidence

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            run = mock.Mock(id="r1", dir=run_dir, image="fedora-44-gnome", created=99.0, ready=0.0, usbvfiod={})
            run.live_domain.return_value = None
            # cp --reflink=always on a file system without reflinks
            no_reflink = subprocess.CalledProcessError(1, ["cp", "--reflink=always"])
            with mock.patch.object(evidence, "REPORTS", Path(tmp) / "reports"), \
                    mock.patch.object(evidence, "file_changes", side_effect=no_reflink):
                self.assertFalse(evidence.reported(run))
                md = (evidence.collect(run, diff=True) / "report.md").read_text()
                self.assertTrue(evidence.reported(run))
        self.assertIn("reflink", md)

    def test_a_report_the_run_went_on_after_is_not_its_report(self):
        import tempfile

        from desk import evidence

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            run = mock.Mock(id="r1", dir=run_dir, created=time.time() - 60, usbvfiod={})
            (Path(tmp) / "r1").mkdir()
            report = Path(tmp) / "r1" / "report.md"
            report.write_text("# a scenario's report, the run kept")
            os.utime(report, (time.time() - 30,) * 2)
            with mock.patch.object(evidence, "REPORTS", Path(tmp)):
                self.assertTrue(evidence.reported(run))
                (run_dir / "log.jsonl").write_text('{"observe": "start"}\n')
                self.assertFalse(evidence.reported(run))

    def test_a_report_directory_without_a_report_is_not_a_report(self):
        import tempfile

        from desk import evidence

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "r1" / "shots").mkdir(parents=True)
            with mock.patch.object(evidence, "REPORTS", Path(tmp)):
                self.assertFalse(evidence.reported(mock.Mock(id="r1", created=0.0)))


class StepLabels(unittest.TestCase):
    def test_messages_go_to_the_step_running_then(self):
        from desk.evidence import _step_labels

        events = [
            {"t": 110.0, "step": 1, "action": "root dnf install", "ok": True, "seconds": 10.0},
            {"t": 120.0, "step": 2, "action": "click Example", "ok": True, "seconds": 2.0},
            # desk clock +1d during step 3: guest times run a day ahead from here on.
            {"t": 125.0, "clock": "+1d", "offset": 86400},
            {"t": 126.0, "step": 3, "action": "clock +1d", "ok": True, "seconds": 3.0},
        ]
        self.assertEqual(_step_labels([50.0, 105.0, 119.0, 123.0 + 86400], events),
                         ["before the first step", "step 1: root dnf install", "step 2: click Example",
                          "step 3: clock +1d"])


class UnitFailures(unittest.TestCase):
    def test_system_and_user_units(self):
        text = ("2026-10-09T15:17:58+02:00 desk-fedora systemd[1]: deskfail.service: Failed with result 'exit-code'.\n"
                "2026-10-09T15:17:59+02:00 desk-fedora systemd[1318]: app.service: Failed with result 'timeout'.\n"
                "2026-10-09T15:18:00+02:00 desk-fedora systemd[1]: Started foo.service.\n")
        self.assertEqual(unit_failures(text), ["15:17:58 deskfail.service: exit-code",
                                               "15:17:59 app.service (user): timeout"])


class DnfTimes(unittest.TestCase):
    def test_utc_becomes_the_guests_local_time_whatever_the_hosts(self):
        from zoneinfo import ZoneInfo

        text = "installed foo-1.0-1.noarch\n 3 dnf install -y /tmp/foo.rpm 2026-10-08 21:30:00     1"
        for host in ("UTC", "America/New_York"):
            with self.subTest(host=host), mock.patch.dict("os.environ", {"TZ": host}):
                time.tzset()
                self.assertEqual(local_dnf_times(text, ZoneInfo("Europe/Warsaw")).splitlines(),
                                 ["installed foo-1.0-1.noarch", " 3 dnf install -y /tmp/foo.rpm 2026-10-08 23:30:00     1"])
                self.assertIn("2026-10-08 17:30:00", local_dnf_times(text, ZoneInfo("America/New_York")))
        time.tzset()


class Timeline(unittest.TestCase):
    def test_long_commands_and_scripts_take_one_line(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock

        from desk import evidence

        script = "set -e\n" + "echo step && " * 40 + "true"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            events = [
                {"t": 100.0, "argv": ["exec", "--root", "--", script]},
                {"t": 101.0, "scenario": "/x/a.toml", "steps": 1},
                {"t": 102.0, "step": 1, "action": f"root {script}", "ok": False, "detail": "exit 1", "seconds": 1.0},
            ]
            (run_dir / "log.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
            run = mock.Mock(id="r1", dir=run_dir, image="fedora-44-gnome", created=99.0, ready=0.0, usbvfiod={})
            run.live_domain.return_value = None
            with mock.patch.object(evidence, "REPORTS", Path(tmp) / "reports"):
                md = (evidence.collect(run) / "report.md").read_text()
        timeline = md.split("## Timeline", 1)[1].strip().splitlines()
        self.assertEqual(len(timeline), 2)
        self.assertTrue(timeline[0].startswith("- +00:01 `desk exec --root -- set -e echo step && "))
        self.assertTrue(timeline[0].endswith("…`"))
        self.assertLess(max(len(line) for line in timeline), 180)
        self.assertIn("failed at step 1 (root set -e echo step", md)


class PruneReports(unittest.TestCase):
    def test_a_report_another_gc_removes_meanwhile_is_skipped(self):
        import os
        import tempfile

        from desk import evidence

        with tempfile.TemporaryDirectory() as tmp:
            for name in ("old", "gone", "new"):
                (Path(tmp) / name).mkdir()
            os.utime(Path(tmp) / "old", (0, 0))
            stat = Path.stat

            def vanishing(path, *args, **kwargs):
                if path.name == "gone":
                    raise FileNotFoundError(path)
                return stat(path, *args, **kwargs)

            with mock.patch.object(evidence, "REPORTS", Path(tmp)), mock.patch.object(Path, "stat", vanishing):
                evidence.prune_reports(verbose=False)
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["gone", "new"])


class SkippedSteps(unittest.TestCase):
    def test_a_step_only_for_other_images_shows_and_is_not_counted_as_passed(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock

        from desk import evidence

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            events = [
                {"t": 100.0, "scenario": "/x/a.toml", "steps": 3},
                {"t": 101.0, "step": 1, "action": "gui app", "ok": True, "detail": "", "seconds": 1.0},
                {"t": 101.5, "step": 2, "action": "click Close", "skipped": "only on kde"},
                {"t": 102.0, "step": 3, "action": "expect Saved", "ok": True, "detail": "", "seconds": 1.0},
            ]
            (run_dir / "log.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
            run = mock.Mock(id="r1", dir=run_dir, image="fedora-44-gnome", created=99.0, ready=0.0, usbvfiod={})
            run.live_domain.return_value = None
            with mock.patch.object(evidence, "REPORTS", Path(tmp) / "reports"):
                md = (evidence.collect(run) / "report.md").read_text()
        self.assertIn("result: passed, 2 of 3 steps passed, 1 skipped on fedora-44-gnome (only)", md)
        self.assertIn("step 2 skipped: click Close → only on kde", md)
