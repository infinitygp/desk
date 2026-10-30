"""The specialist providers with the guest in memory: packet capture (pktmon, netsh trace), IPC
endpoints, USBPcap and debugger servers. Their tools' real behaviour on Windows is for
docs/observe/validation.md (WIN-NET-*, WIN-IPC-*, HW-USB-*, WIN-DBG-*)."""

import base64
import json
import shutil
import tempfile
import unittest
import zipfile
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
    def test_every_provider_says_what_it_is(self):
        for p in observe.PROVIDERS.values():
            with self.subTest(p=p.name):
                self.assertTrue(p.title and p.notes or p.name in ("wpr", "etw"))
                self.assertEqual(p.families, ("windows",))
                p.defaults()


if __name__ == "__main__":
    unittest.main()
