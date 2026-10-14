import unittest

from desk import doctor
from desk.config import list_images, load_image


class Host(unittest.TestCase):
    def test_family(self):
        self.assertEqual(doctor.host_family('NAME="Fedora Linux"\nID=fedora\nVERSION_ID=44\n'), "fedora")
        self.assertEqual(doctor.host_family('NAME="Arch Linux"\nID=arch\n'), "arch")
        self.assertEqual(doctor.host_family('NAME="CachyOS Linux"\nID=cachyos\nID_LIKE=arch\n'), "arch")
        self.assertEqual(doctor.host_family('ID="rocky"\nID_LIKE="rhel centos fedora"\n'), "fedora")
        self.assertIsNone(doctor.host_family("ID=debian\n"))

    def test_install_command(self):
        needs = [doctor.CURL, *doctor.INSTALLERS["windows"][1:], doctor.FEATURES[-1]]
        self.assertEqual(doctor.install_command("arch", needs), "sudo pacman -S --needed 7zip curl gst-plugins-base "
                         "gst-plugins-good gstreamer libisoburn")
        self.assertEqual(doctor.install_command("fedora", needs), "sudo dnf install 7zip curl gstreamer1 "
                         "gstreamer1-plugins-base gstreamer1-plugins-good xorriso")
        both = doctor.install_command(None, needs)
        self.assertIn("pacman -S --needed 7zip", both)
        self.assertIn("dnf install 7zip", both)

    def test_every_need_names_a_package_for_both(self):
        needs = doctor.RUNS + doctor.BUILDS + doctor.FEATURES + [n for v in doctor.INSTALLERS.values() for n in v]
        for n in needs:
            with self.subTest(need=n.what):
                self.assertTrue(n.arch and n.fedora and n.why)

    def test_every_installer_is_known(self):
        # A new kind of install must say what it needs on the host (or that it needs nothing more).
        kinds = {load_image(name).installer for name in list_images()}
        self.assertLessEqual(kinds, set(doctor.INSTALLERS) | {"kickstart"})


if __name__ == "__main__":
    unittest.main()
