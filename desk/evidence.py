"""What a run left behind: guest problems since it started, file changes, a timeline report."""

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import guest
from .config import HOME, DeskError, load_image

REPORTS = HOME / "reports"
REPORT_RETENTION_DAYS = 14

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
'=== failed'
$scm = 7000, 7001, 7009, 7011, 7022, 7023, 7024, 7031, 7034
Ev @{LogName='System'; ProviderName='Service Control Manager'; Id=$scm; StartTime=$S} | % { Line $_ }
'=== journal'
Ev @{LogName='System','Application'; Level=1,2,3; StartTime=$S} | Select-Object -First 400 | % { Line $_ }
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
    r = subprocess.run(["qemu-img", "info", "-U", "--output=json", str(disk)], capture_output=True, check=True)
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
            subprocess.run(["cp", "--reflink=always", str(disk), str(copy)], check=True)
        finally:
            dom.resume()
        disk = copy
    try:
        r = subprocess.run(["virt-diff", "-a", str(_backing_of(disk)), "-A", str(disk)],
                           capture_output=True, text=True)
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
    spec = load_image(run.image)
    if spec.family == "windows":
        r = guest.ssh_run(run.ssh_port, spec.root_user, spec.cmd(WINDOWS_SCRIPT.replace("@S@", str(int(run.created)))),
                          capture=True, timeout=180)
    else:
        r = guest.ssh_run(run.ssh_port, spec.root_user, f"bash -s -- {int(run.created)}",
                          input=GUEST_SCRIPT.encode(), capture=True, timeout=120)
    sections, current = {}, None
    for line in r.stdout.decode(errors="replace").splitlines():
        if line.startswith("=== "):
            current = line[4:].strip()
            sections[current] = []
        elif current:
            sections[current].append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def _count(text: str, pattern: str | None = None) -> int:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return len([ln for ln in lines if re.search(pattern, ln)]) if pattern else len(lines)


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
    live = run.live_domain() is not None
    if live:
        try:
            problems = guest_problems(run)
        except (DeskError, subprocess.TimeoutExpired) as e:
            problems = {"error": str(e)}
        for key in ("journal", "avc", "packages"):
            if problems.get(key):
                (out / f"{key}.txt").write_text(problems[key] + "\n")
    changes = None
    if diff:
        try:
            changes = file_changes(run, raw_out=out / "virt-diff.txt")
            (out / "files.txt").write_text(format_changes(changes, limit=10**6) + "\n")
        except DeskError as e:
            problems["diff error"] = str(e)

    ended = time.time()
    md = [f"# desk report: run {run.id}", "",
          f"- image: `{run.image}`",
          f"- started: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(run.created))}, "
          f"reported: {time.strftime('%H:%M:%S', time.localtime(ended))} "
          f"({(ended - run.created) / 60:.0f} min)", ""]
    md.append("## Found automatically")
    md.append("")
    if not live:
        md.append("- the VM was not running, so no guest logs were collected")
    else:
        failed = problems.get("failed", "")
        md.append(f"- failed units: {_count(failed)}" + (f"\n\n```\n{failed}\n```\n" if failed else ""))
        if load_image(run.image).family != "windows":
            denials = _count(problems.get("avc", ""), r"type=(AVC|USER_AVC|SELINUX_ERR)")
            md.append(f"- SELinux denials since start: {denials}" + (" (see avc.txt)" if problems.get("avc") else ""))
        cores = problems.get("coredumps", "")
        md.append(f"- crashes (coredumps): {_count(cores)}" + (f"\n\n```\n{cores}\n```\n" if cores else ""))
        md.append(f"- warnings and errors in the system log: {_count(problems.get('journal', ''), r'^[0-9]{4}-')}"
                  + (" (see journal.txt)" if problems.get("journal") else ""))
        pkgs = problems.get("packages", "")
        md.append("- package changes:" + (f"\n\n```\n{pkgs}\n```\n" if pkgs else " none"))
    if "error" in problems or "diff error" in problems:
        md.append(f"- collection problems: {problems.get('error', '')} {problems.get('diff error', '')}".rstrip())
    if changes is not None:
        md += ["", "## Files changed since the image", "", "```", format_changes(changes, limit=100), "```",
               "", "Full list in files.txt, raw virt-diff output in virt-diff.txt."]

    md += ["", "## Timeline", ""]
    events = _log(run)
    if not events:
        md.append("(no desk commands were recorded)")
    for ev in events:
        offset = ev["t"] - run.created
        stamp = f"+{int(offset // 60):02d}:{int(offset % 60):02d}"
        if "argv" in ev:
            md.append(f"- {stamp} `desk {' '.join(ev['argv'])}`")
        elif "rc" in ev:
            if ev["rc"] != 0 and md and md[-1].startswith("- "):
                md[-1] += f" → exit {ev['rc']}"
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


def prune_reports(verbose: bool = True) -> None:
    if not REPORTS.exists():
        return
    cutoff = time.time() - REPORT_RETENTION_DAYS * 86400
    for d in REPORTS.iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            if verbose:
                print(f"[desk] gc: removed report {d.name}")
