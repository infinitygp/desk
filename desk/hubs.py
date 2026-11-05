"""Port paths and hubs of the guest's usbvfiod controller (desk up --usbvfiod, desk usb add --at).

Some programs take a product for what it is by the hubs its devices sit behind and place devices by
the hub ports they are on. usbvfiod's controller holds a tree of devices: virtual USB 2.0 hubs on
its root ports, and a device at a port path below them.

A port path is the ports from the root port down, dotted: `5.1.1.4` is port 4 of the hub on port 1
of the hub on port 1 of the hub on root port 5. Root ports 1 to 4 are USB 3 and 5 to 8 USB 2; hubs
(and everything below them) need a USB 2 root port. A path goes through at most five hubs, and a
hub has up to 15 ports. What is already in the controller's tree is the controller's to say (desk
asks it, desk/usbvfiod.py); nothing about it is kept here."""

import re
from dataclasses import dataclass

from .config import DeskError

# The controller's root ports, and the USB 2 ones among them, where hubs can stand.
ROOT_PORTS = 8
USB2_ROOT_PORTS = range(5, 9)
# Five hubs between the root port and a device (the tiers of an xHCI route string).
MAX_DEPTH = 6
MAX_PORTS = 15
# What a hub made without --hub is: an ID usbvfiod needs, and what hubs of products are, a USB 2.0
# hub at high speed.
DEFAULT_ID = (0x0409, 0x55AA)
DEFAULT_PORTS = 8

PATH = re.compile(r"[1-9]\d?(?:\.[1-9]\d?)*")
HUB_OPTION = re.compile(r"(?P<path>[1-9]\d?(?:\.[1-9]\d?)*)=(?:(?P<vid>[0-9a-fA-F]{4}):(?P<pid>[0-9a-fA-F]{4}))?"
                        r"(?:/(?P<ports>\d+))?(?:/(?P<speed>hs|fs))?")
HUB_FORMAT = "PORTPATH=[VENDOR:PRODUCT][/PORTS][/hs|/fs], e.g. 5=067b:2586/4/hs"
# What a device is called by `desk usb rm at:PORTPATH`.
AT = re.compile(r"at:(" + PATH.pattern + ")")


def parse_path(text: str) -> tuple[int, ...]:
    """A dotted port path as numbers."""
    if not isinstance(text, str) or not PATH.fullmatch(text):
        raise DeskError(f"a port path is the ports from the root port down, dotted, e.g. 5.1.4 (got {text!r})")
    path = tuple(int(p) for p in text.split("."))
    if len(path) > MAX_DEPTH:
        raise DeskError(f"port path {text} has {len(path) - 1} hubs on the way; USB allows five")
    if path[0] > ROOT_PORTS:
        raise DeskError(f"port path {text}: the controller has {ROOT_PORTS} root ports")
    if len(path) > 1 and path[0] not in USB2_ROOT_PORTS:
        raise DeskError(f"port path {text}: hubs stand on the USB 2 root ports 5 to {ROOT_PORTS}, not on "
                        f"{path[0]}")
    if any(p > MAX_PORTS for p in path[1:]):
        raise DeskError(f"port path {text}: a hub has at most {MAX_PORTS} ports")
    return path


def fmt(path: tuple[int, ...]) -> str:
    return ".".join(str(p) for p in path)


@dataclass(frozen=True)
class Hub:
    path: tuple[int, ...]
    vendor: int = DEFAULT_ID[0]
    product: int = DEFAULT_ID[1]
    ports: int = DEFAULT_PORTS
    high: bool = True

    @property
    def speed(self) -> str:
        return "high" if self.high else "full"

    def spec(self) -> str:
        return f"{self.vendor:04x}:{self.product:04x}/{self.ports}/{'hs' if self.high else 'fs'}"

    def payload(self) -> dict:
        """The hotplug command that attaches it (desk/hotplug.py)."""
        return {"path": fmt(self.path), "spec": {"vid": self.vendor, "pid": self.product, "ports": self.ports,
                                                 "speed": self.speed}}


def parse_hub(text: str) -> Hub:
    m = HUB_OPTION.fullmatch(text.strip()) if isinstance(text, str) else None
    if not m or not (m["vid"] or m["ports"] or m["speed"]):
        raise DeskError(f"a hub is {HUB_FORMAT} (got {text!r}); the ID, the number of ports and the speed default to "
                        f"{DEFAULT_ID[0]:04x}:{DEFAULT_ID[1]:04x}, {DEFAULT_PORTS} and high")
    path = parse_path(m["path"])
    if path[0] not in USB2_ROOT_PORTS:
        raise DeskError(f"hub {m['path']}: hubs stand on the USB 2 root ports 5 to {ROOT_PORTS}, not on {path[0]}")
    if len(path) > MAX_DEPTH - 1:
        raise DeskError(f"hub {m['path']}: a hub below five others would leave no room for a device")
    ports = int(m["ports"]) if m["ports"] else DEFAULT_PORTS
    if not 1 <= ports <= MAX_PORTS:
        raise DeskError(f"hub {m['path']}: a hub has 1 to {MAX_PORTS} ports (got {ports})")
    vid, pid = (int(m["vid"], 16), int(m["pid"], 16)) if m["vid"] else DEFAULT_ID
    return Hub(path, vid, pid, ports, high=m["speed"] != "fs")


def parse_hubs(options: list[str] | None) -> dict[tuple[int, ...], Hub]:
    out: dict[tuple[int, ...], Hub] = {}
    for text in options or []:
        hub = parse_hub(text)
        if hub.path in out:
            raise DeskError(f"hub {fmt(hub.path)} is given twice")
        out[hub.path] = hub
    return out


def describe(entry: dict) -> str:
    """A tree entry of the controller (`path`, `speed`, `id`, `hub_ports`) in words."""
    if entry.get("hub_ports") is not None:
        return f"a {entry['speed']} speed hub with {entry['hub_ports']} ports"
    return f"the device {entry['id']}"


def plan(path: tuple[int, ...], wanted: dict[tuple[int, ...], Hub], tree: list[dict]) -> list[Hub]:
    """The hubs to attach, top first, for a device at `path` when `tree` is what the controller
    holds. A hub that is there keeps what it is (--hub for it has to agree in ports and speed);
    devices that share a prefix share its hubs. Refused here what the controller would refuse
    later: a hub on the way that is no hub, a port a hub does not have, a high speed hub below a
    full speed one."""
    known = {e["path"]: e for e in tree}
    on_the_way = {path[:i] for i in range(1, len(path))}
    for stray in sorted(set(wanted) - on_the_way):
        raise DeskError(f"--hub {fmt(stray)} is not on the way to {fmt(path)} (hubs: "
                        f"{', '.join(fmt(p) for p in sorted(on_the_way)) or 'none, it is on a root port'})")
    if fmt(path) in known:
        raise DeskError(f"port path {fmt(path)} holds {describe(known[fmt(path)])} already (desk usb ls)")
    new: list[Hub] = []
    parent_high = True  # the root port is no full speed hub
    for depth in range(1, len(path)):
        prefix, want, have = path[:depth], wanted.get(path[:depth]), known.get(fmt(path[:depth]))
        if have is not None:
            if have.get("hub_ports") is None:
                raise DeskError(f"port path {fmt(prefix)} holds {describe(have)}, which is no hub: nothing can "
                                f"be plugged in below it ({fmt(path)})")
            if want is not None and (want.ports, want.speed) != (have["hub_ports"], have["speed"]):
                raise DeskError(f"the hub at {fmt(prefix)} is {describe(have)} already and keeps it until it is "
                                f"removed (desk usb rm at:{fmt(prefix)}), not {want.ports} ports at "
                                f"{want.speed} speed")
            size, high = have["hub_ports"], have["speed"] == "high"
        else:
            hub = want or Hub(prefix)
            if hub.high and not parent_high:
                raise DeskError(f"the hub at {fmt(prefix)} is high speed and the hub above it full speed, which "
                                "the controller does not take: make the one above high speed too (/hs) or this "
                                "one full speed (/fs)")
            new.append(hub)
            size, high = hub.ports, hub.high
        if path[depth] > size:
            raise DeskError(f"the hub at {fmt(prefix)} has {size} ports; port path {fmt(path)} needs port "
                            f"{path[depth]}")
        parent_high = high
    return new
