import errno
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from desk import cli, guest
from desk.cli import _text_args, explain


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
    def wait(self, code: int):
        run = mock.Mock(ssh_port=1, user="tester")
        answer = subprocess.CompletedProcess([], code, b"", b"Connection refused" if code == 255 else b"")
        args = SimpleNamespace(timeout=0, root=False, until_exec="test -f /tmp/x")
        with mock.patch("desk.cli.runs.spec_for"), mock.patch("desk.cli.guest.command", return_value="x"), \
                mock.patch("desk.cli.guest.ssh_run", return_value=answer):
            cli._wait_exec(run, args)

    def test_a_guest_that_never_answered_is_not_a_failed_check(self):
        with self.assertRaisesRegex(guest.NoConnection, "Connection refused"):
            self.wait(255)

    def test_a_command_that_kept_failing_is(self):
        with self.assertRaises(SystemExit) as e:
            self.wait(1)
        self.assertEqual(e.exception.code, 1)


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
