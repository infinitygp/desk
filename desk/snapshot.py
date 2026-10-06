"""Snapshots of a running run, to go back to a state (after an install, halfway through a wizard)
in seconds instead of starting over. The same mechanism as the warm state: the guest's memory is
saved (the VM stops for a moment), its disk and firmware variables are reflink copies, and the
run carries on restored from the saved memory. Snapshots live in the run's directory."""

import json
import os
import re
import shutil
import time
from pathlib import Path

import libvirt

from . import clock, disks, guest
from .config import DeskError
from .virt import conn, reflink_copy

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def _dir(run, name: str | None = None) -> Path:
    base = run.dir / "snapshots"
    if name is None:
        return base
    if not NAME.fullmatch(name):
        raise DeskError(f"snapshot names are letters, digits, . _ - (got {name!r})")
    return base / name


def _resume(run, memory: Path) -> None:
    # The saved domain is this run's own, so its XML (name, UUID, disk, ports) is used as is.
    xml = conn().saveImageGetXMLDesc(str(memory), libvirt.VIR_DOMAIN_SAVE_IMAGE_XML_SECURE)
    conn().restoreFlags(str(memory), xml, libvirt.VIR_DOMAIN_SAVE_RUNNING)
    try:
        clock.apply(run)
    except DeskError as e:
        print(f"[desk] warning: clock not synced: {e}")
    guest.wait_ssh(run.ssh_port, run.user, timeout=60)


def save(run, name: str, force: bool = False) -> float:
    d = _dir(run, name)
    if d.exists():
        if not force:
            raise DeskError(f"snapshot {name} exists (--force to replace it)")
        shutil.rmtree(d)
    dom = run.dom()
    d.mkdir(parents=True)
    t0 = time.monotonic()
    try:
        dom.save(str(d / "memory.save"))
        reflink_copy(run.dir / "disk.qcow2", d / "disk.qcow2")
        reflink_copy(run.dir / "VARS.qcow2", d / "VARS.qcow2")
        for f in disks.directory(run).glob("*.qcow2"):
            (d / "disks").mkdir(exist_ok=True)
            reflink_copy(f, d / "disks" / f.name)
    except BaseException:
        if run.live_domain() is None and (d / "memory.save").exists():
            _resume(run, d / "memory.save")
        shutil.rmtree(d, ignore_errors=True)
        raise
    _resume(run, d / "memory.save")
    took = time.monotonic() - t0
    (d / "meta.json").write_text(json.dumps({"created": time.time(), "seconds": round(took, 1),
                                             "clock_offset": run.clock_offset}))
    run.log_event(snapshot="save", name=name)
    return took


def restore(run, name: str) -> float:
    d = _dir(run, name)
    if not (d / "memory.save").exists():
        raise DeskError(f"no snapshot {name} (desk snapshot list)")
    t0 = time.monotonic()
    dom = run.live_domain()
    if dom is not None:
        dom.destroy()
    for f in ("disk.qcow2", "VARS.qcow2"):
        tmp = run.dir / f".{f}.restore"
        reflink_copy(d / f, tmp)
        tmp.chmod(0o644)
        os.replace(tmp, run.dir / f)
    # The clock as it was then too: whether the guest syncs its time is part of its disk.
    run.clock_offset = json.loads((d / "meta.json").read_text()).get("clock_offset", 0.0)
    run.save()
    # Extra disks as they were then: the saved guest has exactly those plugged in.
    shutil.rmtree(disks.directory(run), ignore_errors=True)
    if (d / "disks").exists():
        disks.directory(run).mkdir()
        for f in (d / "disks").glob("*.qcow2"):
            reflink_copy(f, disks.directory(run) / f.name)
            (disks.directory(run) / f.name).chmod(0o644)
    _resume(run, d / "memory.save")
    run.log_event(snapshot="restore", name=name)
    return time.monotonic() - t0


def listing(run) -> list[dict]:
    out = []
    for d in sorted(_dir(run).glob("*/meta.json")) if _dir(run).exists() else []:
        meta = json.loads(d.read_text())
        size = sum(f.stat().st_blocks * 512 for f in d.parent.rglob("*") if f.is_file())
        out.append({"name": d.parent.name, "created": meta["created"], "bytes": size})
    return out


def delete(run, name: str) -> None:
    d = _dir(run, name)
    if not d.exists():
        raise DeskError(f"no snapshot {name}")
    shutil.rmtree(d)
