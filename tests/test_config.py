import unittest

from desk.config import load_image


class Variants(unittest.TestCase):
    def test_variant_inherits_base(self):
        base = load_image("fedora-44-gnome")
        pl = load_image("fedora-44-gnome-pl")
        self.assertEqual(pl.base_disk, base.base_disk)
        self.assertEqual(pl.base_xml, base.base_xml)
        self.assertNotEqual(pl.warm_dir, base.warm_dir)
        self.assertEqual((pl.user, pl.ready, pl.relogin, pl.layout), (base.user, base.ready, base.relogin, base.layout))

    def test_variant_paths_stay_relative_to_their_image(self):
        pl = load_image("fedora-44-gnome-pl")
        self.assertEqual(pl.provision_scripts, ["../fedora-44-gnome/provision.sh", "provision.sh"])
        for script in pl.provision_scripts:
            self.assertTrue((pl.dir / script).is_file(), script)
        for d in pl.files:
            self.assertTrue((pl.dir / d / "desk-helper@desk.local" / "extension.js").is_file(), d)

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

        pl = load_image("fedora-44-gnome-pl")
        self.assertEqual(pl.base_dir, BASE / "fedora-44-gnome")
        self.assertEqual(pl.warm_dir, WARM / "fedora-44-gnome-pl")
        built = replace(pl, base_version="20261005-030000", warm_version="20261005-040000")
        self.assertEqual(built.base_disk, BASE / "fedora-44-gnome.20261005-030000" / "disk.qcow2")
        self.assertEqual(built.warm_dir, WARM / "fedora-44-gnome-pl.20261005-040000")


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
