import contextlib
import io
import subprocess
import unittest
from dataclasses import replace
from unittest import mock

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


class QemuDevices(unittest.TestCase):
    def setUp(self):
        doctor._qemu_devices.cache_clear()
        self.addCleanup(doctor._qemu_devices.cache_clear)

    def test_usb_host_is_advertised(self):
        with mock.patch.object(doctor.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, 'name "usb-host", bus USB\n')) as probe:
            self.assertTrue(doctor.USB_HOST.present())
        probe.assert_called_once_with(["qemu-system-x86_64", "-device", "help"], capture_output=True,
                                      text=True, timeout=30)

    def test_usb_controller_is_not_host_passthrough(self):
        with mock.patch.object(doctor.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, 'name "qemu-xhci", bus PCI\n')):
            self.assertFalse(doctor.USB_HOST.present())

    def test_missing_command_and_failed_probe_are_not_present(self):
        outcomes = [FileNotFoundError("qemu-system-x86_64"),
                    subprocess.TimeoutExpired("qemu-system-x86_64", 30),
                    subprocess.CompletedProcess([], 1, 'name "usb-host", bus USB\n', "probe failed")]
        for outcome in outcomes:
            with self.subTest(outcome=outcome):
                doctor._qemu_devices.cache_clear()
                kwargs = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
                with mock.patch.object(doctor.subprocess, "run", **kwargs):
                    self.assertFalse(doctor.USB_HOST.present())

    def test_only_usb_missing_is_optional_and_names_install_packages(self):
        present = lambda: True
        with mock.patch.object(doctor, "_qemu_devices", return_value='name "qemu-xhci"\n'), \
             mock.patch.object(doctor, "RUNS", [replace(n, present=present) for n in doctor.RUNS]), \
             mock.patch.object(doctor, "BUILDS", [replace(n, present=present) for n in doctor.BUILDS]), \
             mock.patch.object(doctor, "FEATURES", [doctor.USB_HOST if n is doctor.USB_HOST
                                                   else replace(n, present=present) for n in doctor.FEATURES]), \
             mock.patch.object(doctor, "storage", return_value=([], [])), \
             mock.patch.object(doctor, "libvirt_connection", return_value=("libvirt available", None)), \
             mock.patch.object(doctor, "kvm", return_value=None), \
             mock.patch.object(doctor, "images", return_value=([], {})), \
             mock.patch.object(doctor, "timer_line", return_value="not configured"), \
             mock.patch.object(doctor, "guest_settings", return_value="defaults"):
            for family, package in (("arch", "qemu-hw-usb-host"), ("fedora", "qemu-device-usb-host")):
                with self.subTest(family=family), mock.patch.object(doctor, "host_family", return_value=family), \
                     contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertTrue(doctor.run())
                    self.assertIn("missing for single features:", output.getvalue())
                    self.assertIn(doctor.install_command(family, [doctor.USB_HOST]), output.getvalue())
                    self.assertIn(package, output.getvalue())


if __name__ == "__main__":
    unittest.main()
