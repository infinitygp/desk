"""libvirt connection and domain XML handling."""

import json
import socket
import subprocess
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

import libvirt
import libvirt_qemu

from .config import URI, DeskError

libvirt.registerErrorHandler(lambda _ctx, _err: None, None)

_conn: libvirt.virConnect | None = None


def conn() -> libvirt.virConnect:
    global _conn
    if _conn is None:
        try:
            _conn = libvirt.open(URI)
        except libvirt.libvirtError as e:
            raise DeskError(f"cannot connect to {URI}: {e}") from e
    return _conn


def lookup(name: str) -> libvirt.virDomain | None:
    try:
        return conn().lookupByName(name)
    except libvirt.libvirtError:
        return None


def qmp(dom: libvirt.virDomain, execute: str, **arguments) -> object:
    cmd = {"execute": execute}
    if arguments:
        cmd["arguments"] = arguments
    reply = json.loads(libvirt_qemu.qemuMonitorCommand(dom, json.dumps(cmd), 0))
    if "error" in reply:
        raise DeskError(f"QMP {execute}: {reply['error'].get('desc', reply['error'])}")
    return reply.get("return")


def agent(dom: libvirt.virDomain, execute: str, timeout: int = 10, **arguments) -> object:
    cmd = {"execute": execute}
    if arguments:
        cmd["arguments"] = arguments
    try:
        reply = json.loads(libvirt_qemu.qemuAgentCommand(dom, json.dumps(cmd), timeout, 0))
    except libvirt.libvirtError as e:
        raise DeskError(f"guest agent {execute}: {e}") from e
    return reply.get("return")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def qemu_img_overlay(backing: Path, overlay: Path) -> None:
    subprocess.run(
        ["qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", str(backing), str(overlay)],
        check=True,
    )


def reflink_copy(src: Path, dst: Path) -> None:
    subprocess.run(["cp", "--reflink=auto", str(src), str(dst)], check=True)


def prepare_domain_xml(
    template: str, *, name: str, disk: Path, nvram: Path, ssh_port: int, console_log: Path,
    keep_uuid: bool = False,
) -> str:
    """Turn the golden image's domain XML into a per-run transient domain.

    Works both on the build domain's inactive XML and on the XML stored inside a
    save image, so the guest-visible hardware stays identical between them.
    """
    root = ET.fromstring(template)
    root.find("name").text = name
    uuid_el = root.find("uuid")
    if uuid_el is None:
        uuid_el = ET.SubElement(root, "uuid")
    # The UUID is guest-visible (SMBIOS), so restoring a save image must keep it.
    if not keep_uuid or not uuid_el.text:
        uuid_el.text = str(uuid.uuid4())

    os_el = root.find("os")
    for tag in ("kernel", "initrd", "cmdline"):
        for el in os_el.findall(tag):
            os_el.remove(el)
    os_el.find("nvram").text = str(nvram)

    devices = root.find("devices")
    for d in devices.findall("disk"):
        if d.get("device") == "disk":
            d.find("source").set("file", str(disk))
            # libvirt fills <backingStore> from the qcow2 header itself.
            for bs in d.findall("backingStore"):
                d.remove(bs)
        else:
            devices.remove(d)

    for iface in devices.findall("interface"):
        if iface.get("type") != "user":
            continue
        for pf in iface.findall("portForward"):
            iface.remove(pf)
        pf = ET.SubElement(iface, "portForward", proto="tcp", address="127.0.0.1")
        ET.SubElement(pf, "range", start=str(ssh_port), to="22")

    for serial in devices.findall("serial"):
        src = serial.find("source")
        if src is not None:
            src.set("path", str(console_log))
    # A TPM would tie the save image to swtpm state keyed by the original UUID.
    for tag in ("tpm", "graphics"):
        for el in devices.findall(tag):
            devices.remove(el)
    if not any(i.get("type") == "tablet" for i in devices.findall("input")):
        ET.SubElement(devices, "input", type="tablet", bus="usb")

    # Runtime-only details carried over from a live or saved domain.
    for el in root.findall("seclabel"):
        root.remove(el)
    for ch in devices.findall("channel"):
        src = ch.find("source")
        if src is not None and "path" in src.attrib:
            del src.attrib["path"]
        tgt = ch.find("target")
        if tgt is not None and "state" in tgt.attrib:
            del tgt.attrib["state"]
    return ET.tostring(root, encoding="unicode")


def domain_uuid(xml: str) -> str:
    return ET.fromstring(xml).findtext("uuid")


def uuid_active(u: str) -> bool:
    try:
        return conn().lookupByUUIDString(u).isActive()
    except libvirt.libvirtError:
        return False
