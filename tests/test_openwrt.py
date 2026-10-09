import json
import tarfile
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from desk import images, scenario
from desk.config import DeskError, load_image
from desk.evidence import openwrt_sections
from desk.guest import remote_command

# parted -sm DISK unit s print, for OpenWrt's x86-64 combined image grown to 1 GB
PARTED = """BYT;
/tmp/disk.img:2097152s:file:512:512:gpt::;
128:34s:511s:478s:::bios_grub;
1:512s:33279s:32768s:fat16::legacy_boot;
2:33280s:2097118s:2063839s:ext4::;
"""


class Image(unittest.TestCase):
    def test_headless_openwrt(self):
        spec = load_image("openwrt-25.12")
        self.assertEqual((spec.installer, spec.family, spec.desktop, spec.shell), ("disk", "openwrt", False, "sh"))
        self.assertTrue((spec.dir / spec.disk_files / "etc/uci-defaults/99-desk").is_file())
        with self.assertRaisesRegex(DeskError, "has none"):
            spec.need_desktop("desk a11y")
        load_image("fedora-44-gnome").need_desktop("desk a11y")
        self.assertEqual(load_image("fedora-44-gnome").shell, "bash")

    def test_disk_files_are_part_of_the_install(self):
        spec = load_image("openwrt-25.12")
        self.assertNotEqual(images.install_fingerprint(spec), images.install_fingerprint(replace(spec, disk_files=None)))

    def test_checksum_from_sha256sums(self):
        sums = "aa *openwrt-x86-64-generic-ext4-rootfs.img.gz\nBB *openwrt-x86-64-generic-ext4-combined-efi.img.gz\n"
        self.assertEqual(images.checksum_for(sums, "openwrt-x86-64-generic-ext4-combined-efi.img.gz"), "bb")
        with self.assertRaises(DeskError):
            images.checksum_for(sums, "other.img.gz")

    def test_grows_the_partition_that_ends_last(self):
        self.assertEqual(images.last_partition(PARTED), 2)

    def test_disk_files_filled_in_and_owned_by_root(self):
        spec = load_image("openwrt-25.12")
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "id_ed25519"
            key.with_suffix(".pub").write_text("ssh-ed25519 AAAA desk\n")
            with mock.patch.object(images, "SSH_KEY", key):
                images._disk_files_tar(spec, Path(tmp) / "files.tar")
            with tarfile.open(Path(tmp) / "files.tar") as tar:
                keys = tar.getmember("etc/dropbear/authorized_keys")
                self.assertEqual((keys.uid, keys.gid), (0, 0))
                self.assertEqual(tar.extractfile(keys).read().decode().strip(), "ssh-ed25519 AAAA desk")
                defaults = tar.extractfile("etc/uci-defaults/99-desk").read().decode()
                self.assertIn("echo 'root'", defaults)
                self.assertNotIn("@PASSWORD@", defaults)


class Commands(unittest.TestCase):
    def test_time_limit_without_bash_or_timeout(self):
        cmd = remote_command(["sleep 20"], env={"A": "b"}, limit=3, shell="sh")
        self.assertNotIn("bash", cmd)
        self.assertNotIn("timeout", cmd)
        self.assertIn("setsid sh -c", cmd)
        self.assertIn("env A=b sh -c", cmd)

    def test_bash_keeps_timeout(self):
        self.assertTrue(remote_command(["true"], limit=3).startswith("timeout -k 5 3 bash -c"))

    def test_scenario_artifacts_go_to_tmp(self):
        self.assertEqual(scenario.GUEST_IN["openwrt"], "/tmp/desk")


class Report(unittest.TestCase):
    def test_sections(self):
        services = {
            "crashy": {"instances": {"instance1": {"running": False, "exit_code": 3}}},
            "urandom_seed": {"instances": {"urandom_seed": {"running": False, "exit_code": 0}}},
            "dnsmasq": {"instances": {"cfg01": {"running": True, "pid": 1}}},
            "cron": {},
        }
        log = """\
Sat Oct 10 00:50:00 2026 daemon.err root: before the run
Sat Oct 10 01:06:02 2026 daemon.err root: crashy: giving up
Sat Oct 10 01:06:03 2026 daemon.info procd: Instance crashy::instance1 s in a crash loop 4 crashes, 0 seconds since last crash
Sat Oct 10 01:06:32 2026 kern.err kernel: [    0.386099] Speculative Return Stack Overflow: WARNING
Sat Oct 10 01:06:33 2026 daemon.warn netifd: Wireless module not found
Sat Oct 10 01:07:00 2026 kern.info kernel: [   40.1] foo[123]: segfault at 0 ip 00 sp 00 error 4
"""
        # 2026-10-10 01:00:00 in Warsaw (CEST)
        out = openwrt_sections({"services": json.dumps(services), "log": log, "packages": "installed nano-9.2-r1"},
                               since=1791586800)
        self.assertEqual(out["failed"], "crashy/instance1: not running (exit 3)")
        self.assertEqual(out["journal"], "2026-10-10T01:06:02+02:00 daemon.err root: crashy: giving up")
        self.assertIn("crash loop", out["unitfailures"])
        self.assertIn("segfault at", out["coredumps"])
        self.assertEqual(out["packages"], "installed nano-9.2-r1")


if __name__ == "__main__":
    unittest.main()
