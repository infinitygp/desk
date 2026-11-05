import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from desk import config, derive, guest, images, runs
from desk.config import DeskError

EXPIRES = time.time() + 40 * 86400


class Save(unittest.TestCase):
    """`desk image save`: no VM, no qemu-img; a run whose disk is a file."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = home = Path(tmp.name)
        self.events = []
        self.live = True
        self.locks = {}
        (home / "images").mkdir()
        (home / "images" / "windows-11.toml").write_text('distro = "windows-11"\n')
        self.base = home / "base" / "windows-11.20261001-030000"
        self.base.mkdir(parents=True)
        (self.base / "domain.xml").write_text("<domain>of the install</domain>")
        (self.base / "disk.qcow2").write_text("base")
        self.warm = home / "warm" / "windows-11.20261002-030000"
        self.warm.mkdir(parents=True)
        (self.warm / images.PROVISIONED).write_text("provisioned")
        (self.warm / "meta.json").write_text(json.dumps({"expires": EXPIRES}))
        (home / "warm" / "windows-11").symlink_to(self.warm.name)
        (home / "base" / "windows-11").symlink_to(self.base.name)
        self.dom = mock.Mock()
        for target, value in (
                ("desk.config.IMAGES", home / "images"), ("desk.config.BASE", home / "base"),
                ("desk.derive.BASE", home / "base"), ("desk.config.WARM", home / "warm"),
                ("desk.derive.WARM", home / "warm"), ("desk.runs.RUNS", home / "runs"), ("desk.runs.HOME", home),
                ("desk.derive.backing_files", lambda disk: self.chain),
                ("desk.derive.reflink_copy", shutil.copyfile),
                ("desk.derive.guest.ssh_run", self.ssh_run),
                ("desk.derive.images._power_off", self.power_off),
                ("desk.derive.subprocess.run", self.convert)):
            patch = mock.patch(target, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.run = self.make_run("xlu-run", "windows-11")
        self.chain = [self.run.dir / "disk.qcow2", self.warm / images.PROVISIONED, self.base / "disk.qcow2"]
        live = mock.patch.object(runs.Run, "live_domain", autospec=True,
                                 side_effect=lambda run: self.dom if self.live else None)
        live.start()
        self.addCleanup(live.stop)
        promote = images.promote

        def promoted(link, target):
            self.locks.update(promote=bool(runs._locked.get(home / ".lock")),
                              image=bool(runs._locked.get(home / ".lock-xlu")))
            promote(link, target)

        patch = mock.patch("desk.derive.images.promote", promoted)
        patch.start()
        self.addCleanup(patch.stop)

    def make_run(self, run_id: str, image: str) -> runs.Run:
        run = runs.Run(id=run_id, image=image, domain=f"desk-{run_id}", ssh_port=2222, user="tester", created=1.0,
                       timezone="Europe/Warsaw", layout="pl")
        run.dir.mkdir(parents=True)
        run.save()
        (run.dir / "disk.qcow2").write_text("overlay")
        (run.dir / "VARS.qcow2").write_text("the run's firmware variables")
        return run

    def ssh_run(self, port, user, command, **kwargs):
        self.events.append(("ssh", port, user, command))
        return subprocess.CompletedProcess([], self.ssh_code, b"", b"")

    ssh_code = 0

    def power_off(self, dom, spec, port, timeout=600, after=""):
        self.events.append(("poweroff", port, spec.cmd(spec.poweroff)))
        self.live = False

    def convert(self, argv, **kwargs):
        self.events.append(("convert", argv))
        self.locks.update(convert_image=bool(runs._locked.get(self.home / ".lock-xlu")),
                          convert_state=bool(runs._locked.get(self.home / ".lock")))
        Path(argv[-1]).write_text("flat")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    def save(self, name: str = "xlu", run: runs.Run | None = None) -> tuple[Path, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            build = derive.save(name, run or self.run)
        return build, out.getvalue()

    def builds(self) -> list[str]:
        return sorted(p.name for p in (self.home / "base").iterdir() if p.name.startswith("xlu"))

    def test_a_running_guest_is_shut_down_then_its_disk_flattened_into_an_image(self):
        build, out = self.save()
        spec = config.load_image("windows-11")
        self.assertEqual([e[0] for e in self.events], ["ssh", "poweroff", "convert"])
        self.assertEqual(self.events[0][1:], (2222, spec.root_user, spec.cmd(spec.sync)))
        self.assertEqual(self.events[1][1:], (2222, spec.cmd(spec.poweroff)))
        # The whole chain into one file without a backing file, and the run's own disk is the source.
        self.assertEqual(self.events[2][1], ["qemu-img", "convert", "-O", "qcow2", str(self.run.dir / "disk.qcow2"),
                                             str(build / "disk.qcow2")])
        self.assertEqual(build, self.home / "base" / build.name)
        self.assertRegex(build.name, r"xlu\.\d{8}-\d{6}")
        self.assertEqual((build / "disk.qcow2").read_text(), "flat")
        self.assertEqual((build / "disk.qcow2").stat().st_mode & 0o777, 0o444)
        self.assertEqual((build / "domain.xml").read_text(), "<domain>of the install</domain>")
        self.assertEqual((build / "VARS.qcow2").read_text(), "the run's firmware variables")
        meta = json.loads((build / "meta.json").read_text())
        self.assertEqual({k: meta[k] for k in ("image", "derived_from", "source_run", "source_base", "expires",
                                               "timezone", "layout")},
                         {"image": "xlu", "derived_from": "windows-11", "source_run": "xlu-run",
                          "source_base": str(self.base), "expires": EXPIRES, "timezone": "Europe/Warsaw",
                          "layout": "pl"})
        self.assertIn("fingerprint", meta)
        self.assertEqual(images.current(self.home / "base" / "xlu"), build.resolve())
        self.assertEqual((self.home / "images" / "xlu.toml").read_text().splitlines()[1:],
                         ['derived_from = "windows-11"'])
        spec = config.load_image("xlu")
        self.assertEqual((spec.derived_from, spec.base_dir.resolve(), spec.base_disk.resolve()),
                         ("windows-11", build.resolve(), build.resolve() / "disk.qcow2"))
        for said in (str(build / "disk.qcow2"), "xlu-run", "no backing file", str(self.home / "images" / "xlu.toml")):
            self.assertIn(said, out)
        self.assertRegex(out, r"saved image xlu from run xlu-run \(windows-11\) in \d+ s")
        # The run stays for the caller to take down once the image is whole.
        self.assertTrue(self.run.dir.exists())

    def test_the_locks(self):
        self.save()
        self.assertEqual(self.locks, {"convert_image": True, "convert_state": False, "promote": True, "image": True})
        self.assertFalse(runs._locked)

    def test_the_licence_clock_goes_on_from_the_image_it_came_from(self):
        build, _ = self.save()
        from desk import maintain

        self.assertAlmostEqual(maintain.days_left(config.load_image("xlu")), 40, delta=0.01)
        # An image that tracks none gives the copy none.
        (self.warm / "meta.json").write_text("{}")
        build, _ = self.save("xlu2")
        self.assertNotIn("expires", json.loads((build / "meta.json").read_text()))

    def test_a_run_that_is_off_is_not_asked_to_shut_down(self):
        self.live = False
        build, _ = self.save()
        self.assertEqual([e[0] for e in self.events], ["convert"])
        self.assertTrue((build / "disk.qcow2").exists())

    def test_the_domain_it_was_started_from_is_the_one_under_its_disk(self):
        # The image was built again since: the run still has the hardware of its own build.
        newer = self.home / "base" / "windows-11.20261009-030000"
        newer.mkdir()
        (newer / "domain.xml").write_text("<domain>of the newer install</domain>")
        (self.home / "base" / "windows-11").unlink()
        (self.home / "base" / "windows-11").symlink_to(newer.name)
        build, _ = self.save()
        self.assertEqual((build / "domain.xml").read_text(), "<domain>of the install</domain>")
        self.assertEqual(json.loads((build / "meta.json").read_text())["source_base"], str(self.base))
        # A chain that cannot be read: the image's current build.
        self.chain = []
        build, _ = self.save("xlu2")
        self.assertEqual((build / "domain.xml").read_text(), "<domain>of the newer install</domain>")

    def test_no_domain_definition_at_all_is_refused_before_the_guest_goes_down(self):
        (self.base / "domain.xml").unlink()
        with self.assertRaisesRegex(DeskError, "domain definition the run was started from is gone"):
            self.save()
        self.assertEqual(self.events, [])
        self.assertEqual(self.builds(), [])

    def test_a_derived_image_can_be_saved_from_again(self):
        build, _ = self.save()
        second = self.make_run("second", "xlu")
        self.chain = [second.dir / "disk.qcow2", build.resolve() / "disk.qcow2"]
        again, _ = self.save("xlu2", second)
        self.assertEqual((config.load_image("xlu2").derived_from, config.load_image("xlu2").family), ("xlu", "windows"))
        meta = json.loads((again / "meta.json").read_text())
        self.assertEqual((meta["derived_from"], meta["source_base"]), ("xlu", str(build.resolve())))
        # The licence clock is the one the first copy carries.
        self.assertEqual(meta["expires"], EXPIRES)

    def test_extra_disks_are_said_not_to_be_in_it(self):
        (self.run.dir / "disks").mkdir()
        (self.run.dir / "disks" / "backup.qcow2").write_text("")
        _, out = self.save()
        self.assertIn("not in the image: the run's extra disks (backup)", out)

    def test_a_guest_that_does_not_answer_is_left_running(self):
        self.ssh_code = 255
        with self.assertRaisesRegex(guest.NoConnection, "run xlu-run cannot be shut down cleanly"):
            self.save()
        self.assertEqual([e[0] for e in self.events], ["ssh"])
        self.assertTrue(self.live)
        self.assertEqual(self.builds(), [])

    def test_a_guest_that_hangs_on_the_way_down_is_not_copied(self):
        def hangs(*a, **k):
            raise DeskError("the guest did not power off within 600s after the shutdown was asked for")

        with mock.patch("desk.derive.images._power_off", hangs), self.assertRaisesRegex(DeskError, "did not power off"):
            self.save()
        self.assertEqual(self.builds(), [])
        self.assertFalse((self.home / "images" / "xlu.toml").exists())

    def test_a_copy_that_fails_leaves_no_half_an_image(self):
        def fails(argv, **kwargs):
            Path(argv[-1]).write_text("half")
            raise subprocess.CalledProcessError(1, argv, stderr=b"No space left on device")

        with mock.patch("desk.derive.subprocess.run", fails), self.assertRaises(subprocess.CalledProcessError):
            self.save()
        self.assertEqual(self.builds(), [])
        self.assertFalse((self.home / "images" / "xlu.toml").exists())
        self.assertTrue((self.run.dir / "disk.qcow2").exists())
        # The run is off now, so saving it again goes straight to the copy.
        build, _ = self.save()
        self.assertTrue(build.exists())

    def test_a_definition_that_cannot_be_written_takes_the_build_and_the_link_with_it(self):
        with mock.patch("desk.derive.config.write_image", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.save()
        self.assertEqual(self.builds(), [])
        self.assertFalse(os.path.lexists(self.home / "base" / "xlu"))

    def test_a_definition_that_does_not_load_is_taken_away_again(self):
        with mock.patch("desk.derive.config.load_image", side_effect=DeskError("broken")), \
                self.assertRaisesRegex(DeskError, "broken"):
            self.save()
        self.assertEqual(self.builds(), [])
        self.assertFalse((self.home / "images" / "xlu.toml").exists())

    def test_a_name_taken_while_it_was_copied_is_not_overwritten(self):
        def taken(argv, **kwargs):
            (self.home / "images" / "xlu.toml").write_text('derived_from = "windows-11"\n# somebody else\n')
            return self.convert(argv, **kwargs)

        with mock.patch("desk.derive.subprocess.run", taken), self.assertRaisesRegex(DeskError, "defined already"):
            self.save()
        self.assertIn("somebody else", (self.home / "images" / "xlu.toml").read_text())
        self.assertEqual(self.builds(), [])

    def test_a_run_without_its_disk_or_firmware_variables_has_nothing_to_save(self):
        (self.run.dir / "VARS.qcow2").unlink()
        with self.assertRaisesRegex(DeskError, "run xlu-run has no VARS.qcow2"):
            self.save()
        self.assertEqual(self.events, [])


class Names(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        (self.home / "images").mkdir()
        (self.home / "base").mkdir()
        (self.home / "warm").mkdir()
        for target, value in (("desk.config.IMAGES", self.home / "images"), ("desk.derive.BASE", self.home / "base"),
                              ("desk.derive.WARM", self.home / "warm")):
            patch = mock.patch(target, value)
            patch.start()
            self.addCleanup(patch.stop)

    def test_lower_case_letters_digits_and_dashes(self):
        for good in ("xlu", "win11-xlu", "a", "7zip-ready"):
            with self.subTest(good=good):
                derive.check_name(good)
        for bad in ("", "Xlu", "-xlu", "xlu-", "x lu", "x/lu", "../xlu", "xlu.1", "x_lu", "x" * 49):
            with self.subTest(bad=bad), self.assertRaisesRegex(DeskError, "image names are lower-case letters"):
                derive.check_name(bad)

    def test_a_defined_image_of_any_kind_is_not_replaced(self):
        (self.home / "images" / "windows-11.toml").write_text('distro = "windows-11"\n')
        (self.home / "images" / "xlu.toml").write_text('derived_from = "windows-11"\n')
        (self.home / "images" / "broken.toml").write_text("distro = \n")
        for name in ("windows-11", "xlu", "broken"):
            with self.subTest(name=name), self.assertRaisesRegex(DeskError, f"{name} is defined already"):
                derive.check_name(name)

    def test_the_name_of_an_image_that_blocks_make_is_not_free(self):
        with self.assertRaisesRegex(DeskError, "fedora-44-gnome is the name of an image that blocks make "
                                               r"\(desk image add fedora-44 gnome\)"):
            derive.check_name("fedora-44-gnome")
        # Close to one is fine while no addon of that name exists.
        derive.check_name("windows-11-xlu")

    def test_builds_left_by_an_image_that_was_removed_are_not_taken_over(self):
        (self.home / "base" / "xlu.20261001-030000").mkdir()
        with self.assertRaisesRegex(DeskError, "builds of an image called xlu are left in"):
            derive.check_name("xlu")
        shutil.rmtree(self.home / "base" / "xlu.20261001-030000")
        (self.home / "warm" / "xlu").symlink_to("nowhere")  # a link that leads nowhere is left too
        with self.assertRaisesRegex(DeskError, "builds of an image called xlu are left in"):
            derive.check_name("xlu")


if __name__ == "__main__":
    unittest.main()
