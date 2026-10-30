"""Host USB devices handed to a run (a Stream Deck, a phone, a dongle): plugged into the guest's
USB controller as they are, while the host does without them. QEMU runs as the desk user
(qemu:///session), so the user must be able to open the device node; a udev rule makes it so."""

import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import libvirt

from .config import DeskError

SYS = Path("/sys/bus/usb/devices")
ID = re.compile(r"[0-9a-f]{4}:[0-9a-f]{4}")
ADDRESS = re.compile(r"(\d+)\.(\d+)")
HUB = "09"


def _read(d: Path, name: str) -> str:
    try:
        return (d / name).read_text().strip()
    except OSError:
        return ""


def host_devices(sys_dir: Path = SYS) -> list[dict]:
    """USB devices on the host, hubs left out."""
    out = []
    for d in sorted(sys_dir.iterdir()) if sys_dir.exists() else []:
        vendor, product = _read(d, "idVendor"), _read(d, "idProduct")
        if not vendor or _read(d, "bDeviceClass") == HUB:
            continue
        bus, dev = int(_read(d, "busnum")), int(_read(d, "devnum"))
        node = Path(f"/dev/bus/usb/{bus:03d}/{dev:03d}")
        out.append({"id": f"{vendor}:{product}", "bus": bus, "dev": dev, "node": str(node),
                    "name": " ".join(filter(None, (_read(d, "manufacturer"), _read(d, "product")))),
                    "access": os.access(node, os.R_OK | os.W_OK)})
    return out


def pick(devices: list[dict], query: str) -> dict:
    q = query.lower()
    if ID.fullmatch(q):
        hits = [d for d in devices if d["id"] == q]
    elif m := ADDRESS.fullmatch(q):
        hits = [d for d in devices if (d["bus"], d["dev"]) == (int(m[1]), int(m[2]))]
    else:
        raise DeskError(f"a USB device is VENDOR:PRODUCT (1209:0001) or BUS.DEVICE (3.7), not {query!r}; "
                        "desk usb ls lists them")
    if not hits:
        raise DeskError(f"no USB device {query} on the host (desk usb ls)")
    if len(hits) > 1:
        raise DeskError(f"several USB devices are {query}: "
                        + ", ".join(f"{d['bus']}.{d['dev']}" for d in hits) + "; give BUS.DEVICE")
    return hits[0]


def udev_hint(device: dict) -> str:
    """The rule that gives the device node to the kvm group, as a fish command for the user."""
    vendor, product = device["id"].split(":")
    rule = (f'SUBSYSTEM=="usb", ATTRS{{idVendor}}=="{vendor}", ATTRS{{idProduct}}=="{product}", '
            'MODE="0660", GROUP="kvm"')
    return (f"echo '{rule}' | sudo tee /etc/udev/rules.d/70-desk-usb-{vendor}-{product}.rules; "
            f"and sudo udevadm control --reload; and sudo udevadm trigger --subsystem-match=usb "
            f"--attr-match=idVendor={vendor}")


def _xml(bus: int, dev: int) -> str:
    return f"<hostdev mode='subsystem' type='usb'><source><address bus='{bus}' device='{dev}'/></source></hostdev>"


def attached(dom) -> list[tuple[int, int]]:
    """Host bus and device numbers of the USB devices handed to a live domain."""
    out = []
    for h in ET.fromstring(dom.XMLDesc(0)).iter("hostdev"):
        a = h.find("source/address")
        if h.get("type") == "usb" and a is not None:
            out.append((int(a.get("bus")), int(a.get("device"))))
    return out


def usable(query: str) -> dict:
    """The host device `query` names, if this user can hand it over; refused before a VM starts."""
    from .doctor import USB_HOST, install_command

    if not USB_HOST.present():
        raise DeskError("QEMU usb-host is unavailable (or QEMU's device probe failed); host USB passthrough needs it: "
                        + install_command(None, [USB_HOST]))
    device = pick(host_devices(), query)
    if not device["access"]:
        raise DeskError(f"{device['node']} ({device['id']} {device['name']}) cannot be opened by this user, "
                        f"and QEMU runs as it; give it to the kvm group with a udev rule (fish, as you):\n"
                        f"  {udev_hint(device)}")
    return device


def add(run, query: str) -> dict:
    from . import runs

    device = usable(query)
    here = (device["bus"], device["dev"])
    for other in runs.all_runs():
        dom = other.live_domain()
        if dom is not None and here in attached(dom):
            raise DeskError(f"USB device {device['id']} is in run {other.id} already (desk -r {other.id} usb rm)")
    try:
        run.dom().attachDeviceFlags(_xml(*here), libvirt.VIR_DOMAIN_AFFECT_LIVE)
    except libvirt.libvirtError as e:
        raise DeskError(f"could not hand USB device {device['id']} to the guest: {e}") from None
    run.log_event(usb="add", device=device["id"], name=device["name"])
    return device


def remove(run, query: str) -> dict:
    dom = run.dom()
    here = attached(dom)
    devices = [d for d in host_devices() if (d["bus"], d["dev"]) in here]
    # A device unplugged from the host meanwhile is still in the domain: drop it by its address.
    devices += [{"id": f"{b}.{d}", "bus": b, "dev": d, "name": "", "node": "", "access": False}
                for b, d in here if not any((x["bus"], x["dev"]) == (b, d) for x in devices)]
    try:
        device = pick(devices, query)
    except DeskError:
        raise DeskError(f"run {run.id} has no USB device {query} (desk usb ls)") from None
    try:
        dom.detachDeviceFlags(_xml(device["bus"], device["dev"]), libvirt.VIR_DOMAIN_AFFECT_LIVE)
    except libvirt.libvirtError as e:
        raise DeskError(f"could not take USB device {device['id']} back: {e}") from None
    run.log_event(usb="remove", device=device["id"])
    return device


def listing() -> list[dict]:
    """Host devices, each with the run it is handed to, if any."""
    from . import runs

    held = {}
    for r in runs.all_runs():
        dom = r.live_domain()
        if dom is not None:
            held.update({addr: r.id for addr in attached(dom)})
    return [{**d, "run": held.get((d["bus"], d["dev"]))} for d in host_devices()]
