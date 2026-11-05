import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from desk import snapshot
from desk.config import DeskError


class Run:
    """A run whose guest is a file: saving it writes the memory image, and nothing restores."""
    clock_offset = 0.0
    usbvfiod = {}

    def __init__(self, d, save_fails: bool = False):
        self.dir, self.save_fails, self.destroyed = Path(d), save_fails, False
        (self.dir / "disk.qcow2").write_text("disk")
        (self.dir / "VARS.qcow2").write_text("vars")

    def dom(self):
        return self

    def live_domain(self):
        return self

    @contextmanager
    def edit(self):  # run.json
        yield self

    def save(self, path):
        if self.save_fails:
            raise RuntimeError("no space left on device")
        Path(path).write_text("memory")

    def destroy(self):
        self.destroyed = True

    def log_event(self, **event):
        pass


class Names(unittest.TestCase):
    def test_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Run(tmp)
            self.assertEqual(snapshot._dir(run, "after-install.v2"), run.dir / "snapshots" / "after-install.v2")
            for bad in ("", "../escape", "a/b", "-dash", "x" * 65, "spa ce"):
                with self.subTest(bad=bad), self.assertRaises(DeskError):
                    snapshot._dir(run, bad)
            self.assertEqual(snapshot.listing(run), [])


class SaveRestore(unittest.TestCase):
    def setUp(self):
        resume = mock.patch("desk.snapshot._resume")
        resume.start()
        self.addCleanup(resume.stop)

    def test_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Run(tmp)
            snapshot.save(run, "clean")
            self.assertEqual([s["name"] for s in snapshot.listing(run)], ["clean"])
            self.assertEqual((run.dir / "snapshots" / "clean" / "disk.qcow2").read_text(), "disk")
            with self.assertRaisesRegex(DeskError, "exists"):
                snapshot.save(run, "clean")

    def test_a_failed_replacement_keeps_the_snapshot_it_was_to_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Run(tmp)
            snapshot.save(run, "clean")
            (run.dir / "disk.qcow2").write_text("later")
            run.save_fails = True
            with self.assertRaises(RuntimeError):
                snapshot.save(run, "clean", force=True)
            self.assertEqual((run.dir / "snapshots" / "clean" / "disk.qcow2").read_text(), "disk")
            self.assertEqual([p.name for p in (run.dir / "snapshots").iterdir()], ["clean"])
            # Cut off at the very end, with the old one already moved out of the way.
            run.save_fails = False
            with mock.patch("desk.snapshot.Path.rename", side_effect=[None, OSError("no space left")]), \
                    self.assertRaises(OSError):
                snapshot.save(run, "clean", force=True)
            self.assertEqual((run.dir / "snapshots" / "clean" / "disk.qcow2").read_text(), "disk")
            self.assertEqual(sorted(p.name for p in (run.dir / "snapshots").iterdir()), ["clean"])
            snapshot.save(run, "clean", force=True)
            self.assertEqual((run.dir / "snapshots" / "clean" / "disk.qcow2").read_text(), "later")

    def test_a_guest_that_does_not_resume_well_still_has_its_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Run(tmp)
            snapshot.save(run, "clean")
            with mock.patch("desk.snapshot._resume", side_effect=DeskError("its clock shows the time of the snapshot")), \
                    self.assertRaisesRegex(DeskError, "snapshot clean is saved, but: its clock"):
                snapshot.save(run, "clean", force=True)
            self.assertEqual([s["name"] for s in snapshot.listing(run)], ["clean"])
            self.assertEqual([p.name for p in (run.dir / "snapshots").iterdir()], ["clean"])

    def test_half_a_snapshot_is_not_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Run(tmp)
            snapshot.save(run, "clean")
            (run.dir / "snapshots" / "clean" / "meta.json").unlink()  # a save cut off before its end
            (run.dir / "disk.qcow2").write_text("later")
            with self.assertRaisesRegex(DeskError, "no snapshot clean"):
                snapshot.restore(run, "clean")
            self.assertFalse(run.destroyed)
            self.assertEqual((run.dir / "disk.qcow2").read_text(), "later")
            self.assertEqual(snapshot.listing(run), [])

    def test_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Run(tmp)
            snapshot.save(run, "clean")
            (run.dir / "disk.qcow2").write_text("later")
            snapshot.restore(run, "clean")
            self.assertTrue(run.destroyed)
            self.assertEqual((run.dir / "disk.qcow2").read_text(), "disk")


if __name__ == "__main__":
    unittest.main()
