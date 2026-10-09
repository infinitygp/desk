"""Repeatable end-to-end tests: a TOML file in the project's repository builds the product on the
host, starts a run and drives it step by step with the same real input as the desk commands.
The result is an exit code and the usual report, with the steps in its timeline."""

import glob
import shlex
import subprocess
import time
import tomllib
from pathlib import Path

from . import a11y, browser, clock, disks, display, evidence, guest, record, runs
from .config import DeskError, load_image
from .steps import Steps

# Exactly one per step; the value is the action's argument.
ACTIONS = {"root", "exec", "gui", "focus", "click", "type", "key", "expect", "expect_absent", "shot",
           "reboot", "wait", "clock", "disk_add", "disk_rm", "console_errors", "prepare"}
OPTIONS = {"role", "exact", "timeout", "output", "rc", "name", "soft", "env", "step", "until", "anywhere"}
# Where build artifacts land in the guest.
GUEST_IN = {"linux": "/tmp/desk", "windows": "C:/Users/tester/desk"}


class StepFailed(Exception):
    pass


def split_windows(command: str) -> list[str]:
    """Split a command line the way Windows programs do (CommandLineToArgvW): spaces separate
    unless quoted, and backslashes are literal except before a quote, so paths stay intact."""
    args, cur, quoted, have, i = [], [], False, False, 0
    while i < len(command):
        c = command[i]
        if c == "\\":
            n = 0
            while i < len(command) and command[i] == "\\":
                n += 1
                i += 1
            if i < len(command) and command[i] == '"':
                cur.append("\\" * (n // 2))
                if n % 2:
                    cur.append('"')
                    i += 1
            else:
                cur.append("\\" * n)
            have = True
            continue
        if c == '"':
            quoted, have = not quoted, True
        elif c in " \t" and not quoted:
            if have:
                args.append("".join(cur))
                cur, have = [], False
        else:
            cur.append(c)
            have = True
        i += 1
    if have:
        args.append("".join(cur))
    return args


def load(path: Path) -> dict:
    with path.open("rb") as f:
        data = tomllib.load(f)
    unknown = set(data) - {"image", "record", "build", "disks", "step"}
    if unknown:
        raise DeskError(f"{path}: unknown keys {sorted(unknown)}")
    if "image" not in data:
        raise DeskError(f"{path}: no image")
    if not isinstance(data.get("disks", []), list) or not all(isinstance(d, str) for d in data.get("disks", [])):
        raise DeskError(f"{path}: disks is a list like [\"8G:data\"]")
    build = data.get("build", {})
    if set(build) - {"run", "artifacts"}:
        raise DeskError(f"{path}: [build] takes run and artifacts")
    for i, step in enumerate(data.get("step", []), 1):
        actions = set(step) & ACTIONS
        if len(actions) != 1 or set(step) - ACTIONS - OPTIONS:
            raise DeskError(f"{path}: step {i} needs exactly one of {sorted(ACTIONS)}, "
                            f"plus any of {sorted(OPTIONS)} (got {sorted(step)})")
        if "until" in step and not actions & {"exec", "root"}:
            raise DeskError(f"{path}: step {i}: until goes with exec or root")
    return data


def describe(step: dict) -> tuple[str, str]:
    action = next(k for k in step if k in ACTIONS)
    value = step[action]
    return action, value if isinstance(value, str) else " ".join(map(str, value)) if isinstance(value, list) else ""


def _repo(path: Path) -> Path:
    r = subprocess.run(["git", "-C", str(path.parent), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(r.stdout.strip()) if r.returncode == 0 else path.parent


def _build(data: dict, repo: Path) -> list[Path]:
    build = data.get("build", {})
    if build.get("run"):
        print(f"build: {build['run']}", flush=True)
        if subprocess.run(build["run"], shell=True, cwd=repo).returncode != 0:
            raise DeskError(f"build failed: {build['run']}")
    files = []
    for pattern in build.get("artifacts", []):
        hits = [Path(p) for p in glob.glob(str(repo / pattern))]
        if not hits:
            raise DeskError(f"no build artifact matches {pattern}")
        # Builds leave older versions around; the one just built is the newest.
        files.append(max(hits, key=lambda p: p.stat().st_mtime))
    return files


def tail(text: str, lines: int = 15, chars: int = 1500) -> str:
    """The end of a command's output, where the cause of a failure usually is."""
    return "\n".join(text.strip().splitlines()[-lines:])[-chars:]


def _shell(run, spec, step: dict, as_root: bool) -> str:
    command = step["root" if as_root else "exec"]
    user = spec.root_user if as_root else run.user
    # until: the same command every 2 s until it does what the step expects, e.g. a service is up.
    limit = step.get("timeout", 120 if step.get("until") else 600)
    deadline = time.monotonic() + limit
    while True:
        try:
            r = guest.ssh_run(run.ssh_port, user, spec.cmd(command), capture=True,
                              timeout=max(5, deadline - time.monotonic()) if step.get("until") else limit)
        except subprocess.TimeoutExpired:
            if not step.get("until"):
                raise
            r = subprocess.CompletedProcess([], 124, b"", b"(the command itself did not finish)")
        out, err = r.stdout.decode(errors="replace"), r.stderr.decode(errors="replace")
        problem = None
        if r.returncode != step.get("rc", 0):
            problem = f"exit {r.returncode}"
        elif "output" in step and step["output"] not in out:
            problem = f"output lacks {step['output']!r}"
        if problem is None:
            return out.strip().splitlines()[-1][:120] if out.strip() else ""
        if not step.get("until") or time.monotonic() + 2 > deadline:
            break
        time.sleep(2)
    if step.get("until"):
        problem += f" (still, after {limit:.0f}s)"
    shown = [problem]
    if out.strip():
        shown.append("stdout:\n" + tail(out))
    if err.strip():
        shown.append("stderr:\n" + tail(err))
    raise StepFailed("\n".join(shown))


def _step(run, spec, steps: Steps, step: dict) -> str:
    action, _ = describe(step)
    value, role, timeout = step[action], step.get("role"), step.get("timeout")
    if action in ("root", "exec"):
        return _shell(run, spec, step, action == "root")
    if action == "gui":
        if isinstance(value, list):
            argv = value
        else:
            argv = split_windows(value) if spec.family == "windows" else shlex.split(value)
        steps.gui(*argv, env={k: str(v) for k, v in step.get("env", {}).items()})
    elif action == "focus":
        return steps.focus(value)
    elif action == "click":
        steps.click(value, role, step.get("exact", True))
    elif action == "type":
        steps.type(value, anywhere=step.get("anywhere", False))
    elif action == "key":
        steps.key(*([value] if isinstance(value, str) else value))
    elif action == "expect":
        node = steps.wait_for(value, role, step.get("exact", False), timeout or 30)
        return a11y.format_node(node, indent=False)
    elif action == "expect_absent":
        # Absent while the page is still loading proves nothing; at least let the screen settle.
        steps.settle()
        deadline = time.monotonic() + (timeout or 30)
        while a11y.find(run, value, role, step.get("exact", False), save=False):
            if time.monotonic() > deadline:
                raise StepFailed(f"{value!r} is still on screen")
            time.sleep(1)
    elif action == "shot":
        png, _ = display.wait_stable(run.dom(), timeout=3)
        path = run.next_shot_path(value)
        path.write_bytes(png)
        run.log_event(shot=str(path))
        return str(path)
    elif action == "reboot":
        # true: an ordinary reboot; a string: the command that reboots (dnf offline reboot -y).
        took, _ = guest.reboot(run.ssh_port, spec, timeout=timeout or 300,
                               command=value if isinstance(value, str) else None)
        if run.clock_offset:
            clock.apply(run)
        return f"back in {took:.0f}s"
    elif action == "prepare":
        from .steps import run_prepare

        hook = "prepare" if value is True else value
        run_prepare(spec, run, hook)
        return f"{hook} done"
    elif action == "disk_add":
        size, _, name = value.partition(":")
        return f"disk {disks.add(run, size, name or None)} plugged in"
    elif action == "disk_rm":
        disks.remove(run, value, timeout=timeout or 30)
        return f"disk {value} unplugged"
    elif action == "console_errors":
        # New errors in Firefox's web console since the scenario started or the previous check.
        found = browser.errors(browser.console(run))
        new, steps.console_seen = found[steps.console_seen:], len(found)
        if len(new) > value:
            raise StepFailed(f"{len(new)} console errors (at most {value}): " + " | ".join(new[:3])[:300])
        return f"{len(new)} console errors"
    elif action == "clock":
        if value == "reset":
            clock.reset(run)
        elif step.get("step"):
            ran = [u for _, started in clock.advance(run, value, step["step"], timeout=timeout or 600) for u in started]
            return f"{clock.describe(run)}; started: {', '.join(ran) or 'nothing'}"[:300]
        else:
            clock.set_time(run, value)
        return clock.describe(run)
    elif action == "wait":
        if value is True:
            steps.settle(timeout or 10)
        else:
            time.sleep(float(value))
    return ""


def run_scenario(path: Path, keep: bool = False, image: str | None = None) -> bool:
    data = load(path)
    spec = load_image(image or data["image"])
    artifacts = _build(data, _repo(path))
    agent, _ = runs.agent_process()
    run = runs.up(spec, label=f"scenario {_repo(path).name}/{path.name}" + (f" ({agent})" if agent else ""))
    print(f"run {run.id} ({spec.name})", flush=True)
    ok = True
    try:
        if data.get("record"):
            record.start(run)
        run.log_event(scenario=str(path.resolve()), steps=len(data.get("step", [])))
        if artifacts:
            dest = GUEST_IN[spec.family]
            guest.ssh_run(run.ssh_port, run.user, spec.cmd(f"mkdir -p {dest}" if spec.family == "linux"
                          else f"New-Item -ItemType Directory -Force {dest} | Out-Null"), capture=True)
            for f in artifacts:
                if guest.scp(run.ssh_port, str(f), f"{run.user}@127.0.0.1:{dest}/{f.name}") != 0:
                    raise DeskError(f"copying {f.name} to the guest failed")
        for d in data.get("disks", []):
            size, _, name = d.partition(":")
            disks.add(run, size, name or None)
        steps = Steps(spec, run)
        for i, step in enumerate(data.get("step", []), 1):
            action, arg = describe(step)
            label = step.get("name") or f"{action} {arg}".strip()
            t0 = time.monotonic()
            try:
                detail = _step(run, spec, steps, step)
                passed = True
            except (StepFailed, DeskError, subprocess.TimeoutExpired) as e:
                detail, passed = str(e), False
            run.log_event(step=i, action=label, ok=passed, detail=detail, seconds=round(time.monotonic() - t0, 1))
            first, *rest = detail.splitlines() or [""]
            print(f"{'PASS' if passed else 'FAIL'}  {i:>2}. {label[:60]:<60} {time.monotonic() - t0:5.1f}s  "
                  f"{first[:100]}", flush=True)
            if not passed and rest:
                # A command's error is usually at the end of its output.
                print("\n".join(f"        {line}" for line in rest), flush=True)
            if not passed:
                ok = False
                try:
                    shot = run.next_shot_path(f"failed-step-{i}")
                    shot.write_bytes(display.screenshot(run.dom()))
                    run.log_event(shot=str(shot))
                except DeskError:
                    pass  # the VM itself is gone; the report says so
                # soft: an independent check whose failure should not hide the ones after it.
                if not step.get("soft"):
                    break
    finally:
        report, videos = evidence.finish(run, keep=keep, running=keep)
        if report:
            print(f"report: {report / 'report.md'}")
        for v in videos:
            print(f"video: {v}")
    print("scenario passed" if ok else "scenario FAILED")
    if keep and run.live_domain() is not None:
        print(f"run {run.id} left running: export DESK_RUN={run.id} (fish: set -x DESK_RUN {run.id}); "
              f"desk down {run.id} when done")
    return ok
