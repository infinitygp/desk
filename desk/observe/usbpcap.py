"""USB traffic inside the guest with USBPcap (desowin.org/usbpcap): its filter driver on the
guest's root hubs and USBPcapCMD.exe, which writes pcap (link type 249, LINKTYPE_USBPCAP).

USBPcap is not part of Windows and its driver is installed with its own installer (an
administrator, a reboot); desk installs neither. A USB device handed to the run (desk usb add) is
on the guest's xHCI controller, and seeing it there does not mean USBPcap captures it: the
driver must be installed and attached to that controller's root hub, which only the interfaces
USBPcapCMD lists say.

Interfaces are listed the way Wireshark asks USBPcapCMD for them (its extcap interface,
`--extcap-interfaces`, lines `interface {value=...}{display=...}`); a capture is
`USBPcapCMD.exe -d \\\\.\\USBPcapN -o FILE -s SNAPLEN -A` (all devices of that root hub), and
it is ended by its PID: USBPcapCMD has no command that stops it, so the last packet of the file
may be cut off, which the check of the file says."""

import re

from .base import ArtifactSpec, Capability, Missing, Option, Provider, tool_run
from .formats import LINKTYPE_USBPCAP
from .guestops import win
from .session import Session

DEFAULT_EXE = r"C:\Program Files\USBPcap\USBPcapCMD.exe"
_IFACE = re.compile(r"\\\\\.\\USBPcap\d+")
_EXTCAP = re.compile(r"^interface \{value=([^}]*)\}(?:\{display=([^}]*)\})?", re.M)


def _iface(v: str) -> str | None:
    return None if _IFACE.fullmatch(v) else f"{v!r} is not a USBPcap interface (\\\\.\\USBPcap1)"


def interfaces(text: str) -> list[dict]:
    """USBPcapCMD's --extcap-interfaces output as {value, display}."""
    return [{"value": m[1], "display": m[2] or m[1]} for m in _EXTCAP.finditer(text)]


class UsbPcap(Provider):
    name = "usbpcap"
    title = "USBPcap"
    OPTIONS = {
        "interface": Option(str, "", "the root hub's capture interface, \\\\.\\USBPcapN (default: the only one)",
                            check=_iface),
        "snaplen": Option(int, 65535, "bytes kept of each USB packet"),
        "exe": Option(str, DEFAULT_EXE, "where USBPcapCMD.exe is in the guest"),
    }
    notes = ("USB packets (URBs) of every device on the root hub captured, with their data up to snaplen; HID "
             "reports are in them as transferred",
             "a device handed to the run with desk usb add is captured only where USBPcap's driver is attached to "
             "its root hub")

    def resource_of(self, session: Session) -> str | None:
        return f"usbpcap:{session.options.get('interface') or 'default'}"

    def probe_request(self, opts: dict, host) -> dict:
        return {"files": [opts["exe"]], "services": ["USBPcap"], "usb": True}

    def assess(self, probe: dict, opts: dict, host) -> Capability:
        cap = super().assess(probe, opts, host)
        exe = (probe.get("files") or {}).get(opts["exe"]) or {}
        driver = (probe.get("services") or {}).get("USBPcap")
        if not exe.get("exists"):
            cap.problems.append(f"{opts['exe']} is not in the guest (install USBPcap from https://desowin.org/usbpcap/ "
                                "in the guest; desk does not install drivers)")
        cap.version = exe.get("version") or ""
        if not driver:
            cap.problems.append("the USBPcap driver is not installed in the guest")
        elif driver.get("state") != "Running":
            cap.problems.append(f"the USBPcap driver is installed but {driver.get('state')} (the guest needs the "
                                "reboot its installer asks for)")
        cap.details["driver"] = driver
        cap.details["guest_usb"] = [f"{d.get('name')} ({d.get('id')})" for d in probe.get("usb") or []]
        try:
            handed = host.usb()
        except Exception as e:  # noqa: BLE001 - the host's view is extra information, not a requirement
            handed = []
            cap.details["passthrough_error"] = str(e)
        cap.details["passthrough"] = [f"{d['id']} {d.get('name') or ''}".strip() for d in handed]
        seen = " ".join(d.get("id") or "" for d in probe.get("usb") or []).upper()
        missing = [d["id"] for d in handed if ":" in d["id"] and
                   "VID_{}&PID_{}".format(*d["id"].upper().split(":")) not in seen]
        if missing:
            cap.details["passthrough_not_in_guest"] = missing
        cap.available = not cap.problems
        return cap

    def start(self, guest, session: Session, host) -> None:
        exe = session.options["exe"]
        r = tool_run(guest, exe, ["--extcap-interfaces"], 60, "listing USBPcap's interfaces")
        found = interfaces(r.get("stdout") or "")
        session.stats["interfaces"] = found
        iface = session.options["interface"]
        if not iface:
            if len(found) != 1:
                raise Missing(f"USBPcap lists {len(found)} interfaces ({', '.join(i['value'] for i in found) or 'none'}); "
                              "choose one with --set interface=\\\\.\\USBPcapN")
            iface = found[0]["value"]
        elif iface not in [i["value"] for i in found]:
            raise Missing(f"USBPcap has no interface {iface} (it lists: {', '.join(i['value'] for i in found) or 'none'})")
        pcap = win(session.guest_dir, "usb.pcap")
        proc = guest.launch(exe, ["-d", iface, "-o", pcap, "-s", str(session.options["snaplen"]), "-A"],
                            win(session.guest_dir, "usbpcap.out"), win(session.guest_dir, "usbpcap.err"))
        if proc.get("exited"):
            raise Missing(f"USBPcapCMD ended at once (see {session.guest_dir}\\usbpcap.err)")
        session.tool.update(interface=iface, pcap=pcap, procs=[{**proc, "name": "USBPcapCMD.exe"}])

    def watch_files(self, session: Session) -> list[str]:
        return [session.tool["pcap"]] if session.tool.get("pcap") else []

    def stop(self, guest, session: Session, host, force: bool = False) -> None:
        for p in session.tool.get("procs") or []:
            guest.stop_process(p, tree=True)

    def artifacts(self, session: Session) -> list[ArtifactSpec]:
        return [ArtifactSpec("usb.pcap", session.tool.get("pcap") or win(session.guest_dir, "usb.pcap"), "pcap",
                             description=f"USB capture of {session.tool.get('interface', 'the root hub')}"),
                ArtifactSpec("usbpcap.err", win(session.guest_dir, "usbpcap.err"), "text", required=False)]

    def verify(self, path, art):
        check = super().verify(path, art)
        if art.kind == "pcap" and check.get("ok") and check.get("linktype") != LINKTYPE_USBPCAP:
            check = {**check, "ok": False, "detail": f"pcap of {check.get('link')}, not USBPcap's link type"}
        elif art.kind == "pcap" and not check.get("ok") and check.get("packets") and "cut off" in check["detail"]:
            # Ended by its PID: everything before the last record is whole.
            check = {**check, "ok": True, "detail": f"{check['detail']}; the {check['packets']} packets before it are "
                     "whole (USBPcapCMD is ended by its PID)"}
        return check
