import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from desk.config import IMAGES_DIR, LOCAL_IMAGES, DeskError, list_images, load_image


class Variants(unittest.TestCase):
    def test_variant_inherits_base(self):
        base = load_image("fedora-44-gnome")
        ck = load_image("fedora-44-cockpit")
        self.assertEqual(ck.base_disk, base.base_disk)
        self.assertEqual(ck.base_xml, base.base_xml)
        self.assertNotEqual(ck.warm_dir, base.warm_dir)
        self.assertEqual((ck.user, ck.ready, ck.relogin, ck.layout), (base.user, base.ready, base.relogin, base.layout))

    def test_variant_paths_stay_relative_to_their_image(self):
        ck = load_image("fedora-44-cockpit")
        self.assertEqual(ck.provision_scripts, ["../fedora-44-gnome/provision.sh", "provision.sh"])
        for script in ck.provision_scripts:
            self.assertTrue((ck.dir / script).is_file(), script)
        for d in ck.files:
            self.assertTrue((ck.dir / d / "desk-helper@desk.local" / "extension.js").is_file(), d)

    def test_installers(self):
        self.assertEqual(load_image("fedora-44-gnome").installer, "kickstart")
        arch = load_image("arch-gnome")
        self.assertEqual(arch.installer, "cloudimg")
        self.assertTrue((arch.dir / arch.user_data).is_file())

    def test_prepare_script_is_part_of_the_warm_state(self):
        from desk import images

        ck = load_image("fedora-44-cockpit")
        self.assertEqual(ck.base, "fedora-44-gnome")
        self.assertTrue((ck.dir / ck.prepare).is_file())
        from dataclasses import replace

        self.assertNotEqual(images.warm_fingerprint(ck), images.warm_fingerprint(replace(ck, prepare=None)))

    def test_builds_are_versioned(self):
        from dataclasses import replace

        from desk.config import BASE, WARM

        ck = load_image("fedora-44-cockpit")
        self.assertEqual(ck.base_dir, BASE / "fedora-44-gnome")
        self.assertEqual(ck.warm_dir, WARM / "fedora-44-cockpit")
        built = replace(ck, base_version="20261005-030000", warm_version="20261005-040000")
        self.assertEqual(built.base_disk, BASE / "fedora-44-gnome.20261005-030000" / "disk.qcow2")
        self.assertEqual(built.warm_dir, WARM / "fedora-44-cockpit.20261005-040000")


class HostImages(unittest.TestCase):
    """Definitions of the host's own, in ~/.config/desk/images beside the shipped ones."""

    def define(self, name: str, toml: str, **files: str) -> None:
        d = LOCAL_IMAGES / name
        d.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, d)
        for f, text in {"image.toml": toml, **files}.items():
            (d / f).write_text(text)

    def test_a_variant_of_a_shipped_image(self):
        self.define("fedora-44-gnome-xx", 'base = "fedora-44-gnome"\n'
                    'provision = ["../fedora-44-gnome/provision.sh", "provision.sh"]\n', **{"provision.sh": "true\n"})
        self.assertIn("fedora-44-gnome-xx", list_images())
        xx = load_image("fedora-44-gnome-xx")
        self.assertEqual(xx.base_disk, load_image("fedora-44-gnome").base_disk)
        self.assertEqual([xx.path(s) for s in xx.provision_scripts],
                         [IMAGES_DIR / "fedora-44-gnome" / "provision.sh", LOCAL_IMAGES / "fedora-44-gnome-xx" / "provision.sh"])
        [files] = xx.files
        self.assertTrue((xx.path(files) / "desk-helper@desk.local" / "extension.js").is_file())

    def test_a_definition_moved_to_the_host_keeps_its_builds(self):
        from desk import images

        shipped = load_image("fedora-44-cockpit")
        expected = images.install_fingerprint(shipped), images.warm_fingerprint(shipped)
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        shutil.copytree(IMAGES_DIR, tmp / "shipped", ignore=shutil.ignore_patterns("fedora-44-cockpit"))
        shutil.copytree(IMAGES_DIR / "fedora-44-cockpit", tmp / "own" / "fedora-44-cockpit")
        roots = (tmp / "shipped", tmp / "own")
        with mock.patch("desk.config.IMAGE_ROOTS", roots), mock.patch("desk.images.IMAGE_ROOTS", roots):
            moved = load_image("fedora-44-cockpit")
            self.assertEqual(moved.dir, tmp / "own" / "fedora-44-cockpit")
            self.assertEqual(moved.path(moved.prepare), tmp / "own" / "fedora-44-cockpit" / "prepare.py")
            self.assertEqual((images.install_fingerprint(moved), images.warm_fingerprint(moved)), expected)

    def test_a_name_defined_twice_is_refused(self):
        self.define("fedora-44-gnome", 'base = "arch-gnome"\n')
        with self.assertRaisesRegex(DeskError, "defined twice"):
            load_image("fedora-44-gnome")


class WindowsCommands(unittest.TestCase):
    def test_linux_commands_untouched(self):
        spec = load_image("fedora-44-gnome")
        self.assertEqual(spec.cmd("ls -l"), "ls -l")

    def test_windows_commands(self):
        import base64

        spec = load_image("windows-11")
        command = spec.cmd('Write-Output "Zażółć"')
        self.assertTrue(command.endswith("; exit $LASTEXITCODE"))
        encoded = command.split("-EncodedCommand ")[1].split(";")[0]
        self.assertIn('Write-Output "Zażółć"', base64.b64decode(encoded).decode("utf-16-le"))

    def test_a_time_limit_is_kept_in_the_guest(self):
        import base64

        from desk import guest

        self.assertEqual(guest.command(load_image("fedora-44-gnome"), ["make check"], limit=90),
                         "DESK_CMD='make check' timeout -k 5 90 bash -c 'c=$DESK_CMD; unset DESK_CMD; eval \"$c\"'")
        self.assertIn("setsid sh -c", guest.command(load_image("openwrt-25.12"), ["apk update"], limit=90))
        command = guest.command(load_image("windows-11"), ["msiexec /i a.msi"], env={"WHO": "o'k"}, limit=90)
        script = base64.b64decode(command.split("-EncodedCommand ")[1].split(";")[0]).decode("utf-16-le")
        self.assertIn("$env:WHO = 'o''k'; msiexec /i a.msi", script)
        self.assertIn('"Start-Sleep 90; taskkill.exe /T /F /PID $PID"', script)
        self.assertIn("finally { Stop-Process -Id $deskWatch.Id", script)


if __name__ == "__main__":
    unittest.main()
