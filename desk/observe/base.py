"""What every observation provider is: the options it takes, how it tells whether the guest can
run it, and how it starts, watches and stops one capture and checks what it made. Providers
build command lines and read answers; the orchestration (desk/observe/__init__.py) keeps the
session, the watchdog, the copies and the report, so a provider never decides those.

Providers do not import libvirt or desk's run machinery: they also run on Windows CI."""

import hashlib
import json
import shutil
import subprocess
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import formats
from .guestops import ObserveError, win
from .session import Session


class Missing(ObserveError):
    """A tool or driver the provider needs is not in the guest or not set up on the host."""


class Unsupported(ObserveError):
    """The provider does not work with this guest (its family, its Windows)."""


class Conflict(ObserveError):
    """Something desk does not own holds what the capture needs (another ProcMon, a pktmon)."""


class Privileges(ObserveError):
    """The guest account lacks the rights the tool needs (no elevated administrator token)."""


@dataclass
class Option:
    kind: type
    default: object
    help: str
    choices: tuple | None = None
    # Text the value must match, said in `help` (a profile name, a provider).
    check: object = None


@dataclass
class Capability:
    provider: str
    available: bool
    problems: list = field(default_factory=list)
    details: dict = field(default_factory=dict)
    version: str = ""


@dataclass
class ArtifactSpec:
    """A file the capture leaves in the guest, to be copied, hashed and checked."""
    name: str
    guest_path: str
    kind: str
    required: bool = True
    description: str = ""


def parse_value(name: str, opt: Option, value):
    """A value from the command line (text) or a scenario (TOML) as the option takes it."""
    if isinstance(value, str) and opt.kind is not str:
        text = value.strip()
        try:
            if opt.kind is bool:
                if text.lower() not in ("true", "false", "yes", "no", "1", "0"):
                    raise ValueError
                value = text.lower() in ("true", "yes", "1")
            elif opt.kind is int:
                value = int(text, 0)
            elif opt.kind is float:
                value = float(text)
            elif opt.kind is list:
                value = [v.strip() for v in text.split(",") if v.strip()]
        except ValueError:
            raise ObserveError(f"option {name} takes {opt.kind.__name__} (got {value!r})") from None
    if opt.kind is float and isinstance(value, int) and not isinstance(value, bool):
        value = float(value)
    if opt.kind is list and isinstance(value, str):
        value = [value]
    if not isinstance(value, opt.kind) or (opt.kind is int and isinstance(value, bool)):
        raise ObserveError(f"option {name} takes {opt.kind.__name__} (got {value!r})")
    if opt.kind is list and not all(isinstance(v, str) and v for v in value):
        raise ObserveError(f"option {name} takes a list of texts (got {value!r})")
    if opt.choices and value not in opt.choices:
        raise ObserveError(f"option {name} is one of {', '.join(map(str, opt.choices))} (got {value!r})")
    if opt.check:
        for v in value if isinstance(value, list) else [value]:
            why = opt.check(v)
            if why:
                raise ObserveError(f"option {name}: {why}")
    return value


class Provider:
    name = ""
    title = ""
    # trace: a capture with files; snapshot: listings taken along the way; endpoint: a server.
    kind = "trace"
    families = ("windows",)
    # Runs of this provider that hold the same thing in the guest conflict (one at a time).
    resource: str | None = None
    needs_admin = True
    OPTIONS: dict[str, Option] = {}
    # What a capture of it shows and does not: in `desk observe providers` and the report.
    notes: tuple[str, ...] = ()
    # A tool desk does not ship: the host's copy is set up with `desk observe setup NAME FILE`.
    setup_files: tuple[str, ...] = ()
    setup_from: str = ""
    licence: str = ""

    # Options and identity.

    def options(self, given: dict) -> dict:
        unknown = set(given) - set(self.OPTIONS)
        if unknown:
            takes = ", ".join(sorted(self.OPTIONS)) or "none"
            raise ObserveError(f"{self.name} does not take {', '.join(sorted(unknown))} (it takes: {takes})")
        out = {k: o.default for k, o in self.OPTIONS.items()}
        for k, v in given.items():
            out[k] = parse_value(k, self.OPTIONS[k], v)
        self.check_options(out)
        return out

    def defaults(self) -> dict:
        return {k: o.default for k, o in self.OPTIONS.items()}

    def check_options(self, opts: dict) -> None:
        """Combinations of options that cannot work together."""

    def resource_of(self, session: Session) -> str | None:
        return self.resource

    # Capability.

    def probe_request(self, opts: dict, host) -> dict:
        return {}

    def assess(self, probe: dict, opts: dict, host) -> Capability:
        cap = Capability(self.name, True, details={"user": probe.get("user"), "os": probe.get("os")})
        if self.needs_admin and not probe.get("admin"):
            cap.available = False
            cap.problems.append(f"{probe.get('user') or 'the guest account'} has no elevated administrator token, "
                                f"which {self.title or self.name} needs")
        return cap

    def capability(self, guest, opts: dict, host) -> Capability:
        probe = guest.probe(**self.probe_request(opts, host))
        return self.assess(probe, opts, host)

    def preflight(self, guest, session: Session, host) -> dict:
        """The probe, or the reason the capture cannot start (Missing, Privileges, Conflict)."""
        probe = guest.probe(**self.probe_request(session.options, host))
        cap = self.assess(probe, session.options, host)
        if not cap.available:
            if self.needs_admin and not probe.get("admin"):
                raise Privileges(cap.problems[0])
            raise Missing(f"{self.name}: " + "; ".join(cap.problems))
        conflict = self.conflicts(probe, session)
        if conflict:
            raise Conflict(conflict)
        session.stats["tool_version"] = cap.version
        return probe

    def conflicts(self, probe: dict, session: Session) -> str | None:
        return None

    # The tool desk does not ship.

    def tool_dir(self, session: Session) -> str:
        return win(r"C:\desk\observe\tools", self.name)

    def install_setup(self, source: Path, dest: Path) -> list[str]:
        """Copy what `desk observe setup` was given into the host's cache: a file of the names this
        provider takes, a zip with them, or a directory with them."""
        wanted = {n.lower(): n for n in self.setup_files}
        got = []
        if source.is_dir():
            for f in source.iterdir():
                if f.is_file() and f.name.lower() in wanted:
                    shutil.copy(f, dest / wanted[f.name.lower()])
                    got.append(wanted[f.name.lower()])
        elif zipfile.is_zipfile(source):
            with zipfile.ZipFile(source) as z:
                for info in z.infolist():
                    base = Path(info.filename).name
                    # Only the files it names, by their base name: nothing of the archive's paths.
                    if base.lower() in wanted and not info.is_dir():
                        (dest / wanted[base.lower()]).write_bytes(z.read(info))
                        got.append(wanted[base.lower()])
        elif source.name.lower() in wanted:
            shutil.copy(source, dest / wanted[source.name.lower()])
            got.append(wanted[source.name.lower()])
        if not got:
            raise Missing(f"{source} holds none of {', '.join(self.setup_files)}" +
                          (f" ({self.setup_from})" if self.setup_from else ""))
        return sorted(set(got))

    def deploy(self, guest, session: Session, host, names: list[str] | None = None) -> None:
        """The host's copy of the tool into the guest, where its SHA-256 differs."""
        setup = host.setup(self.name)
        if not setup:
            raise Missing(f"{self.name}: no copy set up on this host (desk observe setup {self.name} FILE"
                          + (f"; {self.setup_from}" if self.setup_from else "") + ")")
        files = [f for f in setup["files"] if names is None or f["name"] in names]
        paths = [win(self.tool_dir(session), f["name"]) for f in files]
        there = guest.files(paths, hash=True, limit=120)
        guest.mkdir(self.tool_dir(session))
        for f, path in zip(files, paths):
            if (there.get(path) or {}).get("sha256") != f["sha256"]:
                guest.push(host.setup_dir(self.name) / f["name"], path)
        session.event("tool deployed", ", ".join(f["name"] for f in files))

    # The capture.

    def start(self, guest, session: Session, host) -> None:
        raise NotImplementedError

    def stop_commands(self, session: Session) -> list[dict]:
        """What ends the capture in the guest: the watchdog runs these at its limit."""
        return []

    def watch_files(self, session: Session) -> list[str]:
        """The files whose size the watchdog holds to max_bytes."""
        return []

    def status(self, guest, session: Session) -> dict:
        procs = session.tool.get("procs") or []
        st = {}
        if procs:
            alive = guest.alive(procs)
            st["running"] = bool(alive) and all(p.get("alive") for p in alive)
        files = self.watch_files(session)
        if files:
            info = guest.files(files)
            st["bytes"] = sum((v or {}).get("size", 0) or sum(x.get("size", 0) for x in (v or {}).get("files") or [])
                              for v in info.values())
        return st

    def capturing(self, state: dict, session: Session) -> bool | None:
        """From the guest's state (helper op `state`): whether the capture still runs; None when
        that cannot be told from it."""
        procs = session.tool.get("procs") or []
        if not procs:
            return None
        keys = {(p["pid"], p["created"]) for p in procs}
        alive = [a for a in state.get("alive") or [] if (a["pid"], a["created"]) in keys]
        return bool(alive) and all(a.get("alive") for a in alive)

    def sample(self, guest, session: Session) -> dict:
        """Figures to keep that are only there while the capture runs (ETW's lost events)."""
        return {}

    def stop(self, guest, session: Session, host, force: bool = False) -> None:
        """End the capture in the guest; nothing to do when it ended already."""
        raise NotImplementedError

    def artifacts(self, session: Session) -> list[ArtifactSpec]:
        """The files the capture leaves in the guest, also when it was cut off."""
        return []

    def convert(self, guest, session: Session, host) -> list[ArtifactSpec]:
        """Files made from the capture in the guest after it stopped (a CSV, a pcapng)."""
        return []

    def verify(self, path: Path, art: ArtifactSpec) -> dict:
        return formats.check(path, art.kind)

    def cleanup(self, guest, session: Session, host=None) -> None:
        """Take back what the provider set up in the guest and still holds, its own only."""
        for p in session.tool.get("procs") or []:
            guest.stop_process(p, tree=True)

    def on_mark(self, guest, session: Session, marker: dict) -> None:
        """What a mark does for this capture besides the host's timeline and the guest's marker."""

    def summary(self, session: Session) -> list[str]:
        """Lines for the report beyond the artifacts."""
        return []


def tool_run(guest, exe: str, args: list[str], timeout: float, what: str, ok=(0,), encoding: str | None = None) -> dict:
    """A tool run to its end in the guest; ObserveError with its own words when it fails."""
    r = guest.exec(exe, args, timeout=timeout, encoding=encoding)
    if not r.get("started", True):
        raise Missing(f"{what}: {exe} could not be started ({r.get('error')})")
    if r.get("timed_out"):
        raise ObserveError(f"{what}: {Path(exe).name} still running after {timeout:.0f}s, stopped")
    if r.get("rc") not in ok:
        said = " ".join((r.get("stderr") or r.get("stdout") or "").split())[-300:]
        raise ObserveError(f"{what}: {Path(exe).name} exit {hexcode(r.get('rc'))}" + (f": {said}" if said else ""))
    return r


def command(exe: str, args: list[str], timeout: float, **extra) -> dict:
    """A command for the guest's watchdog, quoted as Windows programs split their command line."""
    return {"exe": exe, "args": subprocess.list2cmdline(args), "timeout": timeout, **extra}


def hexcode(rc) -> str:
    """A Windows exit code as it is looked up: small ones as they are, HRESULTs in hex."""
    if rc is None:
        return "none"
    return str(rc) if 0 <= rc < 256 else f"{rc} (0x{rc & 0xFFFFFFFF:08X})"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_setup(dest: Path, provider: str, files: list[str], source: Path, accepted: dict | None) -> dict:
    info = {"provider": provider, "source": str(source), "at": time.time(),
            "files": [{"name": n, "sha256": sha256(dest / n), "bytes": (dest / n).stat().st_size} for n in files],
            "licence_accepted": accepted}
    (dest / "setup.json").write_text(json.dumps(info, indent=1))
    return info
