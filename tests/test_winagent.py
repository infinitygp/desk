"""desk's client of the Windows in-session agent against a stand-in on a local port: which requests
carry an id, and which are sent again, under that id, after a lost connection or a timeout."""

import json
import socket
import struct
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from desk import winagent
from desk.config import DeskError


class FakeAgent:
    """Answers each request that is not a ping with the next entry of `script`: a reply dict,
    "reset" (the connection is reset before any answer) or "silent" (no answer until the caller
    gives up). A ping is answered at once, with `ids` when `ids` is true."""

    def __init__(self, script, ids=True):
        self.script = list(script)
        self.ids = ids
        self.seen: list[dict] = []
        self._server = socket.create_server(("127.0.0.1", 0))
        self._server.settimeout(0.05)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        self.run = SimpleNamespace(id="t", agent_port=self._server.getsockname()[1])

    def close(self):
        self._stop.set()
        self._thread.join(5)
        self._server.close()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except TimeoutError:
                continue
            with conn:
                conn.settimeout(5)
                req = json.loads(conn.makefile("rb").readline())
                self.seen.append(req)
                if req["cmd"] == "ping":
                    conn.sendall(json.dumps({"ok": True, **({"ids": True} if self.ids else {})}).encode())
                    continue
                what = self.script.pop(0)
                if what == "reset":
                    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                elif what == "silent":
                    conn.recv(1)
                else:
                    conn.sendall(json.dumps(what).encode())

    def sent(self):
        return [r for r in self.seen if r["cmd"] != "ping"]


class RequestIds(unittest.TestCase):
    def agent(self, script, ids=True):
        fake = FakeAgent(script, ids)
        self.addCleanup(fake.close)
        return fake

    def test_an_action_carries_an_id_and_a_query_none(self):
        fake = self.agent([{"ok": True, "pid": 1}, {"nodes": []}])
        self.assertEqual(winagent.request(fake.run, cmd="run", file="notepad.exe"), {"ok": True, "pid": 1})
        self.assertEqual(winagent.request(fake.run, cmd="uia", op="snapshot"), {"nodes": []})
        action, query = fake.sent()
        self.assertRegex(action["id"], r"^[0-9a-f]{16}$")
        self.assertNotIn("id", query)
        self.assertEqual(len(fake.seen), 2)  # no ping when nothing failed

    def test_an_action_whose_connection_was_reset_is_sent_again_under_its_id(self):
        fake = self.agent(["reset", {"ok": True, "pid": 7}])
        self.assertEqual(winagent.request(fake.run, cmd="run", file="cmd.exe", wait=True), {"ok": True, "pid": 7})
        first, again = fake.sent()
        self.assertEqual(first, again)
        self.assertIn("id", again)
        self.assertEqual([r["cmd"] for r in fake.seen], ["run", "ping", "run"])

    def test_an_older_agent_is_not_sent_an_action_twice(self):
        fake = self.agent(["reset", {"ok": True}], ids=False)
        with self.assertRaises(winagent.AgentGone):
            winagent.request(fake.run, cmd="run", file="cmd.exe")
        self.assertEqual([r["cmd"] for r in fake.seen], ["run", "ping"])

    def test_a_timeout_is_sent_again_under_the_same_id(self):
        fake = self.agent(["silent", {"ok": True, "focused": True}])
        self.assertEqual(winagent.request(fake.run, timeout=0.3, cmd="focus", query="Calc"),
                         {"ok": True, "focused": True})
        first, again = fake.sent()
        self.assertEqual(first["id"], again["id"])

    def test_a_timeout_is_not_sent_again_to_an_older_agent(self):
        fake = self.agent(["silent", {"ok": True}], ids=False)
        with self.assertRaisesRegex(DeskError, "did not answer 'focus' within 0s"):
            winagent.request(fake.run, timeout=0.3, cmd="focus", query="Calc")
        self.assertEqual(len(fake.sent()), 1)

    def test_a_query_is_asked_again_without_an_id(self):
        for first in ("reset", "silent"):
            with self.subTest(first=first):
                fake = self.agent([first, {"nodes": []}], ids=False)
                self.assertEqual(winagent.request(fake.run, timeout=0.3, cmd="uia", op="snapshot"), {"nodes": []})
                self.assertEqual([("id" in r) for r in fake.sent()], [False, False])

    def test_once_only(self):
        fake = self.agent(["reset", "reset", {"ok": True}])
        with self.assertRaises(winagent.AgentGone):
            winagent.request(fake.run, cmd="run", file="cmd.exe")
        self.assertEqual([r["cmd"] for r in fake.seen], ["run", "ping", "run"])

    def test_a_program_still_running_when_asked_again_is_reported_as_lost(self):
        fake = self.agent(["reset", {"error": "request was begun already and is still being worked on",
                                     "begun": True}])
        with self.assertRaisesRegex(winagent.AgentGone, "had started 'cmd.exe' when the connection to it was lost; "
                                    "its result went with that connection, and it was not started again"):
            winagent.request(fake.run, cmd="run", file="cmd.exe", wait=True)

    def test_an_agent_that_stays_away_ends_the_request_with_the_first_failure(self):
        failures = [winagent.AgentGone("not reachable"), winagent.AgentBusy("did not answer 'ping'")]
        with mock.patch("desk.winagent._request", side_effect=failures), \
                mock.patch("desk.winagent.RESTART_WAIT", 0), mock.patch("desk.winagent.time.sleep"), \
                self.assertRaisesRegex(winagent.AgentGone, "not reachable"):
            winagent.request(SimpleNamespace(id="t", agent_port=1), cmd="run", file="cmd.exe")


if __name__ == "__main__":
    unittest.main()
