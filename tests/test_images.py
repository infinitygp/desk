import os
import shutil
import subprocess
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from desk import images
from desk.config import load_image


class Versions(unittest.TestCase):
    def test_versions_and_promote(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("fedora-44-gnome.20261005-030000", "fedora-44-gnome.20261019-030000",
                         "fedora-44-gnome-pl.20261005-030000", "fedora-44-gnome.broken"):
                (root / name).mkdir()
            found = [p.name for p in images.versions(root, "fedora-44-gnome")]
            self.assertEqual(found, ["fedora-44-gnome.20261005-030000", "fedora-44-gnome.20261019-030000"])
            link = root / "fedora-44-gnome"
            self.assertIsNone(images.current(link))
            images.promote(link, root / found[0])
            images.promote(link, root / found[1])
            self.assertEqual(images.current(link), (root / found[1]).resolve())
            self.assertEqual(os.readlink(link), found[1])
            self.assertEqual(images.versions(root, "fedora-44-gnome")[-1].name, found[1])

    def test_new_versions_count_in_utc(self):
        # 02:30 CEST and, an hour later, 02:10 CET: names in local time would go backwards.
        for when, name in ((1792888200, "20261025-003000"), (1792890600, "20261025-011000")):
            with self.subTest(name=name), mock.patch("desk.images.time.gmtime", return_value=time.gmtime(when)):
                self.assertEqual(images.new_version(), name)


class VersionsUnderSymlink(unittest.TestCase):
    def test_paths_compare_equal_through_a_symlinked_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "disk"
            real.mkdir()
            home = Path(tmp) / "vmlab"
            home.symlink_to(real)
            (home / "arch-gnome.20261005-030000").mkdir()
            images.promote(home / "arch-gnome", home / "arch-gnome.20261005-030000")
            self.assertEqual(images.versions(home, "arch-gnome"), [images.current(home / "arch-gnome")])


class Fingerprints(unittest.TestCase):
    def test_memory_and_cpus_are_a_new_warm_state_not_a_new_install(self):
        spec = load_image("fedora-44-gnome")
        bigger = replace(spec, memory_mib=8192, vcpus=8)
        self.assertEqual(images.install_fingerprint(bigger), images.install_fingerprint(spec))
        self.assertNotEqual(images.warm_fingerprint(bigger), images.warm_fingerprint(spec))

    def test_default_settings_do_not_count(self):
        spec = load_image("fedora-44-gnome")
        # As if secure_boot were a new setting: its default must not change existing images.
        self.assertTrue(spec.secure_boot)
        self.assertNotIn("secure_boot", images._settings(spec, images.INSTALL_KEYS))
        self.assertIn("secure_boot", images._settings(replace(spec, secure_boot=False), images.INSTALL_KEYS))

    def test_versions_do_not_change_fingerprints(self):
        spec = load_image("fedora-44-gnome-cockpit")
        built = replace(spec, base_version="20261005-030000", warm_version="20261005-040000")
        self.assertEqual(images.warm_fingerprint(spec), images.warm_fingerprint(built))
        self.assertEqual(images.install_fingerprint(spec), images.install_fingerprint(built))

    def test_maintenance_settings_are_not_guest_state(self):
        spec = load_image("windows-11")
        self.assertEqual(images.warm_fingerprint(spec), images.warm_fingerprint(replace(spec, expiry=None)))
        self.assertNotEqual(images.warm_fingerprint(spec), images.warm_fingerprint(replace(spec, sound=None)))

    def test_install_scripts_are_part_of_the_install(self):
        from unittest import mock

        spec = load_image("arch-gnome")
        self.assertEqual([Path(s).relative_to(spec.dir.parent.parent).as_posix() for s in spec.install],
                         ["distros/arch/install.sh", "desktops/gnome/install.sh"])
        with mock.patch.object(images, "_hash_files") as hashed:
            images.install_fingerprint(spec)
        self.assertLessEqual(set(spec.install), set(hashed.call_args.args[2]))
        self.assertNotEqual(images.install_fingerprint(spec), images.install_fingerprint(replace(spec, install=spec.install[:1])))

    def test_install_ignores_files_provisioning_refreshes(self):
        from unittest import mock

        spec = load_image("windows-11")
        # The agent travels on the answer-file CD and is copied again at every warm-up.
        agent, firstboot = (str(spec.dir / f) for f in ("agent", "firstboot.ps1"))
        self.assertIn(agent, spec.unattend_files)
        with mock.patch.object(images, "_hash_files") as hashed:
            images.install_fingerprint(spec)
        rels = hashed.call_args.args[2]
        self.assertIn(firstboot, rels)
        self.assertNotIn(agent, rels)


class GuestSettings(unittest.TestCase):
    """The time zone and layout reach the guest through provisioning, so they are its warm state."""

    def test_a_new_zone_or_layout_is_a_new_warm_state_not_a_new_install(self):
        spec = replace(load_image("fedora-44-gnome"), timezone="UTC", layout="us")
        for other in (replace(spec, timezone="Asia/Kolkata"), replace(spec, layout="pl")):
            self.assertEqual(images.install_fingerprint(other), images.install_fingerprint(spec))
            self.assertNotEqual(images.warm_fingerprint(other), images.warm_fingerprint(spec))

    def test_posix_rules_for_openwrt(self):
        self.assertEqual(images.posix_tz("Europe/Warsaw"), "CET-1CEST,M3.5.0,M10.5.0/3")
        self.assertEqual(images.posix_tz("UTC"), "UTC0")
        with tempfile.TemporaryDirectory() as tmp, mock.patch("zoneinfo.TZPATH", (tmp,)), \
                self.assertRaisesRegex(images.DeskError, "no TZif file with a POSIX rule for Europe/Warsaw"):
            images.posix_tz("Europe/Warsaw")
        with tempfile.TemporaryDirectory() as tmp, mock.patch("zoneinfo.TZPATH", (tmp,)):
            (Path(tmp) / "Old").write_bytes(b"TZif\0" + b"\0" * 40)  # version 1: no footer
            with self.assertRaises(images.DeskError):
                images.posix_tz("Old")

    def provisioned(self, spec) -> list[str]:
        ok = subprocess.CompletedProcess([], 0, b"", b"")
        with mock.patch("desk.images.guest.ssh_run", return_value=ok) as ssh, \
                mock.patch("desk.images.guest.scp", return_value=0):
            images.provision(spec, 1, relogin=False)
        return [c.args[2] for c in ssh.call_args_list]

    def test_linux_scripts_get_them_in_their_environment(self):
        spec = replace(load_image("fedora-44-gnome"), timezone="Asia/Kolkata", layout="pl")
        commands = [c for c in self.provisioned(spec) if c.endswith(" -s")]
        self.assertEqual(commands, ["DESK_TIMEZONE=Asia/Kolkata DESK_LAYOUT=pl bash -s"] * 2)
        # OpenWrt has no zone files: the rule comes along.
        openwrt = replace(load_image("openwrt-25.12"), timezone="Asia/Kolkata", layout="us")
        commands = [c for c in self.provisioned(openwrt) if c.endswith(" -s")]
        self.assertEqual(commands, ["DESK_TIMEZONE=Asia/Kolkata DESK_LAYOUT=us DESK_POSIX_TZ=IST-5:30 sh -s"])

    def test_windows_scripts_get_them_in_their_environment(self):
        spec = replace(load_image("windows-11"), timezone="America/New_York", layout="us")
        [command] = [c for c in self.provisioned(spec) if "provision.ps1" in c]
        self.assertTrue(command.startswith("$env:DESK_TIMEZONE = 'America/New_York'; $env:DESK_LAYOUT = 'us'; "
                                           "powershell.exe "), command)


class Fetch(unittest.TestCase):
    SHA = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"  # of b"hello"

    def spec(self, **changes):
        return replace(load_image("windows-11"), openssh_msi="ssh.msi", openssh_msi_sha256=self.SHA, **changes)

    def test_a_cached_file_is_checked_and_not_downloaded(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "ssh.msi").write_bytes(b"hello")
            with mock.patch.object(images, "CACHE", Path(tmp)), mock.patch.object(images, "_download") as download:
                self.assertEqual(images._fetch(self.spec(), "openssh_msi", "openssh_msi"), Path(tmp) / "ssh.msi")
                download.assert_not_called()
                (Path(tmp) / "ssh.msi").write_bytes(b"other")
                with self.assertRaisesRegex(images.DeskError, "openssh_msi_sha256"):
                    images._fetch(self.spec(), "openssh_msi", "openssh_msi")

    def test_a_missing_file_is_downloaded_into_a_new_cache(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            with mock.patch.object(images, "CACHE", cache), \
                    mock.patch.object(images, "_download", side_effect=lambda url, to: to.write_bytes(b"hello")) as download:
                images._fetch(self.spec(openssh_msi_url="https://example.org/ssh.msi"), "openssh_msi", "openssh_msi")
                download.assert_called_once_with("https://example.org/ssh.msi", cache / "ssh.msi")
                with self.assertRaisesRegex(images.DeskError, "no virtio_win_url"):
                    images._fetch(self.spec(virtio_win_url=None), "virtio_win_iso", "virtio_win")

    def test_windows_names_where_its_files_come_from(self):
        spec = load_image("windows-11")
        for source in ("iso", "virtio_win", "openssh_msi"):
            self.assertTrue(getattr(spec, f"{source}_url").startswith("https://"), source)
            self.assertRegex(getattr(spec, f"{source}_sha256"), r"^[0-9a-f]{64}$")
        # Naming the source of the same files is no reason to install or warm up again.
        bare = replace(spec, virtio_win_url=None, virtio_win_sha256=None, openssh_msi_url=None, openssh_msi_sha256=None)
        self.assertEqual(images.install_fingerprint(spec), images.install_fingerprint(bare))
        self.assertEqual(images.warm_fingerprint(spec), images.warm_fingerprint(bare))

    def test_a_cloud_image_is_checked_against_the_sums_published_with_it(self):
        import hashlib

        data = b"image"
        for sums_file, algorithm, line in (("SHA512SUMS", "sha512", "{}  debian.qcow2"),
                                           ("SHA256SUMS", "sha256", "{} *debian.qcow2"), (None, "sha256", "{}")):
            spec = replace(load_image("arch-gnome"), cloud_image_url="https://example.org/d/debian.qcow2",
                           cloud_image_sums=sums_file)
            sums = line.format(hashlib.new(algorithm, data).hexdigest()) + "\n"
            with self.subTest(sums=sums_file), tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.object(images, "CACHE", Path(tmp)), mock.patch.object(images, "_download") as download, \
                    mock.patch("desk.images.subprocess.run", return_value=subprocess.CompletedProcess([], 0, sums, "")) as run:
                (Path(tmp) / "debian.qcow2").write_bytes(data)
                self.assertEqual(images._fetch_cloud_image(spec), Path(tmp) / "debian.qcow2")
                download.assert_not_called()
                self.assertEqual(run.call_args.args[0][-1], f"https://example.org/d/{sums_file}" if sums_file
                                 else "https://example.org/d/debian.qcow2.SHA256")


class Key(unittest.TestCase):
    def test_a_new_home_gets_its_key_pair(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "vmlab" / "keys" / "id_ed25519"
            with mock.patch.object(images, "SSH_KEY", key):
                pub = images._pubkey()
                self.assertTrue(pub.startswith("ssh-ed25519 "))
                self.assertEqual(key.stat().st_mode & 0o777, 0o600)
                self.assertEqual(key.parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual(sorted(p.name for p in key.parent.parent.iterdir()), ["keys"])
                # The key guests were installed with stays.
                self.assertEqual(images._pubkey(), pub)

    def test_an_existing_key_is_kept(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "keys" / "id_ed25519"
            key.parent.mkdir()
            key.with_suffix(".pub").write_text("ssh-ed25519 AAAA desk\n")
            with mock.patch.object(images, "SSH_KEY", key):
                self.assertEqual(images._pubkey(), "ssh-ed25519 AAAA desk\n")
            self.assertEqual(sorted(p.name for p in key.parent.iterdir()), ["id_ed25519.pub"])


class InstallProblem(unittest.TestCase):
    def test_the_console_decides(self):
        from desk.images import install_problem

        spec = load_image("windows-11")
        boot = "BdsDxe: starting Boot0004\r\n"
        self.assertIsNone(install_problem(spec, boot + "desk-firstboot: ok\r\n"))
        self.assertEqual(install_problem(spec, boot + "desk-firstboot: FAILED sshd is not running\r\n"),
                         "desk-firstboot: FAILED sshd is not running")
        self.assertIn("did not finish", install_problem(spec, boot))
        # Images without a marker are judged by powering off alone, as before.
        self.assertIsNone(install_problem(load_image("fedora-44-gnome"), ""))



@unittest.skipIf(shutil.which("curl") is None, "curl not installed")
class HttpsOnly(unittest.TestCase):
    def test_plain_http_is_refused_before_anything_is_fetched(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(images, "CACHE", Path(tmp)), \
                self.assertRaises(subprocess.CalledProcessError) as e:
            images._fetch_published("http://127.0.0.1:9/x.img", "http://127.0.0.1:9/x.img.SHA256", str.split)
        self.assertIn("protocol", e.exception.stderr.lower())  # curl's "Protocol "http" not supported or disabled"


if __name__ == "__main__":
    unittest.main()
