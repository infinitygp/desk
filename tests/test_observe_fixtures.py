"""The validation fixtures stay usable: the scenarios load as desk reads them, and expect.py finds
(and misses) what a workload's manifest names in captures made up here. Real captures of the
workload are docs/observe/validation.md's."""

import json
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from desk import scenario
from desk.config import load_image
from tests.observe_fake import pcap_bytes

FIXTURES = Path(__file__).resolve().parent.parent / "docs" / "observe" / "fixtures"
EXPECT = FIXTURES / "expect.py"

MANIFEST = {"token": "ab12", "pid": 500, "steps": {
    "process": {"exit_child": {"pid": 1234, "image": "cmd.exe", "exit_code": 7},
                "killed_child": {"pid": 1240, "image": "PING.EXE"}},
    "files": {"created": "C:\\desk\\observe\\workload\\ab12\\ab12-a.txt",
              "renamed_to": "C:\\desk\\observe\\workload\\ab12\\ab12-b.txt",
              "deleted": "C:\\desk\\observe\\workload\\ab12\\ab12-b.txt"},
    "registry": {"key": "HKCU\\Software\\DeskObserveFixture\\ab12", "value": "value-ab12"},
    "pipe": {"pipe": "desk-fixture-ab12"}, "tcp": {"port": 50123}, "dns": {"query": "ab12.desk-observe.invalid"}},
    "hold": {"pipe": "desk-fixture-hold-ab12", "port": 50200, "pid": 500}}

CSV_ROWS = [
    ("powershell.exe", "500", "Process Create", "C:\\Windows\\System32\\cmd.exe", "SUCCESS", "PID: 1234, Command line: cmd /c exit 7"),
    ("cmd.exe", "1234", "Process Exit", "", "SUCCESS", "Exit Status: 7, User Time: 0.0"),
    ("PING.EXE", "1240", "Process Exit", "", "SUCCESS", "Exit Status: 1"),
    ("powershell.exe", "500", "CreateFile", "C:\\desk\\observe\\workload\\ab12\\ab12-a.txt", "SUCCESS", ""),
    ("powershell.exe", "500", "SetRenameInformationFile", "C:\\desk\\observe\\workload\\ab12\\ab12-a.txt", "SUCCESS",
     "ReplaceIfExists: False, FileName: C:\\desk\\observe\\workload\\ab12\\ab12-b.txt"),
    ("powershell.exe", "500", "SetDispositionInformationEx", "C:\\desk\\observe\\workload\\ab12\\ab12-b.txt", "SUCCESS",
     "Flags: FILE_DISPOSITION_DELETE"),
    ("powershell.exe", "500", "RegCreateKey", "HKCU\\Software\\DeskObserveFixture\\ab12", "SUCCESS", ""),
    ("powershell.exe", "500", "RegSetValue", "HKCU\\Software\\DeskObserveFixture\\ab12\\value-ab12", "SUCCESS", ""),
    ("powershell.exe", "500", "RegDeleteValue", "HKCU\\Software\\DeskObserveFixture\\ab12\\value-ab12", "SUCCESS", ""),
    ("powershell.exe", "500", "RegDeleteKey", "HKCU\\Software\\DeskObserveFixture\\ab12", "SUCCESS", ""),
    ("powershell.exe", "500", "CreateFile", "\\\\.\\Pipe\\desk-fixture-ab12", "SUCCESS", ""),
    ("powershell.exe", "500", "TCP Connect", "localhost:50124 -> localhost:50123", "SUCCESS", ""),
    ("powershell.exe", "600", "CreateFile", "C:\\desk\\observe\\marks\\mark-0001.txt", "SUCCESS", ""),
]


def pcapng_with(payload: bytes) -> bytes:
    def block(kind, body):
        body += b"\0" * (-len(body) % 4)
        return struct.pack("<II", kind, 12 + len(body)) + body + struct.pack("<I", 12 + len(body))

    return (block(0x0A0D0D0A, struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1)) + block(1, struct.pack("<HHI", 1, 0, 128))
            + block(6, struct.pack("<IIIII", 0, 0, 0, len(payload), len(payload)) + payload))


class Expect(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d)
        (self.d / "manifest.json").write_text(json.dumps(MANIFEST))

    def expect(self, *args) -> tuple[int, str]:
        r = subprocess.run([sys.executable, str(EXPECT), str(self.d / "manifest.json"), *args], capture_output=True,
                           text=True)
        return r.returncode, r.stdout + r.stderr

    def csv(self, rows) -> Path:
        import csv

        p = self.d / "procmon.csv"
        with p.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, quoting=csv.QUOTE_ALL)
            w.writerow(["Time of Day", "Process Name", "PID", "Operation", "Path", "Result", "Detail"])
            for r in rows:
                w.writerow(["1:00:00 PM", *r])
        return p

    def test_a_procmon_export_with_every_operation_passes(self):
        rc, out = self.expect("--procmon-csv", str(self.csv(CSV_ROWS)))
        self.assertEqual(rc, 0, out)
        self.assertEqual((out.count("PASS"), out.count("FAIL")), (13, 0))

    def test_an_operation_missing_fails_it(self):
        rows = [r for r in CSV_ROWS if r[2] != "RegDeleteKey"]
        rc, out = self.expect("--procmon-csv", str(self.csv(rows)))
        self.assertEqual(rc, 1)
        self.assertIn("FAIL  procmon: RegDeleteKey", out)

    def test_the_dns_query_in_packets(self):
        query = b"\x04ab12\x0edesk-observe\x07invalid\x00"
        (self.d / "net.pcapng").write_bytes(pcapng_with(b"\0" * 54 + query))
        self.assertEqual(self.expect("--pcapng", str(self.d / "net.pcapng"))[0], 0)
        (self.d / "net.pcapng").write_bytes(pcapng_with(b"\0" * 80))
        self.assertEqual(self.expect("--pcapng", str(self.d / "net.pcapng"))[0], 1)

    def test_ipc_listings_and_tracerpt_text(self):
        (self.d / "ipc-001-start.json").write_text(json.dumps({"pipes": ["desk-fixture-hold-ab12"], "tcp": [
            {"local": "127.0.0.1", "lport": 50200, "remote": "0.0.0.0", "rport": 0, "state": "Listen", "pid": 500}]}))
        self.assertEqual(self.expect("--ipc-dir", str(self.d))[0], 0)
        (self.d / "events.csv").write_text("ProcessStart, 1234, \\Device\\HarddiskVolume3\\Windows\\System32\\cmd.exe\n"
                                           "DeskMark1, 0\n")
        self.assertEqual(self.expect("--tracerpt-csv", str(self.d / "events.csv"))[0], 0)
        (self.d / "events.csv").write_text("ProcessStart, 999, other.exe\n")
        rc, out = self.expect("--tracerpt-csv", str(self.d / "events.csv"))
        self.assertEqual(rc, 1)
        self.assertIn("FAIL  etw: desk's marker event", out)

    def test_usb_captures_and_unreadable_inputs(self):
        (self.d / "usb.pcap").write_bytes(pcap_bytes(3) + struct.pack("<HH", 0x0FD9, 0x0084))
        r = subprocess.run([sys.executable, str(EXPECT), "--usb-pcap", str(self.d / "usb.pcap"), "--usb-id",
                            "0fd9:0084"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.expect("--procmon-csv", str(self.d / "missing.csv"))[0], 2)
        self.assertEqual(self.expect()[0], 2)


class Scenarios(unittest.TestCase):
    def test_every_fixture_scenario_loads_for_windows(self):
        files = sorted((FIXTURES / "scenarios").glob("*.toml"))
        self.assertEqual(len(files), 5)
        for f in files:
            with self.subTest(f=f.name):
                data = scenario.load(f)
                scenario.check_observations(data, load_image(data["image"]))
                self.assertTrue(scenario.observations(f, data))
                for pattern in data["build"]["artifacts"]:
                    self.assertTrue((FIXTURES.parents[2] / pattern).exists(), pattern)


if __name__ == "__main__":
    unittest.main()
