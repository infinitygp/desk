"""Disposable runs: a copy-on-write overlay on a golden image, restored from the warm state."""

import fcntl
import json
import os
import re
import secrets
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import libvirt

from . import guest, images
from .config import HOME, RUNS, Busy, DeskError, ImageSpec, load_image
from .virt import (conn, domain_uuid, free_port, lookup, prepare_domain_xml, qemu_img_overlay,
                   reflink_copy, uuid_active)

# Head room left for the host when deciding whether another VM fits.
HOST_RESERVE_MIB = 1024
# Share of the last minute in which every task waited for memory (PSI "full avg60") from which
# the host counts as short of memory whatever MemAvailable says: it is swapping, and another VM
# would stall the guests' timing (seen at 5 % with swap full and four VMs).
THRASH_PERCENT = 5.0
# How long `desk up` waits for other runs to give memory back before it gives up.
MEMORY_WAIT = 600


# How long a command waits for another one's lock before it gives up and names the holder. The
# state lock is held for seconds (a domain started, a run destroyed), the lock of an image for a
# whole build or `desk maintain` of its family.
STATE_LOCK_WAIT = 15 * 60
IMAGE_LOCK_WAIT = 6 * 3600
# Locks this process holds, so that what it calls while holding one can ask for it again
# (flock on a second descriptor of the file would wait for the first for ever).
_locked: dict[Path, int] = {}


@contextmanager
def _flock(path: Path, waiting: str, timeout: float):
    if _locked.get(path):
        _locked[path] += 1
        try:
            yield
        finally:
            _locked[path] -= 1
        return
    HOME.mkdir(parents=True, exist_ok=True)
    # Not "w": that would wipe the holder's note for everybody waiting.
    with open(path, "a+") as f:
        deadline = time.monotonic() + timeout
        told = False
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                f.seek(0)
                holder = f.read().strip() or "another desk command"
                if not told:
                    print(f"[desk] waiting for {waiting}: {holder}", file=sys.stderr)
                    told = True
                if time.monotonic() > deadline:
                    raise Busy(f"gave up after {timeout / 60:.0f} min waiting for {waiting}, still held by "
                                    f"{holder}; if that command hangs, stop it and try again") from None
                time.sleep(0.5)
        _note(f, f"desk {' '.join(sys.argv[1:4])} (pid {os.getpid()}, since {time.strftime('%H:%M:%S')})")
        _locked[path] = 1
        try:
            yield
        finally:
            del _locked[path]
            _note(f, "")


def _note(f, text: str) -> None:
    """Who holds the lock, for those who wait. Never the holder's problem: `desk gc` on a full
    disk still has to get its lock."""
    try:
        f.seek(0)
        f.truncate()
        f.write(text)
        f.flush()
    except OSError:
        pass


def state_lock():
    """Serialises what creates or destroys VMs (port picking, UUID checks, gc). Held while a
    domain is started or destroyed, not while a guest boots or a report is written."""
    return _flock(HOME / ".lock", "the commands that start and stop VMs", STATE_LOCK_WAIT)


def image_lock(image: str):
    """Held for a whole image build or warm, which can take many minutes."""
    return _flock(HOME / f".lock-{image}", f"the build or warm-up of {image}", IMAGE_LOCK_WAIT)


@contextmanager
def image_lock_if_free(image: str):
    """Yields whether the image lock was free; it stays held for the block if so."""
    HOME.mkdir(parents=True, exist_ok=True)
    with open(HOME / f".lock-{image}", "a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


_held: list[int] = []


@dataclass
class Run:
    id: str
    image: str
    domain: str
    ssh_port: int
    user: str
    created: float
    shots: int = 0
    last_used: float = 0.0
    vnc_password: str = ""
    agent_port: int = 0
    # Monitors (heads of the virtio GPU) and the mode they start in; 1 and None: the image's.
    displays: int = 1
    resolution: list[int] | None = None
    # Scaling in percent set by desk (None: the image's 100 %).
    scale: int | None = None
    # When the run was handed over: a cold boot provisions the image in the run first, and what
    # that changes belongs to the image, not to the test. 0: at creation (warm restore).
    ready: float = 0.0
    # Seconds the guest clock is ahead of the host's (desk clock); 0: in sync.
    clock_offset: float = 0.0
    # Who holds the run (desk ls): a scenario, --label / $DESK_LABEL, or the coding agent and the
    # directory it works in; holder is the agent's PID, to tell when it is gone.
    label: str = ""
    holder: int = 0

    @property
    def since(self) -> float:
        return self.ready or self.created

    @property
    def dir(self) -> Path:
        return RUNS / self.id

    def save(self) -> None:
        """Write the whole run: only for a new one; later changes go through `edit`."""
        # Atomic: other desk processes read run.json at any time.
        tmp = self.dir / f".run.json.{os.getpid()}"
        tmp.write_text(json.dumps(asdict(self), indent=2))
        os.replace(tmp, self.dir / "run.json")

    @contextmanager
    def edit(self):
        """run.json as it is now, written back after the block and taken over by this object.
        Other commands change it meanwhile (desk shot its count, desk clock the offset), and a
        copy loaded earlier saved whole would undo what they wrote."""
        with _flock(self.dir / ".run.lock", f"another command changing run {self.id}", 60):
            current = load(self.id)
            yield current
            current.save()
        for f in fields(self):
            setattr(self, f.name, getattr(current, f.name))

    def hold(self) -> None:
        """Mark the run as in use for the lifetime of this process (and of an exec'd ssh)."""
        fd = os.open(self.dir / ".inuse", os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_SH)
        os.set_inheritable(fd, True)
        _held.append(fd)

    def in_use(self) -> bool:
        try:
            fd = os.open(self.dir / ".inuse", os.O_RDWR | os.O_CREAT, 0o644)
        except FileNotFoundError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True
        finally:
            os.close(fd)

    def log_event(self, **event) -> None:
        """Timeline for the report: commands, their exit codes and screenshots."""
        with open(self.dir / "log.jsonl", "a") as f:
            f.write(json.dumps({"t": time.time(), **event}, ensure_ascii=False) + "\n")

    def touch(self) -> None:
        with self.edit() as run:
            run.last_used = time.time()

    def idle(self) -> float:
        return time.time() - (self.last_used or self.created)

    def live_domain(self) -> libvirt.virDomain | None:
        # Restored runs of one image share a domain name, so match on this run's disk too.
        d = lookup(self.domain)
        try:
            if d is None or not d.isActive() or str(self.dir / "disk.qcow2") not in d.XMLDesc(0):
                return None
        except libvirt.libvirtError:
            return None  # destroyed meanwhile by another command (desk down, gc)
        return d

    def dom(self) -> libvirt.virDomain:
        d = self.live_domain()
        if d is None:
            raise DeskError(f"run {self.id} is not running (desk down {self.id} to clean up)")
        return d

    def next_shot_path(self, label: str = "") -> Path:
        with self.edit() as run:
            run.shots += 1
        shots = self.dir / "shots"
        shots.mkdir(exist_ok=True)
        suffix = f"-{label}" if label else ""
        return shots / f"{self.shots:04d}{suffix}.png"


# A directory under RUNS and part of the domain name: nothing that leads elsewhere.
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}")


def _checked_id(run_id: str) -> str:
    if not RUN_ID.fullmatch(run_id):
        raise DeskError(f"run names are letters, digits, -, _ and ., at most 40, starting with a letter or digit "
                        f"(got {run_id!r})")
    return run_id


def load(run_id: str) -> Run:
    f = RUNS / run_id / "run.json"
    if not f.is_file():
        raise DeskError(f"no run {run_id!r}")
    data = json.loads(f.read_text())
    # Another desk version may have written fields this one does not know (or lack new ones).
    known = {f.name for f in fields(Run)}
    return Run(**{k: v for k, v in data.items() if k in known})


def all_runs() -> list[Run]:
    if not RUNS.exists():
        return []
    found = []
    for p in RUNS.iterdir():
        try:
            found.append(load(p.name))
        except (DeskError, ValueError, TypeError):
            continue  # half-created or foreign directory
    return sorted(found, key=lambda r: r.created)


def resolve(run_id: str | None) -> Run:
    """Explicit id, then $DESK_RUN, then the only active run. Counts as activity for gc."""
    run_id = run_id or os.environ.get("DESK_RUN")
    if run_id:
        # A directory in RUNS, not further: runs named before names were checked still resolve.
        if "/" in run_id or run_id in (".", ".."):
            raise DeskError(f"no run {run_id!r}")
        run = load(run_id)
    else:
        active = [r for r in all_runs() if r.live_domain() is not None]
        if not active:
            raise DeskError("no active run (desk up <image>)")
        if len(active) > 1:
            raise DeskError("several active runs, pick one with -r/--run or DESK_RUN: "
                            + ", ".join(r.id for r in active))
        run = active[0]
    run.touch()
    run.hold()
    run.log_event(argv=sys.argv[1:])
    return run


def idle_ttl() -> int:
    """Runs nobody touched for this long are destroyed by `desk gc` (and by every `desk up`)."""
    value = os.environ.get("DESK_TTL", "")
    try:
        return int(value) if value else 2 * 3600
    except ValueError:
        # Read when needed, not at import: there a bad value would end every command as exit 1.
        raise DeskError(f"DESK_TTL is a number of seconds (got {value!r})") from None


def _mem_available_mib() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    return 0


def _promised_mib() -> int:
    """Memory that guests still booting cold will take but have not touched yet: MemAvailable
    does not show it, and two cold boots started together would each count it as free."""
    promised = 0
    for r in all_runs():
        if r.domain != f"desk-{r.id}" or r.ready:
            continue
        dom = r.live_domain()
        try:
            if dom is not None:
                promised += max(0, dom.maxMemory() - dom.memoryStats().get("rss", dom.maxMemory())) // 1024
        except libvirt.libvirtError:
            continue  # gone meanwhile
    return promised


def _memory_stall_percent() -> float:
    """PSI "full avg60" of memory, 0 where the kernel has no PSI."""
    try:
        for line in Path("/proc/pressure/memory").read_text().splitlines():
            if line.startswith("full "):
                return float(dict(kv.split("=") for kv in line.split()[1:])["avg60"])
    except (OSError, KeyError, ValueError):
        pass
    return 0.0


def host_state(run: Run) -> str:
    """Why a run may have stopped answering, for its failure message and report: whether its VM
    still runs, and what the host is short of. A disk that fills up under a run ends its QEMU
    (seen as SIGABRT in a screendump, then "Connection refused" for SSH)."""
    from .config import URI

    where = [run.dir, (Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") if "session" in URI
                       else Path("/var/cache")) / "libvirt"]
    parts, devices = [], set()
    for path in where:
        try:
            device, free = path.stat().st_dev, shutil.disk_usage(path).free
        except OSError:
            continue
        if device not in devices:
            devices.add(device)
            parts.append(f"{free / 2**30:.1f} GiB free in {path}")
    meminfo = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines() if ":" in line)
    swap = (int(meminfo.get("SwapTotal", "0 kB").split()[0]) - int(meminfo.get("SwapFree", "0 kB").split()[0])) // 1024
    parts.append(f"{_mem_available_mib()} MiB memory available, {swap} MiB swap used")
    others = [r.id for r in all_runs() if r.id != run.id and r.live_domain() is not None]
    parts.append(f"other runs: {', '.join(others) or 'none'}")
    vm = ("the VM is running but does not answer" if run.live_domain() is not None else
          f"the VM is no longer running (QEMU ended; coredumpctl and libvirt's log of {run.domain} may say why)")
    return f"{vm}; host: {'; '.join(parts)}"


def check_memory(spec: ImageSpec) -> None:
    need = spec.memory_mib + HOST_RESERVE_MIB
    avail = _mem_available_mib() - _promised_mib()
    stall = _memory_stall_percent()
    if avail < need or stall >= THRASH_PERCENT:
        running = [r.id for r in all_runs() if r.live_domain() is not None]
        hint = f"; running desk VMs: {', '.join(running)}" if running else ""
        why = (f"{avail} MiB available, {need} MiB needed" if avail < need else
               f"the host is swapping: all tasks waited for memory {stall:.0f} % of the last minute")
        raise Busy(f"not enough free memory for {spec.name}: {why}{hint} (desk down <id>, or --force)")


def warm_meta(spec: ImageSpec) -> dict:
    meta = spec.warm_dir / "meta.json"
    return json.loads(meta.read_text()) if meta.exists() else {}


def warm_is_current(spec: ImageSpec) -> bool:
    """The warm state was made from the base build that is current now."""
    return spec.base_dir.exists() and warm_meta(spec).get("base") == str(spec.base_dir.resolve())


def cold_reason(spec: ImageSpec, warm_dir: Path | None = None) -> str | None:
    """Why a run of this image would boot instead of restoring the warm state; None: it restores."""
    warm = (warm_dir or spec.warm_dir) / "memory.save"
    if not warm.exists():
        return f"no warm state for {spec.name} (desk image warm {spec.name})"
    if not warm_is_current(spec):
        return f"warm state of {spec.name} predates its base image (desk image warm {spec.name} --force)"
    saved_xml = conn().saveImageGetXMLDesc(str(warm), libvirt.VIR_DOMAIN_SAVE_IMAGE_XML_SECURE)
    if uuid_active(domain_uuid(saved_xml)):
        # Restores share the save image's UUID, so only one can run at a time.
        return f"warm state of {spec.name} is in use by another run"
    return None


def abandoned_warmup(name: str, xml: str) -> str | None:
    """The image whose warm-up this domain is, when it is one: a `desk image warm` that was killed
    leaves its guest running. Runs restored from a warm state carry the same domain name, but
    their disk is a run's."""
    from .config import WARM

    if not name.startswith("desk-warm-") or str(RUNS) in xml:
        return None
    for src in ET.fromstring(xml).iter("source"):
        disk = Path(src.get("file") or "")
        if disk.parent.parent == WARM:
            return disk.parent.name.rsplit(".", 1)[0]
    return None


def gc_idle(verbose: bool = True) -> list[str]:
    """Take down runs nobody touched for idle_ttl(), their report written first, and remove old
    reports. Without the state
    lock: reading a guest's logs and encoding a video take their time, and nobody should wait for
    that (evidence.finish takes the lock for the moment the domain is destroyed)."""
    from . import evidence

    evidence.prune_reports(verbose)
    ttl, removed = idle_ttl(), []
    for run in all_runs():
        live = run.live_domain() is not None
        if run.in_use() or not run.last_used:
            # A command is still working with it (long exec, interactive ssh), or it predates
            # activity tracking; start its idle clock now.
            if not run.last_used:
                run.touch()
            continue
        if run.idle() <= ttl:
            continue
        what = f"{'running' if live else 'stopped'}, idle {run.idle() / 3600:.1f} h"
        seen = run.last_used

        def still_idle(run=run, seen=seen) -> bool:
            # Its owner may have come back while the report was written.
            try:
                return not run.in_use() and load(run.id).last_used == seen
            except DeskError:
                return False  # gone: somebody else took it down

        try:
            # Whoever left it may still want what it showed: the report keeps the screenshots,
            # the timeline and, while the guest runs, its logs.
            run.log_event(gc=f"desk gc takes the run down ({what})")
        except OSError:
            pass  # a full disk is when this must go on
        try:
            report, _ = evidence.finish(run, stopped=not evidence.reported(run), unless=lambda: not still_idle())
        except DeskError:
            continue  # another command is taking it down already
        if run.dir.exists():
            continue  # in use again
        removed.append(f"{run.id} ({what})" + (f"; report: {report / 'report.md'}" if report else ""))
    if verbose:
        for r in removed:
            print(f"[desk] gc: removed {r}")
    return removed


def gc(verbose: bool = True) -> list[str]:
    """Kill desk domains nobody owns: those whose run directory is gone, and the guests of
    warm-ups that were killed. With the state lock held."""
    removed = []
    # Only a domain whose run directory is gone is orphaned; one whose run.json this desk cannot
    # read (another version wrote it) still belongs to someone.
    known_disks = {str(d) for d in RUNS.glob("*/disk.qcow2")} if RUNS.exists() else set()
    for dom in conn().listAllDomains(libvirt.VIR_CONNECT_LIST_DOMAINS_ACTIVE):
        try:
            name = dom.name()
            if not name.startswith("desk-"):
                continue
            xml = dom.XMLDesc(0)
            if str(RUNS) in xml and not any(d in xml for d in known_disks):
                dom.destroy()
                removed.append(f"orphaned domain {name}")
                continue
            image = abandoned_warmup(name, xml)
            if image:
                # Whoever warms an image holds its lock for as long as the guest is theirs.
                with image_lock_if_free(image) as free:
                    if free:
                        dom.destroy()
                        removed.append(f"abandoned warm-up domain {name}")
        except libvirt.libvirtError:
            continue  # gone between listing and looking at it
    if verbose:
        for r in removed:
            print(f"[desk] gc: removed {r}")
    return removed


def prune_versions(verbose: bool = True) -> list[str]:
    """Delete base and warm builds that are neither current nor used by a run (running or kept),
    by a warm state that stays or by a libvirt domain (including an unfinished build). Images
    being built or warmed right now are left alone. Call with the state lock held, so that a
    `desk up` cannot pin a build between the checks and the deletion."""
    from .config import BASE, WARM, list_images
    from .virt import backing_files

    used: set[Path] = set()
    for disk in RUNS.glob("*/disk.qcow2"):
        chain = backing_files(disk)
        if not chain:
            # Which builds this run stands on cannot be told, so any of them may be its.
            print(f"[desk] gc: {disk} cannot be read; no old builds deleted this time", file=sys.stderr)
            return []
        used.update(f.parent for f in chain)
    names = list_images()
    keep_warm = {p for n in names for p in images.versions(WARM, n) if p in used or p == images.current(WARM / n)}
    for w in keep_warm:
        used.update(f.parent for f in backing_files(w / "disk.qcow2"))
    for dom in conn().listAllDomains(0):
        try:
            sources = [Path(s.get("file")).resolve() for s in ET.fromstring(dom.XMLDesc(0)).iter("source")
                       if s.get("file")]
        except libvirt.libvirtError:
            continue
        # The whole chain: a variant's warm-up boots a disk of its own on the base build it was made from.
        for f in sources:
            used.update(p.parent for p in backing_files(f) or [f])
    removed = []
    for n in names:
        with image_lock_if_free(n) as free:
            if not free:
                continue
            for root in (WARM, BASE):
                for p in images.versions(root, n):
                    if p in used or p == images.current(root / n) or p in keep_warm:
                        continue
                    for f in p.iterdir():
                        f.chmod(0o644)
                    shutil.rmtree(p)
                    removed.append(str(p))
    if verbose:
        for r in removed:
            print(f"[desk] gc: removed old build {r}")
    return removed


AGENTS = ("claude", "codex", "opencode", "gemini", "aider", "goose")


def agent_process() -> tuple[str, int]:
    """The coding agent this desk command runs under, by walking up the process tree."""
    pid = os.getppid()
    for _ in range(20):
        if pid <= 1:
            break
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            break
        comm = stat[stat.index("(") + 1:stat.rindex(")")].lstrip(".")
        if comm in AGENTS:
            return comm, pid
        pid = int(stat[stat.rindex(")") + 2:].split()[1])
    return "", 0


def default_label() -> tuple[str, int]:
    agent, pid = agent_process()
    label = os.environ.get("DESK_LABEL") or (f"{agent} @ {Path.cwd().name}" if agent else Path.cwd().name)
    return label, pid


def holder_alive(run: Run) -> bool:
    try:
        stat = Path(f"/proc/{run.holder}/stat").read_text()
    except OSError:
        return False
    return stat[stat.index("(") + 1:stat.rindex(")")].lstrip(".") in AGENTS


def up(spec: ImageSpec, run_id: str | None = None, cold: bool = False, force: bool = False,
       displays: int = 1, resolution: tuple[int, int] | None = None, label: str | None = None) -> Run:
    from .maintain import check_usable

    if run_id is not None:
        _checked_id(run_id)
    for warning in check_usable(spec):
        print(f"[desk] warning: {warning}", file=sys.stderr)
    gc_idle()
    # Short of memory, wait in line for other runs to end (parallel scenarios) rather than fail;
    # the lock is let go meanwhile, as ending a run needs it too.
    deadline = time.monotonic() + MEMORY_WAIT
    waiting = False
    while True:
        with ExitStack() as lock:
            lock.enter_context(state_lock())
            gc()
            try:
                if not force:
                    check_memory(spec)
            except DeskError as e:
                if time.monotonic() > deadline:
                    raise
                if not waiting:
                    print(f"[desk] {e}; waiting up to {MEMORY_WAIT // 60} min for other runs to end",
                          file=sys.stderr, flush=True)
                    waiting = True
            else:
                return _up(spec, run_id, cold, displays, resolution, label, started=lock.close)
        time.sleep(15)


def _up(spec: ImageSpec, run_id: str | None, cold: bool, displays: int = 1,
        resolution: tuple[int, int] | None = None, label: str | None = None, started=lambda: None) -> Run:
    """`started` is called once the domain runs: from there on nothing is picked or checked that
    another command could take too, so the state lock is given back while the guest boots."""
    run_id = run_id or secrets.token_hex(3)
    rd = RUNS / run_id
    if rd.exists():
        raise DeskError(f"run {run_id} already exists")
    if not spec.base_xml.exists():
        raise DeskError(f"base image {spec.name} not built yet (desk image build {spec.name})")
    # Pin the builds now: `desk maintain` may switch the current ones while this run starts.
    base_dir, warm_dir = spec.base_dir.resolve(), spec.warm_dir.resolve()
    warm = warm_dir / "memory.save"
    saved_xml = None
    if displays > 1 and spec.family != "windows":
        # Measured on GNOME 50: the second GPU's monitor shows its first frame and is never
        # updated again (mutter's copy to a GPU without 3D), so screenshots would lie.
        raise DeskError("several monitors are supported on Windows guests only; GNOME does not update a second "
                        "virtio GPU's monitor here (resolution and --scale work)")
    if not cold and (displays != 1 or resolution):
        # The warm state was saved with the image's single monitor; other hardware boots fresh.
        print(f"[desk] {displays} display(s){f' at {resolution[0]}x{resolution[1]}' if resolution else ''}: "
              "booting cold (the warm state has one monitor)")
        cold = True
    if not cold:
        reason = cold_reason(spec, warm_dir)
        if reason:
            print(f"[desk] booting cold: {reason}")
            cold = True
        else:
            saved_xml = conn().saveImageGetXMLDesc(str(warm), libvirt.VIR_DOMAIN_SAVE_IMAGE_XML_SECURE)
    rd.mkdir(parents=True)
    # A restore must also keep the saved domain name; run ids stay independent of it.
    domain = f"desk-{run_id}" if cold else ET.fromstring(saved_xml).findtext("name")
    now = time.time()
    auto_label, holder = default_label()
    run = Run(id=run_id, image=spec.name, domain=domain, ssh_port=free_port(),
              user=spec.user, created=now, last_used=now, vnc_password=secrets.token_urlsafe(6)[:8],
              agent_port=free_port() if spec.agent_port else 0, displays=displays,
              resolution=list(resolution) if resolution else None,
              label=label or auto_label, holder=holder)
    run.save()
    # In use by this process until it ends: a scenario or selftest keeps its run through steps
    # longer than the idle limit.
    run.hold()

    disk, nvram = rd / "disk.qcow2", rd / "VARS.qcow2"
    try:
        if cold:
            # The provisioned disk of a current warm build spares provisioning and a relogin, and
            # gives cold runs the files warm ones start from.
            fresh = not ((warm_dir / images.PROVISIONED).exists() and warm_is_current(spec))
            qemu_img_overlay(base_dir / "disk.qcow2" if fresh else warm_dir / images.PROVISIONED, disk)
            reflink_copy((base_dir if fresh else warm_dir) / "VARS.qcow2", nvram)
            template = (base_dir / "domain.xml").read_text()
        else:
            qemu_img_overlay(warm_dir / "disk.qcow2", disk)
            reflink_copy(warm_dir / "VARS.qcow2", nvram)
            template = saved_xml
        nvram.chmod(0o644)
        xml = prepare_domain_xml(template, name=run.domain, disk=disk, nvram=nvram,
                                 ssh_port=run.ssh_port, console_log=rd / "console.log",
                                 keep_uuid=not cold, vnc_password=run.vnc_password,
                                 agent_ports=(run.agent_port, spec.agent_port) if spec.agent_port else None,
                                 sound=spec.sound, displays=displays, resolution=resolution)
        if cold:
            dom = conn().createXML(xml, 0)
        else:
            conn().restoreFlags(str(warm), xml, libvirt.VIR_DOMAIN_SAVE_RUNNING)
            dom = run.dom()
            try:
                guest.agent_set_time(dom)
            except DeskError as e:
                print(f"[desk] warning: clock not synced: {e}")
        started()
        guest.wait_ssh(run.ssh_port, run.user, timeout=300 if cold else 60)
        if cold:
            guest.wait_ready(run.ssh_port, run.user, spec.cmd(spec.ready))
            if fresh:
                images.provision(spec, run.ssh_port)
            from . import steps

            steps.run_prepare(spec, run)
            with run.edit() as r:
                r.ready = time.time()
    except BaseException:
        # What the screen showed is the best clue to a start that failed (prepare not finding
        # what it waits for, a session that did not come up).
        dom = run.live_domain()
        if dom is not None:
            from . import display

            try:
                (rd / "failed-start.png").write_bytes(display.screenshot(dom))
                print(f"[desk] run {run_id} did not start; its screen then: {rd / 'failed-start.png'}", file=sys.stderr)
            except (DeskError, libvirt.libvirtError, OSError) as e:
                print(f"[desk] run {run_id} did not start, and its screen could not be saved: {e}", file=sys.stderr)
        down(run, keep=True)
        raise
    return run


def down(run: Run, keep: bool = False) -> None:
    from . import forward, record

    if not keep:
        record.kill(run)
    try:
        forward.kill(run)
    except OSError as e:
        print(f"[desk] warning: stopping port forwards of {run.id}: {e}", file=sys.stderr)
    dom = run.live_domain()
    if dom is not None:
        dom.destroy()
    if not keep:
        shutil.rmtree(run.dir, ignore_errors=True)


def spec_for(run: Run) -> ImageSpec:
    return load_image(run.image)
