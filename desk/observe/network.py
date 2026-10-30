"""Packet capture inside the guest with what Windows has:

- `pktmon` (Packet Monitor, Windows 10 1809 and later): `pktmon start --capture` with the
  components (--comp), the bytes kept of each packet (--pkt-size, 0: all) and the file's size
  (--file-size, MB), `pktmon stop`, and `pktmon etl2pcap` to PCAPNG, which desk reads back.
- `netsh` (netsh trace): `netsh trace start capture=yes` with netsh's capture filters
  (`netsh trace show capturefilterhelp`), for when a filter at capture time is needed. Its ETL is
  turned into PCAPNG only on request, with pktmon etl2pcap, and only kept when the result reads
  as PCAPNG.

Both are one capture per Windows, which desk does not share: it starts only while no capture
of either runs, and stops only one writing its own file.

What a capture holds is metadata of every packet (addresses, ports, sizes, times) and of its
payload only the bytes kept per packet; encrypted traffic (TLS) stays encrypted. Traffic over
loopback (127.0.0.1) does not pass the network adapter and is not seen with the nics component."""

import re

from .base import ArtifactSpec, Capability, Conflict, Option, Provider, command, tool_run
from .guestops import ObserveError, win
from .session import Session

# The ETW session names the tools record under, to tell whether a capture runs: seen on Windows
# Server 2025 (Windows CI) as PktMon and NetTrace-2; a name with a -N suffix counts as the same.
PKTMON_SESSION = "PktMon"
NETSH_SESSION = "NetTrace"
_FILTER = re.compile(r"[A-Za-z][A-Za-z0-9.]*=[^\s=]+")


def _filter(v: str) -> str | None:
    return None if _FILTER.fullmatch(v) else f"{v!r} is not a netsh capture filter KEY=VALUE (IPv4.Address=10.0.2.2)"


def _capturing(etw_names, names=(PKTMON_SESSION, NETSH_SESSION)) -> str | None:
    """The first trace session of these tools' names (NetTrace, NetTrace-2) among `etw_names`."""
    pattern = re.compile("|".join(re.escape(x) for x in names) + r"(-\d+)?", re.I)
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

    def watch_files(self, session: Session) -> list[str]:
        return [self.etl(session)]

    def _ours(self, guest, session: Session) -> bool:
        return bool((guest.files([self.etl(session)]).get(self.etl(session)) or {}).get("exists"))

    def pcapng(self, guest, session: Session) -> list[ArtifactSpec]:
        out = win(session.guest_dir, "net.pcapng")
        r = guest.exec("pktmon.exe", ["etl2pcap", self.etl(session), "--out", out], timeout=900)
        session.stats["etl2pcap"] = {"rc": r.get("rc"), "said": " ".join((r.get("stdout") or "").split())[-300:]}
        return [ArtifactSpec("net.pcapng", out, "pcapng", required=False,
                             description="the capture as PCAPNG (pktmon etl2pcap)")]

    def artifacts(self, session: Session) -> list[ArtifactSpec]:
        return [ArtifactSpec("net.etl", self.etl(session), "etl", description=f"{self.title} capture (ETL)")]

    def summary(self, session: Session) -> list[str]:
        kept = session.stats.get("payload")
        lines = [kept] if kept else []
        pcap = next((a for a in session.artifacts if a["name"] == "net.pcapng"), None)
        if pcap and pcap.get("check"):
            lines.append(f"PCAPNG: {pcap['check'].get('detail')}")
        elif session.stats.get("etl2pcap"):
            lines.append(f"no PCAPNG: pktmon etl2pcap exit {session.stats['etl2pcap'].get('rc')}: "
                         f"{session.stats['etl2pcap'].get('said')}")
        return lines


class Pktmon(_Capture):
    name = "pktmon"
    title = "Packet Monitor"
    tool_exe = "pktmon.exe"
    OPTIONS = {
        "components": Option(str, "nics", "pktmon --comp: nics (the network adapters), all, or component ids"),
        "pkt_size": Option(int, 128, "bytes kept of each packet (pktmon --pkt-size; 0: whole packets)"),
        "pcapng": Option(bool, True, "turn the capture into PCAPNG in the guest (pktmon etl2pcap)"),
    }
    notes = ("packet metadata of every packet the adapters pass; payload only up to pkt_size bytes per packet "
             "(128 by default), encrypted traffic stays encrypted; loopback traffic is not seen with the nics "
             "component",
             "the capture file is circular at its size limit: once full, the oldest packets are overwritten")

    def check_options(self, opts: dict) -> None:
        if opts["pkt_size"] < 0:
            raise ObserveError("pkt_size is 0 (whole packets) or more")
        if not re.fullmatch(r"nics|all|\d+( \d+)*", opts["components"]):
            raise ObserveError(f"components is nics, all or component ids (got {opts['components']!r})")

    def start(self, guest, session: Session, host) -> None:
        mb = max(1, (session.limits.get("max_bytes") or 512 << 20) >> 20)
        args = ["start", "--capture", "--comp", *session.options["components"].split(),
                "--pkt-size", str(session.options["pkt_size"]), "--file-name", self.etl(session), "--file-size", str(mb)]
        tool_run(guest, "pktmon.exe", args, 120, "starting Packet Monitor")
        session.tool.update(etl=self.etl(session), file_mb=mb)
        n = session.options["pkt_size"]
        session.stats["payload"] = ("whole packets kept" if n == 0 else f"{n} bytes kept of each packet (headers "
                                    "and the start of the payload)")

    def stop_commands(self, session: Session) -> list[dict]:
        return [command("pktmon.exe", ["stop"], 300)]

    def capturing(self, state: dict, session: Session) -> bool | None:
        names = state.get("etw_sessions")
        return None if names is None else bool(_capturing(names, (PKTMON_SESSION,)))

    def stop(self, guest, session: Session, host, force: bool = False) -> None:
        names = guest.call("etw", names=[]).get("names")
        if names is not None and not _capturing(names, (PKTMON_SESSION,)):
            return
        if not self._ours(guest, session):
            raise Conflict("Packet Monitor runs, but not into this session's file: it is somebody else's capture")
        tool_run(guest, "pktmon.exe", ["stop"], 300, "stopping Packet Monitor")

    def convert(self, guest, session: Session, host) -> list[ArtifactSpec]:
        return self.pcapng(guest, session) if session.options.get("pcapng") else []

    def summary(self, session: Session) -> list[str]:
        lines = super().summary(session)
        etl = next((a for a in session.artifacts if a["name"] == "net.etl"), None)
        if etl and etl.get("guest_bytes") and session.tool.get("file_mb") and \
                etl["guest_bytes"] >= 0.95 * (session.tool["file_mb"] << 20):
            lines.append("the capture file reached its size limit: the oldest packets may have been overwritten")
        return lines


class NetshTrace(_Capture):
    name = "netsh"
    title = "netsh trace"
    tool_exe = "netsh.exe"
    OPTIONS = {
        "filters": Option(list, [], "netsh capture filters KEY=VALUE (IPv4.Address=10.0.2.2, Protocol=TCP; "
                          "netsh trace show capturefilterhelp)", check=_filter),
        "truncate": Option(int, 0, "bytes kept of each packet (packettruncatebytes; 0: netsh's default)"),
        "pcapng": Option(bool, False, "try turning the trace into PCAPNG with pktmon etl2pcap (kept only when it "
                         "reads as PCAPNG)"),
    }
    notes = ("packets as netsh's capture filters let them through; payload up to the truncation length, encrypted "
             "traffic stays encrypted",
             "PCAPNG only when pktmon etl2pcap reads netsh's ETL, which is not documented")

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

    def capturing(self, state: dict, session: Session) -> bool | None:
        names = state.get("etw_sessions")
        return None if names is None else bool(_capturing(names, (NETSH_SESSION,)))

    def stop(self, guest, session: Session, host, force: bool = False) -> None:
        names = guest.call("etw", names=[]).get("names")
        if names is not None and not _capturing(names, (NETSH_SESSION,)):
            return
        if not self._ours(guest, session):
            raise Conflict("netsh trace runs, but not into this session's file: it is somebody else's capture")
        tool_run(guest, "netsh.exe", ["trace", "stop"], 600, "stopping netsh trace")

    def convert(self, guest, session: Session, host) -> list[ArtifactSpec]:
        return self.pcapng(guest, session) if session.options.get("pcapng") else []
