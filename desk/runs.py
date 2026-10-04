"""Disposable runs: a copy-on-write overlay on a golden image, restored from the warm state."""

import json
import os
import secrets
import shutil
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path

import libvirt

from . import guest, images
from .config import RUNS, DeskError, ImageSpec, load_image
from .virt import (conn, domain_uuid, free_port, lookup, prepare_domain_xml, qemu_img_overlay,
                   reflink_copy, uuid_active)


@dataclass
class Run:
    id: str
    image: str
    domain: str
    ssh_port: int
    user: str
    created: float
    shots: int = 0

    @property
    def dir(self) -> Path:
        return RUNS / self.id

    def save(self) -> None:
        (self.dir / "run.json").write_text(json.dumps(asdict(self), indent=2))

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
    return sorted(
        (load(p.name) for p in RUNS.iterdir() if (p / "run.json").is_file()), key=lambda r: r.created
    )


def resolve(run_id: str | None) -> Run:
    """Explicit id, then $DESK_RUN, then the only active run."""
    run_id = run_id or os.environ.get("DESK_RUN")
    if run_id:
        return load(run_id)
    active = [r for r in all_runs() if r.live_domain() is not None]
    if len(active) == 1:
        return active[0]
    if not active:
        raise DeskError("no active run (desk up <image>)")
    raise DeskError("several active runs, pick one with -r/--run or DESK_RUN: "
                    + ", ".join(r.id for r in active))


def up(spec: ImageSpec, run_id: str | None = None, cold: bool = False) -> Run:
    run_id = run_id or secrets.token_hex(3)
    rd = RUNS / run_id
    if rd.exists():
        raise DeskError(f"run {run_id} already exists")
    warm = spec.warm_dir / "memory.save"
    if not cold and not warm.exists():
        raise DeskError(f"no warm state for {spec.name} (desk image warm {spec.name}, or --cold)")
    if not spec.base_xml.exists():
        raise DeskError(f"base image {spec.name} not built yet")
    saved_xml = None
    if not cold:
        saved_xml = conn().saveImageGetXMLDesc(str(warm), libvirt.VIR_DOMAIN_SAVE_IMAGE_XML_SECURE)
        if uuid_active(domain_uuid(saved_xml)):
            # Restores share the save image's UUID, so only one can run at a time.
            print(f"[desk] warm state of {spec.name} is in use by another run, booting cold instead")
            cold = True
    rd.mkdir(parents=True)
    # A restore must also keep the saved domain name; run ids stay independent of it.
    domain = f"desk-{run_id}" if cold else ET.fromstring(saved_xml).findtext("name")
    run = Run(id=run_id, image=spec.name, domain=domain, ssh_port=free_port(),
              user=spec.user, created=time.time())
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
