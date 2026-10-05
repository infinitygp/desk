import tempfile
import unittest
from pathlib import Path

from desk import snapshot
from desk.config import DeskError


class Run:
    def __init__(self, d):
        self.dir = Path(d)


class Names(unittest.TestCase):
    def test_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Run(tmp)
            self.assertEqual(snapshot._dir(run, "after-install.v2"), run.dir / "snapshots" / "after-install.v2")
            for bad in ("", "../escape", "a/b", "-dash", "x" * 65, "spa ce"):
                with self.subTest(bad=bad), self.assertRaises(DeskError):
                    snapshot._dir(run, bad)
            self.assertEqual(snapshot.listing(run), [])


if __name__ == "__main__":
    unittest.main()
