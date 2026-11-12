"""The specialist providers with the guest in memory: packet capture (pktmon, netsh trace), IPC
endpoints, USBPcap and debugger servers. Their tools' real behaviour on Windows is for a Windows
guest (tests/fixtures/observe/) and the Windows runner (scripts/observe_windows_ci.py)."""

import base64
import json
import shutil
import tempfile
import unittest
import zipfile
from unittest import mock
from pathlib import Path

from desk import observe
from desk.config import DeskError
from desk.observe import ipc, usbpcap
from desk.observe.base import ArtifactSpec
from desk.observe.session import Store
from tests.observe_fake import FakeHost, FakeRun, FakeWindows, pcap_bytes


def tmpdir(test) -> Path:
    d = Path(tempfile.mkdtemp(prefix="desk-observe-"))
    test.addCleanup(shutil.rmtree, d, ignore_errors=True)
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self.run = FakeRun(tmpdir(self))
        self.guest = FakeWindows()
        self.host = FakeHost(self.run)

    def start(self, provider, **kw):
        return observe.start(self.run, provider, host=self.host, guest=self.guest, **kw)

    def stop(self, ref=None, **kw):
        return observe.stop(self.run, ref, host=self.host, guest=self.guest, **kw)


class PktmonCi(Base):
    def run_ci(self, workload=None):
        # Import with the test's home: the runner otherwise allocates its own temporary home.
        with mock.patch.dict("os.environ", {"DESK_HOME": str(self.run.dir)}):
            from scripts import observe_windows_ci as ci

            with mock.patch.object(ci, "g", self.guest), \
                    mock.patch.object(ci, "results", []), \
                    mock.patch.object(ci, "run_workload", side_effect=workload or (lambda *a: {"token": "ci04"})), \
                    mock.patch.object(ci, "expect", return_value="DNS query found") as expected, \
                    mock.patch("builtins.print"):
                ci.pktmon_capture()
                return ci.results[0], expected.call_args

    def add_segment(self, *args):
        first = self.guest.pktmon_segments[0]
        for n in (2, 3):
            segment = first.replace("net1.etl", f"net{n}.etl")
            self.guest.disk[segment] = b"\x16" * 4096
            self.guest.pktmon_segments.append(segment)
        return {"token": "ci04"}

    def test_ci_discovers_converts_and_checks_every_segment(self):
        result, expected = self.run_ci(self.add_segment)
        self.assertEqual(result["status"], "PASS", result["detail"])
        self.assertEqual([Path(p.replace("\\", "/")).name for p in self.guest.pulled],
                         ["net1.etl", "net2.etl", "net3.etl", "net1.pcapng", "net2.pcapng", "net3.pcapng"])
        self.assertEqual(expected.args[1:], ("--pcapng", str(self.run.dir / "s4/net1.pcapng"),
                                           str(self.run.dir / "s4/net2.pcapng"),
                                           str(self.run.dir / "s4/net3.pcapng")))
        self.assertIn("10.0.26100.1", result["detail"])
        self.assertIn("No events lost", result["detail"])

    def test_ci_missing_segment_fails_before_conversion(self):
        def missing(*args):
            manifest = self.add_segment()
            del self.guest.disk[self.guest.pktmon_segments[0]]
            return manifest

        result, expected = self.run_ci(missing)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("net1.etl missing or empty", result["detail"])
        self.assertIn("files after stop", result["detail"])
        self.assertFalse(any(a[0] == "etl2pcap" for a in self.guest.execs("pktmon.exe")))
        self.assertIsNone(expected)

    def test_ci_conversion_exit_failure_is_not_hidden_by_readable_output(self):
        tool = self.guest.tools["pktmon.exe"]

        def failed(guest, args):
            result = tool(guest, args)
            return {**result, "rc": 1} if args[0] == "etl2pcap" else result

        self.guest.tools["pktmon.exe"] = failed
        result, expected = self.run_ci()
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("'rc': 1", result["detail"])
        self.assertIsNone(expected)

    def test_ci_checks_second_pcapng_not_only_the_last_or_first(self):
        tool = self.guest.tools["pktmon.exe"]

        def corrupt(guest, args):
            result = tool(guest, args)
            if args[0] == "etl2pcap" and args[1].endswith("net2.etl"):
                guest.disk[args[args.index("--out") + 1]] = b"bad"
            return result

        self.guest.tools["pktmon.exe"] = corrupt
        result, expected = self.run_ci(self.add_segment)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("net2.pcapng:", result["detail"])
        self.assertIsNone(expected)


class Pktmon(Base):
    D = "C:\\desk\\observe\\r1-s1"

    def test_segments_captured_stopped_and_each_read_back(self):
        s = self.start("pktmon", max_mb=256)
        self.assertEqual(self.guest.execs("pktmon.exe")[0],
                         ["start", "--capture", "--comp", "nics", "--pkt-size", "128", "--file-name",
                          f"{self.D}\\net.etl", "--file-size", "64", "--log-mode", "multi-file"])
        self.assertEqual(s.stats["payload"], "128 bytes kept of each packet (headers and the start of the payload)")
        # It went on into a second and third segment.
        for n in (2, 3):
            self.guest.disk[f"{self.D}\\net{n}.etl"] = b"\x16" * 4096
            self.guest.pktmon_segments.append(f"{self.D}\\net{n}.etl")
        s, problems = self.stop()
        self.assertEqual(problems, [])
        self.assertEqual(self.guest.execs("pktmon.exe")[1], ["stop"])
        self.assertEqual([a[:2] for a in self.guest.execs("pktmon.exe")[2:]], [["etl2pcap", f"{self.D}\\net{n}.etl"]
                                                                              for n in (1, 2, 3)])
        # The empty fourth one pktmon opens as it stops is no segment of the capture.
        self.assertEqual([a["name"] for a in s.artifacts],
                         ["net1.etl", "net2.etl", "net3.etl", "net1.pcapng", "net2.pcapng", "net3.pcapng"])
        lines = observe.provider("pktmon").summary(s)
        self.assertIn("3 segment(s), 0.0 MiB", lines)
        self.assertIn("PCAPNG: 15 packets in 3 of 3 file(s)", lines)
        self.assertIn("pktmon at its stop: No events lost", lines)
        self.assertIn("events lost (as the trace session counted them before it stopped): EventsLost 0", lines)

    def test_a_segment_missing_from_the_row_makes_it_incomplete(self):
        self.start("pktmon")
        # Three segments, of which the second is gone.
        self.guest.disk[f"{self.D}\\net3.etl"] = b"\x16" * 4096
        self.guest.pktmon_segments += [f"{self.D}\\net2.etl", f"{self.D}\\net3.etl"]
        s, problems = self.stop()
        self.assertEqual(problems, ["net2.etl was not made"])
        self.assertFalse(s.complete)

    def test_whole_packets_and_all_components_on_request(self):
        self.start("pktmon", options={"pkt_size": 0, "components": "all", "pcapng": False})
        self.assertIn("0", self.guest.execs("pktmon.exe")[0])
        s, _ = self.stop()
        self.assertEqual([a["name"] for a in s.artifacts], ["net1.etl"])
        self.assertEqual(s.stats["payload"], "whole packets kept")

    def test_a_capture_desk_did_not_start_is_left_alone(self):
        self.guest.etw["PktMon"] = {}
        with self.assertRaisesRegex(observe.Conflict, "packet capture runs in the guest already"):
            self.start("pktmon")
        self.assertEqual(self.guest.execs("pktmon.exe"), [])

    def test_a_capture_that_is_not_desks_file_is_not_stopped(self):
        self.start("pktmon")
        # Ours ended and somebody started another one meanwhile.
        self.guest.etw["PktMon"]["LocalFilePath"] = "C:\\Users\\tester\\theirs1.etl"
        with self.assertRaisesRegex(observe.Conflict, "PktMon writes C:.*theirs1.etl.*somebody else's capture"):
            self.stop()
        self.assertNotIn(["stop"], self.guest.execs("pktmon.exe"))
        self.assertEqual(Store(self.run).find("s1").state, "running")

    def test_a_limit_below_a_segment_is_refused_and_the_watchdog_holds_the_folder(self):
        with self.assertRaisesRegex(observe.ObserveError, "segments of 64 MiB and more: --max-mb 64 or more"):
            self.start("pktmon", max_mb=32)
        self.assertEqual(self.guest.execs("pktmon.exe"), [])
        s = self.start("pktmon", max_mb=2048)
        order = json.loads(base64.b64decode(self.guest.procs[s.watchdog["pid"]]["argv"][-1]))
        self.assertEqual((order["max_bytes"], order["files"]), (2048 << 20, ["C:\\desk\\observe\\r1-s2"]))
        self.assertEqual(self.guest.execs("pktmon.exe")[0][-3:], ["512", "--log-mode", "multi-file"])

    def test_one_packet_capture_at_a_time(self):
        self.start("pktmon")
        with self.assertRaisesRegex(observe.Conflict, "holds packet-capture"):
            self.start("netsh")

    def test_a_pcapng_whose_copy_differs_is_not_counted(self):
        self.start("pktmon")
        self.guest.pull_hook = lambda remote, data: data[:10]
        s, problems = self.stop()
        self.assertIn("net1.pcapng: the copy differs", " ".join(problems))
        self.assertIn("PCAPNG: 0 packets in 0 of 1 file(s); not read: net1.pcapng (the copy differs from the guest's "
                      "file (SHA-256))", observe.provider("pktmon").summary(s))

    def test_a_failed_conversion_is_said_and_the_etl_stays(self):
        self.start("pktmon")
        tool = self.guest.tools["pktmon.exe"]
        self.guest.tools["pktmon.exe"] = lambda f, argv: ({"rc": 1, "stdout": "Unknown format"}
                                                          if argv[0] == "etl2pcap" else tool(f, argv))
        s, problems = self.stop()
        self.assertEqual(problems, [])  # the PCAPNG is a conversion, the ETL is the capture
        self.assertIn("no PCAPNG of net1.etl: pktmon etl2pcap exit 1: Unknown format",
                      observe.provider("pktmon").summary(s))


class TwoPasses(Base):
    """stop --all, observe_stop = true and desk down stop every capture before copying any."""

    def setUp(self):
        super().setUp()
        from tests.observe_fake import with_procmon

        with_procmon(self.guest)
        self.guest.registry["HKCU:\\Software\\Sysinternals\\Process Monitor\\EulaAccepted"] = 1
        self.guest.disk["C:\\pm\\Procmon64.exe"] = b"MZ"
        self.order = []
        call, pull = self.guest.call, self.guest.pull

        def calling(op, limit=60, **params):
            if op == "exec":
                self.order.append(("exec", Path(params["exe"].replace("\\", "/")).name, params["args"]))
            return call(op, limit, **params)

        def pulling(remote, local, timeout=600):
            self.order.append(("pull", remote.rsplit("\\", 1)[-1]))
            return pull(remote, local, timeout)

        self.guest.call, self.guest.pull = calling, pulling

    def test_every_capture_stops_before_any_is_converted_or_copied(self):
        self.start("procmon", options={"exe": "C:\\pm\\Procmon64.exe"})
        self.start("pktmon")
        self.order.clear()
        results = observe.stop_many(self.run, ["s1", "s2"], host=self.host, guest=self.guest)
        self.assertEqual([r[1] for r in results], [[], []])
        first_pull = next(i for i, o in enumerate(self.order) if o[0] == "pull")
        stops = [i for i, o in enumerate(self.order) if o[0] == "exec" and ("/Terminate" in o[2] or o[2] == "stop")]
        converts = [i for i, o in enumerate(self.order) if o[0] == "exec" and "etl2pcap" in o[2]]
        self.assertEqual(len(stops), 2)
        self.assertLess(max(stops), min(converts))
        self.assertLess(max(stops), first_pull)

    def test_one_that_does_not_stop_stays_to_be_stopped_again_and_the_others_are_collected(self):
        self.start("procmon", options={"exe": "C:\\pm\\Procmon64.exe"})
        self.start("pktmon")
        foreign = self.guest.new_proc("Procmon64.exe")
        results = observe.stop_many(self.run, ["s1", "s2"], host=self.host, guest=self.guest)
        self.assertIsInstance(results[0], observe.Conflict)
        self.assertEqual(results[1][1], [])
        held = Store(self.run).load("s1")
        self.assertEqual((held.state, held.artifacts), ("running", []))
        foreign["alive"] = False
        s, problems = observe.stop(self.run, "s1", host=self.host, guest=self.guest)
        self.assertEqual((s.state, problems), ("stopped", []))


class SessionNames(unittest.TestCase):
    def test_a_numbered_session_of_the_tool_counts(self):
        from desk.observe.network import NETSH_SESSION, PKTMON_SESSION, _capturing

        # As Windows Server 2025 (Windows CI) and Windows 11 26H2 named them.
        self.assertEqual(_capturing(["NetCfgTrace", "NetTrace-2"], (NETSH_SESSION,)), "NetTrace-2")
        self.assertEqual(_capturing(["NetTrace-08c1723f"], (NETSH_SESSION,)), "NetTrace-08c1723f")
        self.assertEqual(_capturing(["PktMon"], (PKTMON_SESSION,)), "PktMon")
        self.assertIsNone(_capturing(["NetCfgTrace", "PktMonitor"]))


class Netsh(Base):
    def test_filters_and_truncation(self):
        self.start("netsh", options={"filters": ["IPv4.Address=10.0.2.2", "Protocol=TCP"], "truncate": 256}, max_mb=32)
        self.assertEqual(self.guest.netsh, ["trace", "start", "capture=yes", "report=disabled", "persistent=no",
                                            "overwrite=yes", "filemode=single", "maxsize=32",
                                            "tracefile=C:\\desk\\observe\\r1-s1\\net.etl", "IPv4.Address=10.0.2.2",
                                            "Protocol=TCP", "packettruncatebytes=256"])
        s, problems = self.stop()
        self.assertEqual(problems, [])
        self.assertEqual(self.guest.execs("netsh.exe")[-1], ["trace", "stop"])
        self.assertEqual([a["name"] for a in s.artifacts], ["net.etl"])

    def test_its_session_of_a_name_of_its_own_runs_on_and_is_stopped_and_blocks_pktmon(self):
        self.start("netsh")
        self.assertEqual(observe.listing(self.run, guest=self.guest)[0].state, "running")
        with self.assertRaisesRegex(observe.Conflict, r"s1 \(netsh, running\) holds packet-capture"):
            observe.start(self.run, "pktmon", host=self.host, guest=self.guest, label="other")
        self.stop("s1")
        self.assertEqual(self.guest.execs("netsh.exe")[-1], ["trace", "stop"])
        self.assertEqual(self.guest.etw.get("NetTrace-08c1723f"), None)


class Ipc(Base):
    def test_listings_at_start_marks_and_stop_and_what_changed(self):
        app = self.guest.new_proc("app.exe")
        self.guest.pipes = ["InitShutdown", "lsass"]
        self.start("ipc", target={"image": "app.exe"})
        self.guest.pipes = ["InitShutdown", "lsass", "app-control"]
        self.guest.tcp = [{"local": "127.0.0.1", "lport": 8123, "remote": "0.0.0.0", "rport": 0, "state": "Listen",
                           "pid": app["pid"]}]
        observe.mark(self.run, "opened", guest=self.guest)
        s, problems = self.stop()
        self.assertEqual(problems, [])
        self.assertEqual([a["name"] for a in s.artifacts], ["ipc-001-start.json", "ipc-002-mark-1.json",
                                                            "ipc-003-stop.json"])
        self.assertTrue(all(a["verified"] for a in s.artifacts))
        last = json.loads(Path(s.artifacts[-1]["path"]).read_text())
        self.assertEqual(last["tcp"][0]["process"], "app.exe")
        lines = observe.provider("ipc").summary(s)
        self.assertIn("pipes from start to stop: +app-control", lines)
        listed = json.loads(Path(s.artifacts[1]["path"]).read_text())
        self.assertLess(abs(listed["after_mark_s"]), 1)
        self.assertAlmostEqual(listed["guest_to"] - listed["host_to"], 2.5, delta=0.5)
        self.assertNotIn("listings made well after", " ".join(lines))
        self.assertIn("new TCP listeners: 127.0.0.1:8123 (app.exe)", lines)
        self.assertIn("TCP endpoints of the processes followed: 127.0.0.1:8123->0.0.0.0:0 Listen (app.exe)", lines)
        # Listings need no elevation and no tool: nothing was launched in the guest.
        self.assertEqual(self.guest.ops("launch"), [])

    def test_a_listing_made_long_after_its_mark_says_so(self):
        self.start("ipc")
        self.guest.ipc_takes = 0.0
        real = self.guest.op_ipc

        def late(**kw):
            r = real(**kw)
            r["_start_ms"] += 40_000  # the guest got to it 40 s after the mark
            r["_now_ms"] += 45_000
            return r

        self.guest.op_ipc = late
        observe.mark(self.run, "clicked", guest=self.guest)
        self.guest.op_ipc = real
        s, _ = self.stop()
        self.assertIn("listings made well after their mark (what they show is of then): mark-1 40 s after it",
                      observe.provider("ipc").summary(s))

    def test_only_pipes_matching(self):
        self.guest.pipes = ["mojo.1", "Other"]
        s = self.start("ipc", options={"match": "MOJO", "tcp": False})
        snap = json.loads((Path(s.tool["host_dir"]) / s.tool["snapshots"][0]["file"]).read_text())
        self.assertEqual((snap["pipes"], snap["tcp"]), (["mojo.1"], []))

    def test_probing_a_pipe(self):
        self.guest.pipe_reply = {"connected": True, "server_pid": 77, "sent": 2, "received_b64": "b2s="}
        r = ipc.probe_pipe(self.guest, "app-control", send=b"hi", read=2)
        self.assertEqual((r["server_pid"], r["received"]), (77, b"ok"))
        op = self.guest.ops("pipeprobe")[0]
        self.assertEqual((op["name"], op["send_b64"], op["read_bytes"], op["timeout_ms"]), ("app-control", "aGk=", 2, 5000))
        for bad in (("", {}), ("x", {"timeout": 0}), ("x" * 300, {})):
            with self.assertRaises(DeskError):
                ipc.probe_pipe(self.guest, bad[0], **bad[1])


def with_usbpcap(fake: FakeWindows, interfaces=("\\\\.\\USBPcap1",)) -> None:
    fake.disk[usbpcap.DEFAULT_EXE] = b"MZ"
    fake.services["USBPcap"] = {"state": "Running", "start": "Manual"}

    def tool(f, argv):
        if argv == ["--extcap-interfaces"]:
            return {"rc": 0, "stdout": "extcap {version=1.5.4.0}{help=https://desowin.org/usbpcap/}\n" +
                    "".join(f"interface {{value={i}}}{{display=USBPcap{n}}}\n" for n, i in enumerate(interfaces, 1))}
        return {"rc": 1}

    def launch(f, argv, p):
        out = argv[argv.index("-o") + 1]
        f.disk[out] = pcap_bytes(0)
        p["on_stop"] = lambda f, p: f.disk.__setitem__(out, pcap_bytes(4, cut=True))
        f.usbpcap = argv

    fake.tools["usbpcapcmd.exe"] = tool
    fake.launchers["usbpcapcmd.exe"] = launch


class UsbPcap(Base):
    def test_interfaces_are_read_as_wireshark_reads_them(self):
        text = ("extcap {version=1.5.4.0}\ninterface {value=\\\\.\\USBPcap1}{display=USBPcap1}\n"
                "interface {value=\\\\.\\USBPcap2}\n")
        self.assertEqual(usbpcap.interfaces(text), [{"value": "\\\\.\\USBPcap1", "display": "USBPcap1"},
                                                     {"value": "\\\\.\\USBPcap2", "display": "\\\\.\\USBPcap2"}])

    def test_the_only_interface_captured_until_ended_by_pid(self):
        with_usbpcap(self.guest)
        s = self.start("usbpcap", options={"snaplen": 4096, "descriptors": False})
        self.assertEqual(self.guest.usbpcap, ["-d", "\\\\.\\USBPcap1", "-o", "C:\\desk\\observe\\r1-s1\\usb.pcap", "-s",
                                              "4096", "-A"])
        self.assertEqual(s.tool["interface"], "\\\\.\\USBPcap1")
        s, problems = self.stop()
        self.assertEqual(problems, [])
        self.start("usbpcap")
        self.assertEqual(self.guest.usbpcap[-1], "--inject-descriptors")
        pcap = s.artifacts[0]
        self.assertTrue(pcap["verified"])
        self.assertIn("the 3 packets before it are whole", pcap["detail"])

    def test_several_interfaces_need_a_choice(self):
        with_usbpcap(self.guest, ("\\\\.\\USBPcap1", "\\\\.\\USBPcap2"))
        with self.assertRaisesRegex(observe.Missing, "lists 2 interfaces"):
            self.start("usbpcap")
        with self.assertRaisesRegex(observe.Missing, "no interface \\\\\\\\.\\\\USBPcap3"):
            self.start("usbpcap", options={"interface": "\\\\.\\USBPcap3"})
        self.start("usbpcap", options={"interface": "\\\\.\\USBPcap2"})
        self.start("usbpcap", options={"interface": "\\\\.\\USBPcap1"})  # another root hub: no conflict

    def test_without_its_driver_nothing_starts_and_passthrough_is_said(self):
        self.host.usb_devices = [{"id": "0fd9:0084", "name": "Elgato", "bus": 3, "dev": 7}]
        cap = observe.provider("usbpcap").capability(self.guest, observe.provider("usbpcap").defaults(), self.host)
        self.assertFalse(cap.available)
        self.assertIn("USBPcapCMD.exe is not in the guest", cap.problems[0])
        self.assertIn("driver is not installed", cap.problems[1])
        self.assertEqual(cap.details["passthrough"], ["0fd9:0084 Elgato"])
        self.assertEqual(cap.details["passthrough_not_in_guest"], ["0fd9:0084"])
        with_usbpcap(self.guest)
        self.guest.services["USBPcap"]["state"] = "Stopped"
        self.guest.usb = [{"id": "USB\\VID_0FD9&PID_0084\\A1", "name": "Stream Deck"}]
        cap = observe.provider("usbpcap").capability(self.guest, observe.provider("usbpcap").defaults(), self.host)
        self.assertIn("reboot", cap.problems[0])
        self.assertNotIn("passthrough_not_in_guest", cap.details)

    def test_a_pcap_of_another_link_type_is_not_a_usb_capture(self):
        d = tmpdir(self)
        (d / "x.pcap").write_bytes(pcap_bytes(2, linktype=1))
        check = observe.provider("usbpcap").verify(d / "x.pcap", ArtifactSpec("x.pcap", "x", "pcap"))
        self.assertFalse(check["ok"])
        self.assertIn("not USBPcap's link type", check["detail"])


def with_server(fake: FakeWindows, exe: str, port: int) -> None:
    fake.disk[exe] = b"MZ"

    def launch(f, argv, p):
        f.listening[port] = [{"address": "127.0.0.1", "pid": p["pid"]}]
        f.server = argv
        f.server_env = p["env"]
        p["on_stop"] = lambda f, p: f.listening.pop(port, None)

    fake.launchers[Path(exe.replace("\\", "/")).name.lower()] = launch


class Debuggers(Base):
    def restored_endpoint(self, provider, *, stopped):
        import copy

        self.run = FakeRun(tmpdir(self))
        self.guest = FakeWindows()
        self.host = FakeHost(self.run)
        port = 23946 if provider == "ida" else 5005
        p = observe.provider(provider)
        with_server(self.guest, p.exe(p.defaults()), port)
        s = self.start(provider)
        snap = tmpdir(self)
        observe.snapshot_saved(self.run, snap)
        procs, listening = copy.deepcopy((self.guest.procs, self.guest.listening))
        if stopped:
            self.stop(s.id)
        self.guest.procs, self.guest.listening = procs, listening
        with mock.patch.object(observe, "Host", return_value=self.host), \
                mock.patch.object(observe, "guest_for", return_value=self.guest):
            said = observe.snapshot_restored(self.run, snap, "debugging")
        restored = Store(self.run).find("s2" if stopped else s.id)
        self.assertEqual(restored.state, "interrupted")
        self.assertFalse(restored.complete)
        self.assertIn("restart required", restored.error)
        self.assertTrue(any("restart required" in line for line in said))
        self.assertNotIn("endpoint", restored.stats)
        self.assertEqual(self.guest.listening, {})
        self.assertEqual(self.guest.running(Path(p.exe(p.defaults()).replace("\\", "/")).name), [])
        self.assertEqual(self.host.forwards, {})
        if provider == "ida":
            self.assertFalse(Path(s.tool["password_file"]).exists())
        if stopped:
            self.assertEqual(Store(self.run).find(s.id).state, "stopped")

    def test_saved_stopped_debugger_is_interrupted_and_cleaned_after_restore(self):
        for provider in ("ida", "dbgsrv"):
            with self.subTest(provider=provider):
                self.restored_endpoint(provider, stopped=True)

    def test_saved_running_debugger_is_interrupted_and_cleaned_after_restore(self):
        for provider in ("ida", "dbgsrv"):
            with self.subTest(provider=provider):
                self.restored_endpoint(provider, stopped=False)

    def test_failed_restored_debugger_cleanup_can_be_retried_with_stop(self):
        with_server(self.guest, observe.debug.SDK_DEBUGGERS + "\\dbgsrv.exe", 5005)
        s = self.start("dbgsrv")
        snap = tmpdir(self)
        observe.snapshot_saved(self.run, snap)
        p = observe.provider("dbgsrv")
        with mock.patch.object(observe, "Host", return_value=self.host), \
                mock.patch.object(observe, "guest_for", return_value=self.guest), \
                mock.patch.object(p, "stop", side_effect=observe.ObserveError("temporary server cleanup failure")):
            with self.assertRaisesRegex(observe.ObserveError, "temporary server cleanup failure"):
                observe.snapshot_restored(self.run, snap, "debugging")
        failed = Store(self.run).find(s.id)
        self.assertEqual(failed.state, "interrupted")
        self.assertIn("temporary server cleanup failure", failed.error)
        self.assertTrue(self.guest.running("dbgsrv.exe"))
        retried, _ = self.stop(s.id)
        self.assertEqual(self.guest.running("dbgsrv.exe"), [])
        self.assertEqual(self.guest.listening, {})
        self.assertEqual(self.host.forwards, {})
        self.assertFalse(retried.complete)

    def test_restore_before_debugger_start_cleans_only_its_host_resources(self):
        snap = tmpdir(self)
        observe.snapshot_saved(self.run, snap)
        with_server(self.guest, observe.debug.SDK_DEBUGGERS + "\\dbgsrv.exe", 5005)
        s = self.start("dbgsrv")
        # The restored guest predates this endpoint. Its host tunnel still needs removal.
        self.guest.procs.clear()
        self.guest.listening.clear()
        with mock.patch.object(observe, "Host", return_value=self.host):
            observe.snapshot_restored(self.run, snap, "before-debugging")
        self.assertEqual(Store(self.run).find(s.id).state, "interrupted")
        self.assertEqual(self.host.forwards, {})

    def test_dbgsrv_is_reachable_on_the_hosts_loopback_only_and_taken_down(self):
        with_server(self.guest, observe.debug.SDK_DEBUGGERS + "\\dbgsrv.exe", 5005)
        s = self.start("dbgsrv")
        self.assertEqual(self.guest.server, ["-t", "tcp:port=5005"])
        self.assertEqual((s.tool["local_port"], s.stats["endpoint"]), (40005, "127.0.0.1:40005"))
        self.assertEqual(s.limits, {"duration": observe.ENDPOINT_DURATION})
        self.assertEqual(observe.provider("dbgsrv").connect_hint(s), "windbg -premote tcp:server=127.0.0.1,port=40005")
        s, problems = self.stop()
        self.assertEqual(problems, [])
        self.assertEqual(self.host.unforwarded, [5005])
        self.assertEqual(self.guest.listening, {})

    def real_forward_host(self):
        """Real tunnel records and locks, but no SSH or background process."""
        self.host = observe.Host(self.run)
        self.host.reachable = lambda local: True
        self.enterContext(mock.patch("desk.forward._alive", return_value=True))
        self.enterContext(mock.patch("desk.forward._free", return_value=True))
        self.enterContext(mock.patch("desk.forward._listening", return_value=True))
        self.enterContext(mock.patch("desk.runs.all_runs", return_value=[self.run]))
        self.spawn = self.enterContext(mock.patch("desk.forward.subprocess.Popen"))
        self.spawn.return_value.pid = 12345
        self.kill = self.enterContext(mock.patch("desk.forward.os.killpg"))
        from desk import forward

        return forward

    def test_manual_forward_is_refused_before_launch_or_secret_creation(self):
        forward = self.real_forward_host()
        exe = observe.provider("ida").exe(observe.provider("ida").defaults())
        with_server(self.guest, exe, 23946)
        forward.start(self.run, 23946)
        before = forward.listing(self.run)
        with self.assertRaisesRegex(observe.Conflict, "live forward already"):
            self.start("ida")
        self.assertEqual(forward.listing(self.run), before)
        self.assertEqual(self.guest.ops("launch"), [])
        self.assertEqual(list(self.run.dir.rglob("*.secret")), [])
        self.kill.assert_not_called()
        self.assertEqual(self.spawn.call_count, 1)

    def test_manual_forward_created_during_launch_survives_start_failure(self):
        forward = self.real_forward_host()
        exe = observe.debug.SDK_DEBUGGERS + "\\dbgsrv.exe"
        with_server(self.guest, exe, 5005)
        launch = self.guest.launchers["dbgsrv.exe"]

        def raced(fake, argv, proc):
            launch(fake, argv, proc)
            forward.start(self.run, 5005)

        self.guest.launchers["dbgsrv.exe"] = raced
        with self.assertRaisesRegex(observe.Conflict, "live forward already"):
            self.start("dbgsrv")
        self.assertTrue(forward.listing(self.run)[0]["alive"])
        self.assertEqual(self.guest.running("dbgsrv.exe"), [])
        self.kill.assert_not_called()
        self.assertEqual(self.spawn.call_count, 1)

    def test_stop_preserves_a_manual_replacement_of_the_owned_forward(self):
        forward = self.real_forward_host()
        with_server(self.guest, observe.debug.SDK_DEBUGGERS + "\\dbgsrv.exe", 5005)
        s = self.start("dbgsrv")
        forward.stop(self.run, 5005)
        self.spawn.return_value.pid = 54321
        forward.start(self.run, 5005)
        self.kill.reset_mock()
        before = forward.listing(self.run)
        _, problems = self.stop(s.id)
        self.assertEqual(problems, [])
        self.assertEqual(forward.listing(self.run), before)
        self.kill.assert_not_called()

    def test_owned_forward_is_removed_when_reachability_fails(self):
        forward = self.real_forward_host()
        self.host.reachable = lambda local: False
        with_server(self.guest, observe.debug.SDK_DEBUGGERS + "\\dbgsrv.exe", 5005)
        with self.assertRaisesRegex(observe.Missing, "does not answer"):
            self.start("dbgsrv")
        self.assertEqual(forward.listing(self.run), [])
        self.assertEqual(self.guest.running("dbgsrv.exe"), [])
        self.kill.assert_called_once()

    def test_exclusive_forward_refuses_reuse_but_manual_forward_remains_idempotent(self):
        forward = self.real_forward_host()
        local = forward.start(self.run, 5005)
        self.assertEqual(forward.start(self.run, 5005), local)
        with self.assertRaisesRegex(DeskError, "live forward already"):
            forward.start(self.run, 5005, exclusive=True)
        self.assertEqual(len(forward.listing(self.run)), 1)
        self.assertEqual(self.spawn.call_count, 1)
        self.kill.assert_not_called()

    def test_a_port_in_use_is_refused_before_starting(self):
        with_server(self.guest, observe.debug.SDK_DEBUGGERS + "\\dbgsrv.exe", 5005)
        other = self.guest.new_proc("other.exe")
        self.guest.listening[5005] = [{"address": "0.0.0.0", "pid": other["pid"]}]
        with self.assertRaisesRegex(observe.Conflict, "guest port 5005 is in use already"):
            self.start("dbgsrv")
        self.assertEqual(self.guest.ops("launch"), [])

    def test_a_server_that_never_listens_or_a_forward_that_does_not_answer_fails_and_is_taken_down(self):
        exe = observe.debug.SDK_DEBUGGERS + "\\dbgsrv.exe"
        self.guest.disk[exe] = b"MZ"
        self.guest.launchers["dbgsrv.exe"] = lambda f, argv, p: p.__setitem__("alive", False)
        with self.assertRaisesRegex(observe.Missing, "ended at once"):
            self.start("dbgsrv")
        with_server(self.guest, exe, 5005)
        self.host.answers = False
        with self.assertRaisesRegex(observe.Missing, "does not answer"):
            self.start("dbgsrv", options={"port": 5005})
        self.assertEqual(self.guest.running("dbgsrv.exe"), [])
        self.assertEqual(self.host.unforwarded, [5005])

    def test_without_the_debugging_tools(self):
        with self.assertRaisesRegex(observe.Missing, "Debugging Tools for Windows"):
            self.start("dbgsrv")

    def test_ida_from_the_hosts_copy_bound_to_the_guests_loopback(self):
        with self.assertRaisesRegex(observe.Missing, "no IDA's Windows remote debugging server set up"):
            self.start("ida")
        z = tmpdir(self) / "dbgsrv.zip"
        with zipfile.ZipFile(z, "w") as f:
            f.writestr("dbgsrv/win64_remote.exe", "MZ ida")
        observe.setup("ida", z)
        self.addCleanup(shutil.rmtree, observe.Host.setup_dir("ida"), ignore_errors=True)
        # IDA 7's name is not what was set up: said before anything starts.
        with self.assertRaisesRegex(observe.Missing, "win64_remote64.exe is not among the files set up "
                                                     r"\(win64_remote.exe\): --set server=win64_remote.exe"):
            self.start("ida", options={"server": "win64_remote64.exe"})
        exe = "C:\\desk\\observe\\tools\\ida\\win64_remote.exe"
        with_server(self.guest, exe, 23946)
        del self.guest.disk[exe]  # it comes from the host
        s = self.start("ida", options={"password": "s3cret"})
        self.assertIn(exe, self.guest.pushed)
        # In the server's environment, never on a command line, in the session or its report.
        self.assertEqual(self.guest.server, ["-i127.0.0.1", "-p23946"])
        self.assertEqual(self.guest.server_env, {"IDA_DBGSRV_PASSWD": "s3cret"})
        self.assertEqual(s.tool["listening"], ["127.0.0.1"])
        self.assertNotIn("s3cret", json.dumps([params for _, params in self.guest.calls]))
        self.assertNotIn("s3cret", (self.run.dir / "observe" / "sessions" / f"{s.id}.json").read_text())
        self.assertEqual(Path(s.tool["password_file"]).read_text().strip(), "s3cret")
        self.assertEqual(Path(s.tool["password_file"]).stat().st_mode & 0o777, 0o600)
        s, _ = self.stop()
        self.assertFalse(Path(s.tool["password_file"]).exists())
        self.assertIn("password protected: the one given", observe.provider("ida").summary(s))
        # None given: one desk makes, as long and random as a password should be.
        s = self.start("ida")
        made = self.guest.server_env["IDA_DBGSRV_PASSWD"]
        self.assertGreaterEqual(len(made), 24)
        self.assertEqual(Path(s.tool["password_file"]).read_text().strip(), made)
        self.stop()


class Registry(unittest.TestCase):
    def test_secret_option_keys_normalized_by_cli_are_redacted_from_the_timeline(self):
        from desk.cli import _observe_options, build_parser
        from desk.runs import logged_argv

        marker = "SYNTHETIC_REDACTION_MARKER"
        parser = build_parser()
        for key in ("password", "password ", " password", " password "):
            for inline in (False, True):
                for flag in ("--s", "--se", "--set"):
                    with self.subTest(key=key, inline=inline, flag=flag):
                        value = f"{key}={marker}"
                        self.assertEqual(_observe_options([value]), {"password": marker})
                        option = [f"{flag}={value}"] if inline else [flag, value]
                        argv = ["observe", "start", "ida", *option, "--set", "port=23946"]
                        original = list(argv)
                        parsed = parser.parse_args(argv)
                        self.assertEqual(_observe_options(parsed.set)["password"], marker)
                        logged = logged_argv(argv)
                        self.assertNotIn(marker, json.dumps(logged))
                        self.assertIn("port=23946", logged)
                        self.assertEqual(argv, original)

    def test_every_provider_says_what_it_is(self):
        for p in observe.PROVIDERS.values():
            with self.subTest(p=p.name):
                self.assertTrue(p.title and p.notes or p.name in ("wpr", "etw"))
                self.assertEqual(p.families, ("windows",))
                p.defaults()


if __name__ == "__main__":
    unittest.main()
