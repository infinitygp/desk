import unittest

from desk.evidence import format_changes, parse_virt_diff

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
            run = mock.Mock(id="r1", dir=run_dir, image="fedora-44-gnome", created=99.0, ready=0.0)
            run.live_domain.return_value = None
            with mock.patch.object(evidence, "REPORTS", Path(tmp) / "reports"):
                md = (evidence.collect(run) / "report.md").read_text()
        self.assertIn("2 of 2 steps passed", md)
        self.assertIn("clock +3d in steps of 1d", md)
        self.assertIn("clock +1d in steps of 6h", md)
