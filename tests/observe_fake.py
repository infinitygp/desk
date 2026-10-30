"""A Windows guest in memory for the observation tests: it answers the helper's operations
(desk/observe/helper.ps1) as the guest would, with tools simulated by the tests. Command lines
go through the helper's argument string and back with Windows' own splitting rules, so the
quoting is tested along the way. Nothing here says the real tools behave so: that is for the
Windows checks in docs/observe/validation.md."""

import hashlib
import struct
import time
from pathlib import Path

from desk.config import DeskError
from desk.observe import Host
from desk.observe.guestops import Guest, ObserveError
from desk.scenario import split_windows


def pml_bytes(events: int = 3, complete: bool = True, computer: str = "DESK-WIN") -> bytes:
    """A Process Monitor log as procmon-parser lays its header out; `complete=False` is one whose
    tables ProcMon never wrote (killed)."""
    header_size = 0x3A8
    body = b"\0" * (64 * events)
    events_at = header_size
    offsets_at = events_at + len(body)
    tables_at = offsets_at + 5 * events
    tail = b"\0" * 64
    head = struct.pack("<4sII64s520sIQQQQQQ", b"PML_", 9, 1, computer.encode("utf-16-le"),
                       "C:\\Windows".encode("utf-16-le"), events, 0,
                       events_at if complete else 0, offsets_at if complete else 0,
                       tables_at if complete else 0, tables_at + 16 if complete else 0,
                       tables_at + 32 if complete else 0)
    head += b"\0" * (header_size - len(head))
    return head + body + b"\0" * (5 * events) + tail


def pcap_bytes(packets: int = 2, linktype: int = 249, cut: bool = False) -> bytes:
    out = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, linktype)
    for i in range(packets):
        data = bytes([i]) * 27
        out += struct.pack("<IIII", 1700000000 + i, 0, len(data), len(data)) + data
    return out[:-5] if cut else out


def pcapng_bytes(packets: int = 3, order: str = "<") -> bytes:
    def block(kind: int, body: bytes) -> bytes:
        body += b"\0" * (-len(body) % 4)
        n = 12 + len(body)
        return struct.pack(order + "II", kind, n) + body + struct.pack(order + "I", n)

    shb = block(0x0A0D0D0A, struct.pack(order + "IHHq", 0x1A2B3C4D, 1, 0, -1))
    idb = block(1, struct.pack(order + "HHI", 1, 0, 65535))
    epbs = b"".join(block(6, struct.pack(order + "IIIII", 0, 0, i, 60, 60) + b"\x00" * 60) for i in range(packets))
    return shb + idb + epbs


class FakeRun:
    def __init__(self, d: Path, image: str = "windows-11", run_id: str = "r1"):
        self.id, self.dir, self.image = run_id, d, image
        self.created = time.time() - 60
        self.label, self.timezone, self.layout = "tests", "", ""
        self.events: list[dict] = []
        self.live = True

    def log_event(self, **event):
        self.events.append({"t": time.time(), **event})

    def live_domain(self):
        return object() if self.live else None

    def dom(self):
        if not self.live:
            raise DeskError(f"run {self.id} is not running")
        return object()


class FakeHost(Host):
    def __init__(self, run=None):
        super().__init__(run)
        self.forwards: dict[int, int] = {}
        self.unforwarded: list[int] = []
        self.usb_devices: list[dict] = []
        self.answers = True

    def usb(self):
        return self.usb_devices

    def forward(self, port):
        self.forwards[port] = 40000 + port % 1000
        return self.forwards[port]

    def unforward(self, port):
        self.unforwarded.append(port)

    def reachable(self, local, timeout=5):
        return self.answers


class FakeWindows(Guest):
    def __init__(self):
        self.disk: dict[str, bytes] = {}
        self.dirs: set[str] = set()
        self.procs: dict[int, dict] = {}
        self.boot_id = "133000000000000000"
        self.etw: dict[str, dict] = {}
        self.admin = True
        self.registry: dict[str, object] = {}
        self.commands = {c: {"path": f"C:\\Windows\\System32\\{c}", "version": "10.0.26100.1"}
                         for c in ("wpr.exe", "logman.exe", "tracerpt.exe", "pktmon.exe", "netsh.exe")}
        self.services: dict[str, dict] = {}
        self.usb: list[dict] = []
        self.listening: dict[int, list[dict]] = {}
        self.pipes: list[str] = []
        self.tcp: list[dict] = []
        self.pipe_reply: dict = {"connected": True, "server_pid": 4242}
        # basename (lower case) -> fn(fake, argv) -> {rc, stdout, stderr} for `exec`;
        # fn(fake, argv, proc) for `launch`.
        self.tools: dict = {}
        self.launchers: dict = {}
        self.calls: list[tuple[str, dict]] = []
        self.pushed: list[str] = []
        self.pulled: list[str] = []
        self.pull_hook = None
        self.fail: dict[str, Exception] = {}
        self.clock_offset = 2.5
        self.arch = "AMD64"
        self._pid = 1000
        self._created = 133_700_000_000_000_000 // 10
        with_windows_tools(self)

    # Processes.

    def new_proc(self, name: str, ppid: int = 4, alive: bool = True, created: int | None = None, **extra) -> dict:
        self._pid += 4
        self._created += 1000
        p = {"pid": self._pid, "ppid": ppid, "name": name, "path": f"C:\\x\\{name}", "cmdline": name,
             "created": self._created if created is None else created, "alive": alive, **extra}
        self.procs[p["pid"]] = p
        return p

    def kill(self, pid: int, tree: bool = False) -> None:
        p = self.procs.get(pid)
        if p and p["alive"]:
            p["alive"] = False
            if p.get("on_stop"):
                p["on_stop"](self, p)
        if tree:
            for c in list(self.procs.values()):
                if c["ppid"] == pid and c["alive"]:
                    self.kill(c["pid"], tree=True)

    def _alive(self, ident: dict) -> bool:
        p = self.procs.get(ident["pid"])
        return bool(p and p["alive"] and abs(p["created"] - ident["created"]) <= 2000)

    def running(self, *names: str) -> list[dict]:
        return [p for p in self.procs.values() if p["alive"] and p["name"] in names]

    # The helper's operations.

    def call(self, op: str, limit: float = 60, **params):
        self.calls.append((op, params))
        if op in self.fail:
            raise self.fail[op]
        result = getattr(self, f"op_{op}")(**params)
        if isinstance(result, dict):
            result.setdefault("_now_ms", (time.time() + self.clock_offset) * 1000)
        return result

    def ops(self, name: str) -> list[dict]:
        return [p for op, p in self.calls if op == name]

    def execs(self, tool: str) -> list[list[str]]:
        return [split_windows(p["args"] or "") for p in self.ops("exec") if Path(p["exe"].replace("\\", "/")).name.lower()
                == tool.lower()]

    def op_probe(self, files=(), commands=(), services=(), registry=(), processes=(), etw=False, usb=False,
                 hash=False):
        out = {"user": "DESK\\tester", "admin": self.admin, "boot": int(self.boot_id),
               "os": {"caption": "Microsoft Windows 11 Enterprise Evaluation", "version": "10.0.26200", "build": "26200",
                      "arch": self.arch},
               "files": {}, "commands": {}, "services": {}, "registry": {}, "processes": []}
        for f in files or []:
            data = self.disk.get(f)
            out["files"][f] = ({"exists": True, "size": len(data), "version": "4.01",
                                **({"sha256": hashlib.sha256(data).hexdigest()} if hash else {})}
                               if data is not None else {"exists": False})
        for c in commands or []:
            out["commands"][c] = self.commands.get(c)
        for s in services or []:
            out["services"][s] = self.services.get(s)
        for r in registry or []:
            out["registry"][f"{r['path']}\\{r['name']}"] = self.registry.get(f"{r['path']}\\{r['name']}")
        out["processes"] = [{k: p[k] for k in ("pid", "ppid", "name", "path", "created")}
                            for p in self.procs.values() if p["alive"] and p["name"] in (processes or [])]
        if etw:
            out["etw_sessions"] = list(self.etw)
        if usb:
            out["usb"] = list(self.usb)
        return out

    def op_launch(self, exe, args, cwd=None):
        argv = split_windows(args or "")
        name = Path(exe.replace("\\", "/")).name
        if exe not in self.disk and name.lower() not in self.launchers and name.lower() != "powershell.exe":
            raise ObserveError(f"in the guest: launch: {exe} not found")
        p = self.new_proc(name, argv=argv, exe=exe)
        handler = self.launchers.get(name.lower())
        if handler:
            handler(self, argv, p)
        return {"pid": p["pid"], "created": p["created"], "exited": not p["alive"]}

    def op_exec(self, exe, args, timeout, encoding=None, cwd=None):
        argv = split_windows(args or "")
        name = Path(exe.replace("\\", "/")).name.lower()
        handler = self.tools.get(name)
        if handler is None:
            return {"started": False, "error": "The system cannot find the file specified", "rc": None}
        r = handler(self, argv)
        return {"started": True, "timed_out": False, "seconds": 0.1, "stdout": "", "stderr": "", "rc": 0, **r}

    def op_alive(self, procs):
        return {"items": [{**p, "alive": self._alive(p)} for p in procs]}

    def op_stopproc(self, pid, created, tree=False):
        if not self._alive({"pid": pid, "created": created}):
            return {"stopped": False, "why": "not running"}
        self.kill(pid, tree)
        return {"stopped": True}

    def op_waitgone(self, procs, timeout):
        return {"gone": not any(self._alive(p) for p in procs)}

    def op_procs(self):
        return {"items": [{k: p[k] for k in ("pid", "ppid", "name", "path", "cmdline", "created")}
                          for p in self.procs.values() if p["alive"]]}

    def op_files(self, paths, hash=False):
        out = {}
        for f in paths:
            if f in self.disk:
                data = self.disk[f]
                out[f] = {"exists": True, "dir": False, "size": len(data),
                          **({"sha256": hashlib.sha256(data).hexdigest()} if hash else {})}
            elif any(k.startswith(f + "\\") for k in self.disk) or f in self.dirs:
                out[f] = {"exists": True, "dir": True, "files": [{"path": k, "size": len(v)} for k, v in self.disk.items()
                                                                 if k.startswith(f + "\\")]}
            else:
                out[f] = {"exists": False}
        return out

    def op_mkdir(self, paths):
        for d in paths:
            assert d.lower().startswith("c:\\desk\\observe\\"), d
            self.dirs.add(d)
        return {"ok": True}

    def op_remove(self, paths):
        removed = []
        for d in paths:
            assert d.lower().startswith("c:\\desk\\observe\\"), d
            gone = [k for k in self.disk if k == d or k.startswith(d + "\\")]
            for k in gone:
                del self.disk[k]
            if gone or d in self.dirs:
                removed.append(d)
            self.dirs.discard(d)
        return {"removed": removed}

    def op_mark(self, path, text, source=None, event=None):
        at = (time.time() + self.clock_offset) * 1000
        self.disk[path] = text.encode()
        return {"utc_ms": at, "path": path, "event": bool(source)}

    def op_etw(self, names):
        return {"available": True, "names": list(self.etw), "sessions": {n: self.etw.get(n) for n in names}}

    def op_ipc(self, pipes=True, tcp=True, udp=False):
        return {**({"pipes": list(self.pipes)} if pipes else {}), **({"tcp": list(self.tcp)} if tcp else {}),
                **({"udp": []} if udp else {})}

    def op_pipeprobe(self, name, timeout_ms, send_b64=None, read_bytes=0):
        return dict(self.pipe_reply)

    def op_listening(self, port):
        return {"items": list(self.listening.get(port, []))}

    def op_boot(self):
        return {"boot": self.boot_id}

    def op_read(self, path):
        return {"exists": path in self.disk, "text": self.disk.get(path, b"").decode()}

    def op_state(self, procs, watch, etw=False):
        out = {"boot": self.boot_id, "alive": [{**p, "alive": self._alive(p)} for p in procs],
               "watch": {f: (self.disk[f].decode() if f in self.disk else None) for f in watch}}
        if etw:
            out["etw_sessions"] = list(self.etw)
        return out

    # Copies and clock.

    def push(self, local, remote, timeout=600):
        self.disk[remote] = Path(local).read_bytes()
        self.pushed.append(remote)

    def pull(self, remote, local, timeout=600):
        if "pull" in self.fail:
            raise self.fail["pull"]
        data = self.disk[remote]
        if self.pull_hook:
            data = self.pull_hook(remote, data)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(data)
        self.pulled.append(remote)

    def clock(self):
        t = time.time()
        return {"host": t, "guest": t + self.clock_offset, "uncertainty": 0.001, "via": "fake"}


def with_procmon(fake: FakeWindows, events: int = 3) -> None:
    """Process Monitor as its documented command line behaves: /BackingFile writes the log, which
    is whole once /Terminate (or the end of /Runtime) closed it."""

    def finish(f, p):
        f.disk[p["backing"]] = pml_bytes(events)

    def launch(f, argv, p):
        p["backing"] = argv[argv.index("/BackingFile") + 1]
        p["eula"] = "/AcceptEula" in argv
        p["on_stop_killed"] = True
        f.disk[p["backing"]] = pml_bytes(events, complete=False)
        p["on_stop"] = lambda f, p: None  # killed by PID: the log stays as it was

    def tool(f, argv):
        if "/WaitForIdle" in argv:
            return {"rc": 0}
        if "/Terminate" in argv:
            for p in f.running("Procmon64.exe", "Procmon.exe"):
                p["alive"] = False
                if "backing" in p:
                    finish(f, p)
            return {"rc": 0}
        if "/SaveAs" in argv:
            f.disk[argv[argv.index("/SaveAs") + 1]] = (
                b'"Time of Day","Process Name","PID","Operation","Path","Result","Detail"\r\n'
                b'"1:00:00.0000000 PM","app.exe","1234","CreateFile","C:\\x","SUCCESS",""\r\n')
            return {"rc": 0}
        return {"rc": 1, "stderr": "unknown"}

    fake.launchers["procmon64.exe"] = launch
    fake.tools["procmon64.exe"] = tool


def with_windows_tools(fake: FakeWindows) -> None:
    """Windows' own tracing tools, following their documented command lines."""
    recordings: dict[str, list] = {}

    def logman(f, argv):
        verb, name = argv[0], argv[1] if len(argv) > 1 else ""
        if verb == "start":
            if name in f.etw:
                return {"rc": -2144337737, "stdout": "Data Collector already exists."}
            f.etw[name] = {"Name": name, "EventsLost": 0, "LogFileMode": 1, "_out": argv[argv.index("-o") + 1],
                           "_providers": [argv[argv.index("-p") + 1]]}
            return {"rc": 0, "stdout": "The command completed successfully."}
        if verb == "update":
            name = argv[2]
            f.etw[name]["_providers"].append(argv[argv.index("-p") + 1])
            return {"rc": 0}
        if verb == "stop":
            s = f.etw.pop(name, None)
            if s is None:
                return {"rc": -2144337918, "stdout": "Data Collector Set was not found."}
            f.disk[s["_out"]] = b"\x14" * 4096
            return {"rc": 0}
        return {"rc": 87}

    def wpr(f, argv):
        name = argv[argv.index("-instancename") + 1] if "-instancename" in argv else "WPR"
        if "-start" in argv:
            if name in recordings:
                return {"rc": -984076288, "stdout": "There are currently active trace sessions."}
            recordings[name] = [argv[i + 1] for i, a in enumerate(argv) if a == "-start"]
            f.etw[f"WPR_initiated_{name}"] = {"EventsLost": 7, "BuffersLost": 0}
            return {"rc": 0}
        if "-stop" in argv:
            if recordings.pop(name, None) is None:
                return {"rc": -984076287, "stdout": "There are no trace profiles running."}
            f.etw.pop(f"WPR_initiated_{name}", None)
            f.disk[argv[argv.index("-stop") + 1]] = b"\x15" * 8192
            return {"rc": 0}
        if "-cancel" in argv:
            if recordings.pop(name, None) is None:
                return {"rc": -984076287}
            f.etw.pop(f"WPR_initiated_{name}", None)
            return {"rc": 0}
        return {"rc": 87}

    def pktmon(f, argv):
        if argv[0] == "start":
            if "PktMon" in f.etw:
                return {"rc": 1, "stdout": "Packet Monitor is already running."}
            f.etw["PktMon"] = {"EventsLost": 0}
            f.disk[argv[argv.index("--file-name") + 1]] = b"\x16" * 2048
            f.pktmon = argv
            return {"rc": 0}
        if argv[0] == "stop":
            f.etw.pop("PktMon", None)
            return {"rc": 0}
        if argv[0] == "etl2pcap":
            f.disk[argv[argv.index("--out") + 1]] = pcapng_bytes(5)
            return {"rc": 0, "stdout": "Processed 5 packets"}
        return {"rc": 87}

    def netsh(f, argv):
        if argv[:2] == ["trace", "start"]:
            if "NetTrace" in f.etw:
                return {"rc": 1, "stdout": "There is already a session in progress."}
            f.etw["NetTrace"] = {"EventsLost": 0}
            out = next(a.split("=", 1)[1] for a in argv if a.startswith("tracefile="))
            f.disk[out] = b"\x17" * 2048
            f.netsh = argv
            return {"rc": 0}
        if argv[:2] == ["trace", "stop"]:
            f.etw.pop("NetTrace", None)
            return {"rc": 0}
        return {"rc": 1}

    def tracerpt(f, argv):
        for flag in ("-o", "-summary", "-report"):
            f.disk[argv[argv.index(flag) + 1]] = b"event\n"
        return {"rc": 0, "stdout": "The command completed successfully."}

    fake.tools.update({"logman.exe": logman, "wpr.exe": wpr, "pktmon.exe": pktmon, "netsh.exe": netsh,
                       "tracerpt.exe": tracerpt})
