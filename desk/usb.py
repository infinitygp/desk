"""Host USB devices handed to a run (a Stream Deck, a phone, a dongle): plugged into the guest's
USB controller as they are, while the host does without them. QEMU runs as the desk user
(qemu:///session), so the user must be able to open the device node; a udev rule makes it so."""

import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import libvirt

from .config import DeskError

SYS = Path("/sys/bus/usb/devices")
ID = re.compile(r"[0-9a-f]{4}:[0-9a-f]{4}")
ADDRESS = re.compile(r"(\d+)\.(\d+)")
# Stay with a device while its BUS.DEVICE changes: the port it is plugged into, its serial number.
PORT = re.compile(r"port:(\d+-\d+(?:\.\d+)*)")
SERIAL = re.compile(r"serial:(\S+)")
QUERY = "VENDOR:PRODUCT (1209:0001), BUS.DEVICE (3.7), port:BUS-PORT (3-1.2) or serial:SERIAL"


def is_query(query: str) -> bool:
    return bool(ID.fullmatch(query.lower()) or ADDRESS.fullmatch(query) or PORT.fullmatch(query)
                or SERIAL.fullmatch(query))
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
                    "port": d.name, "serial": _read(d, "serial"),
                    "name": " ".join(filter(None, (_read(d, "manufacturer"), _read(d, "product")))),
                    "access": os.access(node, os.R_OK | os.W_OK)})
    return out


def present(sys_dir: Path | None = None) -> set[tuple[int, int]]:
    """Bus and device numbers in use on the host, hubs included."""
    sys_dir = sys_dir or SYS
    out = set()
    for d in sys_dir.iterdir() if sys_dir.exists() else []:
        bus, dev = _read(d, "busnum"), _read(d, "devnum")
        if bus.isdigit() and dev.isdigit():
            out.add((int(bus), int(dev)))
    return out


def gone(dom, sys_dir: Path | None = None) -> list[tuple[int, int]]:
    """Entries of a live domain whose device has left the host (unplugged, or enumerated again
    under a new number). libvirt keeps them, and each holds one of the guest's xHCI ports."""
    here = present(sys_dir)
    return [a for a in attached(dom) if a not in here]


def matches(devices: list[dict], query: str) -> list[dict]:
    q = query.lower()
    if ID.fullmatch(q):
        hits = [d for d in devices if d["id"] == q]
    elif m := ADDRESS.fullmatch(q):
        hits = [d for d in devices if (d["bus"], d["dev"]) == (int(m[1]), int(m[2]))]
    elif m := PORT.fullmatch(query):
        hits = [d for d in devices if d.get("port") == m[1]]
    elif m := SERIAL.fullmatch(query):
        hits = [d for d in devices if d.get("serial") == m[1]]
    else:
        raise DeskError(f"a USB device is {QUERY}, not {query!r}; desk usb ls lists them")
    return hits


def pick(devices: list[dict], query: str) -> dict:
    hits = matches(devices, query)
    if not hits:
        raise DeskError(f"no USB device {query} on the host (desk usb ls)")
    if len(hits) > 1:
        raise DeskError(f"several USB devices are {query}: "
                        + ", ".join(f"{d['bus']}.{d['dev']} ({d['id']}, port {d.get('port') or '?'})" for d in hits)
                        + "; give BUS.DEVICE or port:")
    return hits[0]


def wait_present(query: str, timeout: float) -> dict:
    """The host device `query` names, once it is there and udev has given its node to this user."""
    deadline = time.monotonic() + timeout
    while True:
        devices = host_devices()
        if any(d["access"] for d in matches(devices, query)) or time.monotonic() >= deadline:
            return pick(devices, query)
        time.sleep(0.5)


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
        theirs = other.live_domain()
        if theirs is not None and here in attached(theirs):
            raise DeskError(f"USB device {device['id']} is in run {other.id} already (desk -r {other.id} usb rm)")
    dom = run.dom()
    # Only numbers no device on the host has: one that is there, even in a reset, stays the guest's.
    for b, d in gone(dom):
        try:
            dom.detachDeviceFlags(_xml(b, d), libvirt.VIR_DOMAIN_AFFECT_LIVE)
        except libvirt.libvirtError as e:
            raise DeskError(f"could not take out the entry of USB device {b}.{d}, gone from the host: {e}") from None
        run.log_event(usb="remove", device=f"{b}.{d}", reason="gone from the host")
        print(f"[desk] took out USB device {b}.{d}, gone from the host", file=sys.stderr)
    try:
        dom.attachDeviceFlags(_xml(*here), libvirt.VIR_DOMAIN_AFFECT_LIVE)
    except libvirt.libvirtError as e:
        raise DeskError(f"could not hand USB device {device['id']} to the guest: {e}") from None
    run.log_event(usb="add", device=device["id"], address=f"{here[0]}.{here[1]}", name=device["name"])
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
    run.log_event(usb="remove", device=device["id"], address=f"{device['bus']}.{device['dev']}")
    return device


def listing() -> list[dict]:
    """Host devices, each with the run it is handed to, if any, then the entries of runs whose
    device has left the host (`gone`)."""
    from . import runs

    held, stale = {}, []
    for r in runs.all_runs():
        dom = r.live_domain()
        if dom is not None:
            held.update({addr: r.id for addr in attached(dom)})
            stale += [{"id": "", "bus": b, "dev": d, "node": "", "name": "", "access": False, "run": r.id,
                       "gone": True} for b, d in gone(dom)]
    return [{**d, "run": held.get((d["bus"], d["dev"]))} for d in host_devices()] + stale
