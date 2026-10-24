import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from desk import images, maintain
from desk.config import Busy, DeskError, load_image

DAY = 86400


class Families(unittest.TestCase):
    def test_variants_go_with_their_base(self):
        fams = maintain.families()
        self.assertEqual(fams["fedora-44-gnome"][0], "fedora-44-gnome")
        self.assertIn("fedora-44-cockpit", fams["fedora-44-gnome"])
        self.assertNotIn("fedora-44-cockpit", fams)
        self.assertEqual(fams["windows-11"], ["windows-11"])

    def test_a_definition_that_cannot_be_read_does_not_stop_the_others(self):
        def load(name):
            if name == "arch-gnome":
                raise DeskError("images/arch-gnome/image.toml: not valid TOML")
            return load_image(name)

        with mock.patch("desk.maintain.load_image", load):
            broken = {}
            fams = maintain.families(broken)
            self.assertEqual(broken, {"arch-gnome": "images/arch-gnome/image.toml: not valid TOML"})
            self.assertIn("fedora-44-gnome", fams)
            self.assertNotIn("arch-gnome", fams)
            with self.assertRaises(DeskError):
                maintain.families()
            # The unattended run goes on with the rest and ends as failed: desk's failure, not a check's.
            with mock.patch("desk.maintain.selected", return_value=[]), mock.patch("desk.maintain._log"), \
                    self.assertRaisesRegex(DeskError, "not every image"):
                maintain.maintain(dry_run=True)


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
        made = dict(base=str(base), fingerprint=images.warm_fingerprint(self.spec), timezone=self.spec.timezone,
                    layout=self.spec.layout)
        warm = self.build("warm", **made)
        self.assertEqual(maintain.warm_reasons(self.spec), ["no warm state"])  # no saved memory yet
        (warm / "memory.save").write_bytes(b"")
        # Made before cold runs booted the provisioned disk.
        self.assertEqual(maintain.warm_reasons(self.spec), ["no provisioned disk for cold runs"])
        (warm / images.PROVISIONED).write_bytes(b"")
        self.assertEqual(maintain.warm_reasons(self.spec), [])
        # config.toml or the host's zone changed since.
        meta = json.loads((warm / "meta.json").read_text())
        (warm / "meta.json").write_text(json.dumps({**meta, "timezone": "Asia/Kolkata"}))
        self.assertEqual(maintain.warm_reasons(self.spec),
                         [f"guests get {self.spec.timezone} and the {self.spec.layout} layout now"])
        (warm / "meta.json").write_text(json.dumps({k: v for k, v in meta.items() if k not in ("timezone", "layout")}))
        self.assertEqual(maintain.warm_reasons(self.spec), ["made before desk recorded its guest's time zone and layout"])
        (warm / "meta.json").write_text(json.dumps(meta))
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


class ExitCode(unittest.TestCase):
    """1 is a selftest that failed; what kept desk from looking after an image is desk's."""

    def maintain(self, failure: Exception):
        with mock.patch("desk.maintain.families", return_value={"x": ["x"]}), \
                mock.patch("desk.maintain.selected", return_value=["x"]), \
                mock.patch("desk.maintain._family", side_effect=failure), \
                mock.patch("desk.maintain._log"):
            return maintain.maintain(dry_run=True)

    def test_a_failed_selftest_is_a_failed_check(self):
        self.assertFalse(self.maintain(maintain.SelftestFailed("selftest of x failed on the new build")))

    def test_what_kept_desk_from_it_is_not(self):
        for failure in (Busy("not enough free memory for x"), DeskError("download failed")):
            with self.subTest(failure=failure), self.assertRaisesRegex(DeskError, "not every image"):
                self.maintain(failure)


class BusyMember(unittest.TestCase):
    """A member with nothing new whose warm state another run holds does not cost the others theirs."""

    def family(self, new_variant: bool):
        from contextlib import nullcontext

        def selftest(m, reboot, spec, shots_dir):
            if spec.warm_version:
                return True
            raise Busy(f"warm state of {m} is in use by another run")

        warm = {"fedora-44-gnome": [], "fedora-44-cockpit": ["definition changed"] if new_variant else []}
        with mock.patch("desk.maintain.runs.image_lock", return_value=nullcontext()), \
                mock.patch("desk.maintain.runs.check_memory"), \
                mock.patch("desk.maintain.base_reasons", return_value=[]), \
                mock.patch("desk.maintain.warm_reasons", side_effect=lambda spec: warm[spec.name]), \
                mock.patch("desk.maintain.images.warm", return_value="20261019-000000"), \
                mock.patch("desk.maintain.images.promote") as promote, \
                mock.patch("desk.selftest.run_selftest", side_effect=selftest):
            lines = maintain._family("fedora-44-gnome", ["fedora-44-gnome", "fedora-44-cockpit"], Path("/x"), False)
        return lines, promote

    def test_the_new_build_of_another_member_is_promoted(self):
        lines, promote = self.family(new_variant=True)
        self.assertIn("- fedora-44-gnome: selftest skipped", "\n".join(lines))
        self.assertEqual(promote.call_count, 1)

    def test_nothing_tested_at_all_is_skipped(self):
        with self.assertRaises(Busy):
            self.family(new_variant=False)


class SelftestStart(unittest.TestCase):
    def test_a_host_without_room_is_no_result_rather_than_a_failed_one(self):
        from desk import selftest

        with mock.patch("desk.selftest.runs.up", side_effect=Busy("not enough free memory for fedora-44-gnome")), \
                mock.patch("desk.maintain.record") as record, \
                self.assertRaises(Busy):
            selftest.run_selftest("fedora-44-gnome", spec=load_image("fedora-44-gnome"))
        record.assert_not_called()

    def test_a_warm_state_another_run_holds_is_not_passed_over_by_a_cold_boot(self):
        from types import SimpleNamespace

        from desk import selftest

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "memory.save").write_bytes(b"")
            spec = SimpleNamespace(family="linux", base_xml=Path(tmp) / "memory.save", warm_dir=Path(tmp),
                                   warm_version=None)
            held = "warm state of x is in use by another run"
            with mock.patch("desk.selftest.runs.cold_reason", return_value=held), \
                    mock.patch("desk.selftest.runs.warm_is_current", return_value=True), \
                    mock.patch("desk.selftest.runs.up") as up, mock.patch("desk.maintain.record") as record, \
                    self.assertRaisesRegex(Busy, "in use by another run"):
                selftest.run_selftest("x", spec=spec)
        up.assert_not_called()
        record.assert_not_called()

    def test_a_warm_state_taken_while_waiting_to_start_is_not_the_images_failure(self):
        from types import SimpleNamespace

        from desk import selftest

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "memory.save").write_bytes(b"")
            spec = SimpleNamespace(name="x", family="linux", base_xml=Path(tmp) / "memory.save", warm_dir=Path(tmp),
                                   warm_version=None)
            cold = mock.Mock(id="ab12cd", domain="desk-ab12cd")
            with mock.patch("desk.selftest.runs.cold_reason", return_value=None), \
                    mock.patch("desk.selftest.runs.warm_is_current", return_value=True), \
                    mock.patch("desk.selftest.runs.up", return_value=cold), \
                    mock.patch("desk.selftest.runs.down") as down, mock.patch("desk.maintain.record") as record, \
                    self.assertRaisesRegex(Busy, "taken by another run"):
                selftest.run_selftest("x", spec=spec)
        down.assert_called_once_with(cold)
        record.assert_not_called()


if __name__ == "__main__":
    unittest.main()
