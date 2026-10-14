import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from desk import evidence, runs


class NoDomains:
    def listAllDomains(self, _flags):
        return []


class Gc(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        for target, value in (("desk.runs.RUNS", self.home / "runs"), ("desk.runs.HOME", self.home),
                              ("desk.evidence.REPORTS", self.home / "reports"),
                              ("desk.runs.conn", NoDomains), ("desk.runs.lookup", lambda name: None)):
            patch = mock.patch(target, value)
            patch.start()
            self.addCleanup(patch.stop)

    def run_idle_for(self, run_id: str, hours: float) -> runs.Run:
        then = time.time() - hours * 3600
        run = runs.Run(id=run_id, image="fedora-44-gnome", domain=f"desk-{run_id}", ssh_port=1, user="tester",
                       created=then, last_used=then)
        (run.dir / "shots").mkdir(parents=True)
        (run.dir / "shots" / "0001.png").write_bytes(b"png")
        run.save()
        run.log_event(argv=["shot"])
        run.log_event(shot=str(run.dir / "shots" / "0001.png"))
        return run

    def test_an_idle_run_leaves_a_report_behind(self):
        old, fresh = self.run_idle_for("old", 3), self.run_idle_for("fresh", 0.1)
        removed = runs.gc(verbose=False)
        self.assertEqual(len(removed), 1)
        self.assertRegex(removed[0], r"^old \(stopped, idle 3\.0 h\); report: .*/reports/old/report\.md$")
        self.assertFalse(old.dir.exists())
        self.assertTrue(fresh.dir.exists())
        report = (self.home / "reports" / "old" / "report.md").read_text()
        self.assertIn("destroyed by desk gc (stopped, idle 3.0 h)", report)
        self.assertIn("shots/0001.png", report)
        self.assertTrue((self.home / "reports" / "old" / "shots" / "0001.png").exists())

    def test_a_run_reported_before_is_not_reported_again(self):
        old = self.run_idle_for("old", 3)
        evidence.collect(old)  # desk down --keep wrote it then
        runs.gc(verbose=False)
        self.assertFalse(old.dir.exists())
        self.assertEqual([p.name for p in (self.home / "reports").iterdir()], ["old"])


class PruneVersions(unittest.TestCase):
    def test_nothing_is_deleted_while_a_run_disk_cannot_be_read(self):
        def unreachable():
            raise AssertionError("went on to look for builds to delete")

        with tempfile.TemporaryDirectory() as tmp:
            disk = Path(tmp) / "runs" / "kept" / "disk.qcow2"
            disk.parent.mkdir(parents=True)
            disk.write_bytes(b"not a qcow2 image")
            with mock.patch("desk.runs.RUNS", Path(tmp) / "runs"), mock.patch("desk.runs.conn", unreachable), \
                    mock.patch("sys.stderr"):
                self.assertEqual(runs.prune_versions(verbose=False), [])


class AbandonedWarmup(unittest.TestCase):
    def xml(self, disk: Path) -> str:
        return f"<domain><devices><disk device='disk'><source file='{disk}'/></disk></devices></domain>"

    def test_only_a_warm_up_guest_is_one(self):
        from desk.config import RUNS, WARM

        name = "desk-warm-openwrt-25.12-20261013-021139"
        warming = self.xml(WARM / "openwrt-25.12.20261013-021139" / "disk.qcow2")
        self.assertEqual(runs.abandoned_warmup(name, warming), "openwrt-25.12")
        # A run restored from that warm state has its name, and is somebody's.
        self.assertIsNone(runs.abandoned_warmup(name, self.xml(RUNS / "ab12cd" / "disk.qcow2")))
        self.assertIsNone(runs.abandoned_warmup("desk-ab12cd", warming))


if __name__ == "__main__":
    unittest.main()
