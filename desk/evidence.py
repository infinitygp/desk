"""What a run left behind: guest problems since it started, file changes, a timeline report."""

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from pathlib import Path

from . import browser, guest
from .config import HOME, DeskError, load_image

REPORTS = HOME / "reports"
REPORT_RETENTION_DAYS = 14
# virt-diff starts a libguestfs appliance and reads both disks: ~30 s, minutes for a large run.
DIFF_TIMEOUT = 600

# Paths every desktop session touches; hidden from `desk diff` unless --all.
NOISE = (
    "/var/log/", "/var/cache/", "/var/tmp/", "/tmp/", "/run/", "/var/lib/systemd/",
    "/var/lib/dnf/", "/var/lib/rpm/", "/usr/lib/sysimage/rpm/", "/var/lib/PackageKit/",
    "/var/lib/NetworkManager/", "/var/lib/sss/", "/var/lib/gdm/", "/var/lib/chrony/",
    "/var/lib/upower/", "/var/lib/fwupd/", "/var/lib/AccountsService/", "/var/lib/pacman/",
    "/var/lib/flatpak/repo/", "/var/spool/",
    "/root/.cache/", "/etc/.updated", "/var/.updated", "/etc/adjtime", "/etc/ld.so.cache",
    "/var/lib/flatpak/", "/var/lib/colord/", "/var/lib/lastlog/", "/var/lib/plymouth/", "/var/lib/abrt/",
    "/var/lib/rasdaemon/", "/var/lib/sddm/", "/usr/lib/sysimage/libdnf5/", "/boot/grub2/grubenv",
    "/boot/grub/grubenv", "/etc/cups/subscriptions.conf", "/etc/systemd/system.control/",
    # OpenWrt: desk's own log and package list (images/openwrt-*), apk's database
    "/desk/", "/lib/apk/db/",
    # Windows
    "/Windows/Prefetch/", "/Windows/System32/winevt/Logs/", "/Windows/Logs/", "/Windows/SoftwareDistribution/",
    "/Windows/ServiceProfiles/", "/Windows/System32/sru/", "/Windows/System32/Tasks/Microsoft/Windows/",
    "/ProgramData/Microsoft/Windows Defender/", "/ProgramData/Microsoft/EdgeUpdate/", "/ProgramData/USOPrivate/",
    "/ProgramData/USOShared/", "/ProgramData/Microsoft/Windows/WER/", "/$Extend/",
)
# Registry hive transaction logs change constantly; the hives themselves stay listed.
NOISE_RE = re.compile(r"^/Windows/System32/config/.*\.LOG\d*$|^/Users/[^/]+/(NTUSER\.DAT\.LOG\d*|ntuser\.dat\.LOG\d*)$|"
                      r"^/Users/[^/]+/AppData/Local/(Microsoft/(OneDrive|Windows/PowerShell|Windows/WebCache|Edge)|"
                      r"Packages/[^/]+/(AC|LocalCache|TempState))/")
NOISE_HOME = re.compile(r"^/home/[^/]+/\.(cache|local/state|local/share/(gnome-shell|recently-used\.xbel|tracker3|"
                        r"gvfs-metadata|keyrings|evolution|ibus-typing-booster)|mozilla|"
                        r"config/(pulse|dconf|ibus|evolution|user-dirs\.[a-z]+))(/|$)")
ENTRY = re.compile(r"^([+=-]) (\S) (\d{4})\s+(\d+) (/.*)$")

# Fed to `bash -s`, so every command gets </dev/null: tools that read a piped stdin
# (ausearch does) would otherwise swallow the rest of the script.
GUEST_SCRIPT = r"""
S="$1"
section() { echo "=== $1"; }
section failed;    systemctl --failed --no-legend --plain </dev/null 2>&1
# Units that failed at any time since the start, also before a reboot (the journal is persistent),
# system and user units alike: systemd's "unit failed" message.
section unitfailures
journalctl --since "@$S" --no-pager -o short-iso MESSAGE_ID=d9b373ed55a64feb8242e02dbe79a49c </dev/null 2>/dev/null \
    | grep -v '^-- ' | tail -n 200
# chronyd complains about the clock jump `desk up` makes when it restores a saved state.
section journal;   journalctl --since "@$S" -p warning --no-pager -o short-iso </dev/null 2>&1 \
    | grep -v -E 'chronyd\[[0-9]+\]: (Forward time jump|Backward time jump|Can.t synchronise)' | tail -n 400
section avc
if command -v ausearch >/dev/null; then
    ausearch --input-logs -m AVC,USER_AVC,SELINUX_ERR -ts $(date -d "@$S" '+%x %T') </dev/null 2>/dev/null \
        | grep -v '^<no matches>' | tail -n 300
fi
section coredumps; coredumpctl list --since "@$S" --no-pager --no-legend </dev/null 2>/dev/null
section packages
if command -v rpm >/dev/null; then
    rpm -qa --qf '%{INSTALLTIME} %{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}\n' </dev/null \
        | awk -v s="$S" '$1 >= s {print "installed " $2}' | sort
elif [ -f /var/log/pacman.log ]; then
    # [2026-10-05T03:12:44+0200] [ALPM] installed foo (1.0-1), in local time like this date.
    since=$(date -d "@$S" '+%FT%T')
    awk -v s="$since" '$2 == "[ALPM]" && substr($1, 2, 19) >= s && $3 ~ /^(installed|removed|upgraded|downgraded|reinstalled)$/ {
        $1 = $2 = ""; print substr($0, 3) }' /var/log/pacman.log </dev/null
fi
if command -v dnf >/dev/null; then
    # Removals leave no trace in rpm -qa; the transaction history has them.
    # dnf5 prints transaction times in UTC.
    dnf history list </dev/null 2>/dev/null | awk -v s="$(date -u -d "@$S" '+%F %T')" \
        'NR > 1 && match($0, /[0-9-]{10} [0-9:]{8}/) && substr($0, RSTART, RLENGTH) >= s'
fi
""" + "section browser\n" + browser.JOURNAL


# OpenWrt (BusyBox ash): procd's services, the system log (kept in a file across reboots by the
# image, else logd's buffer since the boot) and the packages compared with the list provisioning
# saved; turned into the Linux sections by openwrt_sections().
OPENWRT_LOG = "/desk/syslog"
OPENWRT_PACKAGES = "/desk/packages"
OPENWRT_SCRIPT = rf"""
section() {{ echo "=== $1"; }}
section services; ubus call service list </dev/null 2>/dev/null
section log
if [ -s {OPENWRT_LOG} ]; then cat {OPENWRT_LOG}; else logread </dev/null; fi
section packages
if [ -s {OPENWRT_PACKAGES} ]; then
    apk list -I </dev/null 2>/dev/null | cut -d' ' -f1 | sort > /tmp/desk-packages
    grep -vxF -f {OPENWRT_PACKAGES} /tmp/desk-packages | sed 's/^/installed /'
    grep -vxF -f /tmp/desk-packages {OPENWRT_PACKAGES} | sed 's/^/removed /'
fi
"""


# Windows: the same sections from the event log. Passed as one PowerShell command line.
WINDOWS_SCRIPT = r"""
$S = [DateTimeOffset]::FromUnixTimeSeconds(@S@).LocalDateTime
# Up to now as well: the image's log holds entries from Windows Setup stamped hours ahead (the
# hardware clock read in Setup's default time zone), which would otherwise show in every report.
$E = Get-Date
function Ev($filter) { $filter.EndTime = $E; Get-WinEvent -FilterHashtable $filter -ErrorAction SilentlyContinue }
function Line($e) {
    '{0:s} {1} [{2}] {3}' -f $e.TimeCreated, $e.ProviderName, $e.Id, (($e.Message -split "`n")[0]).Trim()
}
# A reboot kills sshd (shutdown /f) and the service manager then fails to restart it: noise when
# it happens in the last minute before a boot, a finding at any other time.
$boots = @(Ev @{LogName='System'; ProviderName='Microsoft-Windows-Kernel-General'; Id=12; StartTime=$S} |
           % { $_.TimeCreated })
function ShutdownNoise($e) {
    $e.ProviderName -eq 'Service Control Manager' -and $e.Id -in 7000, 7031, 7034 -and
    $e.Message -match 'OpenSSH SSH Server' -and
    @($boots | ? { $_ -gt $e.TimeCreated -and ($_ - $e.TimeCreated).TotalSeconds -lt 60 }).Count -gt 0
}
'=== failed'
$scm = 7000, 7001, 7009, 7011, 7022, 7023, 7024, 7031, 7034
Ev @{LogName='System'; ProviderName='Service Control Manager'; Id=$scm; StartTime=$S} |
    ? { -not (ShutdownNoise $_) } | % { Line $_ }
# The in-session agent's ends (images/_shared/windows/agent-loop.ps1), in the guest's local time.
Get-Content C:\desk\agent.log -ErrorAction SilentlyContinue | ? { $_ -match '^(\S+) ' -and [datetime]$Matches[1] -ge $S }
'=== journal'
Ev @{LogName='System','Application'; Level=1,2,3; StartTime=$S} | ? { -not (ShutdownNoise $_) } |
    Select-Object -First 400 | % { Line $_ }
'=== coredumps'
Ev @{LogName='Application'; Id=1000,1002; StartTime=$S} | % { Line $_ }
'=== packages'
Ev @{LogName='Application'; ProviderName='MsiInstaller'; Id=1033,1034,11707,11724; StartTime=$S} | % { Line $_ }
"""


@dataclass
class FileChanges:
    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    hidden: int = 0


def _noise(path: str) -> bool:
    # Directories are listed without a trailing slash; match them against prefixes too.
    return (path.startswith(NOISE) or (path + "/").startswith(NOISE) or bool(NOISE_HOME.match(path))
            or bool(NOISE_RE.match(path)))


def parse_virt_diff(text: str, all_paths: bool = False) -> FileChanges:
    """virt-diff prints `-`/`+` entry pairs for changed metadata, `=` for changed content,
    and lone `+`/`-` for added/removed files, followed by optional diff hunks."""
    minus, plus, equal, kinds = set(), set(), set(), {}
    for line in text.splitlines():
        m = ENTRY.match(line)
        if not m:
            continue
        sign, kind, _mode, _size, path = m.groups()
        kinds[path] = kind
        {"-": minus, "+": plus, "=": equal}[sign].add(path)
    out = FileChanges()
    changed = (minus & plus) | equal
    for bucket, paths in (("added", plus - minus), ("removed", minus - plus), ("changed", changed)):
        for path in sorted(paths):
            # A directory whose only change is its listing says nothing beyond its entries.
            if bucket == "changed" and kinds.get(path) == "d":
                continue
            if not all_paths and _noise(path):
                out.hidden += 1
                continue
            getattr(out, bucket).append(path + ("/" if kinds.get(path) == "d" else ""))
    return out


def _backing_of(disk: Path) -> Path:
    r = subprocess.run(["qemu-img", "info", "-U", "--output=json", str(disk)], capture_output=True, check=True,
                       timeout=60)
    backing = json.loads(r.stdout).get("full-backing-filename")
    if not backing:
        raise DeskError(f"{disk} has no backing image to compare with")
    return Path(backing)


def file_changes(run, all_paths: bool = False, raw_out: Path | None = None) -> FileChanges:
    """Files the run changed compared with the image it started from (via virt-diff, ~30 s)."""
    disk = run.dir / "disk.qcow2"
    copy = run.dir / ".diff.qcow2"
    dom = run.live_domain()
    if dom is not None:
        # QEMU holds a write lock on the overlay. Flush the guest, pause it for the instant a
        # reflink copy takes, and compare the copy.
        spec = load_image(run.image)
        guest.ssh_run(run.ssh_port, spec.root_user, spec.cmd(spec.sync), capture=True, timeout=60)
        dom.suspend()
        try:
            subprocess.run(["cp", "--reflink=always", str(disk), str(copy)], check=True, timeout=120)
        finally:
            dom.resume()
        disk = copy
    try:
        r = subprocess.run(["virt-diff", "-a", str(_backing_of(disk)), "-A", str(disk)],
                           capture_output=True, text=True, timeout=DIFF_TIMEOUT)
    finally:
        copy.unlink(missing_ok=True)
    if r.returncode != 0:
        raise DeskError(f"virt-diff failed: {r.stderr.strip()[-400:]}")
    if raw_out is not None:
        raw_out.write_text(r.stdout)
    return parse_virt_diff(r.stdout, all_paths)


def format_changes(ch: FileChanges, limit: int = 200) -> str:
    lines = []
    for sign, label, paths in (("+", "added", ch.added), ("~", "changed", ch.changed), ("-", "removed", ch.removed)):
        lines.extend(f"{sign} {p}" for p in paths[:limit])
        if len(paths) > limit:
            lines.append(f"  ... {len(paths) - limit} more {label}")
    lines.append(f"{len(ch.added)} added, {len(ch.changed)} changed, {len(ch.removed)} removed"
                 + (f"; {ch.hidden} routine paths hidden (--all to show)" if ch.hidden else ""))
    return "\n".join(lines)


def guest_problems(run) -> dict[str, str]:
    from .clock import guest_since, zone

    spec = load_image(run.image)
    since = guest_since(run)
    if spec.family == "windows":
        r = guest.ssh_run(run.ssh_port, spec.root_user, spec.cmd(WINDOWS_SCRIPT.replace("@S@", str(int(since)))),
                          capture=True, timeout=180)
    elif spec.family == "openwrt":
        r = guest.ssh_run(run.ssh_port, spec.root_user, "sh -s", input=OPENWRT_SCRIPT.encode(), capture=True,
                          timeout=120)
    else:
        r = guest.ssh_run(run.ssh_port, spec.root_user, f"bash -s -- {int(since)}",
                          input=GUEST_SCRIPT.encode(), capture=True, timeout=120)
    guest.connected(r, "collecting the guest's logs")
    sections, current = {}, None
    for line in r.stdout.decode(errors="replace").splitlines():
        if line.startswith("=== "):
            current = line[4:].strip()
            sections[current] = []
        elif current:
            sections[current].append(line)
    if not sections:
        # The script prints a heading for every section, found something or not.
        raise DeskError(f"collecting the guest's logs gave nothing (exit {r.returncode}): "
                        + (r.stderr.decode(errors="replace").strip()[-300:] or "no output"))
    out = {k: "\n".join(v).strip() for k, v in sections.items()}
    return openwrt_sections(out, since, zone(run)) if spec.family == "openwrt" else out


# Sat Oct 10 00:54:12 2026 daemon.warn odhcpd[2081]: text (logd, in the guest's local time)
SYSLOG = re.compile(r"^\w{3} (\w{3}) +(\d+) (\d\d:\d\d:\d\d) (\d{4}) ([a-z0-9]+)\.([a-z]+) (.*)$")
SEVERE = {"emerg", "alert", "crit", "err", "warn"}
# What every boot of the image in this VM logs (CPU features QEMU does not offer, no Wi-Fi, the
# LAN as a DHCP client) and procd at every `reboot`; a reboot during the run would otherwise fill
# the report with it.
OPENWRT_NOISE = re.compile(
    r"kernel: \[ *[\d.]+\] (Speculative Return Stack Overflow|intel_powerclamp: CPU does not support MWAIT|"
    r"amd_pstate: |Please enable it if your BIOS has the CPPC option|button_hotplug: loading out-of-tree module|"
    r"urandom-seed: Seeding with)|dnsmasq\[\d+\]: no servers found in /tmp/resolv\.conf\.d/|"
    r"netifd: Wireless module not found|netifd: You have delegated IPv6-prefixes but haven't assigned them|"
    r"procd: Got unexpected signal 1$")


def openwrt_sections(raw: dict[str, str], since: float, tz: tzinfo) -> dict[str, str]:
    """OpenWrt's sections in the Linux ones' terms: services procd keeps that are not running
    (failed), crash loops (unitfailures, before reboots too), warnings and errors of the log
    since `since` (journal, ISO times; logd writes the guest's local time, `tz`), segfaults
    (coredumps) and package changes."""
    failed = []
    try:
        services = json.loads(raw.get("services") or "{}")
    except ValueError:
        services = {}
    for name, info in sorted(services.items()):
        for inst, state in sorted((info.get("instances") or {}).items()):
            # One-shot services (urandom_seed) end with 0.
            if not state.get("running") and state.get("exit_code", 1) != 0:
                failed.append(f"{name}/{inst}: not running" + (f" (exit {state['exit_code']})" if "exit_code" in state
                                                                 else ""))
    journal, loops, crashes = [], [], []
    for line in (raw.get("log") or "").splitlines():
        m = SYSLOG.match(line)
        if not m:
            continue
        month, day, clock, year, facility, level, text = m.groups()
        when = datetime.strptime(f"{year} {month} {day} {clock}", "%Y %b %d %H:%M:%S").replace(tzinfo=tz)
        if when.timestamp() < since:
            continue
        stamped = f"{when.isoformat()} {facility}.{level} {text}"
        if level in SEVERE and not OPENWRT_NOISE.search(text):
            journal.append(stamped)
        if "in a crash loop" in text:
            loops.append(stamped)
        if "segfault at" in text:
            crashes.append(stamped)
    return {"failed": "\n".join(failed), "unitfailures": "\n".join(loops), "journal": "\n".join(journal[-400:]),
            "coredumps": "\n".join(crashes), "packages": raw.get("packages", "")}


# 2026-10-08T21:30:00+02:00 desk-fedora systemd[1]: foo.service: Failed with result 'exit-code'.
UNIT_FAILED = re.compile(r"^(\S+) \S+ systemd\[(\d+)\]: (\S+): Failed with result '([^']*)'")


def unit_failures(text: str) -> list[str]:
    """`TIME UNIT (result)` for each time a unit failed, user units marked as such."""
    out = []
    for line in text.splitlines():
        m = UNIT_FAILED.match(line)
        if m:
            stamp, pid, unit, result = m.groups()
            out.append(f"{stamp[11:19]} {unit}{' (user)' if pid != '1' else ''}: {result}")
    return out


# dnf5 prints its transaction times in UTC; the report's times are the guest's local ones.
DNF_TIME = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?!\d)")


def local_dnf_times(text: str, tz: tzinfo) -> str:
    def local(m: re.Match) -> str:
        utc = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        return utc.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")

    return "\n".join(DNF_TIME.sub(local, line) if not line.startswith("installed ") else line
                     for line in text.splitlines())


def _count(text: str, pattern: str | None = None) -> int:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return len([ln for ln in lines if re.search(pattern, ln)]) if pattern else len(lines)


def one_line(text: str, limit: int = 120) -> str:
    """A command or step as the report lists it: a script of many lines keeps its first words."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _step_labels(guest_times: list[float], events: list[dict]) -> list[str]:
    """The scenario step that was running (or had last run) at each guest time. Step events carry
    host times; the guest's clock differs by the offsets desk clock set along the way."""
    offset, starts = 0.0, []
    for ev in events:
        if "offset" in ev:
            offset = ev["offset"]
        elif ev.get("clock") == "reset":
            offset = 0.0
        if "step" in ev and "ok" in ev:
            starts.append((ev["t"] - ev.get("seconds", 0) + offset, one_line(f"step {ev['step']}: {ev['action']}", 60)))
    labels = []
    for when in guest_times:
        # Step durations are logged to a tenth of a second.
        current = [label for start, label in starts if start <= when + 0.1]
        labels.append(current[-1] if current else "before the first step")
    return labels


def _log(run) -> list[dict]:
    f = run.dir / "log.jsonl"
    if not f.exists():
        return []
    return [json.loads(line) for line in f.read_text().splitlines() if line.strip()]


def collect(run, diff: bool = False) -> Path:
    """Write REPORTS/<run>/report.md with the timeline, guest problems and optional file changes."""
    out = REPORTS / run.id
    if out.exists():
        out = REPORTS / f"{run.id}-{int(time.time())}"
    out.mkdir(parents=True)
    if (run.dir / "shots").is_dir():
        shutil.copytree(run.dir / "shots", out / "shots")
    for name in ("console.log", "log.jsonl"):
        if (run.dir / name).exists():
            shutil.copy(run.dir / name, out / name)

    problems: dict[str, str] = {}
    family = load_image(run.image).family
    live = run.live_domain() is not None
    if live:
        try:
            problems = guest_problems(run)
        except (DeskError, subprocess.TimeoutExpired) as e:
            problems = {"error": str(e)}
            print(f"[desk] warning: the report has no guest logs: {e}", file=sys.stderr)
        if family == "linux" and problems.get("packages"):
            from .clock import zone

            problems["packages"] = local_dnf_times(problems["packages"], zone(run))
        for key in ("journal", "avc", "packages"):
            if problems.get(key):
                (out / f"{key}.txt").write_text(problems[key] + "\n")
    changes = None
    if diff:
        try:
            changes = file_changes(run, raw_out=out / "virt-diff.txt")
            (out / "files.txt").write_text(format_changes(changes, limit=10**6) + "\n")
        except (DeskError, OSError, subprocess.SubprocessError) as e:
            # Without reflinks, virt-diff or in time: the rest of the report stands.
            problems["diff error"] = str(e)

    ended = time.time()
    md = [f"# desk report: run {run.id}", "",
          f"- image: `{run.image}`",
          f"- started: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(run.created))}, "
          f"reported: {time.strftime('%H:%M:%S', time.localtime(ended))} "
          f"({(ended - run.created) / 60:.0f} min)"]
    if run.ready:
        md.append(f"- booted cold: the image was provisioned in the run until "
                  f"{time.strftime('%H:%M:%S', time.localtime(run.ready))}; guest logs and package changes "
                  "count from then" + (", the list of changed files includes it" if changes is not None else ""))
    md.append("")
    events = _log(run)
    # Scenario steps; `desk clock --step` logged its step size as "step" before it had a name of its own.
    stepped = [ev for ev in events if "step" in ev and "ok" in ev]
    # The latest start: `scenario run --from N` goes on in the same run, maybe with a changed file.
    scenario = next((ev for ev in reversed(events) if "scenario" in ev), None)
    if scenario:
        # Each step's last attempt; the timeline below keeps every one.
        last = sorted({ev["step"]: ev for ev in stepped}.values(), key=lambda ev: ev["step"])
        failed = [ev for ev in last if not ev["ok"]]
        total = scenario.get("steps", len(last))
        md += ["## Scenario", "", f"- file: `{scenario['scenario']}`",
               f"- result: {'FAILED' if failed or len(last) < total else 'passed'}, "
               f"{len(last) - len(failed)} of {total} steps passed"]
        for ev in failed:
            first, _, rest = ev["detail"].partition("\n")
            md.append(f"- failed at step {ev['step']} ({one_line(ev['action'])}): {first}"
                      + (f"\n\n```\n{rest}\n```\n" if rest else ""))
        md.append("")
    md.append("## Found automatically")
    md.append("")
    if not live:
        md.append("- the VM was not running, so no guest logs were collected")
    elif "error" in problems:
        # Not "0 failed units": nothing was looked at.
        md.append(f"- NOT CHECKED: the guest's logs could not be collected ({problems['error']})")
    else:
        failed = problems.get("failed", "")
        openwrt = family == "openwrt"
        md.append(f"- {'services not running (procd)' if openwrt else 'failed units'}: {_count(failed)}"
                  + (f"\n\n```\n{failed}\n```\n" if failed else ""))
        history = (problems.get("unitfailures", "").splitlines() if openwrt
                   else unit_failures(problems.get("unitfailures", "")))
        if history:
            what = "service crash loops" if openwrt else "unit failures"
            md.append(f"- {what} during the run, before reboots too: {len(history)}\n\n```\n"
                      + "\n".join(history[-30:]) + "\n```\n")
        if family == "linux":
            denials = _count(problems.get("avc", ""), r"type=(AVC|USER_AVC|SELINUX_ERR)")
            md.append(f"- SELinux denials since start: {denials}" + (" (see avc.txt)" if problems.get("avc") else ""))
        cores = problems.get("coredumps", "")
        md.append(f"- crashes ({'segfaults in the log' if openwrt else 'coredumps'}): {_count(cores)}"
                  + (f"\n\n```\n{cores}\n```\n" if cores else ""))
        md.append(f"- warnings and errors in the system log: {_count(problems.get('journal', ''), r'^[0-9]{4}-')}"
                  + (" (see journal.txt)" if problems.get("journal") else ""))
        entries = browser.stamped(problems.get("browser", ""))
        if entries:
            web = [line for _, line in entries]
            errs = browser.errors(web)
            summary = f"{len(errs)} errors, {len(web)} messages"
            unknown = browser.unlevelled(web)
            if unknown:
                summary += f"; {browser.unlevelled_note(len(unknown))}"
            if stepped:
                # Which scenario step each message came in, so what setup (installing, logging in,
                # loading other pages) logged is told apart from what the steps under test did.
                labels = _step_labels([t for t, _ in entries], events)
                web = [f"{line[:8]} [{label}] {line[9:]}" for (_, line), label in zip(entries, labels)]
                errs = [w for w, (_, line) in zip(web, entries) if browser.ERROR.match(line[9:])]
                setup = sum(1 for e in errs if "[before the first step]" in e)
                if setup:
                    summary += f"; {setup} of the errors before the first step"
            (out / "browser-console.txt").write_text("\n".join(web) + "\n")
            md.append(f"- browser console: {summary} (see browser-console.txt)"
                      + ("\n\n```\n" + "\n".join(errs[:20]) + "\n```\n" if errs else ""))
        pkgs = problems.get("packages", "")
        md.append("- package changes:" + (f"\n\n```\n{pkgs}\n```\n" if pkgs else " none"))
    if "diff error" in problems:
        md.append(f"- changed files not listed: {problems['diff error']}")
    if changes is not None:
        md += ["", "## Files changed since the image", "", "```", format_changes(changes, limit=100), "```",
               "", "Full list in files.txt, raw virt-diff output in virt-diff.txt."]

    md += ["", "## Timeline", ""]
    if not events:
        md.append("(no desk commands were recorded)")
    for ev in events:
        offset = ev["t"] - run.created
        stamp = f"+{int(offset // 60):02d}:{int(offset % 60):02d}"
        if "argv" in ev:
            md.append(f"- {stamp} `{one_line('desk ' + ' '.join(ev['argv']))}`")
        elif "rc" in ev:
            if ev["rc"] != 0 and md and md[-1].startswith("- "):
                md[-1] += f" → exit {ev['rc']}"
        elif "step" in ev and "ok" in ev:
            mark = "ok" if ev["ok"] else "FAILED"
            detail = ev["detail"].split("\n", 1)[0]
            md.append(f"- {stamp} step {ev['step']} {mark}: {one_line(ev['action'])} ({ev['seconds']} s)"
                      + (f" → {detail}" if detail else ""))
        elif "clock" in ev:
            step = ev.get("clock_step") or (ev.get("step") if "ok" not in ev else None)
            md.append(f"- {stamp} clock {ev['clock']}" + (f" in steps of {step}" if step else ""))
        elif "snapshot" in ev:
            md.append(f"- {stamp} snapshot {ev['snapshot']}d: {ev['name']}")
        elif "gc" in ev:
            md.append(f"- {stamp} {ev['gc']}")
        elif ev.get("record") == "start":
            md.append(f"- {stamp} recording started ({ev.get('fps')} fps)")
        elif ev.get("record") == "stop" and ev.get("video"):
            md.append(f"- {stamp} recording stopped: [{ev['video']}]({ev['video']}) ({ev['seconds']} s)")
        elif "shot" in ev:
            name = Path(ev["shot"]).name
            if (out / "shots" / name).exists():
                md.append(f"\n  ![{name}](shots/{name})\n")
    (out / "report.md").write_text("\n".join(md) + "\n")
    return out


def reported(run) -> bool:
    """A report of this run exists (not of an earlier run of the same name, nor the directory of
    one that could not be written)."""
    try:
        return (REPORTS / run.id / "report.md").stat().st_mtime >= run.created
    except OSError:
        return False


@contextmanager
def _teardown(run):
    """One command at a time takes a run down: a second `desk down` of it (the first one's caller
    gave up waiting for a video), or gc and its owner at once, would encode and report it twice."""
    try:
        fd = os.open(run.dir / ".teardown", os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        raise DeskError(f"run {run.id} is gone already") from None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError:
        raise DeskError(f"run {run.id} is being taken down by another desk command") from None
    finally:
        os.close(fd)


def finish(run, keep: bool = False, diff: bool = False, report: bool = True,
           running: bool = False, stopped: bool = False, unless=None) -> tuple[Path | None, list[Path]]:
    """Stop recording, write the report, keep the videos with it and destroy the run (unless
    `running`: a scenario run kept for testing by hand). `stopped`: report on a run whose VM is
    gone too, from what its directory holds. `unless`: asked just before the run is destroyed;
    true leaves it as it is (gc, when its owner came back meanwhile)."""
    from . import record, runs

    with _teardown(run):
        try:
            record.stop(run)
        except Exception as e:  # noqa: BLE001 - a video that cannot be made must not keep the VM running
            print(f"[desk] warning: no video ({type(e).__name__}: {e})", file=sys.stderr)
        out = None
        if report and (stopped or run.live_domain() is not None):
            # A report that cannot be written must not keep the VM running.
            try:
                out = collect(run, diff=diff)
            except Exception as e:  # noqa: BLE001
                print(f"[desk] warning: no report ({type(e).__name__}: {e})", file=sys.stderr)
        # Videos outlive the run in its report directory, report or not.
        try:
            videos = record.save_videos(run, out or REPORTS / run.id)
        except OSError as e:
            videos = []
            print(f"[desk] warning: videos not kept ({e})", file=sys.stderr)
        if not running:
            # Like starting one: no other command picks ports or looks for orphans meanwhile. Only
            # here, so that nobody waits for a report to be written.
            with runs.state_lock():
                if not (unless and unless()):
                    runs.down(run, keep=keep)
    return out, videos


def prune_reports(verbose: bool = True) -> None:
    if not REPORTS.exists():
        return
    cutoff = time.time() - REPORT_RETENTION_DAYS * 86400
    old = []
    for d in REPORTS.iterdir():
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                old.append(d)
        except FileNotFoundError:
            continue  # removed meanwhile by another desk's gc
    for d in old:
        shutil.rmtree(d, ignore_errors=True)
    if old and verbose:
        print(f"[desk] gc: removed {len(old)} reports older than {REPORT_RETENTION_DAYS} days")
