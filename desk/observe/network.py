"""Packet capture inside the guest with what Windows has:

- `pktmon` (Packet Monitor, Windows 10 1809 and later): `pktmon start --capture` with the
  components (--comp) and the bytes kept of each packet (--pkt-size, 0: all), in multi-file mode:
  segments net1.etl, net2.etl... of --file-size MB each, as its default circular file overwrites
  what does not fit, and one that went round kept no packet at all (Windows 11 26H2, pktmon saying
  no events were lost). `pktmon stop`, then every segment through `pktmon etl2pcap` to PCAPNG,
  which desk reads back. A segment missing from the row makes the capture incomplete.
- `netsh` (netsh trace): `netsh trace start capture=yes` with netsh's capture filters
  (`netsh trace show capturefilterhelp`), for when a filter at capture time is needed. Its ETL
  stays ETL: the packets are in it, and pktmon etl2pcap formats none of them (0 on Windows 11
  26H2), so desk makes no PCAPNG of it.

Both are one capture per Windows, which desk does not share: it starts only while no capture
of either runs, and stops only one writing its own file.

What a capture holds is metadata of every packet (addresses, ports, sizes, times) and of its
payload only the bytes kept per packet; encrypted traffic (TLS) stays encrypted. Traffic over
loopback (127.0.0.1) does not pass the network adapter and is not seen with the nics component.
Events ETW could not take in time are lost even so: the trace session's counters and pktmon's
own word on them are in the report."""

import re

from .base import ArtifactSpec, Capability, Conflict, Option, Provider, command, tool_run
from .guestops import ObserveError, win
from .session import Session

# The ETW session names the tools record under, to tell whether a capture runs: PktMon, and
# NetTrace with a suffix of its own (NetTrace-2 on Windows Server 2025, NetTrace-08c1723f on
# Windows 11 26H2), which counts as the same. Whose capture one is, the file it writes says.
PKTMON_SESSION = "PktMon"
NETSH_SESSION = "NetTrace"
# Segments came out 80-150 MiB at --file-size 64 (Windows 11 26H2), and the one being written shows
# no size until it is closed: the watchdog's limit holds to within about two segments.
MIN_PKTMON_MB = 64
_SEGMENT = re.compile(r"net(\d+)\.etl", re.I)
_LOST = re.compile(r"\((No|\d+) events? lost\)", re.I)
_FILTER = re.compile(r"[A-Za-z][A-Za-z0-9.]*=[^\s=]+")


def _filter(v: str) -> str | None:
    return None if _FILTER.fullmatch(v) else f"{v!r} is not a netsh capture filter KEY=VALUE (IPv4.Address=10.0.2.2)"


def _capturing(etw_names, names=(PKTMON_SESSION, NETSH_SESSION)) -> str | None:
    """The first trace session of these tools' names (NetTrace, NetTrace-2) among `etw_names`."""
    pattern = re.compile("|".join(re.escape(x) for x in names) + r"(-[0-9A-Za-z]+)?", re.I)
    return next((n for n in etw_names or [] if pattern.fullmatch(n)), None)


class _Capture(Provider):
    resource = "packet-capture"

    def probe_request(self, opts: dict, host) -> dict:
        return {"commands": ["pktmon.exe", "netsh.exe"], "etw": True}

    def assess(self, probe: dict, opts: dict, host) -> Capability:
        cap = super().assess(probe, opts, host)
        found = (probe.get("commands") or {}).get(self.tool_exe)
        if not found:
            cap.problems.append(f"{self.tool_exe} is not in the guest" +
                                (" (Packet Monitor came with Windows 10 1809)" if self.tool_exe == "pktmon.exe" else ""))
        else:
            cap.version = found.get("version") or ""
        cap.details["pcapng"] = bool((probe.get("commands") or {}).get("pktmon.exe"))
        cap.available = not cap.problems
        return cap

    def conflicts(self, probe: dict, session: Session) -> str | None:
        running = _capturing(probe.get("etw_sessions"))
        if running:
            return (f"a packet capture runs in the guest already (trace session {running}); desk does not stop "
                    "captures it did not start: pktmon stop or netsh trace stop first")
        return None

    def etl(self, session: Session) -> str:
        return win(session.guest_dir, "net.etl")

    def capturing(self, state: dict, session: Session) -> bool | None:
        names = state.get("etw_sessions")
        return None if names is None else bool(_capturing(names, self.session_names))

    def watch_files(self, session: Session) -> list[str]:
        return [self.etl(session)]

    def _running(self, guest, session: Session) -> bool:
        """Whether this session's capture still runs: a trace session of the tool that writes its
        file. None of the tool's runs: False; one writing another file: Conflict. Where Windows does
        not list trace sessions, it is taken to run, so that its stop is tried."""
        names = guest.call("etw", names=[]).get("names")
        if names is None:
            return True
        running = [n for n in names if _capturing([n], self.session_names)]
        if not running:
            return False
        sessions = guest.call("etw", names=running).get("sessions") or {}
        files = {n: (p or {}).get("LocalFilePath") for n, p in sessions.items()}
        if any(f and self._ours(session, f) for f in files.values()):
            return True
        raise Conflict(f"{self.title} runs, but not into this session's file (trace session "
                       + ", ".join(f"{n} writes {f or 'no file it says'}" for n, f in files.items())
                       + "): it is somebody else's capture")

    def _ours(self, session: Session, path: str) -> bool:
        return path.lower() == self.etl(session).lower()

    def artifacts(self, session: Session) -> list[ArtifactSpec]:
        return [ArtifactSpec("net.etl", self.etl(session), "etl", description=f"{self.title} capture (ETL)")]

    def summary(self, session: Session) -> list[str]:
        kept = session.stats.get("payload")
        return [kept] if kept else []


class Pktmon(_Capture):
    name = "pktmon"
    title = "Packet Monitor"
    tool_exe = "pktmon.exe"
    session_names = (PKTMON_SESSION,)
    OPTIONS = {
        "components": Option(str, "nics", "pktmon --comp: nics (the network adapters), all, or component ids"),
        "pkt_size": Option(int, 128, "bytes kept of each packet (pktmon --pkt-size; 0: whole packets)"),
        "pcapng": Option(bool, True, "turn every segment into PCAPNG in the guest (pktmon etl2pcap)"),
    }
    notes = ("packet metadata of every packet the adapters pass; payload only up to pkt_size bytes per packet "
             "(128 by default), encrypted traffic stays encrypted; loopback traffic is not seen with the nics "
             "component",
             "kept in segments net1.etl, net2.etl..., each with a PCAPNG; the capture stops within about two "
             "segments (150 MiB each, about 1 GiB each above a 1 GiB limit) of its size limit")

    def check_options(self, opts: dict) -> None:
        if opts["pkt_size"] < 0:
            raise ObserveError("pkt_size is 0 (whole packets) or more")
        if not re.fullmatch(r"nics|all|\d+( \d+)*", opts["components"]):
            raise ObserveError(f"components is nics, all or component ids (got {opts['components']!r})")

    def _ours(self, session: Session, path: str) -> bool:
        return path.lower().rsplit("\\", 1)[0] == session.guest_dir.lower()

    def start(self, guest, session: Session, host) -> None:
        mb = max(1, (session.limits.get("max_bytes") or 512 << 20) >> 20)
        if mb < MIN_PKTMON_MB:
            raise ObserveError(f"pktmon writes segments of 64 MiB and more: --max-mb {MIN_PKTMON_MB} or more")
        segment = 64 if mb <= 1024 else 512
        args = ["start", "--capture", "--comp", *session.options["components"].split(),
                "--pkt-size", str(session.options["pkt_size"]), "--file-name", self.etl(session),
                "--file-size", str(segment), "--log-mode", "multi-file"]
        tool_run(guest, "pktmon.exe", args, 120, "starting Packet Monitor")
        session.tool.update(etl=self.etl(session), segment_mb=segment)
        n = session.options["pkt_size"]
        session.stats["payload"] = ("whole packets kept" if n == 0 else f"{n} bytes kept of each packet (headers "
                                    "and the start of the payload)")

    def watch_files(self, session: Session) -> list[str]:
        return [session.guest_dir]

    def stop_commands(self, session: Session) -> list[dict]:
        return [command("pktmon.exe", ["stop"], 300)]

    def sample(self, guest, session: Session) -> dict:
        names = [n for n in guest.call("etw", names=[]).get("names") or [] if _capturing([n], self.session_names)]
        if not names:
            return {}
        sessions = guest.call("etw", names=names).get("sessions") or {}
        found = {k: v for p in sessions.values() for k, v in (p or {}).items()
                 if "lost" in k.lower() and isinstance(v, (int, float))}
        return {"lost": found} if found else {}

    def stop(self, guest, session: Session, host, force: bool = False) -> None:
        if self._running(guest, session):
            r = tool_run(guest, "pktmon.exe", ["stop"], 300, "stopping Packet Monitor")
            session.stats["pktmon_stop"] = " ".join((r.get("stdout") or "").split())[-300:]

    def found(self, guest, session: Session) -> None:
        listing = guest.files([session.guest_dir]).get(session.guest_dir) or {}
        segments = []
        for f in listing.get("files") or []:
            m = _SEGMENT.fullmatch(f["path"].rsplit("\\", 1)[-1])
            if m:
                segments.append({"n": int(m[1]), "path": f["path"], "bytes": f.get("size") or 0})
        session.tool["segments"] = sorted(segments, key=lambda x: x["n"])

    def _segments(self, session: Session) -> list[dict]:
        """Segments 1 to the last one with data; one missing from the row is one ("missing")."""
        segs = {x["n"]: x for x in session.tool.get("segments") or []}
        last = max([n for n, x in segs.items() if x["bytes"]] or [1])
        return [segs.get(n) or {"n": n, "path": win(session.guest_dir, f"net{n}.etl"), "bytes": 0, "missing": True}
                for n in range(1, last + 1)]

    def artifacts(self, session: Session) -> list[ArtifactSpec]:
        return [ArtifactSpec(f"net{x['n']}.etl", x["path"], "etl", description=f"Packet Monitor segment {x['n']} (ETL)")
                for x in self._segments(session)]

    def convert(self, guest, session: Session, host) -> list[ArtifactSpec]:
        if not session.options.get("pcapng"):
            return []
        out, said = [], {}
        for x in self._segments(session):
            if x.get("missing") or not x["bytes"]:
                continue
            pcap = win(session.guest_dir, f"net{x['n']}.pcapng")
            r = guest.exec("pktmon.exe", ["etl2pcap", x["path"], "--out", pcap], timeout=900)
            said[f"net{x['n']}"] = {"rc": r.get("rc"), "said": " ".join((r.get("stdout") or "").split())[-200:]}
            out.append(ArtifactSpec(f"net{x['n']}.pcapng", pcap, "pcapng", required=False,
                                    description=f"segment {x['n']} as PCAPNG (pktmon etl2pcap)"))
        session.stats["etl2pcap"] = said
        return out

    def summary(self, session: Session) -> list[str]:
        lines = super().summary(session)
        etls = [a for a in session.artifacts if a["kind"] == "etl"]
        pcaps = [a for a in session.artifacts if a["kind"] == "pcapng"]
        if etls:
            lines.append(f"{len(etls)} segment(s), {sum(a.get('guest_bytes') or 0 for a in etls) / 2**20:.1f} MiB")
        read = [a for a in pcaps if a.get("check") and a["check"].get("ok")]
        if pcaps:
            lines.append(f"PCAPNG: {sum(a['check'].get('packets', 0) for a in read)} packets in {len(read)} of "
                         f"{len(pcaps)} file(s)" + ("" if len(read) == len(pcaps) else
                                                    "; not read: " + ", ".join(f"{a['name']} ({a.get('detail')})"
                                                                               for a in pcaps if a not in read)))
        failed = {k: v for k, v in (session.stats.get("etl2pcap") or {}).items() if v.get("rc") != 0}
        for k, v in failed.items():
            lines.append(f"no PCAPNG of {k}.etl: pktmon etl2pcap exit {v.get('rc')}: {v.get('said')}")
        loss = session.stats.get("lost")
        said = session.stats.get("pktmon_stop") or " ".join(
            (r or {}).get("stdout") or "" for r in (session.stats.get("watchdog") or {}).get("stop") or [])
        m = _LOST.search(said or "")
        if loss:
            lines.append("events lost (as the trace session counted them before it stopped): "
                         + ", ".join(f"{k} {v}" for k, v in sorted(loss.items())))
        if m:
            lines.append(f"pktmon at its stop: {m[0].strip('()')}")
        if not loss and not m:
            lines.append("events lost: not measured")
        return lines


class NetshTrace(_Capture):
    name = "netsh"
    title = "netsh trace"
    tool_exe = "netsh.exe"
    session_names = (NETSH_SESSION,)
    OPTIONS = {
        "filters": Option(list, [], "netsh capture filters KEY=VALUE (IPv4.Address=10.0.2.2, Protocol=TCP; "
                          "netsh trace show capturefilterhelp)", check=_filter),
        "truncate": Option(int, 0, "bytes kept of each packet (packettruncatebytes; 0: netsh's default)"),
    }
    notes = ("packets as netsh's capture filters let them through; payload up to the truncation length, encrypted "
             "traffic stays encrypted",
             "ETL only: the packets are in it, and pktmon etl2pcap formats none of them, so there is no PCAPNG")

    def start(self, guest, session: Session, host) -> None:
        mb = max(1, (session.limits.get("max_bytes") or 512 << 20) >> 20)
        args = ["trace", "start", "capture=yes", "report=disabled", "persistent=no", "overwrite=yes",
                "filemode=single", f"maxsize={mb}", f"tracefile={self.etl(session)}", *session.options["filters"]]
        if session.options["truncate"]:
            args.append(f"packettruncatebytes={session.options['truncate']}")
        tool_run(guest, "netsh.exe", args, 180, "starting netsh trace")
        session.tool.update(etl=self.etl(session))
        n = session.options["truncate"]
        session.stats["payload"] = f"{n} bytes kept of each packet" if n else "netsh's default length kept per packet"

    def stop_commands(self, session: Session) -> list[dict]:
        return [command("netsh.exe", ["trace", "stop"], 600)]

    def stop(self, guest, session: Session, host, force: bool = False) -> None:
        if self._running(guest, session):
            tool_run(guest, "netsh.exe", ["trace", "stop"], 600, "stopping netsh trace")
