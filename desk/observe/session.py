"""Observation sessions of one run, kept in its directory (runs/<id>/observe/), so a session is
the run's alone and goes with it. A session is one capture by one provider, from its start to the
artifacts on the host:

    starting -> running -> stopping -> stopped
                   |  \\-> ended        (the capture ended in the guest: its limit, the tool exited)
                   \\----> interrupted  (guest rebooted, snapshot restored, VM gone, desk killed)
    starting -> failed                  (it never ran)

`ended` and `interrupted` sessions are still collected by `desk observe stop`: whatever the guest
kept is pulled and checked, and the session says it is not complete."""

import json
import os
import re
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from ..config import DeskError

SCHEMA = 1
STATES = ("starting", "running", "ended", "interrupted", "stopping", "stopped", "failed")
# Holding the guest's tool or its resources: a second session of the same kind would conflict.
ACTIVE = ("starting", "running", "stopping")
# What `desk observe stop` collects.
COLLECTABLE = ("running", "ended", "interrupted")
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}")
LOCK_WAIT = 60


@dataclass
class Session:
    id: str
    provider: str
    run: str
    created: float
    state: str = "starting"
    label: str = ""
    # Who started it: the run's holder, or the scenario.
    owner: str = ""
    # The desk process working on it while it starts or stops; a dead one leaves it interrupted.
    pid: int = 0
    options: dict = field(default_factory=dict)
    # duration (s) and max_bytes; the guest stops the capture at either.
    limits: dict = field(default_factory=dict)
    # {"image": "app.exe"} or {"pid": N}: processes followed with their descendants.
    target: dict | None = None
    guest_dir: str = ""
    # The guest's boot when the capture started: another one means it ended with a reboot.
    boot: str = ""
    # What the provider started in the guest and needs to stop it: processes (pid, created), names.
    tool: dict = field(default_factory=dict)
    watchdog: dict | None = None
    started: float | None = None
    stopped: float | None = None
    # Host and guest time side by side, at start, marks and stop.
    clock: list = field(default_factory=list)
    markers: list = field(default_factory=list)
    # "pid:created" -> process, first and last seen (host time).
    processes: dict = field(default_factory=dict)
    artifacts: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    # The provider's own words on what the capture can and cannot show.
    notes: list = field(default_factory=list)
    complete: bool | None = None
    error: str = ""
    restored_from: str | None = None
    schema: int = SCHEMA

    def event(self, what: str, detail: str = "") -> None:
        self.events.append({"t": time.time(), "event": what, **({"detail": detail} if detail else {})})

    def name(self) -> str:
        return f"{self.id} ({self.label})" if self.label and self.label != self.id else self.id


class Store:
    """The sessions of one run."""

    def __init__(self, run):
        self.run = run
        self.dir = Path(run.dir) / "observe"

    def _file(self, sid: str) -> Path:
        return self.dir / "sessions" / f"{sid}.json"

    def files(self, sid: str) -> Path:
        """Where the session's artifacts are kept on the host."""
        return self.dir / sid

    @contextmanager
    def _lock(self, name: str, waiting: str, timeout: float = LOCK_WAIT, wait: bool = True):
        import fcntl

        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / name, "a+") as f:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not wait or time.monotonic() > deadline:
                        raise DeskError(f"{waiting} (held by another desk command; try again when it is done)") \
                            from None
                    time.sleep(0.2)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def lock(self):
        """Held for reading and writing session files: a moment, never across a guest command."""
        return self._lock(".lock", f"the observation sessions of run {self.run.id} are being changed")

    def operation(self, sid: str):
        """Held while one command starts or stops a session; a second one is refused at once."""
        return self._lock(f".op-{sid}", f"session {sid} is being started or stopped by another desk command",
                          wait=False)

    def save(self, s: Session) -> None:
        f = self._file(s.id)
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f".{f.name}.{os.getpid()}")
        tmp.write_text(json.dumps(asdict(s), indent=1, ensure_ascii=False))
        os.replace(tmp, f)

    def load(self, sid: str) -> Session:
        f = self._file(sid)
        try:
            data = json.loads(f.read_text())
        except FileNotFoundError:
            raise DeskError(f"run {self.run.id} has no observation session {sid} (desk observe list)") from None
        except ValueError:
            raise DeskError(f"{f} is damaged") from None
        known = {x.name for x in fields(Session)}
        return Session(**{k: v for k, v in data.items() if k in known})

    def all(self) -> list[Session]:
        d = self.dir / "sessions"
        if not d.is_dir():
            return []
        out = []
        for f in d.glob("*.json"):
            try:
                out.append(self.load(f.stem))
            except DeskError:
                continue
        return sorted(out, key=lambda s: (s.created, s.id))

    def find(self, ref: str | None) -> Session:
        """By id or label; without one, the only session that is not finished."""
        sessions = self.all()
        if ref:
            hits = [s for s in sessions if s.id == ref] or [s for s in sessions if s.label == ref]
            if not hits:
                raise DeskError(f"run {self.run.id} has no observation session {ref!r} (desk observe list)")
            # A label used again: the newest session of it.
            return hits[-1]
        open_ = [s for s in sessions if s.state not in ("stopped", "failed")]
        if len(open_) == 1:
            return open_[0]
        if not open_:
            raise DeskError(f"run {self.run.id} has no observation session to collect (desk observe list)")
        raise DeskError("several observation sessions are open, name one: " + ", ".join(s.name() for s in open_))

    def new(self, provider: str, label: str | None, conflicts) -> Session:
        """A new session in state starting; `conflicts(session, others)` refuses it while the lock
        is held, so two commands cannot both start what only one may run."""
        if label is not None and not LABEL.fullmatch(label):
            raise DeskError(f"a session label is letters, digits, -, _ and ., at most 40 (got {label!r})")
        with self.lock():
            others = self.all()
            n = 1 + max((int(s.id[1:]) for s in others if s.id[1:].isdigit()), default=0)
            s = Session(id=f"s{n}", provider=provider, run=self.run.id, created=time.time(), label=label or "",
                        pid=os.getpid())
            if label and any(o.label == label and o.state not in ("stopped", "failed") for o in others):
                raise DeskError(f"session label {label!r} is in use by an open session (desk observe list)")
            conflicts(s, others)
            s.event("starting")
            self.save(s)
        return s

    @contextmanager
    def edit(self, sid: str):
        """The session as it is now, written back after the block."""
        with self.lock():
            s = self.load(sid)
            yield s
            self.save(s)

    def update(self, s: Session) -> None:
        with self.lock():
            self.save(s)


def process_key(p: dict) -> str:
    return f"{p['pid']}:{p['created']}"


def pid_alive(pid: int) -> bool:
    """Whether a desk process on this host still runs (it started or stops a session)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
