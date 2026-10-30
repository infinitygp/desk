"""The observation framework and Process Monitor, with a Windows guest in memory
(tests/observe_fake.py). These decide what desk does with what a guest answers; whether ProcMon
answers so is for the Windows checks (docs/observe/validation.md)."""

import base64
import io
import json
import os
import shutil
import struct
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from desk import observe
from desk.config import DeskError
from desk.observe import base, formats, guestops
from desk.observe.session import Store
from tests.observe_fake import FakeHost, FakeRun, FakeWindows, pcap_bytes, pcapng_bytes, pml_bytes, with_procmon


def tmpdir(test: unittest.TestCase) -> Path:
    d = Path(tempfile.mkdtemp(prefix="desk-observe-"))
    test.addCleanup(shutil.rmtree, d, ignore_errors=True)
    return d


class Formats(unittest.TestCase):
    def write(self, data: bytes, name: str = "f") -> Path:
        p = tmpdir(self) / name
        p.write_bytes(data)
        return p

    def test_a_whole_procmon_log_says_its_events(self):
        c = formats.pml(self.write(pml_bytes(7)))
        self.assertTrue(c["ok"], c)
        self.assertEqual((c["events"], c["computer"], c["version"]), (7, "DESK-WIN", 9))

    def test_the_header_is_read_where_process_monitor_writes_it(self):
        # The offsets of a log Process Monitor 4.01 wrote on Windows 11 26H2, laid out by hand.
        size = 531619992
        head = bytearray(0x3A8)
        head[0:12] = b"PML_" + struct.pack("<II", 9, 1)
        head[12:32] = "DESK-WIN11".encode("utf-16-le")
        head[0x2C:0x40] = "C:\\WINDOWS".encode("utf-16-le")
        struct.pack_into("<I", head, 0x234, 1048604)
        struct.pack_into("<QQQQQ", head, 0x240, 936, 525189288, 530432308, 531119384, 531569900)
        p = self.write(bytes(head))
        with p.open("r+b") as f:
            f.truncate(size)
        c = formats.pml(p)
        self.assertTrue(c["ok"], c)
        self.assertEqual((c["events"], c["computer"], c["system_root"]), (1048604, "DESK-WIN11", "C:\\WINDOWS"))

    def test_a_log_procmon_did_not_close_is_incomplete(self):
        c = formats.pml(self.write(pml_bytes(7, complete=False)))
        self.assertFalse(c["ok"])
        self.assertIn("incomplete", c["detail"])

    def test_a_log_cut_off_by_its_copy_is_incomplete(self):
        data = pml_bytes(50)
        c = formats.pml(self.write(data[:0x400]))
        self.assertFalse(c["ok"])

    def test_what_is_no_procmon_log_says_so(self):
        for data in (b"", b"MZ\x90\x00", b"PML_" + b"\x00" * 10):
            self.assertFalse(formats.pml(self.write(data))["ok"], data)
        self.assertIn("version", formats.pml(self.write(b"PML_" + (8).to_bytes(4, "little") + pml_bytes()[8:]))["detail"])

    def test_pcap_records_are_counted_and_a_cut_last_one_is_found(self):
        whole = formats.pcap(self.write(pcap_bytes(3)))
        self.assertTrue(whole["ok"])
        self.assertEqual((whole["packets"], whole["linktype"], whole["link"]), (3, 249, "USBPcap"))
        cut = formats.pcap(self.write(pcap_bytes(3, cut=True)))
        self.assertFalse(cut["ok"])
        self.assertEqual(cut["packets"], 2)
        self.assertIn("cut off in record 3", cut["detail"])
        self.assertFalse(formats.pcap(self.write(b"\x0a\x0d\x0d\x0a" + b"\0" * 30))["ok"])

    def test_pcapng_blocks_are_walked_in_either_byte_order(self):
        for order in "<>":
            c = formats.pcapng(self.write(pcapng_bytes(4, order)))
            self.assertTrue(c["ok"], c)
            self.assertEqual((c["packets"], c["links"]), (4, [1]))
        data = pcapng_bytes(4)
        self.assertFalse(formats.pcapng(self.write(data[:-10]))["ok"])
        broken = bytearray(data)
        broken[-1] ^= 0xFF  # the last block's trailing length
        self.assertIn("does not end with its length", formats.pcapng(self.write(bytes(broken)))["detail"])
        self.assertFalse(formats.pcapng(self.write(pcap_bytes()))["ok"])

    def test_an_etl_is_only_checked_for_being_there(self):
        self.assertFalse(formats.etl(self.write(b""))["ok"])
        c = formats.etl(self.write(b"\x01" * 100))
        self.assertTrue(c["ok"])
        self.assertEqual(c["level"], "size")
        self.assertIn("not read", c["detail"])

    def test_procmon_csv(self):
        good = self.write(b'\xef\xbb\xbf"Time of Day","Process Name","PID","Operation","Path","Result","Detail"\r\n'
                          b'"1","a.exe","1","CreateFile","C:\\\\x","SUCCESS",""\r\n')
        self.assertEqual(formats.procmon_csv(good)["events"], 1)
        self.assertFalse(formats.procmon_csv(self.write(b"a,b\n1,2\n"))["ok"])

    def test_a_file_that_cannot_be_read(self):
        self.assertFalse(formats.check(Path("/nonexistent/x"), "pml")["ok"])


class Protocol(unittest.TestCase):
    """How desk talks to the helper: one request in, one tagged JSON line out."""

    def test_the_request_names_the_helper_by_its_hash_and_carries_the_operation(self):
        script = guestops.request_script("files", {"paths": ["C:\\a b\\c"]}, digest="ab12")
        self.assertIn("-ne 'ab12'", script)
        payload = script.rsplit("-Request '", 1)[1].split("'")[0]
        self.assertEqual(json.loads(base64.b64decode(payload)), {"op": "files", "paths": ["C:\\a b\\c"]})
        self.assertIn(guestops.MISSING, script)

    def test_the_answer_is_the_tagged_line_whatever_else_was_printed(self):
        out = "noise\r\n" + guestops.REPLY + json.dumps({"ok": True, "result": {"x": 1}, "now_ms": 5}) + "\r\n"
        self.assertEqual(guestops.parse_reply("op", 0, out, ""), {"x": 1, "_now_ms": 5})

    def test_no_answer_is_an_error_with_what_the_guest_said(self):
        with self.assertRaisesRegex(guestops.ObserveError, "no answer to files.*exit 1.*Access is denied"):
            guestops.parse_reply("files", 1, "", "Access is denied")
        with self.assertRaisesRegex(guestops.ObserveError, "in the guest: probe: boom"):
            guestops.parse_reply("probe", 0, guestops.REPLY + '{"ok": false, "error": "probe: boom"}', "")
        with self.assertRaisesRegex(guestops.ObserveError, "cut off"):
            guestops.parse_reply("probe", 0, guestops.REPLY + '{"ok": tr', "")

    def test_a_missing_helper_is_pushed_once(self):
        class Scripted(guestops.Guest):
            def __init__(self, answers):
                self.answers, self.pushed = list(answers), []

            def _powershell(self, script, limit):
                return 0, self.answers.pop(0), ""

            def push(self, local, remote, timeout=600):
                self.pushed.append((Path(local).name, remote))

        ok = guestops.REPLY + '{"ok": true, "result": {"boot": 1}, "now_ms": 1}'
        g = Scripted([guestops.MISSING, ok])
        self.assertEqual(g.call("boot")["boot"], 1)
        self.assertEqual(g.pushed, [("helper.ps1", guestops.GUEST_HELPER)])
        with self.assertRaisesRegex(guestops.ObserveError, "does not reach the guest intact"):
            Scripted([guestops.MISSING, guestops.MISSING]).call("boot")

    def test_every_operation_desk_asks_for_is_one_the_helper_has(self):
        helper = guestops.HELPER.read_text()
        sources = "".join(p.read_text() for p in Path(observe.__file__).parent.glob("*.py"))
        import re

        asked = set(re.findall(r'call\("(\w+)"', sources)) | {"exec", "launch", "alive", "stopproc", "waitgone", "procs",
                                                               "files", "mkdir", "remove", "probe", "boot"}
        for op in asked:
            self.assertIn(f"'{op}' {{", helper, op)

    def test_the_helper_is_windows_powershell_5_syntax(self):
        helper = guestops.HELPER.read_text()
        # PowerShell 7 only: null-coalescing, ternaries, pipeline chains, ConvertFrom-Json -AsHashtable.
        for construct in ("??", "?.", " && ", " || ", "-AsHashtable"):
            self.assertNotIn(construct, helper)
        # A capture is started with none of the session's handles: redirecting its output would
        # hand it the pipe SSH reads, and the command that started it would wait for it to end.
        launch = helper[helper.index("function Op-Launch"):helper.index("function Op-Alive")]
        self.assertNotIn("RedirectStandard", launch)
        # It refuses to create, write, read or remove anything outside its folder.
        self.assertEqual(helper.count("Under-Root"), 6)

    def test_command_lines_are_quoted_as_windows_splits_them(self):
        from desk.scenario import split_windows

        args = ["/BackingFile", "C:\\desk\\observe\\r1-s1\\procmon.pml", "a b", 'q"uote', "trail\\"]
        self.assertEqual(split_windows(base.command("x.exe", args, 5)["args"]), args)


class Options(unittest.TestCase):
    def test_text_and_toml_values_become_the_option_types(self):
        p = observe.provider("procmon")
        self.assertEqual(p.options({"csv": "true"})["csv"], True)
        self.assertEqual(p.options({"csv": False})["csv"], False)
        opt = base.Option(list, [], "")
        self.assertEqual(base.parse_value("x", opt, "a,b"), ["a", "b"])
        self.assertEqual(base.parse_value("x", base.Option(int, 0, ""), "0x10"), 16)

    def test_mistakes_are_refused_before_anything_starts(self):
        for name, given, said in (("procmon", {"cvs": True}, "does not take cvs"),
                                  ("procmon", {"csv": "maybe"}, "takes bool"),
                                  ("procmon", {"config": "x.pmc"}, "full guest path")):
            with self.subTest(name=name, given=given):
                with self.assertRaisesRegex(DeskError, said):
                    observe.provider(name).options(given)
        with self.assertRaisesRegex(DeskError, "takes int"):
            base.parse_value("n", base.Option(int, 0, ""), True)

    def test_limits(self):
        self.assertEqual(observe._limits(observe.provider("procmon"), None, None),
                         {"duration": observe.DEFAULT_DURATION, "max_bytes": observe.DEFAULT_MAX_BYTES})
        self.assertEqual(observe._limits(observe.provider("procmon"), 60, 10), {"duration": 60, "max_bytes": 10 << 20})
        snapshot, endpoint = base.Provider(), base.Provider()
        snapshot.kind, endpoint.kind = "snapshot", "endpoint"
        self.assertEqual(observe._limits(snapshot, None, None), {})
        self.assertEqual(observe._limits(endpoint, None, None), {"duration": observe.ENDPOINT_DURATION})
        for bad in ((0, None), (None, -1)):
            with self.assertRaises(DeskError):
                observe._limits(observe.provider("procmon"), *bad)

    def test_an_unknown_provider_names_those_there_are(self):
        with self.assertRaisesRegex(DeskError, "procmon"):
            observe.provider("strace")


class Sessions(unittest.TestCase):
    def setUp(self):
        self.run = FakeRun(tmpdir(self))
        self.store = Store(self.run)

    def test_ids_count_up_and_labels_find_sessions(self):
        a = self.store.new("procmon", "app", lambda s, o: None)
        b = self.store.new("wpr", None, lambda s, o: None)
        self.assertEqual((a.id, b.id, a.state), ("s1", "s2", "starting"))
        self.assertEqual(self.store.find("app").id, "s1")
        self.assertEqual(self.store.find("s2").provider, "wpr")
        with self.assertRaisesRegex(DeskError, "several observation sessions are open"):
            self.store.find(None)
        with self.assertRaisesRegex(DeskError, "has no observation session 'nope'"):
            self.store.find("nope")

    def test_a_label_in_use_and_a_bad_label_are_refused(self):
        self.store.new("procmon", "app", lambda s, o: None)
        with self.assertRaisesRegex(DeskError, "in use"):
            self.store.new("wpr", "app", lambda s, o: None)
        with self.assertRaisesRegex(DeskError, "letters, digits"):
            self.store.new("wpr", "../x", lambda s, o: None)

    def test_a_refused_start_leaves_no_session(self):
        def refuse(s, others):
            raise observe.Conflict("no")

        with self.assertRaises(observe.Conflict):
            self.store.new("procmon", None, refuse)
        self.assertEqual(self.store.all(), [])

    def test_a_session_file_of_another_desk_version_still_loads(self):
        s = self.store.new("procmon", None, lambda s, o: None)
        f = self.store._file(s.id)
        data = json.loads(f.read_text())
        data["from_the_future"] = 1
        del data["markers"]
        f.write_text(json.dumps(data))
        self.assertEqual(self.store.load(s.id).markers, [])

    def test_a_second_operation_on_a_session_is_refused_at_once(self):
        s = self.store.new("procmon", None, lambda s, o: None)
        with self.store.operation(s.id):
            pid = os.fork()
            if pid == 0:
                try:
                    with self.store.operation(s.id):
                        os._exit(0)
                except DeskError:
                    os._exit(7)
            _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 7)


class Following(unittest.TestCase):
    def test_children_and_grandchildren_by_pid_and_creation_time(self):
        table = [{"pid": 10, "ppid": 1, "name": "App.exe", "created": 100},
                 {"pid": 11, "ppid": 10, "name": "child.exe", "created": 110},
                 {"pid": 12, "ppid": 11, "name": "grandchild.exe", "created": 120},
                 # PID 10's number was a dead process's before App.exe started: not its child.
                 {"pid": 13, "ppid": 10, "name": "older.exe", "created": 50},
                 {"pid": 14, "ppid": 1, "name": "other.exe", "created": 130}]
        found = observe.descendants(table, {"image": "app.exe"})
        self.assertEqual([p["pid"] for p in found], [10, 11, 12])
        self.assertEqual([p["pid"] for p in observe.descendants(table, {"pid": 11})], [11, 12])
        self.assertEqual(observe.descendants(table, {"image": "none.exe"}), [])

    def test_a_process_that_is_its_own_parent_ends_the_walk(self):
        table = [{"pid": 10, "ppid": 10, "name": "a.exe", "created": 1}]
        self.assertEqual(len(observe.descendants(table, {"pid": 10})), 1)


class Setup(unittest.TestCase):
    def zip(self, names) -> Path:
        p = tmpdir(self) / "ProcessMonitor.zip"
        with zipfile.ZipFile(p, "w") as z:
            for n in names:
                z.writestr(n, f"MZ {n}")
        return p

    def tearDown(self):
        shutil.rmtree(observe.Host.setup_dir("procmon"), ignore_errors=True)

    def test_the_tool_is_kept_with_its_hashes_and_the_licence_as_the_user_said(self):
        info = observe.setup("procmon", self.zip(["Procmon.exe", "Procmon64.exe", "Eula.txt", "../../evil.exe"]),
                             accept_licence=True, who="me")
        self.assertEqual(sorted(f["name"] for f in info["files"]), ["Eula.txt", "Procmon.exe", "Procmon64.exe"])
        self.assertEqual(info["licence_accepted"]["by"], "me")
        d = observe.Host.setup_dir("procmon")
        self.assertFalse((d / "evil.exe").exists())
        self.assertEqual(observe.Host().setup("procmon")["files"][0]["sha256"], base.sha256(d / "Eula.txt"))
        again = observe.setup("procmon", self.zip(["Procmon64.exe"]))
        self.assertIsNone(again["licence_accepted"])

    def test_an_archive_without_the_tool_is_refused_and_the_old_setup_stays(self):
        observe.setup("procmon", self.zip(["Procmon64.exe"]), accept_licence=True)
        with self.assertRaisesRegex(observe.Missing, "holds none of"):
            observe.setup("procmon", self.zip(["readme.txt"]))
        self.assertTrue(observe.Host().setup("procmon")["licence_accepted"])

    def test_tools_windows_has_need_no_setup(self):
        with mock.patch.object(observe.provider("procmon"), "setup_files", ()), \
                self.assertRaisesRegex(DeskError, "needs nothing set up"):
            observe.setup("procmon", Path("/"))


class ProcMonLife(unittest.TestCase):
    """A Process Monitor session from start to the log on the host."""

    def setUp(self):
        self.run = FakeRun(tmpdir(self))
        self.guest = FakeWindows()
        with_procmon(self.guest)
        self.host = FakeHost(self.run)
        z = tmpdir(self) / "ProcessMonitor.zip"
        with zipfile.ZipFile(z, "w") as f:
            f.writestr("Procmon64.exe", "MZ procmon")
            f.writestr("Eula.txt", "terms")
        observe.setup("procmon", z, accept_licence=True, who="tests")
        self.addCleanup(shutil.rmtree, observe.Host.setup_dir("procmon"), ignore_errors=True)

    def start(self, **kw):
        return observe.start(self.run, "procmon", host=self.host, guest=self.guest, **kw)

    def stop(self, ref=None, **kw):
        return observe.stop(self.run, ref, host=self.host, guest=self.guest, **kw)

    def test_start_deploys_procmon_waits_until_it_captures_and_watches_its_limits(self):
        s = self.start(duration=120, max_mb=50)
        self.assertEqual(s.state, "running")
        exe = "C:\\desk\\observe\\tools\\procmon\\Procmon64.exe"
        self.assertIn(exe, self.guest.pushed)
        launched = [p for p in self.guest.procs.values() if p["name"] == "Procmon64.exe"][0]
        self.assertEqual(launched["argv"], ["/AcceptEula", "/Quiet", "/Minimized", "/BackingFile",
                                            "C:\\desk\\observe\\r1-s1\\procmon.pml", "/Runtime", "120"])
        self.assertIn(["/AcceptEula", "/WaitForIdle"], self.guest.execs("Procmon64.exe"))
        watchdog = [p for p in self.guest.procs.values() if p["name"] == "powershell.exe"][0]
        order = json.loads(base64.b64decode(watchdog["argv"][watchdog["argv"].index("-Watch") + 1]))
        self.assertEqual((order["seconds"], order["max_bytes"]), (120, 50 << 20))
        self.assertEqual(order["files"], ["C:\\desk\\observe\\r1-s1\\procmon.pml"])
        self.assertEqual(order["stop"][0]["alone"]["names"], ["Procmon.exe", "Procmon64.exe", "Procmon64a.exe"])
        self.assertEqual(order["tool"]["pid"], launched["pid"])
        self.assertEqual(self.run.events[-1]["observe"], "start")
        # The tool is pushed again only when the guest's copy differs.
        self.stop()
        self.start()
        self.assertEqual(self.guest.pushed.count(exe), 1)

    def test_stop_terminates_copies_and_checks_the_log(self):
        self.start()
        s, problems = self.stop()
        self.assertEqual(problems, [])
        self.assertEqual((s.state, s.complete), ("stopped", True))
        pml = next(a for a in s.artifacts if a["name"] == "procmon.pml")
        self.assertTrue(pml["verified"] and pml["transferred"])
        self.assertEqual(pml["check"]["events"], 3)
        self.assertEqual(pml["sha256"], base.sha256(Path(pml["path"])))
        self.assertIn(["/AcceptEula", "/Terminate"], self.guest.execs("Procmon64.exe"))
        self.assertFalse(any(k.startswith("C:\\desk\\observe\\r1-s1\\") for k in self.guest.disk))
        self.assertEqual([c["at"] for c in s.clock], ["start", "stop"])
        self.assertAlmostEqual(s.clock[0]["guest"] - s.clock[0]["host"], 2.5, places=2)

    def test_csv_on_request(self):
        self.start(options={"csv": True})
        s, problems = self.stop()
        csv = next(a for a in s.artifacts if a["name"] == "procmon.csv")
        self.assertEqual(csv["check"]["events"], 1)

    def test_a_procmon_desk_did_not_start_is_never_terminated(self):
        foreign = self.guest.new_proc("Procmon64.exe")
        with self.assertRaisesRegex(observe.Conflict, "already runs.*close it first"):
            self.start()
        self.assertTrue(foreign["alive"])
        s = Store(self.run).find("s1")
        self.assertEqual(s.state, "failed")
        self.assertNotIn("/Terminate", str(self.guest.calls))

    def test_one_that_appears_meanwhile_holds_the_stop_until_force_ends_desks_own_by_pid(self):
        self.start()
        foreign = self.guest.new_proc("Procmon64.exe")
        first = Store(self.run).find("s1").watchdog
        with self.assertRaisesRegex(observe.Conflict, "besides this session's"):
            self.stop()
        held = Store(self.run).find("s1")
        self.assertEqual(held.state, "running")
        # The capture goes on, and a watchdog with it: not the one the failed stop ended.
        self.assertFalse(self.guest.procs[first["pid"]]["alive"])
        self.assertNotEqual(held.watchdog["pid"], first["pid"])
        self.assertTrue(self.guest.procs[held.watchdog["pid"]]["alive"])
        s, problems = self.stop(force=True)
        self.assertTrue(foreign["alive"])
        self.assertNotIn(["/AcceptEula", "/Terminate"], self.guest.execs("Procmon64.exe"))
        self.assertFalse(s.complete)
        self.assertIn("incomplete", problems[0])
        self.assertIn("ended by its PID", " ".join(s.notes))

    def test_a_second_procmon_session_of_the_run_is_refused(self):
        self.start()
        with self.assertRaisesRegex(observe.Conflict, "holds procmon"):
            self.start()
        self.assertEqual(len(Store(self.run).all()), 1)

    def test_without_the_tool_the_licence_or_an_elevated_token_nothing_starts(self):
        self.guest.admin = False
        with self.assertRaises(observe.Privileges):
            self.start()
        self.guest.admin = True
        observe.setup("procmon", self.make_zip(), accept_licence=False)
        with self.assertRaisesRegex(observe.Missing, "not accepted"):
            self.start()
        self.guest.registry["HKCU:\\Software\\Sysinternals\\Process Monitor\\EulaAccepted"] = 1
        s = self.start()
        self.assertNotIn("/AcceptEula", [p for p in self.guest.procs.values() if p["name"] == "Procmon64.exe"][0]["argv"])
        self.stop(s.id)
        shutil.rmtree(observe.Host.setup_dir("procmon"))
        with self.assertRaisesRegex(observe.Missing, "desk observe setup procmon"):
            self.start()
        self.assertEqual([x.state for x in Store(self.run).all()], ["failed", "failed", "stopped", "failed"])

    def make_zip(self) -> Path:
        z = tmpdir(self) / "p.zip"
        with zipfile.ZipFile(z, "w") as f:
            f.writestr("Procmon64.exe", "MZ procmon")
        return z

    def test_a_procmon_that_dies_leaves_an_incomplete_log_said_so(self):
        s = self.start()
        procmon = [p for p in self.guest.procs.values() if p["name"] == "Procmon64.exe"][0]
        procmon["alive"] = False  # crashed: its log never closed
        listed = observe.listing(self.run, guest=self.guest)
        self.assertEqual(listed[0].state, "ended")
        s, problems = self.stop()
        self.assertFalse(s.complete)
        self.assertIn("incomplete", problems[0])
        # What there was is on the host all the same, marked as not verified.
        pml = next(a for a in s.artifacts if a["name"] == "procmon.pml")
        self.assertEqual((pml["transferred"], pml["verified"]), (True, False))
        self.assertEqual(Path(pml["path"]).read_bytes(), pml_bytes(3, complete=False))

    def test_a_start_that_times_out_fails_and_takes_procmon_down_again(self):
        def slow(f, argv):
            return {"timed_out": True, "rc": None} if "/WaitForIdle" in argv else {"rc": 0}

        self.guest.tools["procmon64.exe"] = slow
        with self.assertRaisesRegex(observe.ObserveError, "still running after 90s"):
            self.start()
        self.assertEqual(self.guest.running("Procmon64.exe"), [])
        s = Store(self.run).find("s1")
        self.assertEqual(s.state, "failed")
        self.assertIn("still running", s.error)
        self.assertEqual(self.run.events[-1]["observe"], "failed")

    def test_a_copy_that_fails_or_differs_keeps_the_file_in_the_guest(self):
        self.start()
        self.guest.fail["pull"] = guestops.ObserveError("scp exit 1")
        s, problems = self.stop()
        self.assertIn("was not copied", problems[0])
        self.assertIn("C:\\desk\\observe\\r1-s1\\procmon.pml", self.guest.disk)
        self.assertFalse(s.complete)
        self.assertIn("the guest keeps", " ".join(s.notes))
        del self.guest.fail["pull"]
        self.start()
        self.guest.pull_hook = lambda remote, data: data[:-1] + b"X"
        s, problems = self.stop("s2")
        self.assertIn("differs from the guest's file", problems[0])
        self.assertIn("C:\\desk\\observe\\r1-s2\\procmon.pml", self.guest.disk)

    def test_a_log_that_was_never_made_is_missing(self):
        self.start()
        del self.guest.disk["C:\\desk\\observe\\r1-s1\\procmon.pml"]
        self.guest.tools["procmon64.exe"] = lambda f, argv: {"rc": 0}
        for p in self.guest.running("Procmon64.exe"):
            p["alive"] = False
        s, problems = self.stop()
        self.assertEqual(problems, ["procmon.pml was not made"])

    def test_the_watchdog_ending_it_at_its_limit_is_told_and_collected(self):
        s = self.start(duration=5)
        out = s.watchdog["out"]
        for p in self.guest.running("Procmon64.exe"):
            self.guest.tools["procmon64.exe"](self.guest, ["/Terminate"])
        self.guest.disk[out] = json.dumps({"state": "done", "reason": "size", "stop": [{"rc": 0}]}).encode()
        listed = observe.listing(self.run, guest=self.guest)
        self.assertEqual(listed[0].state, "ended")
        self.assertIn("size limit", listed[0].events[-1]["detail"])
        s, problems = self.stop()
        self.assertEqual(problems, [])
        self.assertTrue(s.complete)
        self.assertIn("size limit", " ".join(s.notes))

    def test_a_watchdog_that_ended_without_a_verdict_is_said_not_taken_for_a_guard(self):
        s = self.start(duration=600)
        self.guest.procs[s.watchdog["pid"]]["alive"] = False
        listed = observe.listing(self.run, guest=self.guest)
        self.assertEqual(listed[0].state, "running")
        self.assertEqual(listed[0].events[-1]["event"], "watchdog gone")
        observe.listing(self.run, guest=self.guest)
        self.assertEqual([e["event"] for e in self.run_store().all()[0].events].count("watchdog gone"), 1)
        s, problems = self.stop()
        self.assertEqual(problems, [])
        self.assertIn("limits were not kept", " ".join(s.notes))

    def run_store(self):
        from desk.observe.session import Store

        return Store(self.run)

    def test_a_reboot_interrupts_the_capture_and_what_is_left_is_collected(self):
        self.start()
        self.guest.boot_id = "133000000000009999"
        for p in self.guest.procs.values():
            p["alive"] = False
        listed = observe.listing(self.run, guest=self.guest)
        self.assertEqual((listed[0].state, listed[0].error), ("interrupted", "the guest rebooted, and the capture "
                                                                             "ended with it"))
        before = len(self.guest.ops("stopproc"))
        s, problems = self.stop()
        self.assertEqual(len(self.guest.ops("stopproc")), before)  # nothing of the old boot is stopped
        self.assertFalse(s.complete)
        self.assertIn("interrupted", problems[0])

    def test_a_vm_that_is_gone_interrupts_and_nothing_is_asked_of_it(self):
        self.start()
        self.run.live = False
        calls = len(self.guest.calls)
        s, problems = self.stop()
        self.assertEqual(len(self.guest.calls), calls)
        self.assertFalse(s.complete)
        self.assertIn("the VM is no longer running", problems[0])
        self.assertIn("nothing could be collected", problems[1])

    def test_a_desk_command_killed_while_starting_leaves_the_session_interrupted(self):
        s = Store(self.run).new("procmon", None, lambda s, o: None)
        with Store(self.run).edit(s.id) as cur:
            cur.pid = 999999
        listed = observe.listing(self.run, guest=self.guest)
        self.assertEqual(listed[0].state, "interrupted")
        self.assertIn("starting it (pid 999999) ended", listed[0].error)

    def test_stopping_what_is_finished_is_refused(self):
        self.start()
        self.stop()
        with self.assertRaisesRegex(DeskError, "is stopped"):
            self.stop("s1")

    def test_following_a_process_keeps_its_identity_from_start_to_stop(self):
        app = self.guest.new_proc("app.exe")
        s = self.start(target={"image": "app.exe"})
        self.assertEqual([p["pid"] for p in s.processes.values()], [app["pid"]])
        child = self.guest.new_proc("helper.exe", ppid=app["pid"])
        s, _ = self.stop()
        self.assertEqual(sorted(p["pid"] for p in s.processes.values()), sorted([app["pid"], child["pid"]]))
        first = s.processes[f"{app['pid']}:{app['created']}"]
        self.assertLessEqual(first["first_seen"], first["last_seen"])

    def test_on_a_linux_run_procmon_is_refused(self):
        run = FakeRun(tmpdir(self), image="fedora-44-gnome")
        with self.assertRaisesRegex(observe.Unsupported, "linux"):
            observe.start(run, "procmon", host=self.host, guest=self.guest)

    def test_collect_all_stops_what_is_open_and_says_what_is_wrong(self):
        self.assertEqual(observe.collect_all(FakeRun(tmpdir(self)), "down", guest=self.guest), [])
        self.start()
        self.assertEqual(observe.collect_all(self.run, "desk down", guest=self.guest, host=self.host), [])
        self.assertEqual(Store(self.run).find("s1").state, "stopped")
        self.start()
        self.run.live = False
        warnings = observe.collect_all(self.run, "desk down", guest=self.guest, host=self.host)
        self.assertIn("nothing could be collected", warnings[0])


class Marks(unittest.TestCase):
    def setUp(self):
        self.run = FakeRun(tmpdir(self))
        self.guest = FakeWindows()
        with_procmon(self.guest)
        self.host = FakeHost(self.run)
        self.guest.registry["HKCU:\\Software\\Sysinternals\\Process Monitor\\EulaAccepted"] = 1
        self.guest.disk["C:\\pm\\Procmon64.exe"] = b"MZ"

    def start(self, **kw):
        return observe.start(self.run, "procmon", options={"exe": "C:\\pm\\Procmon64.exe"}, host=self.host,
                             guest=self.guest, **kw)

    def test_a_mark_needs_a_running_session(self):
        with self.assertRaisesRegex(DeskError, "no running observation session"):
            observe.mark(self.run, "x", guest=self.guest)

    def test_a_mark_is_in_the_guest_the_timeline_and_the_running_sessions(self):
        self.start()
        m1 = observe.mark(self.run, "clicked Save", guest=self.guest)
        m2 = observe.mark(self.run, "second", ref="s1", guest=self.guest)
        self.assertEqual((m1["seq"], m2["seq"]), (1, 2))
        self.assertEqual(m1["sessions"], ["s1"])
        self.assertEqual(self.guest.disk[m1["file"]], b"clicked Save")
        self.assertTrue(m1["file"].startswith("C:\\desk\\observe\\marks\\"))
        mark_op = self.guest.ops("mark")[0]
        self.assertEqual((mark_op["source"], mark_op["event"]), (observe.MARKER_SOURCE, "DeskMark1"))
        # The guest's moment in host time: the clocks are 2.5 s apart.
        self.assertAlmostEqual(m1["offset"], 2.5, places=2)
        self.assertAlmostEqual(m1["guest_as_host"], m1["host"], delta=0.2)
        s1 = Store(self.run).find("s1")
        self.assertEqual([m["seq"] for m in s1.markers], [1, 2])
        self.assertEqual([c["at"] for c in s1.clock], ["start", "mark 1", "mark 2"])
        self.assertEqual([e["seq"] for e in self.run.events if e.get("observe") == "mark"], [1, 2])

    def test_a_mark_of_a_session_that_is_not_running_is_refused(self):
        self.start()
        observe.stop(self.run, "s1", host=self.host, guest=self.guest)
        with self.assertRaisesRegex(DeskError, "is stopped, not running"):
            observe.mark(self.run, "x", ref="s1", guest=self.guest)


class Snapshots(unittest.TestCase):
    def setUp(self):
        self.run = FakeRun(tmpdir(self))
        self.guest = FakeWindows()
        with_procmon(self.guest)
        self.guest.registry["HKCU:\\Software\\Sysinternals\\Process Monitor\\EulaAccepted"] = 1
        self.guest.disk["C:\\pm\\Procmon64.exe"] = b"MZ"
        self.host = FakeHost(self.run)
        self.snap = tmpdir(self)

    def start(self):
        return observe.start(self.run, "procmon", options={"exe": "C:\\pm\\Procmon64.exe"}, host=self.host,
                             guest=self.guest)

    def test_a_capture_started_after_the_save_is_gone_with_the_restore(self):
        observe.snapshot_saved(self.run, self.snap)
        self.start()
        said = observe.snapshot_restored(self.run, self.snap, "before")
        s = Store(self.run).find("s1")
        self.assertEqual(s.state, "interrupted")
        self.assertIn("snapshot before restored", s.error)
        self.assertIn("interrupted", said[0])
        self.assertEqual(self.run.events[-1]["observe"], "restore")
        # Its tools went with the guest's memory: nothing of it is stopped, what is left is collected.
        s, problems = observe.stop(self.run, "s1", host=self.host, guest=self.guest)
        self.assertNotIn(["/Terminate"], self.guest.execs("Procmon64.exe"))
        self.assertFalse(s.complete)

    def test_a_capture_running_at_the_save_goes_on_with_a_gap_said(self):
        self.start()
        observe.snapshot_saved(self.run, self.snap)
        observe.snapshot_restored(self.run, self.snap, "mid")
        s = Store(self.run).find("s1")
        self.assertEqual(s.state, "running")
        self.assertEqual(s.stats["restores"][0]["snapshot"], "mid")
        self.assertIn("gone from its files", s.events[-1]["detail"])

    def test_a_capture_collected_since_comes_back_as_a_new_session(self):
        self.start()
        observe.snapshot_saved(self.run, self.snap)
        observe.stop(self.run, "s1", host=self.host, guest=self.guest)
        said = observe.snapshot_restored(self.run, self.snap, "mid")
        s1, s2 = Store(self.run).all()
        self.assertEqual((s1.state, s2.state, s2.restored_from), ("stopped", "running", "s1"))
        self.assertEqual(s2.tool["pml"], s1.tool["pml"])
        self.assertIn("session s2", said[0])

    def test_without_sessions_nothing_is_saved(self):
        observe.snapshot_saved(self.run, self.snap)
        self.assertFalse((self.snap / "observe-sessions").exists())
        self.assertEqual(observe.snapshot_restored(self.run, self.snap, "x"), [])


class Cli(unittest.TestCase):
    def test_the_observe_commands_parse(self):
        from desk.cli import build_parser

        p = build_parser()
        a = p.parse_args(["observe", "start", "procmon", "--duration", "60", "--max-mb", "100", "--process", "app.exe",
                          "--set", "csv=true", "--label", "app"])
        self.assertEqual((a.provider, a.duration, a.max_mb, a.process, a.set, a.label),
                         ("procmon", 60.0, 100.0, "app.exe", ["csv=true"], "app"))
        self.assertTrue(p.parse_args(["observe", "stop", "--all", "--force"]).all)
        self.assertEqual(p.parse_args(["observe", "mark", "saved", "--shot"]).text, "saved")
        self.assertEqual(p.parse_args(["observe", "pipe", "mypipe", "--send", "hi", "--read", "4"]).read, 4)
        self.assertTrue(p.parse_args(["observe", "setup", "procmon", "x.zip", "--accept-eula"]).accept_eula)

    def test_options_and_targets_from_the_command_line(self):
        from desk import cli

        self.assertEqual(cli._observe_options(["a=1", "b=x=y"]), {"a": "1", "b": "x=y"})
        with self.assertRaisesRegex(DeskError, "KEY=VALUE"):
            cli._observe_options(["nokey"])
        args = mock.Mock(process="a.exe", pid=None)
        self.assertEqual(cli._observe_target(args), {"image": "a.exe"})
        with self.assertRaisesRegex(DeskError, "not both"):
            cli._observe_target(mock.Mock(process="a.exe", pid=4))

    def test_providers_without_a_run_lists_them_without_asking(self):
        from desk import cli

        out = io.StringIO()
        with redirect_stdout(out):
            cli.main(["observe", "providers", "--offline"])
        for name in observe.PROVIDERS:
            self.assertIn(name, out.getvalue())

    def test_starting_without_a_run_is_desks_failure(self):
        from desk import cli

        with self.assertRaises(SystemExit) as e, redirect_stdout(io.StringIO()), \
                mock.patch("sys.stderr", new=io.StringIO()):
            cli.main(["observe", "start", "procmon"])
        self.assertEqual(e.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
