"""Sysinternals Process Monitor: file system, registry, process and thread activity of the whole
guest, written to a native PML log. Command line as Sysinternals documents it (procmon /?):
/AcceptEula, /Quiet, /Minimized, /BackingFile, /LoadConfig, /Runtime, /WaitForIdle, /Terminate,
/OpenLog with /SaveAs.

ProcMon is not part of Windows and its licence is Sysinternals': the host's copy is set up once
with `desk observe setup procmon ProcessMonitor.zip`, and its licence terms are accepted there,
by the user, with --accept-eula, which desk then passes on as /AcceptEula. Without that desk
starts ProcMon only where the guest's registry says its terms were accepted already.

/Terminate ends every ProcMon in the guest, so desk runs it only while its own is the only one;
a ProcMon desk did not start is never ended."""

from .base import ArtifactSpec, Capability, Conflict, Missing, Option, Provider, command, tool_run
from .guestops import win
from .session import Session

NAMES = ("Procmon.exe", "Procmon64.exe", "Procmon64a.exe")
EULA_KEY = r"HKCU:\Software\Sysinternals\Process Monitor"


def _guest_path(path: str) -> str | None:
    return None if path.startswith(("C:\\", "c:\\", "C:/", "c:/")) else "a full guest path (C:\\...)"


class ProcMon(Provider):
    name = "procmon"
    title = "Process Monitor"
    resource = "procmon"
    OPTIONS = {
        "csv": Option(bool, False, "also save the log as CSV in the guest after it stops (ProcMon's /SaveAs; "
                      "minutes for a large log)"),
        "config": Option(str, "", "a ProcMon configuration (.pmc) already in the guest, for /LoadConfig: its "
                         "filter and columns", check=_guest_path),
        "exe": Option(str, "", "a Procmon64.exe already in the guest instead of the host's copy", check=_guest_path),
    }
    notes = (
        "the whole guest's file system, registry and process activity as ProcMon's driver sees it, filtered by "
        "ProcMon's own default filter (or the configuration given); network events only as ProcMon shows them",
        "no data written or read, only the operations and their results",
    )
    setup_files = ("Procmon64.exe", "Procmon64a.exe", "Procmon.exe", "Eula.txt")
    setup_from = "ProcessMonitor.zip from https://learn.microsoft.com/sysinternals/downloads/procmon"
    licence = "Sysinternals Software License Terms"

    def exe(self, session: Session, arch: str = "AMD64") -> str:
        if session.options.get("exe"):
            return session.options["exe"]
        return win(self.tool_dir(session), "Procmon64a.exe" if arch == "ARM64" else "Procmon64.exe")

    def eula_args(self, session: Session) -> list[str]:
        return ["/AcceptEula"] if session.tool.get("accept_eula") else []

    def probe_request(self, opts: dict, host) -> dict:
        files = [opts["exe"]] if opts.get("exe") else [win(r"C:\desk\observe\tools\procmon", n) for n in NAMES]
        if opts.get("config"):
            files.append(opts["config"])
        return {"files": files, "registry": [{"path": EULA_KEY, "name": "EulaAccepted"}], "processes": list(NAMES)}

    def assess(self, probe: dict, opts: dict, host) -> Capability:
        cap = super().assess(probe, opts, host)
        setup = host.setup(self.name)
        arch = (probe.get("os") or {}).get("arch") or "AMD64"
        want = "Procmon64a.exe" if arch == "ARM64" else "Procmon64.exe"
        if opts.get("exe"):
            info = (probe.get("files") or {}).get(opts["exe"]) or {}
            if not info.get("exists"):
                cap.problems.append(f"{opts['exe']} is not in the guest")
            cap.version = info.get("version") or ""
        elif not setup or want not in [f["name"] for f in setup["files"]]:
            cap.problems.append(f"no {want} set up on this host (desk observe setup procmon ProcessMonitor.zip "
                                "--accept-eula; get it from https://learn.microsoft.com/sysinternals/downloads/procmon)")
        else:
            info = (probe.get("files") or {}).get(win(r"C:\desk\observe\tools\procmon", want)) or {}
            cap.version = info.get("version") or ""
            cap.details["deployed"] = bool(info.get("exists"))
        if opts.get("config") and not ((probe.get("files") or {}).get(opts["config"]) or {}).get("exists"):
            cap.problems.append(f"configuration {opts['config']} is not in the guest")
        accepted = bool(setup and setup.get("licence_accepted"))
        in_guest = (probe.get("registry") or {}).get(EULA_KEY + r"\EulaAccepted") == 1
        cap.details["licence"] = ("accepted at setup" if accepted else "accepted in the guest's registry" if in_guest
                                  else "not accepted")
        if not accepted and not in_guest:
            cap.problems.append(f"the {self.licence} are not accepted: read them (Eula.txt in ProcessMonitor.zip) "
                                "and set the tool up with --accept-eula, or accept them in the guest yourself")
        cap.details["running"] = [f"{p['name']} pid {p['pid']}" for p in probe.get("processes") or []]
        cap.available = not cap.problems
        return cap

    def conflicts(self, probe: dict, session: Session) -> str | None:
        running = probe.get("processes") or []
        if running:
            p = running[0]
            return (f"{p['name']} already runs in the guest (pid {p['pid']}), not started by this session; desk "
                    "does not stop a ProcMon it did not start, and /Terminate would: close it first")
        return None

    def start(self, guest, session: Session, host) -> None:
        setup = host.setup(self.name)
        arch = session.stats.get("arch") or "AMD64"
        if not session.options.get("exe"):
            self.deploy(guest, session, host)
        session.tool["accept_eula"] = bool(setup and setup.get("licence_accepted"))
        exe = self.exe(session, arch)
        pml = win(session.guest_dir, "procmon.pml")
        args = [*self.eula_args(session), "/Quiet", "/Minimized", "/BackingFile", pml]
        if session.options.get("config"):
            args += ["/LoadConfig", session.options["config"]]
        if session.limits.get("duration"):
            args += ["/Runtime", str(int(session.limits["duration"]))]
        proc = guest.launch(exe, args)
        if proc.get("exited") or proc.get("created") is None:
            raise Missing(f"{exe} ended at once (licence terms not accepted, or not a program for this Windows)")
        session.tool.update(exe=exe, pml=pml, procs=[{**proc, "name": exe.rsplit("\\", 1)[-1]}])
        # Capturing only from here: ProcMon says so with /WaitForIdle.
        tool_run(guest, exe, [*self.eula_args(session), "/WaitForIdle"], 90, "waiting for Process Monitor to start")
        alive = guest.alive(session.tool["procs"])
        if not alive or not alive[0].get("alive"):
            raise Missing("Process Monitor ended right after it started (licence terms not accepted, or its driver "
                          "could not load)")

    def stop_commands(self, session: Session) -> list[dict]:
        return [command(session.tool["exe"], [*self.eula_args(session), "/Terminate"], 120,
                        alone={"names": list(NAMES), "procs": session.tool.get("procs") or []})]

    def watch_files(self, session: Session) -> list[str]:
        return [session.tool["pml"]] if session.tool.get("pml") else []

    def stop(self, guest, session: Session, host, force: bool = False) -> None:
        procs = session.tool.get("procs") or []
        running = [p for p in guest.alive(procs) if p.get("alive")]
        if not running:
            return
        mine = {p["pid"] for p in procs}
        others = [p for p in guest.probe(processes=list(NAMES)).get("processes") or [] if p["pid"] not in mine]
        if others and not force:
            raise Conflict(f"{others[0]['name']} (pid {others[0]['pid']}) runs besides this session's, and "
                           "/Terminate would end it too: close it, or stop with --force, which ends this "
                           "session's ProcMon by its PID (its log may then be incomplete)")
        if others:
            for p in running:
                guest.stop_process(p)
            session.notes.append("ended by its PID (--force): ProcMon may not have finished its log")
        else:
            tool_run(guest, session.tool["exe"], [*self.eula_args(session), "/Terminate"], 120,
                     "stopping Process Monitor")
        if not guest.wait_gone(procs, 120):
            raise Conflict("Process Monitor is still running 120 s after it was told to stop")

    def artifacts(self, session: Session) -> list[ArtifactSpec]:
        return [ArtifactSpec("procmon.pml", session.tool.get("pml") or win(session.guest_dir, "procmon.pml"), "pml",
                             description="Process Monitor log")]

    def convert(self, guest, session: Session, host) -> list[ArtifactSpec]:
        if not session.options.get("csv"):
            return []
        csv = win(session.guest_dir, "procmon.csv")
        tool_run(guest, session.tool["exe"], [*self.eula_args(session), "/Quiet", "/OpenLog", session.tool["pml"],
                                              "/SaveAs", csv], 900, "saving the Process Monitor log as CSV")
        return [ArtifactSpec("procmon.csv", csv, "procmon-csv", required=False, description="the log as CSV")]
