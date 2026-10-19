import tempfile
import unittest
from pathlib import Path

from desk import disks
from desk.config import DeskError


class Dom:
    def __init__(self, xml: str):
        self.xml = xml

    def XMLDesc(self, _flags):
        return self.xml


class Run:
    def __init__(self, d):
        self.dir = Path(d)


class Parse(unittest.TestCase):
    def test_size_and_name(self):
        self.assertEqual(disks.parse("10G"), ("10G", None))
        self.assertEqual(disks.parse("512M:backup-2"), ("512M", "backup-2"))
        for bad in ("", "10", "0G", "10g", "ten", "10G:Backup", "10G:with space", "10G:" + "x" * 21, "1G:-a"):
            with self.subTest(bad=bad), self.assertRaises(DeskError):
                disks.parse(bad)


class Remove(unittest.TestCase):
    def test_only_names_of_extra_disks(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base" / "disk.qcow2"
            base.parent.mkdir()
            base.write_bytes(b"")
            run = Run(Path(tmp) / "runs" / "r1")
            disks.directory(run).mkdir(parents=True)
            for bad in ("../../../base/disk", "disk1.qcow2", ""):
                with self.subTest(bad=bad), self.assertRaisesRegex(DeskError, "disk names are"):
                    disks.remove(run, bad)
            self.assertTrue(base.exists())


class Attached(unittest.TestCase):
    def test_only_this_runs_extra_disks(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Run(tmp)
            mine = disks.directory(run) / "backup.qcow2"
            xml = ("<domain><devices>"
                   f"<disk device='disk'><source file='{run.dir / 'disk.qcow2'}'/><target dev='vda'/></disk>"
                   + disks._xml(mine, "backup", "vdb") + "<disk device='cdrom'><target dev='sda'/></disk>"
                   "</devices></domain>")
            self.assertEqual(disks._attached(run, Dom(xml)), {"backup": "vdb"})
            self.assertIn("<serial>backup</serial>", disks._xml(mine, "backup", "vdb"))


if __name__ == "__main__":
    unittest.main()
