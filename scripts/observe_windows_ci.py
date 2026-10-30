#!/usr/bin/env python3
"""The guest side of desk observe on a Windows machine without a VM (GitHub's Windows runners):
helper.ps1's operations and the providers' command lines, run by the same Python code desk runs
against a guest, through `LocalGuest` instead of SSH. Every check says PASS, FAIL or SKIP (the
tool is not on this Windows), and the results go to --out as JSON; the exit code is 1 when one
failed. Checks are named CI-* in docs/observe/validation.md.

What this does not show: anything of a desk run (SSH, the guest's session 0, warm states,
snapshots), Windows 11 itself (runners are Windows Server), Process Monitor (its licence terms
are the user's to accept) and USBPcap (a driver and hardware).

    python scripts/observe_windows_ci.py --out observe-ci.json
"""

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DESK_HOME", tempfile.mkdtemp(prefix="desk-ci-"))

from desk.config import ImageSpec  # noqa: E402
from desk.observe import MARKER_SOURCE, PROVIDERS, Host, _watch  # noqa: E402
from desk.observe.guestops import ROOT as GROOT  # noqa: E402
from desk.observe.guestops import LocalGuest, ObserveError, items, win  # noqa: E402
from desk.observe.session import Session  # noqa: E402

SPEC = ImageSpec(name="windows-ci", dir=ROOT, os_variant="", memory_mib=0, vcpus=0, disk_gb=0, user="runner",
                 ready="", family="windows")
WORKLOAD = ROOT / "docs" / "observe" / "fixtures" / "workload.ps1"
EXPECT = ROOT / "docs" / "observe" / "fixtures" / "expect.py"
results: list[dict] = []


def record(cid: str, status: str, detail: str = "") -> None:
    results.append({"id": cid, "status": status, "detail": detail})
    print(f"{status:<5} {cid}: {detail}", flush=True)


def case(cid: str):
    def wrap(fn):
        def run(*a):
            try:
                out = fn(*a)
                if out is None:
                    record(cid, "PASS")
                elif isinstance(out, tuple):
                    record(cid, *out)
                else:
                    record(cid, "PASS", str(out))
            except AssertionError as e:
                record(cid, "FAIL", str(e) or "assertion failed")
            except Exception as e:  # noqa: BLE001 - every check is reported, none stops the others
                record(cid, "FAIL", f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")
        run.__name__ = fn.__name__
        CASES.append(run)
        return run
    return wrap


CASES = []
g = LocalGuest(SPEC)


class CiHost(Host):
    def setup(self, name):
        return None


def session(provider: str, sid: str, **options) -> Session:
    p = PROVIDERS[provider]
    s = Session(id=sid, provider=provider, run="ci", created=time.time(), options=p.options(options),
                limits={"duration": 600, "max_bytes": 256 << 20}, guest_dir=win(GROOT, f"ci-{sid}"))
    s.tool["host_dir"] = str(Path(os.environ["DESK_HOME"]) / sid)
    g.remove(s.guest_dir)
    g.mkdir(s.guest_dir)
    return s


def launch(exe: str, args: list[str], name: str) -> dict:
    """`name` only says what it is in a failure."""
    return g.launch(exe, args)


def watch(order: dict, timeout: float = 60) -> dict:
    payload = __import__("base64").b64encode(json.dumps(order).encode()).decode()
    launch("powershell.exe", ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                              "-File", win(GROOT, "helper.ps1"), "-Watch", payload], f"watch-{time.time_ns()}")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        said = g.call("read", path=order["out"])
        if said.get("exists"):
            verdict = json.loads(said["text"].lstrip("\ufeff"))
            if verdict.get("state") == "done":
                return verdict
        time.sleep(1)
    raise AssertionError(f"the watchdog wrote no verdict within {timeout:.0f}s")


def run_workload(token: str, steps: str) -> dict:
    g.push(WORKLOAD, win(GROOT, "ci", "workload.ps1"))
    r = g.exec("powershell.exe", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", win(GROOT, "ci", "workload.ps1"),
                                  "-Token", token, "-Steps", steps], timeout=300, encoding="utf8")
    assert r.get("rc") == 0, f"workload exit {r.get('rc')}: {r.get('stderr')[-500:]}"
    said = g.call("read", path=win(GROOT, "workload", token, "manifest.json"))
    assert said.get("exists"), "no manifest"
    return json.loads(said["text"].lstrip("\ufeff"))


def expect(manifest: dict, *args: str) -> str:
    m = Path(os.environ["DESK_HOME"]) / f"manifest-{manifest['token']}.json"
    m.write_text(json.dumps(manifest))
    r = subprocess.run([sys.executable, str(EXPECT), str(m), *args], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    return " | ".join(ln for ln in r.stdout.splitlines())


# The helper.


@case("CI-H-01")
def probe():
    p = g.probe(commands=["wpr.exe", "logman.exe", "pktmon.exe", "netsh.exe", "tracerpt.exe"], etw=True,
                processes=["python.exe"], usb=True)
    assert p["admin"] is True, f"not elevated: {p.get('user')}"
    assert p["boot"], "no boot time"
    have = [c for c, v in p["commands"].items() if v]
    return "PASS", f"{p['os']}; tools: {', '.join(have)}; ETW sessions: {len(p.get('etw_sessions') or [])}"


@case("CI-H-02")
def procs_identity():
    table = g.processes()
    me = [p for p in table if p["pid"] == os.getpid()]
    assert me and me[0]["created"], "this process is not in the table with its creation time"
    alive = g.alive([{"pid": os.getpid(), "created": me[0]["created"]},
                     {"pid": os.getpid(), "created": me[0]["created"] + 10_000_000}])
    assert [a["alive"] for a in alive] == [True, False], f"identity by creation time: {alive}"
    return "PASS", f"{len(table)} processes; a PID with another creation time is another process"


@case("CI-H-03")
def launch_and_stop():
    p = launch("ping.exe", ["-n", "120", "127.0.0.1"], "ping")
    assert g.alive([p])[0]["alive"], "launched process not alive"
    r = g.stop_process(p)
    assert r["stopped"], r
    assert not g.alive([p])[0]["alive"], "still alive after stopproc"
    assert g.wait_gone([p], 5)


@case("CI-H-04")
def exec_limits():
    r = g.exec("cmd.exe", ["/c", "exit", "3"], timeout=30)
    assert r["rc"] == 3, r
    t0 = time.monotonic()
    r = g.exec("ping.exe", ["-n", "30", "127.0.0.1"], timeout=2)
    assert r["timed_out"] and r["rc"] is None, r
    assert time.monotonic() - t0 < 40, "the time limit was not kept"
    r = g.exec("no-such-tool.exe", [], timeout=5)
    assert r["started"] is False, r
    return "PASS", "exit codes, a time limit kept in the guest, a missing program"


@case("CI-H-05")
def files_and_root():
    g.mkdir(win(GROOT, "ci"))
    data = os.urandom(100_000)
    Path(win(GROOT, "ci", "x.bin")).write_bytes(data)
    info = g.files([win(GROOT, "ci", "x.bin")], hash=True)[win(GROOT, "ci", "x.bin")]
    assert info.get("sha256") == hashlib.sha256(data).hexdigest(), f"the guest's answer: {info}"
    for op, params in (("remove", {"paths": ["C:\\Windows\\Temp\\desk-ci"]}), ("mkdir", {"paths": ["C:\\desk-ci"]}),
                       ("read", {"path": "C:\\Windows\\win.ini"})):
        try:
            g.call(op, **params)
            raise AssertionError(f"{op} outside {GROOT} was not refused")
        except ObserveError as e:
            assert "refusing" in str(e), e


@case("CI-H-06")
def mark_event():
    r = g.call("mark", path=win(GROOT, "marks", "mark-ci.txt"), text="ci", source=MARKER_SOURCE, event="DeskMarkCI")
    assert Path(r["path"]).read_text() == "ci"
    assert r["event"] is True, f"no EventSource event: {r.get('event_error')}"


@case("CI-H-07")
def ipc_and_listening():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    port = srv.getsockname()[1]
    try:
        listing = g.call("ipc", pipes=True, tcp=True, udp=True)
        assert listing["pipes"], "no pipes listed"
        mine = [c for c in listing["tcp"] if c["lport"] == port and c["state"] == "Listen"]
        assert mine and mine[0]["pid"] == os.getpid(), f"listener not listed with this pid: {mine}"
        owners = items(g.call("listening", port=port))
        assert [o["pid"] for o in owners] == [os.getpid()], owners
    finally:
        srv.close()
    return "PASS", f"{len(listing['pipes'])} pipes, {len(listing['tcp'])} TCP, {len(listing['udp'])} UDP"


@case("CI-H-08")
def pipe_probe():
    from desk.observe import ipc

    script = ("$s = New-Object IO.Pipes.NamedPipeServerStream('desk-ci-probe'); $s.WaitForConnection(); "
              "$b = New-Object byte[] 2; [void]$s.Read($b, 0, 2); $r = [Text.Encoding]::ASCII.GetBytes('pong'); "
              "$s.Write($r, 0, 4); $s.Flush(); Start-Sleep 2; $s.Dispose()")
    server = launch("powershell.exe", ["-NoProfile", "-Command", script], "pipe-server")
    time.sleep(3)
    r = ipc.probe_pipe(g, "desk-ci-probe", send=b"hi", read=4, timeout=10)
    assert r.get("connected"), r
    assert r.get("server_pid") == server["pid"], f"server pid {r.get('server_pid')}, launched {server['pid']}"
    assert r.get("received") == b"pong", r


@case("CI-H-09")
def watchdog_duration():
    tool = launch("ping.exe", ["-n", "120", "127.0.0.1"], "wd-tool")
    out = win(GROOT, "ci", "wd-duration.json")
    v = watch({"seconds": 3, "max_bytes": 0, "files": [], "tool": tool, "out": out,
               "stop": [{"exe": "taskkill.exe", "args": f"/F /PID {tool['pid']}", "timeout": 30}]})
    assert v["reason"] == "duration" and v["stop"][0]["rc"] == 0, v
    assert not g.alive([tool])[0]["alive"], "the tool runs on"


@case("CI-H-10")
def watchdog_size_and_tool_end():
    f = win(GROOT, "ci", "grow.bin")
    Path(f).write_bytes(b"x" * 100)
    tool = launch("ping.exe", ["-n", "120", "127.0.0.1"], "wd-size")
    v = watch({"seconds": 600, "max_bytes": 50, "files": [f], "tool": tool, "out": win(GROOT, "ci", "wd-size.json"),
               "stop": [{"exe": "taskkill.exe", "args": f"/F /PID {tool['pid']}", "timeout": 30}]})
    assert v["reason"] == "size", v
    short = launch("ping.exe", ["-n", "2", "127.0.0.1"], "wd-short")
    v = watch({"seconds": 600, "max_bytes": 0, "files": [], "tool": short, "out": win(GROOT, "ci", "wd-end.json"),
               "stop": [{"exe": "cmd.exe", "args": "/c exit 9", "timeout": 30}]})
    assert v["reason"] == "tool-ended" and not v["stop"], v


@case("CI-H-11")
def watchdog_leaves_foreign_tools():
    ours = launch("ping.exe", ["-n", "120", "127.0.0.1"], "ours")
    foreign = subprocess.Popen(["ping.exe", "-n", "120", "127.0.0.1"], stdout=subprocess.DEVNULL)
    try:
        v = watch({"seconds": 2, "max_bytes": 0, "files": [], "tool": ours, "out": win(GROOT, "ci", "wd-alone.json"),
                   "stop": [{"exe": "taskkill.exe", "args": "/F /IM PING.EXE", "timeout": 30,
                             "alone": {"names": ["PING.EXE", "ping.exe"], "procs": [ours]}}]})
        assert v.get("forced") and "skipped" in v["stop"][0], v
        assert foreign.poll() is None, "the foreign ping was ended"
        assert not g.alive([ours])[0]["alive"], "ours runs on"
    finally:
        foreign.kill()


@case("CI-H-12")
def state_round_trip():
    probe = g.probe()
    out = win(GROOT, "ci", "state.json")
    Path(out).write_text('{"state": "done"}')
    st = g.call("state", procs=[], watch=[out], etw=True)
    assert str(st["boot"]) == str(probe["boot"]), (st["boot"], probe["boot"])
    assert json.loads(st["watch"][out])["state"] == "done"
    return "PASS", f"{len(st.get('etw_sessions') or [])} ETW sessions"


# The providers' command lines.


@case("CI-P-01")
def logman_markers_and_processes():
    p = PROVIDERS["etw"]
    s = session("etw", "s1", providers=["Microsoft-Windows-Kernel-Process:0x10:4"], tracerpt=True)
    host = CiHost()
    p.preflight(g, s, host)
    p.start(g, s, host)
    etw = g.call("etw", names=[s.tool["name"]])
    assert s.tool["name"] in (etw.get("names") or []), f"{s.tool['name']} not among the ETW sessions"
    props = sorted((etw.get("sessions") or {}).get(s.tool["name"]) or {})
    manifest = run_workload("ci01", "process")
    g.call("mark", path=win(GROOT, "marks", "mark-ci01.txt"), text="ci01", source=MARKER_SOURCE, event="DeskMark1")
    s.stats.update(p.sample(g, s))
    assert "EventsLost" in s.stats.get("lost", {}), f"no lost-event counts from ControlTrace: {props}"
    p.stop(g, s, host)
    arts = p.artifacts(s) + p.convert(g, s, host)
    local = Path(os.environ["DESK_HOME"]) / "s1"
    for a in arts:
        g.pull(a.guest_path, local / a.name)
    check = p.verify(local / "trace.etl", arts[0])
    assert check["ok"], check
    assert s.stats.get("tracerpt", {}).get("rc") == 0, s.stats.get("tracerpt")
    said = expect(manifest, "--tracerpt-csv", str(local / "events.csv"))
    return "PASS", f"lost: {s.stats.get('lost', 'not reported')}; session properties: {props}; {said}"


@case("CI-P-02")
def logman_watchdog_stops_it():
    p = PROVIDERS["etw"]
    s = session("etw", "s2", providers=["Microsoft-Windows-Kernel-Process:0x10:4"], markers=False)
    s.limits = {"duration": 4, "max_bytes": 64 << 20}
    p.start(g, s, CiHost())
    _watch(g, p, s)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and s.tool["name"] in (g.call("etw", names=[]).get("names") or []):
        time.sleep(1)
    assert s.tool["name"] not in (g.call("etw", names=[]).get("names") or []), "the watchdog did not stop it"
    v = json.loads(g.call("read", path=s.watchdog["out"])["text"].lstrip("\ufeff"))
    assert v["reason"] == "duration" and v["stop"][0]["rc"] == 0, v


@case("CI-P-03")
def wpr_instance():
    if not g.probe(commands=["wpr.exe"])["commands"].get("wpr.exe"):
        return "SKIP", "wpr.exe is not on this Windows"
    p = PROVIDERS["wpr"]
    s = session("wpr", "s3", profile=["CPU.Light"], pdbgen=False)
    p.start(g, s, CiHost())
    time.sleep(5)
    during = [n for n in g.call("etw", names=[]).get("names") or [] if "wpr" in n.lower() or "desk" in n.lower()]
    s.stats.update(p.sample(g, s))
    p.stop(g, s, CiHost())
    info = g.files([p.etl(s)])[p.etl(s)]
    assert info.get("exists") and info.get("size"), f"no ETL: {info}"
    p.cleanup(g, s)
    return "PASS", f"{info['size']} bytes; lost: {s.stats.get('lost', 'not reported')}; sessions while recording: {during}"


@case("CI-P-04")
def pktmon_capture():
    tools = g.probe(commands=["pktmon.exe"])["commands"]
    if not tools.get("pktmon.exe"):
        return "SKIP", "pktmon.exe is not on this Windows"
    p = PROVIDERS["pktmon"]
    s = session("pktmon", "s4", pkt_size=256)
    probe = p.preflight(g, s, CiHost())
    del probe
    p.start(g, s, CiHost())
    names = g.call("etw", names=[]).get("names") or []
    manifest = run_workload("ci04", "dns")
    p.stop(g, s, CiHost())
    left = [n for n in g.call("etw", names=[]).get("names") or [] if "pktmon" in n.lower()]
    assert not left, f"still capturing after stop: {left}"
    # Production collection discovers multi-file captures before listing or converting them.
    p.found(g, s)
    listing = g.files([s.guest_dir]).get(s.guest_dir)
    diagnostic = (f"pktmon: {tools['pktmon.exe']}; stop: {s.stats.get('pktmon_stop')}; "
                  f"files after stop: {listing}; segments: {s.tool.get('segments')}")
    etls = p.artifacts(s)
    info = g.files([a.guest_path for a in etls])
    for a in etls:
        found = info.get(a.guest_path) or {}
        assert found.get("exists") and found.get("size"), f"{a.name} missing or empty: {found}; {diagnostic}"
    pcaps = p.convert(g, s, CiHost())
    diagnostic += f"; etl2pcap: {s.stats.get('etl2pcap')}"
    assert len(pcaps) == len(etls), f"not every segment has a PCAPNG; {diagnostic}"
    assert all(r.get("rc") == 0 for r in s.stats["etl2pcap"].values()), diagnostic
    local = Path(os.environ["DESK_HOME"]) / "s4"
    checks = []
    for a in etls + pcaps:
        g.pull(a.guest_path, local / a.name)
        check = p.verify(local / a.name, a)
        assert check["ok"], f"{a.name}: {check}; {diagnostic}"
        checks.append(f"{a.name}: {check['detail']}")
    sessions = [n for n in names if "pkt" in n.lower()]
    said = expect(manifest, "--pcapng", *(str(local / a.name) for a in pcaps))
    return "PASS", f"{'; '.join(checks)}; ETW sessions while capturing: {sessions}; {said}; {diagnostic}"


@case("CI-P-05")
def netsh_capture():
    p = PROVIDERS["netsh"]
    s = session("netsh", "s5", filters=["Protocol=UDP"], truncate=128)
    p.preflight(g, s, CiHost())
    p.start(g, s, CiHost())
    names = g.call("etw", names=[]).get("names") or []
    run_workload("ci05", "dns")
    from desk.observe.network import NETSH_SESSION, _capturing

    assert _capturing(names, (NETSH_SESSION,)), f"netsh's session not recognised among {names}"
    p.stop(g, s, CiHost())
    left = g.call("etw", names=[]).get("names") or []
    assert not _capturing(left, (NETSH_SESSION,)), f"still capturing after stop: {left}"
    info = g.files([p.etl(s)])[p.etl(s)]
    assert info.get("exists") and info.get("size"), f"no ETL: {info}"
    return "PASS", f"{info['size']} bytes; ETW sessions while capturing: {[n for n in names if 'net' in n.lower()]}"


@case("CI-P-06")
def ipc_listing_sees_held_endpoints():
    p = PROVIDERS["ipc"]
    s = session("ipc", "s6", udp=True)
    g.push(WORKLOAD, win(GROOT, "ci", "workload.ps1"))
    launch("powershell.exe", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", win(GROOT, "ci", "workload.ps1"),
                              "-Token", "ci06", "-Steps", "cpu", "-HoldSeconds", "40"], "hold")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not g.call("read", path=win(GROOT, "workload", "ci06", "manifest.json"))["exists"]:
        time.sleep(1)
    manifest = json.loads(g.call("read", path=win(GROOT, "workload", "ci06", "manifest.json"))["text"].lstrip("\ufeff"))
    p.take(g, s, "mark-1")
    return "PASS", expect(manifest, "--ipc-dir", s.tool["host_dir"])


@case("CI-W-01")
def workload_runs():
    m = run_workload("ci07", "process,files,registry,pipe,tcp,cpu,io,crash")
    assert m["steps"]["process"]["exit_child"]["exit_code"] == 7, m["steps"]["process"]
    assert m["steps"]["pipe"]["reply"] == "pong-ci07", m["steps"]["pipe"]
    assert m["steps"]["tcp"]["received"] == "tcp-ci07", m["steps"]["tcp"]
    assert m["steps"]["io"]["bytes_read"] == 32 << 20
    return "PASS", f"cpu digest {m['steps']['cpu']['digest'][:16]}, crash exit {m['steps']['crash']['exit_hex']}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("observe-ci.json"))
    a = ap.parse_args()
    if os.name != "nt":
        print("observe_windows_ci: runs on Windows only", file=sys.stderr)
        sys.exit(2)
    g.mkdir(win(GROOT, "ci"))
    for run in CASES:
        run()
    a.out.write_text(json.dumps({"results": results, "python": sys.version}, indent=1))
    failed = [r["id"] for r in results if r["status"] == "FAIL"]
    print(f"{len(results)} checks, {len(failed)} failed" + (f": {', '.join(failed)}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
