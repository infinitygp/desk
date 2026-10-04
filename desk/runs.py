"""Disposable runs: a copy-on-write overlay on a golden image, restored from the warm state."""

import fcntl
import json
import os
import secrets
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

import libvirt

from . import guest, images
from .config import HOME, RUNS, DeskError, ImageSpec, load_image
from .virt import (conn, domain_uuid, free_port, lookup, prepare_domain_xml, qemu_img_overlay,
                   reflink_copy, uuid_active)

# Runs nobody touched for this long are destroyed by `desk gc` (and by every `desk up`).
IDLE_TTL = int(os.environ.get("DESK_TTL", 2 * 3600))
# Head room left for the host when deciding whether another VM fits.
HOST_RESERVE_MIB = 1024


@contextmanager
def _flock(path: Path, waiting: str):
    HOME.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"[desk] {waiting}", file=sys.stderr)
            fcntl.flock(f, fcntl.LOCK_EX)
        yield


def state_lock():
    """Serialises commands that create or destroy VMs (port picking, UUID checks, gc)."""
    return _flock(HOME / ".lock", "waiting for another desk command (up, down, gc)...")


def image_lock(image: str):
    """Held for a whole image build or warm, which can take many minutes."""
    return _flock(HOME / f".lock-{image}", f"waiting for another build/warm of {image}...")


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

    @property
    def dir(self) -> Path:
        return RUNS / self.id

    def save(self) -> None:
        # Atomic: other desk processes read run.json at any time.
        tmp = self.dir / f".run.json.{os.getpid()}"
        tmp.write_text(json.dumps(asdict(self), indent=2))
        os.replace(tmp, self.dir / "run.json")

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
        self.last_used = time.time()
        self.save()

    def idle(self) -> float:
        return time.time() - (self.last_used or self.created)

    def live_domain(self) -> libvirt.virDomain | None:
        # Restored runs of one image share a domain name, so match on this run's disk too.
        d = lookup(self.domain)
        if d is None or not d.isActive() or str(self.dir / "disk.qcow2") not in d.XMLDesc(0):
            return None
        return d

    def dom(self) -> libvirt.virDomain:
        d = self.live_domain()
        if d is None:
            raise DeskError(f"run {self.id} is not running (desk down {self.id} to clean up)")
        return d

    def next_shot_path(self, label: str = "") -> Path:
        self.shots += 1
        self.save()
        shots = self.dir / "shots"
        shots.mkdir(exist_ok=True)
        suffix = f"-{label}" if label else ""
        return shots / f"{self.shots:04d}{suffix}.png"


def load(run_id: str) -> Run:
    f = RUNS / run_id / "run.json"
    if not f.is_file():
        raise DeskError(f"no run {run_id!r}")
    return Run(**json.loads(f.read_text()))


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


def _mem_available_mib() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    return 0


def check_memory(spec: ImageSpec) -> None:
    need = spec.memory_mib + HOST_RESERVE_MIB
    avail = _mem_available_mib()
    if avail < need:
        running = [r.id for r in all_runs() if r.live_domain() is not None]
        hint = f"; running desk VMs: {', '.join(running)}" if running else ""
        raise DeskError(f"not enough free memory for {spec.name}: {avail} MiB available, "
                        f"{need} MiB needed{hint} (desk down <id>, or --force)")


def warm_is_current(spec: ImageSpec) -> bool:
    meta = spec.warm_dir / "meta.json"
    if not meta.exists() or not spec.base_disk.exists():
        return False
    return json.loads(meta.read_text()).get("base_mtime_ns") == spec.base_disk.stat().st_mtime_ns


def active_runs_of(image: str) -> list[Run]:
    return [r for r in all_runs() if r.image == image and r.live_domain() is not None]


def gc(verbose: bool = True) -> list[str]:
    """Destroy idle runs, delete stopped ones past the TTL, and kill orphaned desk domains."""
    removed = []
    known_disks = set()
    for run in all_runs():
        live = run.live_domain() is not None
        if run.in_use() or not run.last_used:
            # A command is still working with it (long exec, interactive ssh), or it predates
            # activity tracking; start its idle clock now.
            if not run.last_used:
                run.touch()
            known_disks.add(str(run.dir / "disk.qcow2"))
            continue
        if run.idle() > IDLE_TTL:
            down(run)
            removed.append(f"{run.id} ({'running' if live else 'stopped'}, idle {run.idle() / 3600:.1f} h)")
        else:
            known_disks.add(str(run.dir / "disk.qcow2"))
    for dom in conn().listAllDomains(libvirt.VIR_CONNECT_LIST_DOMAINS_ACTIVE):
        name = dom.name()
        if not name.startswith("desk-"):
            continue
        xml = dom.XMLDesc(0)
        if str(RUNS) in xml and not any(d in xml for d in known_disks):
            dom.destroy()
            removed.append(f"orphaned domain {name}")
    if verbose:
        for r in removed:
            print(f"[desk] gc: removed {r}")
    return removed


def up(spec: ImageSpec, run_id: str | None = None, cold: bool = False, force: bool = False) -> Run:
    with state_lock():
        gc()
        if not force:
            check_memory(spec)
        return _up(spec, run_id, cold)


def _up(spec: ImageSpec, run_id: str | None, cold: bool) -> Run:
    run_id = run_id or secrets.token_hex(3)
    rd = RUNS / run_id
    if rd.exists():
        raise DeskError(f"run {run_id} already exists")
    warm = spec.warm_dir / "memory.save"
    if not spec.base_xml.exists():
        raise DeskError(f"base image {spec.name} not built yet (desk image build {spec.name})")
    saved_xml = None
    if not cold:
        if not warm.exists():
            print(f"[desk] no warm state for {spec.name}, booting cold (desk image warm {spec.name})")
            cold = True
        elif not warm_is_current(spec):
            print(f"[desk] warm state of {spec.name} predates its base image, booting cold "
                  f"(desk image warm {spec.name} --force)")
            cold = True
    if not cold:
        saved_xml = conn().saveImageGetXMLDesc(str(warm), libvirt.VIR_DOMAIN_SAVE_IMAGE_XML_SECURE)
        if uuid_active(domain_uuid(saved_xml)):
            # Restores share the save image's UUID, so only one can run at a time.
            print(f"[desk] warm state of {spec.name} is in use by another run, booting cold instead")
            cold = True
    rd.mkdir(parents=True)
    # A restore must also keep the saved domain name; run ids stay independent of it.
    domain = f"desk-{run_id}" if cold else ET.fromstring(saved_xml).findtext("name")
    now = time.time()
    run = Run(id=run_id, image=spec.name, domain=domain, ssh_port=free_port(),
              user=spec.user, created=now, last_used=now)
    run.save()

    disk, nvram = rd / "disk.qcow2", rd / "VARS.qcow2"
    try:
        if cold:
            qemu_img_overlay(spec.base_disk, disk)
            reflink_copy(spec.base_nvram, nvram)
            template = spec.base_xml.read_text()
        else:
            qemu_img_overlay(spec.warm_dir / "disk.qcow2", disk)
            reflink_copy(spec.warm_dir / "VARS.qcow2", nvram)
            template = saved_xml
        nvram.chmod(0o644)
        xml = prepare_domain_xml(template, name=run.domain, disk=disk, nvram=nvram,
                                 ssh_port=run.ssh_port, console_log=rd / "console.log",
                                 keep_uuid=not cold)
        if cold:
            dom = conn().createXML(xml, 0)
        else:
            conn().restoreFlags(str(warm), xml, libvirt.VIR_DOMAIN_SAVE_RUNNING)
            dom = run.dom()
            try:
                guest.agent_sync_time(dom)
            except DeskError as e:
                print(f"[desk] warning: clock not synced: {e}")
        guest.wait_ssh(run.ssh_port, run.user, timeout=300 if cold else 60)
        if cold:
            guest.wait_ready(run.ssh_port, run.user, spec.ready)
            images.provision(spec, run.ssh_port)
    except BaseException:
        down(run, keep=True)
        raise
    return run


def down(run: Run, keep: bool = False) -> None:
    dom = run.live_domain()
    if dom is not None:
        dom.destroy()
    if not keep:
        shutil.rmtree(run.dir, ignore_errors=True)


def spec_for(run: Run) -> ImageSpec:
    return load_image(run.image)
