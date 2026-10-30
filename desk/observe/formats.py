"""What a capture file holds, read from the file itself: a trace counts as made only when its
format says so, never because the tool that wrote it exited 0. Each check returns `ok` (the file
is whole and readable as its format), `detail` (one line for the report) and the facts it read.

- PML (Process Monitor): the header layout is not documented by Microsoft; the offsets here are
  those of procmon-parser (github.com/eronnen/procmon-parser), format version 9.
- pcap (USBPcap) and pcapng (pktmon etl2pcap): the formats are documented (pcap: libpcap's
  file format; pcapng: draft-ietf-opsawg-pcapng), so whole files and cut-off ones are told apart.
- ETL has no header desk could check without Windows' own tools; its check says only that the
  file is there and not empty, and the providers add what tracerpt said of it when asked to.
"""

import csv
import struct
from pathlib import Path

PML_SIGNATURE = b"PML_"
PML_VERSION = 9
# signature, version, is_64bit, computer name (0x20 bytes), system root (0x208 bytes), events at
# 0x234, unused, then the offsets of the events, the events' offset array, the process, string and
# icon tables (procmon-parser's PmlHeader; so laid out by Process Monitor 4.01 on Windows 11 26H2).
_PML = struct.Struct("<4sII32s520sIQQQQQQ")
LINKTYPE_USBPCAP = 249
LINKTYPES = {1: "Ethernet", 101: "raw IP", 113: "Linux cooked", LINKTYPE_USBPCAP: "USBPcap"}


def _result(ok: bool, detail: str, **facts) -> dict:
    return {"ok": ok, "detail": detail, **facts}


def _utf16(raw: bytes) -> str:
    return raw.decode("utf-16-le", errors="replace").split("\0", 1)[0]


def pml(path: Path) -> dict:
    """A Process Monitor log: its header, and whether the tables ProcMon writes when it closes the
    file are there (a ProcMon that was killed leaves them out)."""
    size = path.stat().st_size
    with path.open("rb") as f:
        head = f.read(_PML.size)
    if len(head) < 4 or head[:4] != PML_SIGNATURE:
        return _result(False, "not a Process Monitor log (no PML_ signature)", format="pml", bytes=size)
    if len(head) < _PML.size:
        return _result(False, f"cut off inside its header ({size} bytes)", format="pml", bytes=size)
    (_, version, is64, computer, root, events, _unused, events_at, offsets_at, processes_at, strings_at,
     icons_at) = _PML.unpack(head)
    facts = {"format": "pml", "bytes": size, "version": version, "is_64bit": bool(is64),
             "computer": _utf16(computer), "system_root": _utf16(root), "events": events}
    if version != PML_VERSION:
        return _result(False, f"PML version {version}, which desk does not know (it reads {PML_VERSION})", **facts)
    tables = {"events": events_at, "event offsets": offsets_at, "process table": processes_at,
              "string table": strings_at, "icon table": icons_at}
    missing = [name for name, at in tables.items() if at == 0]
    beyond = [name for name, at in tables.items() if at >= size]
    if missing or beyond:
        why = (f"{', '.join(missing)} not written" if missing else f"{', '.join(beyond)} beyond the end of the file")
        return _result(False, f"incomplete: {why} (ProcMon did not close the log, or the copy is cut off)", **facts)
    if offsets_at + 5 * events > size:
        return _result(False, f"incomplete: the offsets of its {events} events run past the end of the file", **facts)
    return _result(True, f"Process Monitor log, {events} events, from {facts['computer'] or 'an unnamed computer'}",
                   **facts)


_PCAP_MAGIC = {b"\xd4\xc3\xb2\xa1": ("<", "us"), b"\xa1\xb2\xc3\xd4": (">", "us"),
               b"\x4d\x3c\xb2\xa1": ("<", "ns"), b"\xa1\xb2\x3c\x4d": (">", "ns")}


def pcap(path: Path) -> dict:
    """A libpcap file: its link type and every record, the last one possibly cut off (a capture
    tool that was stopped hard)."""
    size = path.stat().st_size
    with path.open("rb") as f:
        head = f.read(24)
        if len(head) < 24 or head[:4] not in _PCAP_MAGIC:
            return _result(False, "not a pcap file (no pcap magic number)", format="pcap", bytes=size)
        order, resolution = _PCAP_MAGIC[head[:4]]
        major, minor, _zone, _sigfigs, snaplen, linktype = struct.unpack(order + "HHiIII", head[4:])
        facts = {"format": "pcap", "bytes": size, "version": f"{major}.{minor}", "snaplen": snaplen,
                 "linktype": linktype, "link": LINKTYPES.get(linktype, f"linktype {linktype}"),
                 "resolution": resolution}
        packets = captured = 0
        first = last = None
        while True:
            rec = f.read(16)
            if not rec:
                break
            if len(rec) < 16:
                return _result(False, f"cut off in the header of record {packets + 1}", packets=packets, **facts)
            sec, frac, incl, orig = struct.unpack(order + "IIII", rec)
            data = f.read(incl)
            if len(data) < incl:
                return _result(False, f"cut off in record {packets + 1} ({len(data)} of {incl} bytes)",
                               packets=packets, **facts)
            stamp = sec + frac / (1e9 if resolution == "ns" else 1e6)
            first = stamp if first is None else first
            last = stamp
            packets += 1
            captured += incl
    return _result(True, f"pcap ({facts['link']}), {packets} packets", packets=packets, captured_bytes=captured,
                   first=first, last=last, **facts)


_SHB = 0x0A0D0D0A
_BLOCK_NAMES = {1: "interface", 2: "packet", 3: "simple packet", 5: "statistics", 6: "enhanced packet"}


def pcapng(path: Path) -> dict:
    """A pcapng file: its sections, interfaces (link types) and packets, block by block."""
    size = path.stat().st_size
    facts = {"format": "pcapng", "bytes": size}
    with path.open("rb") as f:
        head = f.read(12)
        if len(head) < 12 or struct.unpack("<I", head[:4])[0] != _SHB:
            return _result(False, "not a pcapng file (no section header block)", **facts)
        bom = head[8:12]
        if bom == b"\x4d\x3c\x2b\x1a":
            order = "<"
        elif bom == b"\x1a\x2b\x3c\x4d":
            order = ">"
        else:
            return _result(False, "not a pcapng file (unknown byte-order magic)", **facts)
        f.seek(0)
        sections = packets = 0
        links: list[int] = []
        at = 0
        while at < size:
            block = f.read(8)
            if len(block) < 8:
                return _result(False, f"cut off at byte {at}", sections=sections, packets=packets,
                               links=links, **facts)
            kind, length = struct.unpack(order + "II", block)
            if kind == _SHB:
                # A new section may change the byte order.
                bom = f.read(4)
                order = "<" if bom == b"\x4d\x3c\x2b\x1a" else ">" if bom == b"\x1a\x2b\x3c\x4d" else None
                if order is None:
                    return _result(False, f"section {sections + 1} has an unknown byte-order magic",
                                   sections=sections, packets=packets, links=links, **facts)
                length = struct.unpack(order + "I", block[4:])[0]
                body = bom + f.read(length - 12 - 4)
                sections += 1
            else:
                body = f.read(max(0, length - 12))
            if length < 12 or length % 4 or len(body) < length - 12:
                return _result(False, f"cut off in a {_BLOCK_NAMES.get(kind, 'block')} block at byte {at}",
                               sections=sections, packets=packets, links=links, **facts)
            trailer = f.read(4)
            if len(trailer) < 4 or struct.unpack(order + "I", trailer)[0] != length:
                return _result(False, f"block at byte {at} does not end with its length (damaged or cut off)",
                               sections=sections, packets=packets, links=links, **facts)
            if kind == 1 and len(body) >= 2:
                links.append(struct.unpack(order + "H", body[:2])[0])
            elif kind in (2, 3, 6):
                packets += 1
            at += length
    if not sections:
        return _result(False, "no section header block", **facts)
    names = sorted({LINKTYPES.get(lt, f"linktype {lt}") for lt in links})
    return _result(True, f"pcapng, {len(links)} interface(s) ({', '.join(names) or 'none'}), {packets} packets",
                   sections=sections, packets=packets, links=links, **facts)


def etl(path: Path) -> dict:
    """An ETW trace: there and not empty. Whether Windows can read it is tracerpt's to say."""
    size = path.stat().st_size
    if size == 0:
        return _result(False, "empty trace file", format="etl", bytes=0)
    return _result(True, f"ETW trace, {size} bytes (contents not read on the host)", format="etl", bytes=size,
                   level="size")


# Columns of Process Monitor's default CSV export.
PROCMON_COLUMNS = ("Time of Day", "Process Name", "PID", "Operation", "Path", "Result", "Detail")


def procmon_csv(path: Path) -> dict:
    """A Process Monitor log saved as CSV (procmon /OpenLog ... /SaveAs ...csv)."""
    size = path.stat().st_size
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header or not set(PROCMON_COLUMNS) <= set(header):
            return _result(False, "not a Process Monitor CSV export (its columns are missing)", format="csv",
                           bytes=size)
        rows = sum(1 for _ in reader)
    return _result(True, f"Process Monitor CSV, {rows} events", format="csv", bytes=size, events=rows,
                   columns=header)


def text(path: Path) -> dict:
    size = path.stat().st_size
    return _result(True, f"{size} bytes of text", format="text", bytes=size)


CHECKS = {"pml": pml, "pcap": pcap, "pcapng": pcapng, "etl": etl, "procmon-csv": procmon_csv, "text": text,
          "json": text}


def check(path: Path, kind: str) -> dict:
    try:
        return CHECKS[kind](path)
    except OSError as e:
        return _result(False, f"cannot be read: {e.strerror or e}", format=kind)
