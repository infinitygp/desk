"""Dynamic observation of a run's guest: captures by native tools (Process Monitor, ETW through
WPR and logman, pktmon and netsh trace, USBPcap), listings of local IPC endpoints, and debugger
servers reachable from the host, each one a session of the run (`desk observe start PROVIDER`).

Here is what all providers share: a session is started only after its provider said the guest
can run it, ends by itself at its limits (a watchdog in the guest), is stopped only by what desk
itself started, and ends with its files copied, compared by SHA-256 with the guest's and read as
their format, or with the reason it could not. The guest's clock is sampled at start, at every
mark and at stop, so guest times line up with the run's timeline, screenshots and video.

Providers are in their own modules; the session store in session.py; the guest side in
guestops.py and helper.ps1; the report's section in report.py."""

import base64
import json
import os
import shutil
import sys
import time
from contextlib import ExitStack
from pathlib import Path

from ..config import CACHE, DeskError
from .base import ArtifactSpec, Conflict, Missing, Privileges, Provider, Unsupported, sha256, write_setup
from .guestops import GUEST_HELPER, ROOT, Guest, ObserveError, win
from .session import ACTIVE, COLLECTABLE, Session, Store, pid_alive, process_key

__all__ = ["ObserveError", "Missing", "Unsupported", "Conflict", "Privileges", "PROVIDERS", "start", "stop",
           "stop_many", "status", "listing", "mark", "setup", "capabilities", "collect_all", "snapshot_saved",
           "snapshot_restored"]

# A capture nobody stops ends by itself after this long, or at this size of its files.
DEFAULT_DURATION = 30 * 60
DEFAULT_MAX_BYTES = 4 << 30
# Endpoints (debugger servers) and listings hold no growing files; a server stays up this long.
ENDPOINT_DURATION = 4 * 3600
# The ETW provider whose events mark a moment in a trace: an EventSource, so its GUID follows from
# this name (etw.eventsource_guid) and the logman sessions desk starts enable it.
MARKER_SOURCE = "Desk-Observe-Marker"


def _providers() -> dict[str, Provider]:
    from .debug import DbgSrv, IdaServer
    from .etw import Logman, Wpr
    from .ipc import Ipc
    from .network import NetshTrace, Pktmon
    from .procmon import ProcMon
    from .usbpcap import UsbPcap

    return {p.name: p for p in (ProcMon(), Wpr(), Logman(), Pktmon(), NetshTrace(), Ipc(), UsbPcap(), DbgSrv(),
                                IdaServer())}


PROVIDERS = _providers()


def provider(name: str) -> Provider:
    try:
        return PROVIDERS[name]
    except KeyError:
        raise DeskError(f"no observation provider {name!r} (desk observe providers lists them: "
                        f"{', '.join(PROVIDERS)})") from None


class Host:
    """What providers need from the host: tools set up for them, the run's USB devices and port
    forwards. Tests and Windows CI give their own."""

    def __init__(self, run=None):
        self.run = run

    @staticmethod
    def setup_dir(name: str) -> Path:
        return CACHE / "observe" / name

    def setup(self, name: str) -> dict | None:
        f = self.setup_dir(name) / "setup.json"
        try:
            return json.loads(f.read_text())
        except (OSError, ValueError):
            return None

    def usb(self) -> list[dict]:
        """The host USB devices handed to this run (desk usb add)."""
        from .. import usb

        dom = self.run.live_domain()
        if dom is None:
            return []
        here = set(usb.attached(dom))
        found = [d for d in usb.host_devices() if (d["bus"], d["dev"]) in here]
        known = {(d["bus"], d["dev"]) for d in found}
        return found + [{"id": f"{b}.{d}", "bus": b, "dev": d, "name": "(gone from the host)"} for b, d in here
                        if (b, d) not in known]

    def forward(self, port: int) -> int:
        from .. import forward

        return forward.start(self.run, port)

    def unforward(self, port: int) -> None:
        from .. import forward

        forward.stop(self.run, port)

    def reachable(self, local: int, timeout: float = 5) -> bool:
        import socket

        try:
            with socket.create_connection(("127.0.0.1", local), timeout=timeout):
                return True
        except OSError:
            return False


def guest_for(run, spec=None) -> Guest:
    from .. import runs
    from .guestops import SshGuest

    return SshGuest(run, spec or runs.spec_for(run))


# Setup and capabilities.


def setup(name: str, source: Path, accept_licence: bool = False, who: str = "") -> dict:
    """The host's copy of a tool desk does not ship, for the provider `name`. Licence terms are
    accepted here, by the user, or not at all."""
    p = provider(name)
    if not p.setup_files:
        raise DeskError(f"{name} needs nothing set up on the host (Windows has its tools)")
    if not source.exists():
        raise DeskError(f"{source} does not exist")
    dest = Host.setup_dir(name)
    tmp = dest.with_name(f".{name}.setup")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        got = p.install_setup(source, tmp)
        accepted = {"terms": p.licence, "by": who, "at": time.time()} if accept_licence and p.licence else None
        info = write_setup(tmp, name, got, source.resolve(), accepted)
        shutil.rmtree(dest, ignore_errors=True)
        tmp.rename(dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return info


def capabilities(run, names: list[str] | None = None) -> list[tuple[Provider, object]]:
    """Each provider with what this run's guest says of it: a Capability, or the reason it was
    not asked (another family, the guest not answering)."""
    from .. import runs

    spec = runs.spec_for(run) if run else None
    host = Host(run)
    guest = guest_for(run, spec) if run else None
    out = []
    for p in [provider(n) for n in names] if names else PROVIDERS.values():
        if spec is None:
            out.append((p, "no run to ask (desk observe providers -r RUN asks a guest)"))
        elif spec.family not in p.families:
            out.append((p, f"not for {spec.family} guests (only {', '.join(p.families)})"))
        else:
            try:
                out.append((p, p.capability(guest, p.defaults(), host)))
            except DeskError as e:
                out.append((p, f"could not ask the guest: {e}"))
    return out


# Starting.


def _limits(p: Provider, duration: float | None, max_mb: float | None) -> dict:
    if duration is not None and duration <= 0:
        raise DeskError("--duration is a number of seconds above 0")
    if max_mb is not None and max_mb <= 0:
        raise DeskError("--max-mb is a number of MiB above 0")
    if p.kind == "snapshot":
        return {}
    default = ENDPOINT_DURATION if p.kind == "endpoint" else DEFAULT_DURATION
    limits = {"duration": duration or default}
    if p.kind == "trace":
        limits["max_bytes"] = int(max_mb * 2**20) if max_mb else DEFAULT_MAX_BYTES
    return limits


def _check_conflicts(p: Provider, s: Session, others: list[Session]) -> None:
    mine = p.resource_of(s)
    if not mine:
        return
    for o in others:
        if o.state in ACTIVE and o.id != s.id and PROVIDERS.get(o.provider) and \
                PROVIDERS[o.provider].resource_of(o) == mine:
            raise Conflict(f"session {o.name()} ({o.provider}, {o.state}) holds {mine} in this run already; "
                           f"desk observe stop {o.id} first")


def start(run, name: str, label: str | None = None, options: dict | None = None, duration: float | None = None,
          max_mb: float | None = None, target: dict | None = None, owner: str = "", host: Host | None = None,
          guest: Guest | None = None) -> Session:
    from .. import runs

    p = provider(name)
    spec = runs.spec_for(run)
    if spec.family not in p.families:
        raise Unsupported(f"{name} observes {', '.join(p.families)} guests, and {run.image} is a {spec.family} one")
    opts = p.options(options or {})
    # Secrets go to the provider only: the session, its report and observe.json say whether one was given.
    secrets = {k: opts[k] for k, o in p.OPTIONS.items() if o.secret and opts[k]}
    opts.update({k: "(given)" if opts[k] else "" for k, o in p.OPTIONS.items() if o.secret})
    limits = _limits(p, duration, max_mb)
    run.dom()
    host = host or Host(run)
    guest = guest or guest_for(run, spec)
    store = Store(run)
    reconcile(run, guest=guest)

    def conflicts(s: Session, others: list[Session]) -> None:
        s.options, s.limits, s.target, s.owner = opts, limits, target, owner or run.label
        s.notes = list(p.notes)
        _check_conflicts(p, s, others)

    s = store.new(name, label, conflicts)
    s.secrets = secrets
    try:
        probe = p.preflight(guest, s, host)
        s.stats["arch"] = (probe.get("os") or {}).get("arch") or "AMD64"
        s.stats["os"] = probe.get("os")
        s.boot = str(probe.get("boot"))
        s.guest_dir = win(ROOT, f"{run.id}-{s.id}")
        # Kept now: a desk killed while the tool starts leaves a session whose cleanup finds it.
        store.update(s)
        # A folder of this name left by a snapshot restore holds nothing of this session.
        guest.remove(s.guest_dir)
        guest.mkdir(s.guest_dir)
        s.clock.append({**guest.clock(), "at": "start"})
        s.tool["host_dir"] = str(store.files(s.id))
        p.start(guest, s, host)
        s.started = time.time()
        _watch(guest, p, s)
        if target:
            _follow(guest, s)
        s.state = "running"
        s.pid = 0
        s.event("running")
        store.update(s)
    except BaseException as e:
        s.state, s.error = "failed", (str(e) or type(e).__name__)
        s.event("failed", s.error)
        try:
            p.cleanup(guest, s, host)
            if s.watchdog:
                guest.stop_process(s.watchdog)
        except Exception as cleanup_error:  # noqa: BLE001 - the error that stopped the start is the one to report
            s.event("cleanup failed", str(cleanup_error))
            print(f"[desk] warning: cleaning up after the failed start: {cleanup_error}", file=sys.stderr)
        s.pid = 0
        store.update(s)
        run.log_event(observe="failed", session=s.id, provider=name, detail=s.error[:300])
        raise
    run.log_event(observe="start", session=s.id, provider=name, label=s.label)
    return s


def _watch(guest: Guest, p: Provider, s: Session, seconds: float | None = None) -> None:
    """A watchdog in the guest that ends the capture at its limits, also when desk is gone."""
    commands = p.stop_commands(s)
    seconds = seconds or s.limits.get("duration")
    if not seconds or (not commands and not s.tool.get("procs")):
        return
    if not commands:
        # A server or capture ended by its PID (no command of its own stops it).
        commands = [{"exe": "taskkill.exe", "args": f"/T /F /PID {pr['pid']}", "timeout": 30}
                    for pr in s.tool.get("procs") or []]
    out = win(s.guest_dir, "watchdog.json")
    order = {"seconds": seconds, "max_bytes": s.limits.get("max_bytes", 0), "files": p.watch_files(s),
             "tool": (s.tool.get("procs") or [None])[0], "stop": commands, "out": out}
    payload = base64.b64encode(json.dumps(order).encode()).decode()
    proc = guest.launch("powershell.exe", ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                           "-WindowStyle", "Hidden", "-File", GUEST_HELPER, "-Watch", payload])
    s.watchdog = {"pid": proc["pid"], "created": proc["created"], "out": out}


# Following processes.


def descendants(table: list[dict], target: dict) -> list[dict]:
    """The processes `target` names ({"image": "app.exe"} or {"pid": N}) and every descendant,
    each by PID and creation time: a child is one whose parent PID is the process's and that was
    created after it (Windows reuses PIDs, and a parent PID may be a dead process's)."""
    image = (target.get("image") or "").lower()
    roots = [p for p in table if (target.get("pid") and p["pid"] == target["pid"])
             or (image and (p.get("name") or "").lower() == image)]
    found = {process_key(p): p for p in roots}
    queue = list(roots)
    while queue:
        parent = queue.pop()
        for child in table:
            if child["ppid"] == parent["pid"] and child["pid"] != parent["pid"] and \
                    (child.get("created") or 0) >= (parent.get("created") or 0) and process_key(child) not in found:
                found[process_key(child)] = child
                queue.append(child)
    return sorted(found.values(), key=lambda p: (p.get("created") or 0, p["pid"]))


def _follow(guest: Guest, s: Session) -> None:
    now = time.time()
    for p in descendants(guest.processes(), s.target):
        key = process_key(p)
        seen = s.processes.get(key) or {**{k: p.get(k) for k in ("pid", "ppid", "name", "path", "cmdline", "created")},
                                        "first_seen": now}
        seen["last_seen"] = now
        s.processes[key] = seen


# The state of open sessions.


def reconcile(run, guest: Guest | None = None, store: Store | None = None) -> list[Session]:
    """Sessions as the guest has them now: one whose desk command died is interrupted, as is one
    whose guest rebooted or whose VM is gone; one whose capture ended by its limit or by itself
    is ended. One guest round trip for all of them."""
    store = store or Store(run)
    sessions = store.all()
    changed = []
    for s in sessions:
        if s.state in ("starting", "stopping") and s.pid and not pid_alive(s.pid):
            s.error = s.error or f"the desk command that was {s.state[:-3]}ing it (pid {s.pid}) ended"
            s.state = "interrupted"
            s.event("interrupted", s.error)
            changed.append(s)
    watching = [s for s in sessions if s.state == "running"]
    if watching:
        if run.live_domain() is None:
            for s in watching:
                s.state, s.error = "interrupted", "the VM is no longer running"
                s.event("interrupted", s.error)
                changed.append(s)
        else:
            guest = guest or guest_for(run)
            procs = [pr for s in watching
                     for pr in [*(s.tool.get("procs") or []), *([s.watchdog] if s.watchdog else [])]]
            state = guest.call("state", procs=procs, watch=[s.watchdog["out"] for s in watching if s.watchdog],
                               etw=any(PROVIDERS[s.provider].name in ("wpr", "etw", "pktmon", "netsh")
                                       for s in watching))
            for s in watching:
                if s.boot and str(state.get("boot")) != s.boot:
                    s.state, s.error = "interrupted", "the guest rebooted, and the capture ended with it"
                    s.event("interrupted", s.error)
                    changed.append(s)
                    continue
                said = (state.get("watch") or {}).get((s.watchdog or {}).get("out"))
                if said:
                    try:
                        verdict = json.loads(said.lstrip("\ufeff"))
                    except ValueError:
                        verdict = {"state": "stopping", "reason": "unknown"}
                    s.stats["watchdog"] = verdict
                    if verdict.get("state") == "done" or verdict.get("reason") == "tool-ended":
                        s.state = "ended"
                        s.event("ended", _ended_why(verdict))
                        changed.append(s)
                        continue
                elif s.watchdog and not s.stats.get("watchdog_gone") and any(
                        a["pid"] == s.watchdog["pid"] and a["created"] == s.watchdog["created"] and not a.get("alive")
                        for a in state.get("alive") or []):
                    s.stats["watchdog_gone"] = True
                    s.event("watchdog gone", "the watchdog ended without a verdict: the capture's time and size limits "
                                             "are no longer kept in the guest")
                    changed.append(s)
                capturing = PROVIDERS[s.provider].capturing(state, s)
                if capturing is False:
                    s.state = "ended"
                    s.event("ended", "the capture is no longer running in the guest")
                    changed.append(s)
    for s in changed:
        with store.edit(s.id) as current:
            if current.state in ("running", "starting", "stopping"):
                for k in ("state", "error", "events", "stats"):
                    setattr(current, k, getattr(s, k))
                run.log_event(observe=current.state, session=current.id, provider=current.provider,
                              detail=current.events[-1].get("detail", ""))
    return store.all()


def _ended_why(verdict: dict) -> str:
    reason = verdict.get("reason")
    if reason == "duration":
        return "its time limit was reached; the watchdog stopped it"
    if reason == "size":
        return "its files reached their size limit; the watchdog stopped it"
    if reason == "tool-ended":
        return "the tool ended by itself"
    return f"ended ({reason})"


def listing(run, guest: Guest | None = None) -> list[Session]:
    try:
        return reconcile(run, guest=guest)
    except DeskError as e:
        print(f"[desk] warning: the guest was not asked about its captures ({e}); states as last known",
              file=sys.stderr)
        return Store(run).all()


def status(run, ref: str | None = None, guest: Guest | None = None) -> tuple[Session, dict]:
    store = Store(run)
    guest = guest or guest_for(run)
    reconcile(run, guest=guest, store=store)
    s = store.find(ref)
    live = {}
    if s.state == "running":
        p = PROVIDERS[s.provider]
        live = p.status(guest, s)
        live.update({k: v for k, v in p.sample(guest, s).items()})
        if s.target:
            _follow(guest, s)
            with store.edit(s.id) as cur:
                cur.processes = s.processes
    return store.load(s.id), live


# Marks.


def mark(run, text: str, ref: str | None = None, guest: Guest | None = None) -> dict:
    """A moment in every running session (or the one named): on the host's timeline, as a file
    the guest writes (seen by ProcMon and file I/O traces) and as an ETW event of MARKER_SOURCE."""
    store = Store(run)
    guest = guest or guest_for(run)
    sessions = [s for s in reconcile(run, guest=guest, store=store) if s.state == "running"]
    if ref:
        named = store.find(ref)
        sessions = [s for s in sessions if s.id == named.id]
        if not sessions:
            raise DeskError(f"session {named.name()} is {named.state}, not running")
    if not sessions:
        raise DeskError(f"run {run.id} has no running observation session to mark (desk observe start)")
    with store.lock():
        counter = store.dir / "marks"
        seq = int(counter.read_text()) + 1 if counter.exists() else 1
        counter.write_text(str(seq))
    before = time.time()
    said = guest.call("mark", path=win(ROOT, "marks", f"mark-{seq:04d}.txt"), text=text, source=MARKER_SOURCE,
                      event=f"DeskMark{seq}")
    after = time.time()
    sample = guest.clock()
    offset = sample["guest"] - sample["host"]
    m = {"seq": seq, "text": text, "host": (before + after) / 2, "host_window": [before, after],
         "guest": said["utc_ms"] / 1000, "guest_as_host": said["utc_ms"] / 1000 - offset,
         "offset": offset, "uncertainty": sample["uncertainty"], "file": said.get("path"),
         "event": said.get("event"), "event_error": said.get("event_error"), "sessions": [s.id for s in sessions]}
    for s in sessions:
        try:
            PROVIDERS[s.provider].on_mark(guest, s, m)
        except DeskError as e:
            m.setdefault("problems", []).append(f"{s.id}: {e}")
        with store.edit(s.id) as cur:
            cur.markers.append(m)
            cur.clock.append({**sample, "at": f"mark {seq}"})
            cur.tool = s.tool
    run.log_event(observe="mark", text=text, seq=seq, sessions=m["sessions"])
    return m


# Stopping and collecting.


def stop(run, ref: str | None = None, force: bool = False, keep_guest: bool = False, reason: str = "",
         host: Host | None = None, guest: Guest | None = None) -> tuple[Session, list[str]]:
    """Stop the capture (if it still runs), copy its files, check them; the session and what is
    wrong with what it left (nothing: complete). A capture that could not be stopped (another
    ProcMon in the way) stays as it was, to be stopped again."""
    [done] = stop_many(run, [ref], force=force, keep_guest=keep_guest, reason=reason, host=host, guest=guest)
    if isinstance(done, BaseException):
        raise done
    return done


def stop_many(run, refs: list[str | None], force: bool = False, keep_guest: bool = False, reason: str = "",
              host: Host | None = None, guest: Guest | None = None) -> list:
    """Several sessions in two passes: every capture is stopped first, and only then is any of
    them converted and copied, so that no capture still running records desk taking the others'
    files. Per session, in the order given: (session, problems), or the error that kept it from
    stopping; such a one stays as it was, to be stopped again, and the others go on."""
    store = Store(run)
    host = host or Host(run)
    live = run.live_domain() is not None
    guest = guest or (guest_for(run) if live else None)
    reconcile(run, guest=guest, store=store)
    results: dict[int, object] = {}
    ended = []
    with ExitStack() as held:
        # Pass 1: stop.
        for i, ref in enumerate(refs):
            try:
                s = store.find(ref)
                if s.state not in COLLECTABLE:
                    raise DeskError(f"session {s.name()} is {s.state}" + (
                        ": another desk command is stopping it" if s.state == "stopping" else " (desk observe list)"))
                held.enter_context(store.operation(s.id))
                ended.append((i, *_end_capture(store, guest, host, s.id, live, force, reason)))
            except DeskError as e:
                results[i] = e
        # Pass 2: convert, copy, check, clean up.
        for i, s, before, gone in ended:
            results[i] = (s, _finish(run, store, guest, host, s, before, gone, live, keep_guest))
    return [results[i] for i in range(len(refs))]


def _end_capture(store: Store, guest: Guest, host: Host, sid: str, live: bool, force: bool,
                 reason: str) -> tuple[Session, str, bool]:
    s = store.load(sid)
    before = s.state
    p = PROVIDERS[s.provider]
    # The tools of a rebooted or restored guest are gone with it; there is nothing to stop.
    gone = before == "interrupted" and s.error.startswith(("the guest rebooted", "snapshot", "the VM"))
    s.state, s.pid = "stopping", os.getpid()
    s.event("stopping", reason)
    store.update(s)
    watchdog_ended = False
    try:
        if live and not gone and (before == "running" or s.tool.get("procs")):
            if before == "running":
                s.stats.update(p.sample(guest, s))
            _end_watchdog(guest, s)
            watchdog_ended = True
            p.stop(guest, s, host, force=force)
    except BaseException as e:
        s.state, s.pid = before, 0
        s.event("stop failed", str(e) or type(e).__name__)
        if watchdog_ended and before == "running" and s.started:
            # The capture goes on, so its limits do: what is left of its time, from now.
            try:
                _watch(guest, p, s, seconds=max(10.0, s.limits.get("duration", 0) - (time.time() - s.started)))
            except DeskError as again:
                s.event("watchdog gone", f"not started again after the failed stop: {again}")
        store.update(s)
        raise
    store.update(s)
    return s, before, gone


def _finish(run, store: Store, guest: Guest, host: Host, s: Session, before: str, gone: bool, live: bool,
            keep_guest: bool) -> list[str]:
    p = PROVIDERS[s.provider]
    problems: list[str] = [f"interrupted: {s.error}"] if before == "interrupted" else []
    try:
        if not live:
            problems.append("the VM is not running: nothing could be collected from the guest")
        else:
            if s.target and not gone:
                _follow(guest, s)
            try:
                p.found(guest, s)
            except DeskError as e:
                problems.append(f"looking for its files in the guest: {e}")
            arts = p.artifacts(s)
            if not gone:
                try:
                    arts += p.convert(guest, s, host)
                except DeskError as e:
                    s.notes.append(f"conversion failed: {e}")
            problems += _collect(guest, store, s, p, arts)
            try:
                s.clock.append({**guest.clock(), "at": "stop"})
            except DeskError:
                pass
            if not gone:
                try:
                    p.cleanup(guest, s, host)
                except DeskError as e:
                    problems.append(f"cleaning up in the guest: {e}")
            if not s.guest_dir:
                pass  # it never got as far as a folder in the guest
            elif not keep_guest and all(a.get("transferred") or not a.get("in_guest") for a in s.artifacts):
                guest.remove(s.guest_dir)
            elif s.artifacts:
                s.notes.append(f"the guest keeps {s.guest_dir}")
    except BaseException as e:
        problems.append(f"collecting stopped: {e or type(e).__name__}")
        raise
    finally:
        s.state, s.pid, s.stopped = "stopped", 0, time.time()
        verdict = s.stats.get("watchdog") or {}
        if verdict.get("reason") in ("duration", "size"):
            s.notes.append(_ended_why(verdict))
        if s.stats.get("watchdog_gone"):
            s.notes.append("its watchdog ended early: the time and size limits were not kept from then on")
        s.complete = not problems
        s.error = "; ".join(problems)
        s.event("stopped", "complete" if s.complete else s.error)
        store.update(s)
        run.log_event(observe="stop", session=s.id, provider=s.provider, complete=s.complete,
                      artifacts=[a["name"] for a in s.artifacts if a.get("transferred")])
    return problems


def _end_watchdog(guest: Guest, s: Session) -> None:
    w = s.watchdog
    if not w:
        return
    verdict = s.stats.get("watchdog") or {}
    if verdict.get("state") == "stopping":
        # It is stopping the capture right now: let it finish rather than cut the tool off.
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            said = guest.call("read", path=w["out"])
            if said.get("exists"):
                try:
                    verdict = json.loads(said["text"].lstrip("\ufeff"))
                except ValueError:
                    verdict = {}
                if verdict.get("state") == "done":
                    s.stats["watchdog"] = verdict
                    break
            time.sleep(2)
    guest.stop_process(w, tree=False)


def _collect(guest: Guest, store: Store, s: Session, p: Provider, arts: list[ArtifactSpec]) -> list[str]:
    problems = []
    remote = [a.guest_path for a in arts if a.guest_path]
    there = guest.files(remote, hash=True, limit=900) if remote else {}
    dest = store.files(s.id)
    dest.mkdir(parents=True, exist_ok=True)
    s.artifacts = []
    for a in arts:
        info = there.get(a.guest_path) or {}
        rec = {"name": a.name, "kind": a.kind, "guest_path": a.guest_path, "required": a.required,
               "description": a.description, "in_guest": bool(info.get("exists")), "transferred": False,
               "verified": False}
        s.artifacts.append(rec)
        if a.guest_path is None:
            # Made on the host (listings): checked like the others, nothing to copy.
            local = dest / a.name
            rec.update(in_guest=False, transferred=local.exists())
            if not local.exists():
                rec["detail"] = "not made"
                if a.required:
                    problems.append(f"{a.name} was not made")
                continue
            rec.update(path=str(local), bytes=local.stat().st_size, sha256=sha256(local))
            check = p.verify(local, a)
            rec.update(check=check, verified=bool(check.get("ok")), detail=check.get("detail", ""))
            continue
        if not info.get("exists"):
            rec["detail"] = "not in the guest"
            if a.required:
                problems.append(f"{a.name} was not made")
            continue
        rec["guest_bytes"], rec["guest_sha256"] = info.get("size"), info.get("sha256")
        local = dest / a.name
        try:
            # 600 s, or 20 MB/s for a large one.
            guest.pull(a.guest_path, local, timeout=max(600.0, (info.get("size") or 0) / 20e6))
        except DeskError as e:
            rec["detail"] = f"not copied: {e}"
            problems.append(f"{a.name} was not copied ({e}); it stays in the guest at {a.guest_path}")
            continue
        rec["path"] = str(local)
        rec["bytes"] = local.stat().st_size
        rec["sha256"] = sha256(local)
        if info.get("sha256") and rec["sha256"] != info["sha256"]:
            rec["detail"] = "the copy differs from the guest's file (SHA-256)"
            problems.append(f"{a.name}: the copy differs from the guest's file; it stays in the guest at "
                            f"{a.guest_path}")
            continue
        rec["transferred"] = True
        check = p.verify(local, a)
        rec["check"] = check
        rec["verified"] = bool(check.get("ok"))
        rec["detail"] = check.get("detail", "")
        if not check.get("ok") and a.required:
            problems.append(f"{a.name}: {check.get('detail')}")
    return problems


def collect_all(run, reason: str, guest: Guest | None = None, host: Host | None = None) -> list[str]:
    """Before a run goes down (desk down, gc, the end of a scenario): every session still open is
    stopped and collected; with the VM gone, they say so. Warnings, never an exception."""
    store = Store(run)
    if not (store.dir / "sessions").is_dir():
        return []
    warnings = []
    open_ = [s for s in store.all() if s.state in COLLECTABLE or s.state in ("starting", "stopping")]
    if not open_:
        return []
    try:
        results = stop_many(run, [s.id for s in open_], reason=reason, guest=guest, host=host)
    except DeskError as e:
        return [f"observation sessions not collected: {e}"]
    for s, done in zip(open_, results):
        if isinstance(done, BaseException):
            warnings.append(f"observation {s.name()} ({s.provider}) not collected: {done}")
        elif done[1]:
            warnings.append(f"observation {done[0].name()} ({done[0].provider}): " + "; ".join(done[1]))
    return warnings


# Snapshots.


def snapshot_saved(run, snapdir: Path) -> None:
    """The sessions as they are when a snapshot is saved: restoring it takes the guest back to
    them, captures running then included."""
    src = Store(run).dir / "sessions"
    if src.is_dir():
        shutil.copytree(src, snapdir / "observe-sessions")


def snapshot_restored(run, snapdir: Path, name: str) -> list[str]:
    """After a restore: captures that ran when the snapshot was saved run again in the guest
    (from the moment of the save; what they recorded after it is gone from their files), and
    captures started after it are gone with the guest's memory. What the host knows of them
    follows; nothing in the guest is touched here."""
    store = Store(run)
    saved: dict[str, Session] = {}
    d = snapdir / "observe-sessions"
    if not d.is_dir() and not (store.dir / "sessions").is_dir():
        return []
    if d.is_dir():
        for f in d.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                saved[data["id"]] = Session(**{k: v for k, v in data.items() if k in Session.__dataclass_fields__})
            except (OSError, ValueError, KeyError, TypeError):
                continue
    said = []
    with store.lock():
        current = {s.id: s for s in store.all()}
        for s in current.values():
            old = saved.get(s.id)
            if s.state in ("starting", "running", "ended", "stopping"):
                if old and old.state in ("running", "ended"):
                    s.state = old.state
                    s.stats.setdefault("restores", []).append({"t": time.time(), "snapshot": name})
                    s.event("snapshot restored", f"{name}: the capture is back at the moment of the save; what it "
                                                 "recorded since is gone from its files")
                    said.append(f"observation {s.name()} goes on from snapshot {name}'s moment")
                else:
                    s.state, s.error = "interrupted", f"snapshot {name} restored: the guest went back to before this " \
                                                      "capture"
                    s.event("interrupted", s.error)
                    said.append(f"observation {s.name()} is interrupted: {s.error}")
                store.save(s)
        for sid, old in saved.items():
            now = current.get(sid)
            if old.state == "running" and now is not None and now.state in ("stopped", "failed"):
                n = 1 + max((int(x[1:]) for x in current if x[1:].isdigit()), default=0)
                again = Session(**{**old.__dict__, "id": f"s{n}", "restored_from": sid, "label": "",
                                   "artifacts": [], "events": list(old.events)})
                again.event("restored", f"snapshot {name} brought session {sid}'s capture back in the guest")
                current[again.id] = again
                store.save(again)
                said.append(f"snapshot {name} brought the capture of {sid} back: it is session {again.id} now "
                            f"(desk observe stop {again.id})")
    if said:
        # "name", not "snapshot": that key is desk's own snapshot events', which the report reads apart.
        run.log_event(observe="restore", name=name, detail="; ".join(said))
    return said

