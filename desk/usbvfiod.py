"""A USB controller per run, served by usbvfiod (desk up IMAGE --usbvfiod).

usbvfiod is a vfio-user server: it emulates an xHCI controller for QEMU's `vfio-user-pci` device,
with virtual USB 2.0 hubs at port paths, host devices handed to it at a path, and devices that
another process emulates, attached through its hotplug socket. desk starts one per run, in
the run's directory, before the domain, and adds the controller to the guest once the guest has
booted. QEMU 11.1.3 aborts the whole VM when the vfio-user device is removed and when the server
dies, so the device is never hot-removed and the server is stopped only after the domain is gone
(runs.down).

What of it a run has is `run.usbvfiod` in run.json; the tree of hubs and devices is the
controller's to say (desk/hotplug.py), desk keeps no copy of it. It notes only the host devices it
handed over (`held`), to give them back to their drivers when usbvfiod ended without doing so."""

import os
import re
import shutil
import signal
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from . import hotplug, hubs, virt
from .config import DeskError

ENV = "DESK_USBVFIOD"
# The id of the vfio-user device in QEMU.
DEVICE_ID = "usbvfiod0"
# How long usbvfiod has to open its sockets, and to end after SIGTERM (then SIGKILL).
START_WAIT = 10.0
STOP_WAIT = 5.0
# The pcap of the guest's USB traffic goes into the report up to this size.
PCAP_LIMIT = 32 << 20
# sockaddr_un holds 107 bytes of path.
SOCKET_PATH_MAX = 100
# The servers this process started, to reap them: one that ended is a zombie until its parent looks.
_children: dict[int, subprocess.Popen] = {}
HOST_ID = re.compile(r"host (\d+):(\d+)")
# The fork of cyberus-technology/usbvfiod that desk needs (hubs at port paths, QEMU's vfio-user-pci).
# Its releases carry static binaries; `gh release download` without a tag takes the newest.
FORK = "infinitygp/usbvfiod"
BUILD_HINT = (f"install a release of the fork {FORK} (https://github.com/{FORK}/releases), with gh: "
              f"`gh release download --repo {FORK} --pattern 'usbvfiod-*.tar.gz'`, unpack it and run its "
              "install.sh (static binaries into ~/.local/bin); "
              f"or build it with `cargo install --locked --git https://github.com/{FORK} usbvfiod` "
              "(it lands in ~/.cargo/bin); "
              f"or name the binary in ${ENV}")


def binary() -> str:
    """The usbvfiod to run: $DESK_USBVFIOD, else the one in PATH."""
    named = os.environ.get(ENV)
    if named:
        if not (os.path.isfile(named) and os.access(named, os.X_OK)):
            raise DeskError(f"${ENV} is {named!r}, which is not an executable file; {BUILD_HINT}")
        return named
    found = shutil.which("usbvfiod")
    if not found:
        raise DeskError(f"usbvfiod is not installed: {BUILD_HINT}")
    return found


def check() -> str:
    """What `desk up --usbvfiod` needs on this host, refused before a VM is started for it: the
    binary and QEMU's vfio-user-pci. Returns the binary."""
    from .doctor import qemu_device

    exe = binary()
    if not qemu_device("vfio-user-pci")():
        raise DeskError("QEMU has no vfio-user-pci device (QEMU 10.1 or later; `qemu-system-x86_64 -device help` "
                        "lists what this one has), which the USB controller of usbvfiod is")
    return exe


def describe() -> str:
    """For `desk doctor`: whether --usbvfiod works here. Never an error: it is optional."""
    from .doctor import qemu_device

    try:
        exe = binary()
    except DeskError as e:
        return str(e)
    try:
        version = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        version = "version unknown"
    qemu = qemu_device("vfio-user-pci")()
    return (f"{exe} ({version}); QEMU vfio-user-pci: {'yes' if qemu else 'NO, needs QEMU 10.1 or later'}"
            + ("" if qemu else "; desk up --usbvfiod is refused"))


def files(run) -> dict[str, Path]:
    d = run.dir / "usbvfiod"
    return {"dir": d, "vfio": d / "vfio.sock", "hotplug": d / "hotplug.sock", "log": d / "usbvfiod.log",
            "pcap": d / "usb.pcap"}


def _alive(pid: int, vfio: str) -> bool:
    """Whether `pid` is still the usbvfiod of this socket (and no zombie, whose cmdline is empty)."""
    try:
        return vfio.encode() in Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False


def alive(run) -> bool:
    state = run.usbvfiod
    return bool(state) and _alive(state["pid"], state["vfio"])


def tail(path: Path, lines: int = 8, width: int = 160) -> str:
    """The end of a log, each line cut to `width` (the server's lines about guest memory are long)."""
    try:
        return "\n".join(line if len(line) <= width else line[:width - 3] + "..."
                         for line in path.read_text(errors="replace").strip().splitlines()[-lines:])
    except OSError:
        return ""


def start(run) -> None:
    """Start usbvfiod for the run, in a session of its own so that it outlives this command, and
    note it in run.json. It listens on its sockets when this returns."""
    exe = binary()
    f = files(run)
    if len(str(f["hotplug"])) > SOCKET_PATH_MAX:
        raise DeskError(f"{f['hotplug']} is too long for a unix socket; DESK_HOME in a shorter place")
    f["dir"].mkdir(exist_ok=True)
    # Debug level: the hotplug commands, attaches and detaches, errors. Trace adds every register access of
    # the guest's driver, 4 MB for a Windows boot.
    cmd = [exe, "-v", "--no-color", "--socket-path", str(f["vfio"]), "--hotplug-socket-path", str(f["hotplug"]),
           "--pcap-path", str(f["pcap"])]
    with open(f["log"], "w") as log:
        p = subprocess.Popen(cmd, cwd=f["dir"], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             start_new_session=True)
    _children[p.pid] = p
    with run.edit() as r:
        r.usbvfiod = {"pid": p.pid, "binary": exe, "vfio": str(f["vfio"]), "hotplug": str(f["hotplug"]),
                      "log": str(f["log"]), "pcap": str(f["pcap"]), "started": time.time(), "bus": ""}
    deadline = time.monotonic() + START_WAIT
    while not (f["vfio"].exists() and f["hotplug"].exists()):
        if p.poll() is not None or time.monotonic() > deadline:
            why = f"ended with {p.returncode}" if p.poll() is not None else f"opened no sockets in {START_WAIT:.0f} s"
            raise DeskError(f"usbvfiod {why}: {tail(f['log']) or 'its log is empty'} ({f['log']})")
        time.sleep(0.05)
    run.log_event(usbvfiod="start", pid=p.pid)


def stop(run) -> bool:
    """Stop the run's usbvfiod, by its PID; after the domain is gone, as QEMU aborts when the
    server dies first. True when it had to be stopped."""
    state = run.usbvfiod
    if not state or not _alive(state["pid"], state["vfio"]):
        _reap(state.get("pid") if state else None)
        return False
    pid = state["pid"]
    for sig, wait in ((signal.SIGTERM, STOP_WAIT), (signal.SIGKILL, STOP_WAIT)):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            break
        deadline = time.monotonic() + wait
        while _alive(pid, state["vfio"]) and time.monotonic() < deadline:
            time.sleep(0.05)
        if not _alive(pid, state["vfio"]):
            break
    else:
        raise DeskError(f"usbvfiod (pid {pid}) of run {run.id} does not end")
    _reap(pid)
    return True


def _reap(pid: int | None) -> None:
    child = _children.pop(pid, None)
    if child is not None:
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass  # still on its way out; init takes it when this command ends


def free_root_port(dom) -> str:
    """The highest PCIe root port of the live domain without a device (libvirt hands out the
    lowest, so extra disks added later do not take the one this device sits in; libvirt does not
    know the vfio-user device, which it did not add)."""
    root = ET.fromstring(dom.XMLDesc(0))
    used = {int(a.get("bus"), 16) for a in root.iterfind("devices//address[@type='pci']") if a.get("bus")}
    free = []
    for c in root.iterfind("devices/controller[@type='pci']"):
        alias = c.find("alias")
        if c.get("model") == "pcie-root-port" and alias is not None and int(c.get("index")) not in used:
            free.append((int(c.get("index")), alias.get("name")))
    if not free:
        raise DeskError("the guest has no free PCIe root port for the USB controller")
    return max(free)[1]


def attach_controller(run, dom) -> None:
    """Add the controller to the booted guest. It is never removed again: see the module docstring."""
    state = run.usbvfiod
    if not alive(run):
        raise DeskError(f"usbvfiod of run {run.id} ended before the controller was added: {tail(Path(state['log']))}")
    bus = free_root_port(dom)
    try:
        virt.qmp(dom, "device_add", driver="vfio-user-pci", id=DEVICE_ID, bus=bus,
                 socket={"type": "unix", "path": state["vfio"]})
    except DeskError as e:
        raise DeskError(f"could not add the USB controller on {bus}: {e}; usbvfiod says: "
                        f"{tail(Path(state['log'])) or 'nothing'}") from None
    with run.edit() as r:
        r.usbvfiod = {**r.usbvfiod, "bus": bus}
    run.log_event(usbvfiod="controller", bus=bus)


def require(run) -> dict:
    """run.usbvfiod, for a command that needs the controller to be there and the server running."""
    state = run.usbvfiod
    if not state:
        raise DeskError(f"run {run.id} has no usbvfiod controller (desk up IMAGE --usbvfiod)")
    if not _alive(state["pid"], state["vfio"]):
        raise DeskError(ended(run))
    return state


def ended(run) -> str:
    """Why a run whose usbvfiod is gone is no use any more."""
    state = run.usbvfiod
    log = tail(Path(state["log"]), 2, 120).replace("\n", " / ")
    return (f"usbvfiod (pid {state['pid']}) of run {run.id} has ended: the guest has lost its USB controller, and "
            f"QEMU can abort the VM with it (desk down {run.id}; the report has the log, {state['log']})"
            + (f"; its log ends with: {log}" if log else ""))


def hotplug_socket(run) -> Path:
    return Path(require(run)["hotplug"])


def tree(run, timeout: float = hotplug.TIMEOUT) -> list[dict]:
    return hotplug.tree(hotplug_socket(run), timeout=timeout)


def lock(run):
    """One desk command at a time changes the tree: it reads it, plans the hubs and then attaches."""
    from .runs import _flock

    return _flock(run.dir / ".usbvfiod.lock", f"another command changing the USB tree of run {run.id}", 60)


def host_entries(entries: list[dict]) -> dict[tuple[int, int], dict]:
    """The host devices in a tree, by bus and device number."""
    out = {}
    for e in entries:
        m = HOST_ID.fullmatch(e["id"])
        if m:
            out[(int(m[1]), int(m[2]))] = e
    return out


def describe_entry(e: dict) -> str:
    if e.get("hub_ports") is not None:
        return f"USB hub, {e['hub_ports']} ports, {e['speed']} speed"
    return f"{e['id']}, {e['speed']} speed"


def attach(run, device: dict, at: str | None, wanted: dict[tuple[int, ...], hubs.Hub]) -> dict:
    """Hand a host device to the run's controller: at a port path below the hubs that are missing
    (made as `wanted` says, else as default hubs), or on the first free root port that fits it."""
    path = hubs.parse_path(at) if at else None
    if wanted and not path:
        raise DeskError("--hub goes with --at: it says what the hubs on the way to that port path are")
    at = hubs.fmt(path) if path else None
    here = (device["bus"], device["dev"])
    with lock(run):
        sock = hotplug_socket(run)
        entries = hotplug.tree(sock)
        have = host_entries(entries).get(here)
        if have is not None:
            raise DeskError(f"USB device {device['id']} is in run {run.id} already, at {have['path']} "
                            f"(desk -r {run.id} usb rm)")
        new = hubs.plan(path, wanted, entries) if path else []
        try:
            fd = os.open(device["node"], os.O_RDWR)
        except OSError as e:
            raise DeskError(f"cannot open {device['node']} ({device['id']} {device['name']}): {e.strerror}") from None
        made = []
        try:
            for hub in new:
                hotplug.attach_hub(sock, hub)
                made.append(hub)
                run.log_event(usb="hub", at=hubs.fmt(hub.path), spec=hub.spec())
            hotplug.attach_host(sock, device["bus"], device["dev"], fd, at=at)
        except DeskError:
            # Hubs made for a device that did not attach go again (the first takes the others with it).
            if made:
                try:
                    hotplug.detach_at(sock, hubs.fmt(made[0].path))
                except DeskError:
                    pass  # the error that stopped the attach is the one to report
            raise
        finally:
            os.close(fd)
        _note_held(run, lambda h: (h["bus"], h["dev"]) != here,
                   {"bus": here[0], "dev": here[1], "port": device.get("port", "")})
        # Looked at, not assumed: where the controller says the device is.
        placed = host_entries(hotplug.tree(sock)).get(here)
        if placed is None:
            raise DeskError(f"usbvfiod accepted USB device {device['id']} but its tree does not show it (it "
                            "detaches a device that does not enumerate; the log of the run's usbvfiod says why)")
        run.log_event(usb="add", device=device["id"], address=f"{here[0]}.{here[1]}", name=device["name"],
                      at=placed["path"], hubs=[hubs.fmt(h.path) for h in made])
        return {**device, "at": placed["path"], "hubs": [hubs.fmt(h.path) for h in made]}


def remove(run, query: str) -> dict:
    """Take a host device, or what is at `at:PORTPATH` (with everything below it), out of the controller."""
    from . import usb

    with lock(run):
        sock = hotplug_socket(run)
        entries = hotplug.tree(sock)
        m = hubs.AT.fullmatch(query)
        if m:
            there = {e["path"]: e for e in entries}.get(m[1])
            if there is None:
                raise DeskError(f"run {run.id} has nothing at port path {m[1]} (desk usb ls)")
            below = [e for e in entries if e["path"].startswith(m[1] + ".")]
            hotplug.detach_at(sock, m[1])
            gone = host_entries([there, *below])
            _note_held(run, lambda h: (h["bus"], h["dev"]) not in gone)
            run.log_event(usb="remove", at=m[1], below=[e["path"] for e in below])
            return {"id": f"at:{m[1]}", "tree": True, "at": m[1], "name": describe_entry(there),
                    "below": [e["path"] for e in below]}
        held = host_entries(entries)
        devices = [d for d in usb.host_devices() if (d["bus"], d["dev"]) in held]
        # One unplugged from the host meanwhile may still be listed: name it by its address.
        devices += [{"id": f"{b}.{d}", "bus": b, "dev": d, "name": "", "node": "", "access": False, "port": ""}
                    for (b, d) in held if not any((x["bus"], x["dev"]) == (b, d) for x in devices)]
        try:
            device = usb.pick(devices, query)
        except DeskError:
            raise DeskError(f"run {run.id} has no USB device {query} (desk usb ls)") from None
        at = held[(device["bus"], device["dev"])]["path"]
        hotplug.detach_at(sock, at)
        _note_held(run, lambda h: (h["bus"], h["dev"]) != (device["bus"], device["dev"]))
        run.log_event(usb="remove", device=device["id"], address=f"{device['bus']}.{device['dev']}", at=at)
        return {**device, "at": at}


def _note_held(run, keep, add: dict | None = None) -> None:
    """Change the host devices the run notes as handed over: keep those `keep` says, and add `add`."""
    with run.edit() as r:
        state = dict(r.usbvfiod or {})
        state["held"] = [h for h in state.get("held", []) if keep(h)] + ([add] if add else [])
        r.usbvfiod = state


def give_back(run) -> list[str]:
    """After usbvfiod ended: bind the drivers of the host devices the run held again. usbvfiod
    does so when it ends; one that was killed or crashed leaves them without a driver. Returns the
    devices that needed it, or why it failed for one."""
    from . import usb

    out = []
    for h in (run.usbvfiod or {}).get("held", []):
        if not h.get("port"):
            continue
        name = f"{h['bus']}.{h['dev']} (port:{h['port']})"
        try:
            if usb.rebind(h["port"], h["bus"], h["dev"]):
                out.append(name)
        except OSError as e:
            out.append(f"{name}: {e.strerror}")
    return out


def held_by_runs() -> tuple[dict[tuple[int, int], tuple[str, str]], list[dict]]:
    """For `desk usb ls`: the host devices the controllers of the live runs hold (bus and device
    number -> run, port path), and the other entries of their trees (hubs, virtual and external
    devices) or, for a controller that did not answer, a row that says so."""
    from . import runs

    held, rows = {}, []
    for r in runs.all_runs():
        if not r.usbvfiod or r.live_domain() is None:
            continue
        try:
            entries = tree(r, timeout=3)
        except DeskError as e:
            rows.append({"id": "", "unread": f"the USB tree of run {r.id} could not be read: {e}", "run": r.id})
            continue
        for key, e in host_entries(entries).items():
            held[key] = (r.id, e["path"])
        rows += [{"id": "hub" if e.get("hub_ports") is not None else e["id"], "tree": True, "at": e["path"],
                  "run": r.id, "name": describe_entry(e)}
                 for e in entries if not HOST_ID.fullmatch(e["id"])]
    return held, rows


def report(run, out: Path) -> str:
    """The lines for the run's report: what became of the server, its log copied to `out`, and the
    tree it held when the report was written."""
    state = run.usbvfiod
    log = Path(state["log"])
    if log.exists():
        shutil.copy(log, out / "usbvfiod.log")
    running = alive(run)
    bad = len(re.findall(r"\b(WARN|ERROR)\b", log.read_text(errors="replace"))) if log.exists() else 0
    domain = run.live_domain() is not None
    if running:
        line = f"- usbvfiod: running (pid {state['pid']}), controller on {state['bus'] or 'no PCIe port yet'}"
    elif domain:
        line = (f"- usbvfiod: ENDED while the VM runs (pid {state['pid']}): the guest has lost its controller, and "
                "QEMU can abort the VM with it")
    else:
        line = (f"- usbvfiod: ended (pid {state['pid']}), and the VM is gone too: QEMU can abort when the server dies "
                "and the server ends when QEMU does, so its log tells which came first")
    line += f"; {bad} warnings and errors in its log (see usbvfiod.log)"
    pcap = Path(state["pcap"])
    if pcap.exists():
        if pcap.stat().st_size <= PCAP_LIMIT:
            shutil.copy(pcap, out / "usb.pcap")
            line += "; the USB traffic of the guest is in usb.pcap"
        else:
            line += f"; its USB traffic ({pcap.stat().st_size >> 20} MiB) is too big for the report: {pcap}"
    if not running:
        line += f"\n\n  the end of its log:\n\n```\n{tail(log, 6) or '(nothing)'}\n```\n"
    if running:
        try:
            entries = tree(run, timeout=3)
        except DeskError as e:
            return line + f"\n- USB tree not read: {e}"
        shown = [f"{e['path']:<14} {describe_entry(e)}" for e in entries]
        line += "\n- USB tree at the end:\n\n```\n" + ("\n".join(shown) or "(empty)") + "\n```\n"
    return line
