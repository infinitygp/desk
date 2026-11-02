"""A guest cut off from the outside (desk offline, the offline step): its outgoing connections
refused, or only those to some ports, while DNS and desk's own ways in (SSH and the in-session
agent, both connections into the guest) keep working. passt, which connects the guest, cannot filter
what goes out, so this is the guest's own firewall, as a test would set it by hand: an nft table of
desk's on Linux, loaded again at boot by a unit; a chain included by fw4 on OpenWrt, which keeps it
over reboots; a rule group in Windows Firewall."""

import shlex

from . import guest
from .config import DeskError, NotDone

NAME = "desk_offline"
NFT_FILE = "/etc/desk-offline.nft"
UNIT = "/etc/systemd/system/desk-offline.service"
FW4_FILE = "/etc/nftables.d/99-desk-offline.nft"
GROUP = "desk offline"


def _run(run, spec, script: str) -> tuple[int, str]:
    r = guest.ssh_run(run.ssh_port, spec.root_user, guest.command(spec, [script], limit=120), capture=True,
                      timeout=150)
    guest.lost(r, run.ssh_port, spec.root_user, "desk offline")
    return r.returncode, (r.stdout + r.stderr).decode(errors="replace").strip()


def _chain(ports: list[int] | None) -> str:
    """The output chain: what goes out is refused (only to `ports`, when given); not DNS, not the
    answers of connections into the guest (desk's SSH), not loopback."""
    block = (f"meta l4proto {{ tcp, udp }} th dport {{ {', '.join(map(str, ports))} }} reject" if ports
             else "reject")
    return ("type filter hook output priority filter; policy accept;\n"
            '\t\toifname "lo" accept\n'
            "\t\tct state established,related accept\n"
            "\t\tmeta l4proto { tcp, udp } th dport 53 accept\n"
            f"\t\t{block}\n")


def _linux(ports: list[int] | None) -> str:
    rules = f"table inet {NAME} {{\n\tchain output {{\n\t\t{_chain(ports)}\t}}\n}}\n"
    unit = ("[Unit]\nDescription=desk: outgoing connections refused (desk offline)\nBefore=network-pre.target\n"
            "Wants=network-pre.target\n[Service]\nType=oneshot\nRemainAfterExit=yes\n"
            f"ExecStart=/bin/sh -c 'nft delete table inet {NAME} 2>/dev/null; nft -f {NFT_FILE}'\n"
            "[Install]\nWantedBy=multi-user.target\n")
    return ("command -v nft >/dev/null || { echo 'this guest has no nft (the nftables package)'; exit 4; }; "
            f"printf %s {shlex.quote(rules)} > {NFT_FILE} && printf %s {shlex.quote(unit)} > {UNIT} && "
            "systemctl daemon-reload && systemctl enable --quiet desk-offline.service && "
            f"systemctl restart desk-offline.service && nft list table inet {NAME} >/dev/null")


def _openwrt(ports: list[int] | None) -> str:
    rules = f"chain {NAME} {{\n\t\t{_chain(ports)}\t}}\n"
    return (f"printf %s {shlex.quote(rules)} > {FW4_FILE} && fw4 reload >/dev/null 2>&1; "
            f"nft list chain inet fw4 {NAME} >/dev/null")


def _windows(ports: list[int] | None) -> str:
    # A block rule wins over every allow rule; all remote ports but DNS's when none are given.
    remote = ",".join(map(str, ports)) if ports else "1-52,54-65535"
    rules = "; ".join(f"New-NetFirewallRule -Group '{GROUP}' -DisplayName '{GROUP} ({proto})' -Direction Outbound "
                      f"-Protocol {proto} -RemotePort {remote} -Action Block | Out-Null" for proto in ("TCP", "UDP"))
    return (f"Remove-NetFirewallRule -Group '{GROUP}' -ErrorAction SilentlyContinue; {rules}; "
            "if (@(Get-NetFirewallProfile | Where-Object { -not $_.Enabled }).Count) "
            "{ 'Windows Firewall is off'; exit 5 }")


def on(run, spec, ports: list[int] | None = None) -> str:
    script = {"windows": _windows, "openwrt": _openwrt}.get(spec.family, _linux)(ports)
    rc, said = _run(run, spec, script)
    if rc in (4, 5):
        raise DeskError(f"cannot cut the guest off: {said}")
    if rc != 0 or not state(run, spec):
        raise NotDone(f"the guest's firewall did not take desk's rules: {said or f'exit {rc}'}")
    what = f"connections to port {', '.join(map(str, ports))}" if ports else "every connection but DNS"
    return f"offline: {what} going out of the guest refused"


def off(run, spec) -> str:
    if spec.family == "windows":
        script = f"Remove-NetFirewallRule -Group '{GROUP}' -ErrorAction SilentlyContinue; exit 0"
    elif spec.family == "openwrt":
        # fw4 flushes the chains of its table when it reloads but keeps one it does not know, empty.
        script = f"rm -f {FW4_FILE}; fw4 reload >/dev/null 2>&1; nft delete chain inet fw4 {NAME} 2>/dev/null; exit 0"
    else:
        script = (f"systemctl disable --now --quiet desk-offline.service 2>/dev/null; rm -f {UNIT} {NFT_FILE}; "
                  f"systemctl daemon-reload; nft delete table inet {NAME} 2>/dev/null; exit 0")
    _run(run, spec, script)
    if state(run, spec):
        raise NotDone("desk's firewall rules are still in the guest")
    return "online: desk's firewall rules removed"


def state(run, spec) -> bool:
    """Whether desk's rules are in the guest's firewall now."""
    script = {"windows": f"@(Get-NetFirewallRule -Group '{GROUP}' -ErrorAction SilentlyContinue).Count",
              "openwrt": f"nft list chain inet fw4 {NAME} 2>/dev/null | grep -q reject && echo 1 || echo 0"}.get(
        spec.family, f"nft list table inet {NAME} >/dev/null 2>&1 && echo 1 || echo 0")
    rc, said = _run(run, spec, script)
    if rc != 0 or not said.splitlines() or not said.splitlines()[-1].strip().isdigit():
        raise NotDone(f"cannot read the guest's firewall: {said or f'exit {rc}'}")
    return int(said.splitlines()[-1]) > 0
