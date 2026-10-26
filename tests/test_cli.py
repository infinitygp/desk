import errno
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from desk import cli, config, guest
from desk.cli import _cpus_arg, _memory_arg, _text_args, explain


class Size(unittest.TestCase):
    def test_memory(self):
        self.assertEqual((_memory_arg("8G"), _memory_arg("1536M")), (8192, 1536))
        for text in ("8", "0G", "8GB", "1.5G", "-1G"):
            with self.subTest(text=text), self.assertRaises(cli.argparse.ArgumentTypeError):
                _memory_arg(text)

    def test_cpus_up_to_the_hosts(self):
        with mock.patch("desk.cli.os.cpu_count", return_value=8):
            self.assertEqual(_cpus_arg("8"), 8)
            for text in ("0", "9", "x", "-2"):
                with self.subTest(text=text), self.assertRaises(cli.argparse.ArgumentTypeError):
                    _cpus_arg(text)


class TextArgs(unittest.TestCase):
    def test_text_starting_with_a_dash_is_text(self):
        self.assertEqual(_text_args(["type", "---"]), ["type", "--", "---"])
        self.assertEqual(_text_args(["-r", "abc", "type", "--shot", "-v"]), ["-r", "abc", "type", "--shot", "--", "-v"])
        self.assertEqual(_text_args(["type", "--settle", "2", "-x"]), ["type", "--settle", "2", "--", "-x"])
        self.assertEqual(_text_args(["set-text", "@e3", "-5"]), ["set-text", "@e3", "--", "-5"])

    def test_everything_else_untouched(self):
        for argv in (["type", "--", "-a"], ["type", "abc"], ["click", "--name", "-x"], ["type", "--anywhere", "-"],
                     ["type", "-"]):
            with self.subTest(argv=argv):
                self.assertEqual(_text_args(argv), argv)


class WaitUntilExec(unittest.TestCase):
    def wait(self, code: int, probe: int = 0):
        run = mock.Mock(ssh_port=1, user="tester")
        answers = [subprocess.CompletedProcess([], c, b"", b"Connection refused" if c == 255 else b"")
                   for c in (code, probe)]
        args = SimpleNamespace(timeout=0, root=False, until_exec="test -f /tmp/x")
        with mock.patch("desk.cli.runs.spec_for"), mock.patch("desk.cli.guest.command", return_value="x"), \
                mock.patch("desk.cli.guest.ssh_run", side_effect=answers):
            cli._wait_exec(run, args)

    def test_a_guest_that_never_answered_is_not_a_failed_check(self):
        with self.assertRaisesRegex(guest.NoConnection, "Connection refused"):
            self.wait(255, probe=255)

    def test_a_command_that_kept_failing_is(self):
        for code in (1, 255):  # 255 too when the guest answers otherwise: the program's own
            with self.subTest(code=code), self.assertRaises(SystemExit) as e:
                self.wait(code)
            self.assertEqual(e.exception.code, 1)


class Exec(unittest.TestCase):
    def exec(self, code: int, probe: int = 0) -> int:
        run = mock.Mock(ssh_port=1, user="tester")
        answers = [subprocess.CompletedProcess([], c, b"", b"Connection refused" if c == 255 else b"")
                   for c in (code, probe)]
        args = SimpleNamespace(run=None, timeout=None, command=["--", "true"], agent=False, root=False, gui=False,
                               wait=False, env=None)
        with mock.patch("desk.cli.runs.resolve", return_value=run), mock.patch("desk.cli.runs.spec_for"), \
                mock.patch("desk.cli.guest.command", return_value="x"), \
                mock.patch("desk.cli.guest.ssh_run", side_effect=answers), self.assertRaises(SystemExit) as e:
            cli.cmd_exec(args)
        return e.exception.code

    def test_a_guest_that_does_not_answer_is_desks_failure(self):
        with self.assertRaisesRegex(guest.NoConnection, "exec 'true': no SSH connection.*Connection refused"):
            self.exec(255, probe=255)

    def test_the_commands_own_code_goes_through(self):
        self.assertEqual(self.exec(255), 255)
        self.assertEqual(self.exec(3), 3)

    def test_a_code_that_does_not_fit_an_exit_status_is_said_and_fails(self):
        # Windows' codes: PowerShell's for a missing -File would otherwise end as 0, a success.
        for code, said in ((-196608, "0xFFFD0000"), (3221225781, "0xC0000135")):
            with self.subTest(code=code), mock.patch("sys.stderr") as err:
                self.assertEqual(cli._exit_status(code), 1)
            self.assertIn(f"exit code {code} ({said}) does not fit", "".join(c.args[0] for c in err.write.call_args_list))
        self.assertEqual(cli._exit_status(255), 255)


class ScenarioFrom(unittest.TestCase):
    def test_from_0_is_refused_not_a_fresh_run(self):
        args = SimpleNamespace(file="a.toml", action="run", from_step=0, run=None, image=None, keep=False)
        data = {"image": "a", "step": [{}]}
        with mock.patch("desk.cli.runs.resolve", return_value=mock.Mock(image="a")), \
                mock.patch("desk.scenario.load", return_value=data), \
                mock.patch("desk.scenario.load_image", return_value=mock.Mock()) as image, \
                mock.patch("desk.scenario.run_scenario") as fresh, \
                self.assertRaisesRegex(cli.DeskError, "--from 0"):
            image.return_value.name = "a"
            cli.cmd_scenario(args)
        fresh.assert_not_called()


class Failures(unittest.TestCase):
    def test_what_desk_relies_on_gets_one_line(self):
        self.assertEqual(explain(subprocess.CalledProcessError(1, ["qemu-img", "create"], stderr=b"No space left\n")),
                         "qemu-img failed (exit 1): No space left")
        self.assertEqual(explain(subprocess.TimeoutExpired(["ssh", "-p", "1"], 60)),
                         "the guest did not answer over SSH within 60s")
        self.assertIn("guestfish is not installed", explain(FileNotFoundError(errno.ENOENT, "No such file", "guestfish")))
        self.assertEqual(explain(FileNotFoundError(errno.ENOENT, "No such file or directory", "/tmp/x.rpm")),
                         "No such file or directory: /tmp/x.rpm")
        self.assertIn("no space left", explain(OSError(errno.ENOSPC, "No space left on device")))
        self.assertIsNone(explain(KeyError("a bug")))

    def run_main(self, error: BaseException) -> int:
        def fail(_args):
            raise error
        parser = mock.Mock()
        parser.parse_args.return_value = mock.Mock(func=fail)
        with mock.patch("desk.cli.build_parser", return_value=parser), mock.patch("sys.stderr"), \
                self.assertRaises(SystemExit) as stopped:
            cli.main(["ls"])
        return stopped.exception.code

    def test_exit_1_is_kept_for_a_failed_check(self):
        self.assertEqual(self.run_main(subprocess.CalledProcessError(1, ["cp"])), 2)
        self.assertEqual(self.run_main(KeyError("a bug")), 2)
        self.assertEqual(self.run_main(KeyboardInterrupt()), 130)
        # What a command decides itself goes through: a failed scenario, a guest command's code.
        self.assertEqual(self.run_main(SystemExit(1)), 1)
        self.assertEqual(self.run_main(SystemExit(124)), 124)


class Down(unittest.TestCase):
    def test_a_run_whose_vm_is_gone_is_reported_before_its_files_go(self):
        import json
        import tempfile
        import time
        from pathlib import Path

        from desk import evidence

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            (run_dir / "shots").mkdir(parents=True)
            (run_dir / "shots" / "0001.png").write_bytes(b"png")
            (run_dir / "log.jsonl").write_text(json.dumps({"t": time.time(), "shot": str(run_dir / "shots" / "0001.png")})
                                               + "\n")
            run = mock.Mock(id="gone", dir=run_dir, image="fedora-44-gnome", created=time.time() - 60, ready=0.0)
            run.live_domain.return_value = None  # QEMU ended under it
            args = SimpleNamespace(id=None, run=None, all=False, keep=False, diff=False, no_report=False)
            with mock.patch.object(evidence, "REPORTS", Path(tmp) / "reports"), \
                    mock.patch("desk.cli.runs.resolve", return_value=run), mock.patch("sys.stdout"):
                cli.cmd_down(args)
            md = (Path(tmp) / "reports" / "gone" / "report.md").read_text()
            self.assertFalse(run_dir.exists())
        self.assertIn("the VM was not running", md)
        self.assertIn("shots/0001.png", md)


class Copy(unittest.TestCase):
    def copy(self, rc: int, family: str = "linux"):
        run = mock.Mock(ssh_port=1, user="tester")
        args = SimpleNamespace(run=None, root=False, local="a.rpm", remote="/tmp/", timeout=600)
        with mock.patch("desk.cli.runs.resolve", return_value=run), \
                mock.patch("desk.cli.runs.spec_for", return_value=SimpleNamespace(family=family)), \
                mock.patch("desk.cli.guest.scp", return_value=rc), mock.patch("sys.stderr"):
            cli._copy(args, push=True)

    def test_a_failed_copy_is_desks_failure_not_a_failed_check(self):
        with self.assertRaisesRegex(cli.DeskError, r"copying failed \(scp exit 1\); its folder may not exist, or.*--root"):
            self.copy(1)

    def test_the_timeline_has_the_exit_code_of_desk(self):
        run = mock.Mock(ssh_port=1, user="tester")
        args = SimpleNamespace(run=None, root=False, local="a.rpm", remote="/tmp/", timeout=600)
        with mock.patch("desk.cli.runs.resolve", return_value=run), mock.patch("desk.cli.runs.spec_for"), \
                mock.patch("desk.cli.guest.scp", return_value=1), self.assertRaises(cli.DeskError):
            cli._copy(args, push=True)
        run.log_event.assert_called_once_with(rc=2)

    def test_windows_is_not_told_of_root(self):
        # tester is an administrator there, and --root changes nothing.
        with self.assertRaises(cli.DeskError) as e:
            self.copy(1, family="windows")
        self.assertEqual(str(e.exception), "copying failed (scp exit 1); its folder may not exist")

    def test_running_out_of_time_is_124(self):
        with self.assertRaises(SystemExit) as e:
            self.copy(124)
        self.assertEqual(e.exception.code, 124)


class ImageCommands(unittest.TestCase):
    def setUp(self):
        import shutil
        import tempfile
        from pathlib import Path

        self.images = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.images)
        patch = mock.patch("desk.config.IMAGES", self.images)
        patch.start()
        self.addCleanup(patch.stop)

    def desk(self, *argv: str) -> tuple[int, str, str]:
        import io
        from contextlib import redirect_stderr, redirect_stdout

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                cli.main(list(argv))
                code = 0
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_add_show_set_rm(self):
        code, out, _ = self.desk("image", "add", "fedora-44", "gnome", "--addon", "cockpit", "--memory", "6G")
        self.assertEqual(code, 0)
        self.assertIn("added fedora-44-gnome, whose install fedora-44-gnome-cockpit runs on", out)
        self.assertIn("desk maintain --only fedora-44-gnome-cockpit", out)
        code, out, _ = self.desk("image", "show", "fedora-44-gnome-cockpit")
        self.assertIn("memory_mib = 6144\n", out)
        self.assertIn("provision = [shared/desktop.sh, desktops/gnome/provision.sh, addons/cockpit/provision.sh]\n", out)
        self.assertIn('agent_wrapper = ["/usr/local/libexec/desk-ga-run"]\n', out)
        self.assertIn('base = "fedora-44-gnome"\n', out)
        self.assertEqual(self.desk("image", "set", "fedora-44-gnome", "--disk", "60G")[0], 0)
        code, _, err = self.desk("image", "set", "fedora-44-gnome-cockpit", "--disk", "60G")
        self.assertEqual(code, 2)
        self.assertIn("set disk_gb there", err)
        self.assertEqual(self.desk("image", "set", "fedora-44-gnome")[0], 2)
        code, out, _ = self.desk("image", "rm", "fedora-44-gnome-cockpit")
        self.assertEqual(code, 0)
        warm = config.WARM / "fedora-44-gnome-cockpit"
        self.assertIn(f"its builds stay until deleted by hand ({warm}, {warm}.*)\n", out)
        self.assertEqual(sorted(p.name for p in self.images.iterdir()), ["fedora-44-gnome.toml"])
        code, out, _ = self.desk("image", "rm", "fedora-44-gnome")
        base, warm = config.BASE / "fedora-44-gnome", config.WARM / "fedora-44-gnome"
        self.assertIn(f"({base}, {base}.*, {warm}, {warm}.*)\n", out)
        # A variant whose file is broken has warm builds only all the same.
        self.assertEqual(self.desk("image", "add", "fedora-44", "gnome", "--addon", "cockpit")[0], 0)
        (self.images / "fedora-44-gnome-cockpit.toml").write_text("distro = \n")
        code, out, _ = self.desk("image", "rm", "fedora-44-gnome-cockpit")
        warm = config.WARM / "fedora-44-gnome-cockpit"
        self.assertIn(f"({warm}, {warm}.*)\n", out)

    def test_a_broken_definition_hides_no_other(self):
        self.assertEqual(self.desk("image", "add", "arch", "gnome")[0], 0)
        (self.images / "fedora-44-twm.toml").write_text('distro = "fedora-44"\ndesktop = "twm"\n')
        (self.images / "fedora-44-gnome.toml").write_text('distro = "fedora-44"\ndesktop = 5\n')
        code, out, _ = self.desk("image", "list")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"arch-gnome +- ")
        self.assertRegex(out, r"fedora-44-twm +- .*broken definition: fedora-44 needs one of the desktops gnome")
        self.assertRegex(out, r"fedora-44-gnome +- .*broken definition: .*desktop is the name of a block")

    def test_shot_of_an_install_is_of_that_image(self):
        import shutil
        import tempfile

        self.assertEqual(self.desk("image", "add", "fedora-44", "gnome")[0], 0)
        doms = [mock.Mock(**{"name.return_value": n, "isActive.return_value": True})
                for n in ("desk-install-fedora-44-gnome-xx-20261025-030135",
                          "desk-install-fedora-44-gnome-20261025-024826")]
        logs = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, logs)
        libvirt = mock.Mock(**{"listAllDomains.return_value": doms})
        with mock.patch("desk.virt.lookup", return_value=None), mock.patch("desk.virt.conn", return_value=libvirt), \
                mock.patch("desk.cli.display.screenshot", return_value=b"png") as shot, \
                mock.patch("desk.images.LOGS", logs):
            self.assertEqual(self.desk("image", "shot", "fedora-44-gnome")[0], 0)
            shot.assert_called_once_with(doms[1])
            libvirt.listAllDomains.return_value = doms[:1]
            code, _, err = self.desk("image", "shot", "fedora-44-gnome")
            self.assertEqual(code, 2)
            self.assertIn("no install of fedora-44-gnome is running", err)

    def test_what_blocks_do_not_make_is_refused(self):
        code, _, err = self.desk("image", "add", "windows-11", "gnome")
        self.assertEqual(code, 2)
        self.assertIn("windows-11 comes with its desktop", err)
        code, _, err = self.desk("up", "arch-gnome")
        self.assertEqual(code, 2)
        self.assertIn("define it with: desk image add arch gnome", err)

    def test_blocks(self):
        code, out, _ = self.desk("image", "blocks")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"distros\n(  .*\n)*  fedora-44 +Fedora 44\b.*\(desktops: gnome, kde\b")
        self.assertRegex(out, r"addons\n(  .*\n)*  cockpit +Cockpit.*\(distros: fedora-44\)")
        own = config.CONFIG_DIR / "addons" / "my-lang"
        own.mkdir(parents=True)
        self.addCleanup(own.rmdir)
        (own / "block.toml").touch()
        self.addCleanup((own / "block.toml").unlink)
        code, out, _ = self.desk("image", "blocks")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"  cockpit +Cockpit")
        self.assertRegex(out, r"  my-lang +refused: .*my-lang: the name of a desktop or an addon has no '-'")
        (own.parent / "xx").mkdir()
        self.addCleanup((own.parent / "xx").rmdir)
        (own.parent / "xx" / "block.toml").write_text("distros = \n")
        self.addCleanup((own.parent / "xx" / "block.toml").unlink)
        code, out, _ = self.desk("image", "blocks")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"  xx +broken: .*block.toml: not valid TOML")
