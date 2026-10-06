"""Extra disks for a run (backup targets, a second data disk, a disk to fill up): empty qcow2 files
in the run's directory, plugged into the running guest as virtio disks, as a USB or SATA disk
would be. The serial is the disk's name, so the guest finds it by
/dev/disk/by-id/virtio-NAME on Linux and by its serial number on Windows."""

import json
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import libvirt

from .config import DeskError

# virtio-blk serials hold at most 20 bytes.
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,19}")
SIZE = re.compile(r"[1-9][0-9]*[MGT]")


def directory(run) -> Path:
    return run.dir / "disks"


def _attached(run, dom) -> dict[str, str]:
    """Target dev of each extra disk plugged into the live domain, by name."""
    out = {}
    for d in ET.fromstring(dom.XMLDesc(0)).iter("disk"):
        src = d.find("source")
        if src is not None and Path(src.get("file", "")).parent == directory(run):
            out[Path(src.get("file")).stem] = d.find("target").get("dev")
    return out


def _xml(path: Path, name: str, dev: str) -> str:
    return (f"<disk type='file' device='disk'><driver name='qemu' type='qcow2' discard='unmap'/>"
            f"<source file='{path}'/><target dev='{dev}' bus='virtio'/><serial>{name}</serial></disk>")


def listing(run) -> list[dict]:
    dom = run.live_domain()
    live = _attached(run, dom) if dom is not None else {}
    out = []
    for f in sorted(directory(run).glob("*.qcow2")) if directory(run).exists() else []:
        info = subprocess.run(["qemu-img", "info", "--output=json", "-U", str(f)], capture_output=True, text=True)
        size = json.loads(info.stdout).get("virtual-size", 0) if info.returncode == 0 else 0
        out.append({"name": f.stem, "size": size, "used": f.stat().st_blocks * 512, "attached": f.stem in live})
    return out


def add(run, size: str, name: str | None = None) -> str:
    if not SIZE.fullmatch(size):
        raise DeskError(f"disk size is a number with M, G or T, e.g. 10G (got {size!r})")
    dom = run.dom()
    existing = {f.stem for f in directory(run).glob("*.qcow2")} if directory(run).exists() else set()
    if name is None:
        name = next(f"disk{i}" for i in range(1, 100) if f"disk{i}" not in existing)
    if not NAME.fullmatch(name):
        raise DeskError(f"disk names are 1-20 of a-z, 0-9 and - (got {name!r})")
    if name in existing:
        raise DeskError(f"disk {name} exists (desk disk ls)")
    used = {t.get("dev") for t in ET.fromstring(dom.XMLDesc(0)).iter("target") if t.get("dev")}
    dev = next((f"vd{c}" for c in "bcdefghijklmnopqrstuvwxyz" if f"vd{c}" not in used), None)
    if dev is None:
        raise DeskError("no free disk name in the guest")
    directory(run).mkdir(exist_ok=True)
    path = directory(run) / f"{name}.qcow2"
    subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", str(path), size], check=True)
    path.chmod(0o644)
    try:
        dom.attachDeviceFlags(_xml(path, name, dev), libvirt.VIR_DOMAIN_AFFECT_LIVE)
    except libvirt.libvirtError as e:
        path.unlink()
        raise DeskError(f"could not plug in the disk: {e}") from None
    run.log_event(disk="add", name=name, size=size)
    return name


def remove(run, name: str, timeout: float = 30) -> None:
    path = directory(run) / f"{name}.qcow2"
    if not path.exists():
        raise DeskError(f"no disk {name} (desk disk ls)")
    dom = run.live_domain()
    if dom is not None and name in _attached(run, dom):
        dev = _attached(run, dom)[name]
        try:
            dom.detachDeviceFlags(_xml(path, name, dev), libvirt.VIR_DOMAIN_AFFECT_LIVE)
        except libvirt.libvirtError as e:
            raise DeskError(f"could not unplug the disk: {e}") from None
        # The guest acknowledges the unplug; until then the device is still there.
        deadline = time.monotonic() + timeout
        while name in _attached(run, dom):
            if time.monotonic() > deadline:
                raise DeskError(f"the guest has not released disk {name} after {timeout:.0f}s "
                                "(still mounted or in use?)")
            time.sleep(0.5)
    path.unlink()
    run.log_event(disk="remove", name=name)
