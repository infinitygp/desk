#!/usr/bin/python3
"""Whether a capture holds what workload.ps1 did: the events named in its manifest, looked for in
the files `desk observe stop` copied to the host (or the report's observe/ folder).

    python3 tests/fixtures/observe/expect.py manifest.json --procmon-csv observe/s1/procmon.csv
    python3 tests/fixtures/observe/expect.py manifest.json --tracerpt-csv observe/s2/events.csv
    python3 tests/fixtures/observe/expect.py manifest.json --pcapng observe/s3/net*.pcapng
    python3 tests/fixtures/observe/expect.py manifest.json --ipc-dir observe/s4
    python3 tests/fixtures/observe/expect.py --usb-pcap observe/s5/usb.pcap --usb-id 0fd9:0084

One line per expectation, PASS, FAIL or SKIP (the manifest has no such step); exit 1 when one
failed, 2 when the files cannot be read. The text matched in ProcMon's CSV are its operation names
as its UI shows them; in tracerpt's CSV only the PIDs, image names and marker names, as its column
layout differs between event types. Where a check proves too strict or too loose on real
captures, this file is fixed."""

import argparse
import csv
import glob
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from desk.observe import formats  # noqa: E402

results: list[tuple[str, str, str]] = []


def say(state: str, what: str, detail: str = "") -> None:
    results.append((state, what, detail))
    print(f"{state:<5} {what}" + (f": {detail}" if detail else ""))


def check(ok: bool, what: str, detail: str = "") -> None:
    say("PASS" if ok else "FAIL", what, detail)


def procmon(manifest: dict, path: Path) -> None:
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as f:
        rows = list(csv.DictReader(f))
    steps = manifest["steps"]

    def any_row(pred) -> bool:
        return any(pred(r) for r in rows)

    if "process" in steps:
        child = steps["process"]["exit_child"]
        check(any_row(lambda r: r["Operation"] == "Process Create" and f"PID: {child['pid']}," in r["Detail"]),
              "procmon: Process Create of the child", f"pid {child['pid']}")
        check(any_row(lambda r: r["Operation"] == "Process Exit" and r["PID"] == str(child["pid"])
                      and "Exit Status: 7" in r["Detail"]), "procmon: Process Exit of the child with status 7")
        killed = steps["process"]["killed_child"]
        check(any_row(lambda r: r["Operation"] == "Process Exit" and r["PID"] == str(killed["pid"])),
              "procmon: Process Exit of the child ended from outside", f"pid {killed['pid']}")
    else:
        say("SKIP", "procmon: processes", "no process step")
    if "files" in steps:
        s = steps["files"]
        check(any_row(lambda r: r["Operation"] == "CreateFile" and r["Path"].lower() == s["created"].lower()),
              "procmon: file created", s["created"])
        check(any_row(lambda r: r["Operation"].startswith("SetRenameInformation") and
                      r["Path"].lower() == s["created"].lower() and s["renamed_to"].lower() in r["Detail"].lower()),
              "procmon: file renamed", s["renamed_to"])
        check(any_row(lambda r: r["Operation"].startswith("SetDisposition") and
                      r["Path"].lower() == s["deleted"].lower()), "procmon: file deleted", s["deleted"])
    if "registry" in steps:
        s = steps["registry"]
        for op, suffix in (("RegCreateKey", s["key"]), ("RegSetValue", f"{s['key']}\\{s['value']}"),
                           ("RegDeleteValue", f"{s['key']}\\{s['value']}"), ("RegDeleteKey", s["key"])):
            check(any_row(lambda r, op=op, suffix=suffix: r["Operation"] == op and
                          r["Path"].lower().endswith(suffix.lower())), f"procmon: {op}", suffix)
    if "pipe" in steps:
        name = steps["pipe"]["pipe"]
        check(any_row(lambda r: r["Operation"] == "CreateFile" and name.lower() in r["Path"].lower()),
              "procmon: named pipe opened", name)
    if "tcp" in steps:
        port = steps["tcp"]["port"]
        check(any_row(lambda r: r["Operation"].startswith("TCP") and f":{port}" in r["Path"]),
              "procmon: loopback TCP to the workload's port", str(port))
    marks = [r for r in rows if "\\desk\\observe\\marks\\mark-" in r["Path"].lower().replace("/", "\\")]
    say("PASS" if marks else "SKIP", "procmon: desk observe mark files",
        f"{len(marks)} operations" if marks else "none (no mark during the capture?)")


def tracerpt(manifest: dict, path: Path) -> None:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    steps = manifest["steps"]
    if "process" in steps:
        child = steps["process"]["exit_child"]
        lines = [ln for ln in text.splitlines() if str(child["pid"]) in ln]
        check(any("cmd.exe" in ln.lower() for ln in lines), "etw: an event of the child process (pid and cmd.exe)",
              f"pid {child['pid']}, {len(lines)} lines with it")
    check("DeskMark" in text, "etw: desk's marker event (Desk-Observe-Marker)",
          "found" if "DeskMark" in text else "no DeskMark event: no mark, or the marker provider not enabled")


def packets(path: Path):
    """Packet data of a pcapng file, block by block."""
    data = path.read_bytes()
    order, at = "<", 0
    while at + 12 <= len(data):
        kind, length = struct.unpack_from(order + "II", data, at)
        if kind == 0x0A0D0D0A:
            order = "<" if data[at + 8:at + 12] == b"\x4d\x3c\x2b\x1a" else ">"
            length = struct.unpack_from(order + "I", data, at + 4)[0]
        elif kind == 6:
            captured = struct.unpack_from(order + "I", data, at + 20)[0]
            yield data[at + 28:at + 28 + captured]
        elif kind == 3:
            yield data[at + 12:at + length - 4]
        if length < 12:
            break
        at += length


def pcapng(manifest: dict, paths: list[Path]) -> None:
    """One file or a capture's segments (pktmon's net1.pcapng, net2.pcapng...)."""
    for path in paths:
        c = formats.pcapng(path)
        check(c["ok"], f"pcapng: whole file {path.name}", c["detail"])
    if "dns" not in manifest["steps"]:
        say("SKIP", "pcapng: the workload's DNS query", "no dns step")
        return
    label = manifest["token"].encode()
    wire = bytes([len(label)]) + label
    hits = sum(1 for path in paths for p in packets(path) if wire in p)
    check(hits > 0, "pcapng: the workload's DNS query left through the adapter", f"{hits} packets with "
          f"{manifest['steps']['dns']['query']}")


def ipc(manifest: dict, folder: Path) -> None:
    listings = [json.loads(Path(f).read_text()) for f in sorted(glob.glob(str(folder / "ipc-*.json")))]
    check(bool(listings), "ipc: listings", f"{len(listings)} in {folder}")
    hold = manifest.get("hold")
    if not hold:
        say("SKIP", "ipc: the workload's held endpoints", "run the workload with -HoldSeconds and mark meanwhile")
        return
    check(any(hold["pipe"] in snap["pipes"] for snap in listings), "ipc: the held pipe listed", hold["pipe"])
    check(any(c["lport"] == hold["port"] and c["state"] == "Listen" and c["pid"] == hold["pid"]
              for snap in listings for c in snap["tcp"]), "ipc: the held TCP listener with the workload's pid",
          f"port {hold['port']}, pid {hold['pid']}")


def usb(path: Path, ident: str | None) -> None:
    c = formats.pcap(path)
    check(c.get("linktype") == formats.LINKTYPE_USBPCAP, "usb: a USBPcap capture", c["detail"])
    check((c.get("packets") or 0) > 0, "usb: packets captured", str(c.get("packets")))
    if ident:
        vid, pid = (int(x, 16) for x in ident.split(":"))
        needle = struct.pack("<HH", vid, pid)  # idVendor, idProduct as a device descriptor holds them
        check(needle in path.read_bytes(), f"usb: the device descriptor of {ident}",
              "found" if needle in path.read_bytes() else "not seen: the device was not (re)enumerated while capturing")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest", nargs="?")
    ap.add_argument("--procmon-csv", type=Path)
    ap.add_argument("--tracerpt-csv", type=Path)
    ap.add_argument("--pcapng", type=Path, nargs="+")
    ap.add_argument("--ipc-dir", type=Path)
    ap.add_argument("--usb-pcap", type=Path)
    ap.add_argument("--usb-id", help="VENDOR:PRODUCT of a device handed to the run")
    a = ap.parse_args()
    try:
        manifest = json.loads(Path(a.manifest).read_text(encoding="utf-8-sig")) if a.manifest else None
        if a.procmon_csv:
            procmon(manifest, a.procmon_csv)
        if a.tracerpt_csv:
            tracerpt(manifest, a.tracerpt_csv)
        if a.pcapng:
            pcapng(manifest, a.pcapng)
        if a.ipc_dir:
            ipc(manifest, a.ipc_dir)
        if a.usb_pcap:
            usb(a.usb_pcap, a.usb_id)
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"expect: cannot read the inputs: {e}", file=sys.stderr)
        sys.exit(2)
    if not results:
        print("expect: nothing to check (give a capture)", file=sys.stderr)
        sys.exit(2)
    sys.exit(1 if any(state == "FAIL" for state, _, _ in results) else 0)


if __name__ == "__main__":
    main()
