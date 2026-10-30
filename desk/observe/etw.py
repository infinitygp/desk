"""Event Tracing for Windows, through the tools Windows has:

- `wpr` (Windows Performance Recorder): built-in profiles (GeneralProfile, CPU, DiskIO, FileIO,
  Registry, Network, Heap...) or a .wprp in the guest, recorded under an instance name of desk's
  own (-instancename), so desk's stop never stops a recording of somebody else's;
- `etw` (logman): a trace session of desk's own name with the providers asked for, by name or
  GUID, with keywords and level, plus desk's marker provider (MARKER_SOURCE), whose events mark
  `desk observe mark` in the trace.

Both write native ETL. tracerpt (also part of Windows) turns it into CSV and a summary on request.
Events lost are read from the trace session while it runs (ControlTrace's query, as
EVENT_TRACE_PROPERTIES counts them: EventsLost, LogBuffersLost, RealTimeBuffersLost); where Windows
does not answer, the report says they were not measured, never that none were lost.
"""

import hashlib
import re
import uuid

from .base import ArtifactSpec, Capability, Missing, Option, Provider, command, tool_run
from .guestops import win
from .session import Session

# The namespace .NET's EventSource hashes a provider's name in to get its GUID.
_EVENTSOURCE_NAMESPACE = bytes([0x48, 0x2C, 0x2D, 0xB2, 0xC3, 0x90, 0x47, 0xC8,
                                0x87, 0xF8, 0x1A, 0x15, 0xBF, 0xC1, 0x30, 0xFB])


def eventsource_guid(name: str) -> str:
    """The ETW provider GUID of an EventSource named `name` (EventSource.GetGuid: SHA-1 of the
    namespace and the upper-case name in UTF-16 big-endian, as a version 5 GUID)."""
    digest = bytearray(hashlib.sha1(_EVENTSOURCE_NAMESPACE + name.upper().encode("utf-16-be")).digest()[:16])
    digest[7] = (digest[7] & 0x0F) | 0x50
    return str(uuid.UUID(bytes_le=bytes(digest)))


def session_name(session: Session) -> str:
    """A trace session or WPR instance name that says it is desk's and whose."""
    return re.sub(r"[^A-Za-z0-9_]", "_", f"desk_{session.run}_{session.id}")


_PROFILE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(\.(Light|Verbose))?|[A-Za-z]:\\.+\.wprp(![A-Za-z0-9_.]+)?")
# NAME or {GUID}, then :KEYWORDS (hex) and :LEVEL (0-5).
_ETW_PROVIDER = re.compile(r"(?P<name>\{[0-9A-Fa-f-]{36}\}|[A-Za-z][A-Za-z0-9_.-]*)"
                           r"(:(?P<keywords>0x[0-9A-Fa-f]{1,16}|\d+))?(:(?P<level>[0-5]))?")


def _profile(v: str) -> str | None:
    return None if _PROFILE.fullmatch(v) else (f"{v!r} is no WPR profile name (GeneralProfile, CPU.Light) or "
                                               "guest .wprp path (C:\\...\\x.wprp!Profile)")


def _etw_provider(v: str) -> str | None:
    return None if _ETW_PROVIDER.fullmatch(v) else (f"{v!r} is not NAME[:KEYWORDS[:LEVEL]] "
                                                    "(Microsoft-Windows-Kernel-Process:0x10:4, {GUID}:0xFF)")


def provider_args(spec: str) -> list[str]:
    """`-p` arguments of logman for NAME[:KEYWORDS[:LEVEL]]."""
    m = _ETW_PROVIDER.fullmatch(spec)
    args = [m["name"]]
    if m["keywords"] or m["level"]:
        args.append(m["keywords"] or "0xFFFFFFFFFFFFFFFF")
    if m["level"]:
        args.append(f"0x{int(m['level']):x}")
    return args


def lost(props: dict | None) -> dict:
    """What a trace session says it lost, by the names Windows gives those figures."""
    return {k: v for k, v in (props or {}).items() if "lost" in k.lower() and isinstance(v, (int, float))}


class _EtwBase(Provider):
    notes = ("the events of the providers enabled, as ETW delivered them; what ETW dropped is counted where the "
             "trace session reports it",)

    def probe_request(self, opts: dict, host) -> dict:
        return {"commands": [self.tool_exe, "tracerpt.exe"], "etw": True}

    def assess(self, probe: dict, opts: dict, host) -> Capability:
        cap = super().assess(probe, opts, host)
        found = (probe.get("commands") or {}).get(self.tool_exe)
        if not found:
            cap.problems.append(f"{self.tool_exe} is not in the guest")
        else:
            cap.version = found.get("version") or ""
            cap.details["path"] = found.get("path")
        cap.details["tracerpt"] = bool((probe.get("commands") or {}).get("tracerpt.exe"))
        if probe.get("etw_sessions") is None:
            cap.details["etw_sessions"] = "Get-EtwTraceSession is not available: lost events cannot be read"
        cap.available = not cap.problems
        return cap

    def conflicts(self, probe: dict, session: Session) -> str | None:
        name = session_name(session)
        if name in (probe.get("etw_sessions") or []):
            return f"a trace session named {name} runs already (left by a snapshot restore?); logman stop {name} -ets"
        return None

    def etl(self, session: Session) -> str:
        return win(session.guest_dir, "trace.etl")

    def convert(self, guest, session: Session, host) -> list[ArtifactSpec]:
        if not session.options.get("tracerpt"):
            return []
        d = session.guest_dir
        r = guest.exec("tracerpt.exe", [self.etl(session), "-o", win(d, "events.csv"), "-of", "CSV",
                                        "-summary", win(d, "summary.txt"), "-report", win(d, "report.xml"), "-y"],
                       timeout=900)
        session.stats["tracerpt"] = {"rc": r.get("rc"), "timed_out": r.get("timed_out"),
                                     "said": " ".join((r.get("stdout") or "").split())[-300:]}
        return [ArtifactSpec("events.csv", win(d, "events.csv"), "text", required=False,
                             description="every event as CSV (tracerpt)"),
                ArtifactSpec("summary.txt", win(d, "summary.txt"), "text", required=False,
                             description="events per provider (tracerpt)"),
                ArtifactSpec("report.xml", win(d, "report.xml"), "text", required=False,
                             description="tracerpt's report")]

    def summary(self, session: Session) -> list[str]:
        out = []
        loss = session.stats.get("lost")
        if loss:
            out.append("events lost (as the trace session reported them before it stopped): "
                       + ", ".join(f"{k} {v}" for k, v in sorted(loss.items())))
        else:
            out.append("events lost: not measured (the trace session did not report it)")
        tr = session.stats.get("tracerpt")
        if tr:
            out.append("tracerpt read the trace" if tr.get("rc") == 0 else
                       f"tracerpt could not read the trace (exit {tr.get('rc')}): {tr.get('said')}")
        return out


class Wpr(_EtwBase):
    name = "wpr"
    title = "Windows Performance Recorder"
    resource = "wpr"
    tool_exe = "wpr.exe"
    OPTIONS = {
        "profile": Option(list, ["GeneralProfile"], "built-in profiles (GeneralProfile, CPU, DiskIO, FileIO, "
                          "Registry, Network, Heap; .Light/.Verbose) or C:\\...\\x.wprp!Name in the guest",
                          check=_profile),
        "pdbgen": Option(bool, True, "let WPR make the NGEN PDBs of .NET code at stop (false: -skipPdbGen, faster)"),
        "tracerpt": Option(bool, False, "also turn the trace into CSV and a summary in the guest (tracerpt)"),
    }

    def start(self, guest, session: Session, host) -> None:
        name = session_name(session)
        temp = win(session.guest_dir, "wpr-temp")
        guest.mkdir(temp)
        args = [a for p in session.options["profile"] for a in ("-start", p)]
        args += ["-filemode", "-recordtempto", temp, "-instancename", name]
        tool_run(guest, "wpr.exe", args, 180, "starting WPR")
        session.tool.update(instance=name, etl=self.etl(session), temp=temp)

    def _stop_args(self, session: Session) -> list[str]:
        args = ["-stop", self.etl(session), "-instancename", session.tool["instance"]]
        return args + ([] if session.options.get("pdbgen", True) else ["-skipPdbGen"])

    def stop_commands(self, session: Session) -> list[dict]:
        return [command("wpr.exe", self._stop_args(session), 900)]

    def watch_files(self, session: Session) -> list[str]:
        return [session.tool["temp"]] if session.tool.get("temp") else []

    def sample(self, guest, session: Session) -> dict:
        etw = guest.call("etw", names=[])
        mine = [n for n in etw.get("names") or [] if session.tool.get("instance", "") in n]
        if not mine:
            return {}
        sessions = guest.call("etw", names=mine).get("sessions") or {}
        found = {}
        for n, props in sessions.items():
            found.update({f"{n}.{k}": v for k, v in lost(props).items()})
        return {"lost": found} if found else {}

    def stop(self, guest, session: Session, host, force: bool = False) -> None:
        verdict = session.stats.get("watchdog") or {}
        if verdict.get("state") == "done" and any((r or {}).get("rc") == 0 for r in verdict.get("stop") or []):
            return  # the watchdog's wpr -stop wrote the trace
        tool_run(guest, "wpr.exe", self._stop_args(session), 900, "stopping WPR")

    def artifacts(self, session: Session) -> list[ArtifactSpec]:
        return [ArtifactSpec("trace.etl", self.etl(session), "etl", description="WPR recording (ETL)")]

    def cleanup(self, guest, session: Session, host=None) -> None:
        # A recording left running under desk's own instance name (a stop that failed, a desk
        # killed while WPR started) is cancelled.
        instance = session.tool.get("instance") or session_name(session)
        names = guest.call("etw", names=[]).get("names")
        if names is None or any(instance in n for n in names):
            guest.exec("wpr.exe", ["-cancel", "-instancename", instance], timeout=120)


class Logman(_EtwBase):
    name = "etw"
    title = "ETW trace session (logman)"
    tool_exe = "logman.exe"
    OPTIONS = {
        "providers": Option(list, [], "providers to enable: NAME[:KEYWORDS[:LEVEL]] or {GUID}[:...], e.g. "
                            "Microsoft-Windows-Kernel-Process:0x10:4", check=_etw_provider),
        "markers": Option(bool, True, "also enable desk's marker provider, so desk observe mark shows in the trace"),
        "tracerpt": Option(bool, False, "also turn the trace into CSV and a summary in the guest (tracerpt)"),
    }

    def check_options(self, opts: dict) -> None:
        if not opts["providers"]:
            raise Missing("etw needs providers (--set providers=Microsoft-Windows-Kernel-Process:0x10:4)")

    def start(self, guest, session: Session, host) -> None:
        from . import MARKER_SOURCE

        name = session_name(session)
        providers = list(session.options["providers"])
        if session.options.get("markers", True):
            providers.append("{" + eventsource_guid(MARKER_SOURCE) + "}")
        args = ["start", name, "-ets", "-o", self.etl(session), "-p", *provider_args(providers[0])]
        if session.limits.get("max_bytes"):
            args += ["-max", str(max(1, session.limits["max_bytes"] >> 20))]
        tool_run(guest, "logman.exe", args, 120, "starting the trace session")
        session.tool.update(name=name, etl=self.etl(session), providers=providers)
        for p in providers[1:]:
            tool_run(guest, "logman.exe", ["update", "trace", name, "-ets", "-p", *provider_args(p)], 120,
                     f"enabling {p}")

    def stop_commands(self, session: Session) -> list[dict]:
        return [command("logman.exe", ["stop", session.tool["name"], "-ets"], 120)]

    def watch_files(self, session: Session) -> list[str]:
        return [session.tool["etl"]] if session.tool.get("etl") else []

    def capturing(self, state: dict, session: Session) -> bool | None:
        names = state.get("etw_sessions")
        return None if names is None else session.tool.get("name") in names

    def sample(self, guest, session: Session) -> dict:
        sessions = guest.call("etw", names=[session.tool["name"]]).get("sessions") or {}
        found = lost(sessions.get(session.tool["name"]))
        return {"lost": found} if found else {}

    def stop(self, guest, session: Session, host, force: bool = False) -> None:
        names = guest.call("etw", names=[]).get("names")
        if names is not None and session.tool.get("name") not in names:
            return  # ended already (its watchdog, or a full file)
        tool_run(guest, "logman.exe", ["stop", session.tool["name"], "-ets"], 120, "stopping the trace session")

    def artifacts(self, session: Session) -> list[ArtifactSpec]:
        return [ArtifactSpec("trace.etl", self.etl(session), "etl", description="ETW trace (logman)")]

    def cleanup(self, guest, session: Session, host=None) -> None:
        # Only a session of desk's own name that is still there.
        name = session.tool.get("name") or session_name(session)
        if name in (guest.call("etw", names=[]).get("names") or []):
            guest.exec("logman.exe", ["stop", name, "-ets"], timeout=120)

    def summary(self, session: Session) -> list[str]:
        lines = [f"providers: {', '.join(session.tool.get('providers') or session.options['providers'])}"]
        return lines + super().summary(session)

