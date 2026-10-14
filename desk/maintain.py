"""Unattended upkeep of the golden images (`desk maintain`, run weekly by a systemd user timer).

An image family (a base image and its variants) gets a new install when its base is
`rebuild_days` old, when its install definition changed or when its licence is about to run out,
and new warm states when their definition changed. New builds are tested with the selftest
next to the current ones and become current only if every member of the family passes;
otherwise the current builds stay and the failure is recorded for `desk image list` and
`desk up` to show.
"""

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import traceback
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path

from . import images, runs
from .config import BASE, HOME, PROJECT, URI, WARM, DeskError, ImageSpec, list_images, load_image

MAINTAIN = HOME / "maintain"
STATUS = MAINTAIN / "status.json"
# A licence closer to its end than this gets the image reinstalled (the timer runs weekly).
RENEW_DAYS = 14
# `desk up` starts warning this long before the licence ends.
WARN_DAYS = 21
# The weekly timer would otherwise find a build made at 03:10 a few minutes short of its age.
SLACK_DAYS = 1
KEEP_REPORTS = 20
# How long a selftest waits for other VMs to give memory back before the image is skipped.
MEMORY_WAIT = 600


def _log(msg: str) -> None:
    print(f"[desk] {msg}", flush=True)


def load_status() -> dict:
    try:
        return json.loads(STATUS.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def record(image: str, key: str, **data) -> None:
    """Remember the latest selftest or maintenance outcome of an image."""
    MAINTAIN.mkdir(parents=True, exist_ok=True)
    with open(MAINTAIN / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        status = load_status()
        status.setdefault(image, {})[key] = {"t": time.time(), **data}
        tmp = STATUS.with_name(f".status.{os.getpid()}")
        tmp.write_text(json.dumps(status, indent=2, ensure_ascii=False))
        os.replace(tmp, STATUS)


def days_left(spec: ImageSpec) -> float | None:
    expires = runs.warm_meta(spec).get("expires")
    return None if expires is None else (expires - time.time()) / 86400


def check_usable(spec: ImageSpec) -> list[str]:
    """Warnings for `desk up`; raises when the image can no longer be used."""
    warnings = []
    left = days_left(spec)
    if left is not None and left <= 0:
        raise DeskError(f"the licence of {spec.name} ran out {-left:.0f} days ago; the guest shuts itself down "
                        f"hourly. Reinstall it: desk maintain --only {spec.name} (or desk image build --force)")
    if left is not None and left < WARN_DAYS:
        warnings.append(f"the licence of {spec.name} runs out in {left:.0f} days (desk maintain reinstalls it)")
    last = load_status().get(spec.name, {}).get("maintain")
    # A warm state made after the failure (e.g. a manual rebuild) supersedes it.
    if last and last["ok"] is False and last["t"] > runs.warm_meta(spec).get("created", 0):
        when = time.strftime("%Y-%m-%d", time.localtime(last["t"]))
        warnings.append(f"the last maintenance of {spec.name} failed on {when}: {last['note']} "
                        f"(desk image list; report {last.get('report', '-')})")
    return warnings


def base_reasons(spec: ImageSpec) -> list[str]:
    cur = images.current(BASE / spec.name)
    if cur is None:
        return ["not built"]
    meta_file = cur / "meta.json"
    meta = json.loads(meta_file.read_text()) if meta_file.exists() else {}
    reasons = []
    if meta.get("fingerprint") != images.install_fingerprint(spec):
        reasons.append("install definition changed")
    age = (time.time() - meta.get("created", (cur / "disk.qcow2").stat().st_mtime)) / 86400
    if spec.rebuild_days and age >= spec.rebuild_days - SLACK_DAYS:
        reasons.append(f"base is {age:.0f} days old")
    left = days_left(spec)
    if spec.expiry and left is not None and left < RENEW_DAYS:
        reasons.append(f"licence runs out in {left:.0f} days")
    return reasons


def warm_reasons(spec: ImageSpec) -> list[str]:
    cur = images.current(WARM / spec.name)
    if cur is None or not (cur / "memory.save").exists():
        return ["no warm state"]
    meta = runs.warm_meta(spec)
    reasons = []
    if meta.get("base") != str(spec.base_dir.resolve()):
        reasons.append("base build changed")
    if meta.get("fingerprint") != images.warm_fingerprint(spec):
        reasons.append("image definition changed")
    return reasons


def families() -> dict[str, list[str]]:
    specs = {n: load_image(n) for n in list_images()}
    return {n: [n] + [v for v, s in specs.items() if s.base == n] for n, s in specs.items() if not s.base}


def selected(base: str, members: list[str], only: str | None) -> list[str]:
    """The members of a family that maintenance looks after: those the host has a warm state of,
    and the one `--only` names. A host keeps the images it tests on; an image nobody built or
    warmed here is not installed because a timer went off. [] leaves the family alone."""
    chosen = [m for m in members if m == only or images.current(WARM / m) is not None]
    if images.current(BASE / base) is None and only not in members:
        return []
    return chosen


def _family(base: str, members: list[str], out: Path, dry_run: bool) -> list[str]:
    """Maintain one family; returns report lines. Raises when it failed."""
    from .selftest import run_selftest

    def decide() -> tuple[list[str], dict[str, list[str]], list[str]]:
        reasons = base_reasons(specs[base])
        warm = {m: warm_reasons(specs[m]) for m in members}
        lines = [f"- {base}: new install ({', '.join(reasons)})"] if reasons else []
        lines += [f"- {m}: new warm state ({', '.join(warm[m])})" for m in members if warm[m] and not reasons]
        if not reasons and not any(warm.values()):
            lines.append(f"- {', '.join(members)}: up to date, selftest only")
        return reasons, warm, lines

    # The base's definition and lock too when only a variant of it is looked after.
    specs = {m: load_image(m) for m in dict.fromkeys([base, *members])}
    if dry_run:
        return decide()[2]
    with ExitStack() as stack:
        for m in specs:
            stack.enter_context(runs.image_lock(m))
        reasons, plan, lines = decide()
        runs.check_memory(specs[base])
        new_base = images.build(specs[base], promote_it=False) if reasons else None
        tested: dict[str, ImageSpec] = {}
        for m in members:
            spec = replace(specs[m], base_version=new_base) if new_base else specs[m]
            if new_base or plan[m]:
                try:
                    spec = replace(spec, warm_version=images.warm(spec, promote_it=False))
                except BaseException:
                    _keep_screens(m, out / m)
                    raise
            # Other runs may have taken the memory since the family started: wait for it rather
            # than fail the selftest (and with it the new build) for a reason that is not the image's.
            _wait_for_memory(spec)
            if not run_selftest(m, reboot=True, spec=spec, shots_dir=out / m):
                raise DeskError(f"selftest of {m} failed" + (" on the new build" if spec.warm_version else ""))
            tested[m] = spec
        if new_base:
            images.promote(BASE / base, replace(specs[base], base_version=new_base).base_dir)
        for m, spec in tested.items():
            if spec.warm_version:
                images.promote(WARM / m, spec.warm_dir)
        if new_base or any(s.warm_version for s in tested.values()):
            lines.append("  new builds are current")
    return lines


def _wait_for_memory(spec: ImageSpec, timeout: float = MEMORY_WAIT) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            runs.check_memory(spec)
            return
        except DeskError:
            if time.monotonic() > deadline:
                raise  # "not enough free memory": reported as skipped
            time.sleep(30)


def _keep_screens(image: str, dest: Path) -> None:
    """The failed warm-up's screenshots and console, before `prune_versions` deletes them."""
    attempts = images.versions(WARM, image)
    if attempts and attempts[-1] != images.current(WARM / image):
        dest.mkdir(parents=True, exist_ok=True)
        for f in [*attempts[-1].glob("*.png"), *attempts[-1].glob("console.log")]:
            shutil.copy(f, dest / f"warm-{f.name}")


def maintain(only: str | None = None, dry_run: bool = False) -> bool:
    # systemd stops the unit with SIGTERM; as SystemExit it unwinds through the cleanups that
    # destroy a half-made build or warm-up domain.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = MAINTAIN / stamp
    if not dry_run:
        out.mkdir(parents=True, exist_ok=True)
    report = [f"# desk maintain {time.strftime('%Y-%m-%d %H:%M')}", ""]
    all_ok = True
    fams = families()
    if only and not any(only in members for members in fams.values()):
        raise DeskError(f"unknown image {only!r}")
    for base, family in fams.items():
        if only and only not in family:
            continue
        members = selected(base, family, only)
        if not members:
            line = f"- {base}: not in use on this host, left alone (desk maintain --only {base} builds it)"
            report += [line, ""]
            _log(line)
            continue
        t0 = time.monotonic()
        try:
            lines = _family(base, members, out, dry_run)
            ok, note = True, "ok"
        except DeskError as e:
            lines, ok, note = [f"- {base}: {e}"], False, str(e)
        except Exception as e:  # noqa: BLE001 - one broken family must not stop the others
            (out / f"{base}.traceback").write_text(traceback.format_exc())
            lines, ok, note = [f"- {base}: {type(e).__name__}: {e} (see {base}.traceback)"], False, str(e)
        skipped = "not enough free memory" in note
        # Skipped is not success either: the systemd unit shows it as failed.
        all_ok &= ok
        result = "SKIPPED" if skipped else "done" if ok else "FAILED"
        report += lines + [f"  {result} in {(time.monotonic() - t0) / 60:.0f} min", ""]
        for line in lines:
            _log(line.strip())
        if not dry_run:
            for m in members:
                previous = load_status().get(m, {}).get("maintain")
                if skipped and previous and previous["ok"] is False:
                    continue  # keep showing the earlier failure
                record(m, "maintain", ok=None if skipped else ok, note=note, report=str(out / "report.md"))
    if dry_run:
        return all_ok
    with runs.state_lock():
        report += [f"gc: removed {p}" for p in runs.prune_versions(verbose=False)]
    (out / "report.md").write_text("\n".join(report) + "\n")
    _log(f"report: {out / 'report.md'}")
    for old in sorted(p for p in MAINTAIN.iterdir() if p.is_dir())[:-KEEP_REPORTS]:
        shutil.rmtree(old, ignore_errors=True)
    return all_ok


SERVICE = """[Unit]
Description=desk: rebuild and selftest the golden images

[Service]
Type=oneshot
ExecStart="{desk}" maintain
{environment}Nice=10
# Builds time out on their own (3 h each); this catches anything else that hangs, so the next
# week's run is not blocked by this one.
TimeoutStartSec=12h
"""

TIMER = """[Unit]
Description=desk: weekly maintenance of the golden images

[Timer]
# The night from Sunday to Monday; a run missed while the host was off happens at the next boot.
OnCalendar=Mon *-*-* 03:00:00
Persistent=true

[Install]
WantedBy=timers.target
"""


def install_timer() -> None:
    units = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user"
    units.mkdir(parents=True, exist_ok=True)
    # As resolved here, from the environment or the host's config file: the unit has neither
    # this shell's variables nor, necessarily, its XDG directories.
    settings = {"DESK_HOME": str(HOME), "DESK_URI": URI, **{k: os.environ[k] for k in ("DESK_TTL",) if k in os.environ}}
    env = "".join(f'Environment="{k}={v}"\n' for k, v in settings.items())
    (units / "desk-maintain.service").write_text(SERVICE.format(desk=PROJECT / "bin" / "desk", environment=env))
    (units / "desk-maintain.timer").write_text(TIMER)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", "desk-maintain.timer"], check=True)
    subprocess.run(["systemctl", "--user", "list-timers", "desk-maintain.timer", "--no-pager"], check=False,
                   stdout=sys.stdout)
    linger = subprocess.run(["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger", "--value"],
                            capture_output=True, text=True).stdout.strip()
    if linger != "yes":
        print("warning: lingering is off, so the timer only runs while you are logged in "
              "(sudo loginctl enable-linger $USER)")
