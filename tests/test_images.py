import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

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
    def test_default_settings_do_not_count(self):
        spec = load_image("fedora-44-gnome")
        # As if secure_boot were a new setting: its default must not change existing images.
        self.assertTrue(spec.secure_boot)
        self.assertNotIn("secure_boot", images._settings(spec, images.INSTALL_KEYS))
        self.assertIn("secure_boot", images._settings(replace(spec, secure_boot=False), images.INSTALL_KEYS))

    def test_versions_do_not_change_fingerprints(self):
        spec = load_image("fedora-44-gnome-pl")
        built = replace(spec, base_version="20261005-030000", warm_version="20261005-040000")
        self.assertEqual(images.warm_fingerprint(spec), images.warm_fingerprint(built))
        self.assertEqual(images.install_fingerprint(spec), images.install_fingerprint(built))

    def test_maintenance_settings_are_not_guest_state(self):
        spec = load_image("windows-11")
        self.assertEqual(images.warm_fingerprint(spec), images.warm_fingerprint(replace(spec, expiry=None)))
        self.assertNotEqual(images.warm_fingerprint(spec), images.warm_fingerprint(replace(spec, sound=None)))

    def test_install_ignores_files_provisioning_refreshes(self):
        from unittest import mock

        spec = load_image("windows-11")
        # The agent travels on the answer-file CD and is copied again at every warm-up.
        self.assertIn("../_shared/windows", spec.unattend_files)
        with mock.patch.object(images, "_hash_files") as hashed:
            images.install_fingerprint(spec)
        rels = hashed.call_args.args[2]
        self.assertIn("firstboot.ps1", rels)
        self.assertNotIn("../_shared/windows", rels)


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


if __name__ == "__main__":
    unittest.main()
