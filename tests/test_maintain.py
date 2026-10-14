import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from desk import images, maintain
from desk.config import DeskError, load_image

DAY = 86400


class Families(unittest.TestCase):
    def test_variants_go_with_their_base(self):
        fams = maintain.families()
        self.assertEqual(fams["fedora-44-gnome"][0], "fedora-44-gnome")
        self.assertIn("fedora-44-cockpit", fams["fedora-44-gnome"])
        self.assertIn("fedora-44-gnome-pl", fams["fedora-44-gnome"])
        self.assertNotIn("fedora-44-cockpit", fams)
        self.assertEqual(fams["windows-11"], ["windows-11"])


class Due(unittest.TestCase):
    """What makes `desk maintain` reinstall an image or warm it up again."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        for target in ("desk.maintain.BASE", "desk.config.BASE"):
            self.patch(target, self.home / "base")
        for target in ("desk.maintain.WARM", "desk.config.WARM"):
            self.patch(target, self.home / "warm")
        self.spec = load_image("windows-11")

    def patch(self, target, value):
        p = mock.patch(target, value)
        p.start()
        self.addCleanup(p.stop)

    def build(self, root: str, age_days: float = 0, **meta) -> Path:
        d = self.home / root / f"windows-11.2026100{len(list((self.home / root).glob('*.*'))) + 1}-030000" \
            if (self.home / root).exists() else self.home / root / "windows-11.20261001-030000"
        d.mkdir(parents=True)
        (d / "disk.qcow2").write_bytes(b"")
        (d / "meta.json").write_text(json.dumps({"created": time.time() - age_days * DAY, **meta}))
        images.promote(self.home / root / "windows-11", d)
        return d

    def test_base(self):
        self.assertEqual(maintain.base_reasons(self.spec), ["not built"])
        self.build("base", fingerprint=images.install_fingerprint(self.spec))
        self.assertEqual(maintain.base_reasons(self.spec), [])
        self.build("base", fingerprint="of another definition")
        self.assertEqual(maintain.base_reasons(self.spec), ["install definition changed"])

    def test_base_by_age(self):
        fedora = load_image("fedora-44-gnome")
        d = self.home / "base" / "fedora-44-gnome.20261001-030000"
        d.mkdir(parents=True)
        (d / "disk.qcow2").write_bytes(b"")
        (d / "meta.json").write_text(json.dumps({"created": time.time() - 20 * DAY,
                                                 "fingerprint": images.install_fingerprint(fedora)}))
        images.promote(self.home / "base" / "fedora-44-gnome", d)
        self.assertEqual(maintain.base_reasons(fedora), ["base is 20 days old"])

    def test_licence(self):
        self.build("base", fingerprint=images.install_fingerprint(self.spec))
        warm = self.build("warm", expires=time.time() + 5 * DAY)
        (warm / "memory.save").write_bytes(b"")
        self.assertEqual(maintain.base_reasons(self.spec), ["licence runs out in 5 days"])
        self.assertEqual(maintain.check_usable(self.spec),
                         ["the licence of windows-11 runs out in 5 days (desk maintain reinstalls it)"])

    def test_an_expired_licence_stops_runs(self):
        warm = self.build("warm", expires=time.time() - 3 * DAY)
        (warm / "memory.save").write_bytes(b"")
        with self.assertRaisesRegex(DeskError, "ran out 3 days ago"):
            maintain.check_usable(self.spec)

    def test_warm(self):
        self.assertEqual(maintain.warm_reasons(self.spec), ["no warm state"])
        base = self.build("base")
        warm = self.build("warm", base=str(base), fingerprint=images.warm_fingerprint(self.spec))
        self.assertEqual(maintain.warm_reasons(self.spec), ["no warm state"])  # no saved memory yet
        (warm / "memory.save").write_bytes(b"")
        self.assertEqual(maintain.warm_reasons(self.spec), [])
        self.build("base")
        self.assertEqual(maintain.warm_reasons(self.spec), ["base build changed"])

    def test_only_images_in_use_here_are_looked_after(self):
        family = ["windows-11", "windows-11-pl"]
        # Nothing built: a timer must not install it; naming it does.
        self.assertEqual(maintain.selected("windows-11", family, None), [])
        self.assertEqual(maintain.selected("windows-11", family, "windows-11"), ["windows-11"])
        self.assertEqual(maintain.selected("windows-11", family, "windows-11-pl"), ["windows-11-pl"])
        # Installed, but no warm state anywhere: nobody uses it yet.
        self.build("base")
        self.assertEqual(maintain.selected("windows-11", family, None), [])
        # In use: renewed, and only the variants that are in use with it.
        self.build("warm")
        self.assertEqual(maintain.selected("windows-11", family, None), ["windows-11"])
        self.assertEqual(maintain.selected("windows-11", family, "windows-11-pl"), family)

    def test_a_failed_maintenance_is_shown_until_a_newer_warm_state(self):
        warm = self.build("warm", age_days=2)
        (warm / "memory.save").write_bytes(b"")
        failed = {"windows-11": {"maintain": {"t": time.time() - DAY, "ok": False, "note": "selftest of windows-11 failed"}}}
        with mock.patch("desk.maintain.load_status", return_value=failed):
            self.assertIn("selftest of windows-11 failed", maintain.check_usable(self.spec)[0])
            self.build("warm")
            self.assertEqual(maintain.check_usable(self.spec), [])


if __name__ == "__main__":
    unittest.main()
