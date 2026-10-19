import socket
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from desk import guest, winagent
from desk.config import DeskError


class Limits(unittest.TestCase):
    def test_under_a_second_is_one_not_none(self):
        # 0 means no limit to timeout and systemd, and kills at once on Windows.
        self.assertIn("timeout -k 5 1 bash", guest.remote_command(["sleep 1000"], limit=0.3))
        self.assertIn("RuntimeMaxSec=1 ", guest.remote_command(["app"], gui=True, limit=0.3))
        self.assertIn("_ 1 $p", guest.remote_command(["sleep 1000"], limit=0.3, shell="sh"))
        windows = SimpleNamespace(family="windows", cmd=lambda script: script)
        self.assertIn("Start-Sleep 1;", guest.command(windows, ["Start-Sleep 1000"], limit=0.3))
        self.assertIn("timeout -k 5 90 bash", guest.remote_command(["make"], limit=90.4))


class Waits(unittest.TestCase):
    """A wait that runs out says what the last attempt said."""

    def failing(self, wait, answer, *args):
        with mock.patch("desk.guest.ssh_run", side_effect=answer), mock.patch("desk.guest.time.sleep"):
            with self.assertRaises(DeskError) as raised:
                wait(1, "tester", *args, timeout=0.05)
        return str(raised.exception)

    def test_ssh_refusing_the_key(self):
        refused = subprocess.CompletedProcess([], 255, b"", b"tester@127.0.0.1: Permission denied (publickey).\n")
        self.assertIn("Permission denied (publickey)", self.failing(guest.wait_ssh, lambda *a, **k: refused))

    def test_ssh_not_answering(self):
        def hang(*a, **k):
            raise subprocess.TimeoutExpired("ssh", 15)
        self.assertIn("did not answer", self.failing(guest.wait_ssh, hang))

    def test_ready_check_still_failing(self):
        not_yet = subprocess.CompletedProcess([], 3, b"", b"inactive\n")
        message = self.failing(guest.wait_ready, lambda *a, **k: not_yet, "systemctl --user is-active x")
        self.assertIn("the check still exits 3", message)
        self.assertIn("systemctl --user is-active x", message)


class SetTime(unittest.TestCase):
    def test_tried_again_while_the_agent_is_not_back(self):
        calls = []

        def agent(dom, execute, **arguments):
            calls.append(arguments["time"])
            if len(calls) < 3:
                raise DeskError("guest agent guest-set-time: QEMU guest agent is not connected")

        with mock.patch("desk.guest.agent", agent), mock.patch("desk.guest.time.sleep"):
            guest.agent_set_time(None, offset=86400)
        self.assertEqual(len(calls), 3)

    def test_a_refusal_is_not_waited_out(self):
        with mock.patch("desk.guest.agent", side_effect=DeskError("guest agent guest-set-time: Invalid time")) as agent, \
                mock.patch("desk.guest.time.sleep"), self.assertRaisesRegex(DeskError, "Invalid time"):
            guest.agent_set_time(None)
        self.assertEqual(agent.call_count, 1)

    def test_gives_up_when_it_stays_away(self):
        with mock.patch("desk.guest.agent", side_effect=DeskError("not connected")), mock.patch("desk.guest.time.sleep"):
            with self.assertRaisesRegex(DeskError, "not connected"):
                guest.agent_set_time(None, wait=0.05)


class WindowsAgent(unittest.TestCase):
    def test_busy_is_not_unreachable(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            run = SimpleNamespace(id="t", agent_port=server.getsockname()[1])
            with self.assertRaisesRegex(DeskError, "did not answer 'uia' within 0s"):
                winagent.request(run, timeout=0.2, cmd="uia")
        with self.assertRaisesRegex(DeskError, "not reachable"):
            winagent.request(run, timeout=0.2, cmd="uia")


if __name__ == "__main__":
    unittest.main()
