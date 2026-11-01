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


class SshsOwnFailure(unittest.TestCase):
    """ssh's 255 told from a command's: by what ssh says last, as OpenSSH prints it at LogLevel=ERROR."""

    RESET = subprocess.CompletedProcess([], 255, b"",
                                        b"kex_exchange_identification: read: Connection reset by peer\r\n")

    def run_ssh(self, *answers):
        with mock.patch("desk.guest.subprocess.run", side_effect=list(answers)) as ran, \
                mock.patch("desk.guest.time.sleep"):
            r = guest.ssh_run(2222, "tester", "true", capture=True)
        return r, ran.call_count

    def test_a_reset_handshake_is_sent_again(self):
        ok = subprocess.CompletedProcess([], 0, b"done\n", b"")
        self.assertEqual(self.run_ssh(self.RESET, ok), (ok, 2))

    def test_a_handshake_that_keeps_failing_is_tried_three_times(self):
        self.assertEqual(self.run_ssh(self.RESET, self.RESET, self.RESET), (self.RESET, 3))

    def test_a_command_that_may_have_started_is_not_sent_again(self):
        exits = subprocess.CompletedProcess([], 255, b"", b"")
        printed = subprocess.CompletedProcess([], 255, b"half\n", self.RESET.stderr)
        dropped = subprocess.CompletedProcess([], 255, b"", b"client_loop: send disconnect: Broken pipe\n")
        for r in (exits, printed, dropped):
            with self.subTest(r=r):
                self.assertEqual(self.run_ssh(r), (r, 1))

    def test_lost_believes_ssh_although_exit_0_answers_again(self):
        answered = subprocess.CompletedProcess([], 0, b"", b"")
        for said in (self.RESET.stderr, b"ssh: connect to host 127.0.0.1 port 2222: Connection refused\n",
                     b"packet_write_wait: Connection to 127.0.0.1 port 2222: Broken pipe\n",
                     b"client_loop: send disconnect: Broken pipe\n",
                     b"tester@127.0.0.1: Permission denied (publickey).\n"):
            r = subprocess.CompletedProcess([], 255, b"", said)
            with self.subTest(said=said), mock.patch("desk.guest.ssh_run", return_value=answered), \
                    self.assertRaisesRegex(guest.NoConnection, "step 'x': no SSH connection"):
                guest.lost(r, 2222, "tester", "step 'x'")

    def test_a_programs_own_255_is_its_result(self):
        answered = subprocess.CompletedProcess([], 0, b"", b"")
        # Another port, another host: what a program in the guest that uses ssh itself prints.
        for said in (b"", b"error: not found\n", b"ssh: connect to host 10.0.0.5 port 22: Connection refused\n",
                     b"packet_write_wait: Connection to 127.0.0.1 port 22: Broken pipe\n"):
            r = subprocess.CompletedProcess([], 255, b"", said)
            with self.subTest(said=said), mock.patch("desk.guest.ssh_run", return_value=answered):
                guest.lost(r, 2222, "tester", "step 'x'")  # no exception


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

    def test_the_connection_stays_open_while_the_caller_waits(self):
        # The agent leaves a request undone when its connection is closed by the time it gets to it.
        import threading

        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            seen = {}

            def agent():
                conn, _ = server.accept()
                with conn:
                    seen["request"] = conn.makefile("rb").readline()
                    try:
                        seen["closed"] = conn.recv(1, socket.MSG_DONTWAIT) == b""
                    except BlockingIOError:
                        seen["closed"] = False
                    conn.sendall(b'{"ok": true}')

            thread = threading.Thread(target=agent)
            thread.start()
            run = SimpleNamespace(id="t", agent_port=server.getsockname()[1])
            self.assertEqual(winagent.request(run, timeout=5, cmd="ping"), {"ok": True})
            thread.join()
        self.assertEqual(seen, {"request": b'{"cmd": "ping", "open": true}\n', "closed": False})

    def test_a_query_is_asked_again_once_the_agent_is_back(self):
        gone = winagent.AgentGone("in-session agent not reachable")
        run = SimpleNamespace(id="t", agent_port=1)
        with mock.patch("desk.winagent._request", side_effect=[gone, gone, {"ok": True}, {"nodes": []}]) as req, \
                mock.patch("desk.winagent.time.sleep"):
            self.assertEqual(winagent.request(run, cmd="uia", op="snapshot"), {"nodes": []})
        self.assertEqual([c.args[2]["cmd"] for c in req.call_args_list], ["uia", "ping", "ping", "uia"])

    def test_an_action_is_not(self):
        run = SimpleNamespace(id="t", agent_port=1)
        with mock.patch("desk.winagent._request", side_effect=[winagent.AgentGone("not reachable"), {"ok": True}]), \
                self.assertRaisesRegex(DeskError, "not reachable"):
            winagent.request(run, cmd="uia", op="action")

    def test_an_agent_that_stays_away_is_not_waited_for_ever(self):
        run = SimpleNamespace(id="t", agent_port=1)
        with mock.patch("desk.winagent._request", side_effect=winagent.AgentGone("not reachable")), \
                mock.patch("desk.winagent.RESTART_WAIT", 0), mock.patch("desk.winagent.time.sleep"), \
                self.assertRaisesRegex(DeskError, "not reachable"):
            winagent.request(run, cmd="uia", op="snapshot")


if __name__ == "__main__":
    unittest.main()
