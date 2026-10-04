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
        ub = load_image("ubuntu-26.04-gnome")
        self.assertEqual(ub.installer, "autoinstall")
        self.assertTrue((ub.dir / ub.user_data).is_file())


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


if __name__ == "__main__":
    unittest.main()
