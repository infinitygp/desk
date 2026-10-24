"""The single-file release: built from the checkout, unpacked on first start, installed and
updated without the repository."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Stands in for `gh release download ... --output FILE`, handing out $FAKE_RELEASE.
FAKE_GH = """#!/bin/sh
while [ $# -gt 0 ]; do [ "$1" = "--output" ] && out=$2; shift; done
cp "$FAKE_RELEASE" "$out"
"""


class Release(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.pyz = cls.build("v9.9.9-test")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def build(cls, version: str, name: str = "desk.pyz") -> Path:
        out = cls.dir / name
        subprocess.run([sys.executable, str(ROOT / "scripts" / "build.py"), "--version", version, "--output", str(out)],
                       check=True, capture_output=True)
        return out

    def desk(self, launcher: Path, *args: str, home: Path, **env) -> subprocess.CompletedProcess:
        environ = {**os.environ, "HOME": str(home), "XDG_CACHE_HOME": str(home / ".cache"),
                   "XDG_CONFIG_HOME": str(home / ".config"), "PATH": f"{home / 'bin'}:{os.environ['PATH']}", **env}
        environ.pop("DESK_LAUNCHER", None)
        # The HTTPS download fails at once (nothing listens on port 9), so updates come from the fake gh.
        environ.update(https_proxy="http://127.0.0.1:9", HTTPS_PROXY="http://127.0.0.1:9", no_proxy="", NO_PROXY="")
        return subprocess.run([sys.executable, str(launcher), *args], capture_output=True, text=True, env=environ,
                              timeout=120)

    def home(self) -> Path:
        home = Path(tempfile.mkdtemp(dir=self.dir))
        (home / ".claude").mkdir()
        (home / "bin").mkdir()
        (home / "bin" / "gh").write_text(FAKE_GH)
        (home / "bin" / "gh").chmod(0o755)
        return home

    def test_contents(self):
        with zipfile.ZipFile(self.pyz) as z:
            names = set(z.namelist())
            build = json.loads(z.read("BUILD.json"))
            provision = z.getinfo("images/desktops/gnome/provision.sh")
        self.assertLessEqual({"__main__.py", "desk/cli.py", "desk/guest_a11y.py", "images/distros/windows-11/block.toml",
                              "images/distros/windows-11/agent/agent.ps1", "skill/SKILL.md"}, names)
        self.assertFalse([n for n in names if n.startswith(("tests/", ".git", "dist/")) or "__pycache__" in n])
        self.assertEqual(build["version"], "v9.9.9-test")
        self.assertRegex(build["repo"], r"^[\w.-]+/[\w.-]+$")
        self.assertTrue(provision.external_attr >> 16 & 0o111)  # a script keeps its mode
        self.assertTrue(self.pyz.read_bytes().startswith(b"#!/usr/bin/python3\n"))

    def test_the_build_id_covers_the_entry_point(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("build", ROOT / "scripts" / "build.py")
        build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        tree = self.dir / "tree"
        (tree / "scripts").mkdir(parents=True)
        (tree / "desk").mkdir()
        (tree / "desk" / "cli.py").write_text("code\n")
        (tree / "scripts" / "pyz_main.py").write_text("entry\n")
        before = build.build_id(tree, ["desk/cli.py"])
        (tree / "scripts" / "pyz_main.py").write_text("entry, changed\n")
        self.assertNotEqual(build.build_id(tree, ["desk/cli.py"]), before)

    def test_runs_unpacked_with_the_files_modes(self):
        home = self.home()
        r = self.desk(self.pyz, "--version", home=home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"desk v9.9.9-test (release ", r.stdout)
        self.assertIn(str(self.pyz), r.stdout)
        [unpacked] = (home / ".cache" / "desk").iterdir()
        self.assertTrue(os.access(unpacked / "images/desktops/gnome/provision.sh", os.X_OK))

    def test_install_and_update_without_the_repository(self):
        home = self.home()
        launcher = home / ".local" / "bin" / "desk"
        launcher.parent.mkdir(parents=True)
        launcher.write_bytes(self.pyz.read_bytes())
        r = self.desk(launcher, "install", "--quiet", home=home)
        self.assertEqual(r.returncode, 0, r.stderr)
        skill = home / ".agents" / "skills" / "desk" / "SKILL.md"
        self.assertEqual(skill.read_text(), (ROOT / "skill" / "SKILL.md").read_text())
        self.assertFalse(skill.parent.is_symlink())  # a copy: the unpacked build is pruned some day
        self.assertEqual((home / ".claude" / "skills" / "desk").resolve(), skill.parent)

        same = self.desk(launcher, "update", home=home, FAKE_RELEASE=str(self.pyz))
        self.assertIn("is the newest release", same.stdout, same.stderr)
        broken = self.dir / "broken.pyz"
        broken.write_text("<html>rate limited</html>")
        r = self.desk(launcher, "update", home=home, FAKE_RELEASE=str(broken))
        self.assertEqual(r.returncode, 2)
        self.assertIn("is not a desk release", r.stderr)
        self.assertEqual(sorted(p.name for p in launcher.parent.iterdir()), ["desk"])  # no download left behind

        # A later release, whose skill says something else and has one file less.
        self.assertTrue((skill.parent / "openwrt.md").exists())
        rebuilt = self.dir / "newer.pyz"
        with zipfile.ZipFile(self.pyz) as src, zipfile.ZipFile(rebuilt, "w") as dst:
            for item in src.infolist():
                if item.filename == "skill/openwrt.md":
                    continue
                data = src.read(item.filename)
                if item.filename == "BUILD.json":
                    data = json.dumps({**json.loads(data), "version": "v9.9.10-test", "id": "newer-build"}).encode()
                elif item.filename == "skill/SKILL.md":
                    data = b"the newer skill\n"
                dst.writestr(item, data)
        rebuilt.write_bytes(b"#!/usr/bin/python3\n" + rebuilt.read_bytes())
        r = self.desk(launcher, "update", home=home, FAKE_RELEASE=str(rebuilt))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("-> v9.9.10-test", r.stdout)
        self.assertIn("v9.9.10-test", self.desk(launcher, "--version", home=home).stdout)
        self.assertEqual(skill.read_text(), "the newer skill\n")
        self.assertFalse((skill.parent / "openwrt.md").exists())

    def test_without_libvirt_it_says_what_to_install(self):
        home = self.home()
        blocker = home / "nolibvirt"
        blocker.mkdir()
        (blocker / "libvirt.py").write_text("raise ModuleNotFoundError(name='libvirt')\n")
        r = self.desk(self.pyz, "--version", home=home, PYTHONPATH=str(blocker))
        self.assertEqual(r.returncode, 2)
        self.assertIn("sudo dnf install python3-libvirt", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_another_desk_on_path_is_left_alone(self):
        home = self.home()
        other = home / ".local" / "bin" / "desk"
        other.parent.mkdir(parents=True)
        other.write_text("#!/bin/sh\necho another desk\n")
        r = self.desk(self.pyz, "install", "--quiet", home=home)
        self.assertIn("is another desk", r.stdout)
        self.assertEqual(other.read_text(), "#!/bin/sh\necho another desk\n")

    def test_a_checkout_links_and_is_updated_by_git(self):
        home = self.home()
        r = self.desk(ROOT / "bin" / "desk", "install", "--quiet", home=home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((home / ".local" / "bin" / "desk").resolve(), ROOT / "bin" / "desk")
        self.assertEqual((home / ".agents" / "skills" / "desk").resolve(), ROOT / "skill")
        r = self.desk(ROOT / "bin" / "desk", "update", home=home)
        self.assertEqual(r.returncode, 2)
        self.assertIn("git pull", r.stderr)


class Download(unittest.TestCase):
    """desk update fetches over HTTPS and turns to gh only when that fails."""

    def setUp(self):
        from desk import install

        self.install = install
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name) / "desk.pyz"

    def response(self, *chunks: bytes):
        from unittest import mock

        r = mock.MagicMock()
        r.__enter__.return_value.read.side_effect = [*chunks, b""]
        return r

    def test_https_first_without_gh(self):
        from unittest import mock

        with mock.patch("urllib.request.urlopen", return_value=self.response(b"PK", b"zip")) as urlopen, \
                mock.patch("subprocess.run") as run:
            self.install._download("owner/desk", self.out)
        self.assertEqual(urlopen.call_args.args[0], "https://github.com/owner/desk/releases/latest/download/desk.pyz")
        run.assert_not_called()
        self.assertEqual(self.out.read_bytes(), b"PKzip")

    def test_gh_when_https_fails(self):
        import urllib.error
        from unittest import mock

        missing = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        self.addCleanup(missing.close)
        done = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch("urllib.request.urlopen", side_effect=missing), \
                mock.patch("shutil.which", return_value="/usr/bin/gh"), mock.patch("subprocess.run", return_value=done) as run:
            self.install._download("owner/desk", self.out)
        self.assertEqual(run.call_args.args[0][:5], ["gh", "release", "download", "--repo", "owner/desk"])

    def test_both_failing_names_both(self):
        import urllib.error
        from unittest import mock

        from desk.config import DeskError

        refused = urllib.error.URLError("Connection refused")
        failed = subprocess.CompletedProcess([], 1, "", "HTTP 401: Bad credentials")
        with mock.patch("urllib.request.urlopen", side_effect=refused), \
                mock.patch("shutil.which", return_value="/usr/bin/gh"), mock.patch("subprocess.run", return_value=failed), \
                self.assertRaises(DeskError) as e:
            self.install._download("owner/desk", self.out)
        self.assertIn("Connection refused", str(e.exception))
        self.assertIn("Bad credentials", str(e.exception))
        with mock.patch("urllib.request.urlopen", side_effect=refused), mock.patch("shutil.which", return_value=None), \
                self.assertRaises(DeskError) as e:
            self.install._download("owner/desk", self.out)
        self.assertIn("gh auth login", str(e.exception))

    def test_a_download_that_trickles_ends_at_the_deadline(self):
        from unittest import mock

        with mock.patch("urllib.request.urlopen", return_value=self.response(b"a", b"b", b"c")), \
                mock.patch("time.monotonic", side_effect=[0, 1, 400, 401]), self.assertRaises(TimeoutError):
            self.install._fetch("https://example.org/desk.pyz", self.out, limit=300)


class Unpack(unittest.TestCase):
    def test_an_old_build_another_desk_removes_meanwhile_is_skipped(self):
        import importlib.util
        from unittest import mock

        spec = importlib.util.spec_from_file_location("pyz_main", ROOT / "scripts" / "pyz_main.py")
        pyz_main = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pyz_main)
        with tempfile.TemporaryDirectory() as tmp:
            archive, cache = Path(tmp) / "desk.pyz", Path(tmp) / "cache"
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("BUILD.json", json.dumps({"id": "new"}))
            (cache / "old").mkdir(parents=True)
            stat = Path.stat

            def gone(path, *args, **kwargs):
                if path.name == "old":
                    raise FileNotFoundError(path)
                return stat(path, *args, **kwargs)

            with mock.patch.object(Path, "stat", gone):
                self.assertEqual(pyz_main.unpack(archive, cache), cache / "new")

    def test_an_unpack_cut_off_leaves_nothing_behind(self):
        import errno
        import importlib.util
        from unittest import mock

        spec = importlib.util.spec_from_file_location("pyz_main", ROOT / "scripts" / "pyz_main.py")
        pyz_main = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pyz_main)
        with tempfile.TemporaryDirectory() as tmp:
            archive, cache = Path(tmp) / "desk.pyz", Path(tmp) / "cache"
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("BUILD.json", json.dumps({"id": "new"}))
            with mock.patch.object(zipfile.ZipFile, "extract", side_effect=OSError(errno.ENOSPC, "No space left")), \
                    self.assertRaises(OSError):
                pyz_main.unpack(archive, cache)
            self.assertEqual(list(cache.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
