"""Repeatable end-to-end tests: a TOML file in the project's repository builds the product on the
host, starts a run and drives it step by step with the same real input as the desk commands.
The result is an exit code and the usual report, with the steps in its timeline."""

import glob
import re
import shlex
import subprocess
import time
import tomllib
from datetime import UTC
from pathlib import Path

from . import a11y, browser, clock, disks, display, evidence, guest, record, runs, usb
from .config import DeskError, load_image
from .steps import Steps

# Exactly one per step; the value is the action's argument.
ACTIONS = {"root", "exec", "gui", "focus", "click", "type", "key", "expect", "expect_absent", "shot",
           "reboot", "wait", "clock", "disk_add", "disk_rm", "console_errors", "prepare", "open", "viewport",
           "observe_start", "observe_stop", "mark"}
OPTIONS = {"role", "exact", "timeout", "output", "rc", "name", "soft", "env", "step", "until", "anywhere", "browser",
           "button", "window"}
# The options each action reads, besides name and soft; any other would be ignored without a word.
READS = {
    "root": {"timeout", "output", "rc", "until"}, "exec": {"timeout", "output", "rc", "until"},
    "gui": {"env"}, "click": {"role", "exact", "timeout", "button", "window"}, "type": {"anywhere"},
    "expect": {"role", "exact", "timeout"}, "expect_absent": {"role", "exact", "timeout"},
    "reboot": {"timeout"}, "wait": {"timeout"}, "clock": {"step", "timeout"}, "disk_rm": {"timeout"},
    "open": {"timeout", "browser"}, "viewport": {"browser"},
}


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _text(v) -> bool:
    return isinstance(v, str) and bool(v)


def _texts(v) -> bool:
    return _text(v) or (isinstance(v, list) and bool(v) and all(_text(x) for x in v))


# What each action and option takes, as (check, description).
VALUES = {
    **{a: (_text, "a text") for a in ("root", "exec", "focus", "click", "type", "expect", "expect_absent", "shot",
                                      "clock", "disk_add", "disk_rm", "mark")},
    "observe_start": (_text, "the label of an [[observe]] table"),
    "observe_stop": (lambda v: v is True or _text(v), "true (every open session) or an [[observe]] label"),
    "gui": (_texts, "a command line or a list of arguments"),
    "key": (_texts, "a key combo or a list of them"),
    "reboot": (lambda v: v is True or _text(v), "true or the command that reboots"),
    "prepare": (lambda v: v is True or _text(v), "true or the name of a prepare step"),
    "wait": (lambda v: v is True or (_number(v) and v > 0), "true (until the screen is still) or seconds"),
    "console_errors": (lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0, "a count, e.g. 0"),
    "open": (lambda v: _text(v) and browser.address(v), "a path on the site shown (/system) or a whole address"),
    "viewport": (lambda v: isinstance(v, str) and (v == "reset" or re.fullmatch(r"[1-9]\d*x[1-9]\d*", v)),
                 "WIDTHxHEIGHT (390x700) or reset"),
    "role": (_text, "a text"), "output": (_text, "a text"), "name": (_text, "a text"), "step": (_text, "a text"),
    "window": (_text, "a text"), "button": (lambda v: v in ("left", "right", "middle"), "left, right or middle"),
    "browser": (lambda v: isinstance(v, str) and v in browser.NAMES, " or ".join(sorted(browser.NAMES))),
    "timeout": (lambda v: _number(v) and v > 0, "seconds"),
    "rc": (lambda v: isinstance(v, int) and not isinstance(v, bool), "an exit code"),
    **{o: (lambda v: isinstance(v, bool), "true or false") for o in ("exact", "soft", "until", "anywhere")},
    "env": (lambda v: isinstance(v, dict) and all(isinstance(x, (str, int, float)) for x in v.values()),
            "a table like {NAME = \"value\"}"),
}
# What an [[observe]] table takes: a session started before the first step (start = "before") or
# by an observe_start step (start = "step"), stopped by an observe_stop step or when the run ends.
OBSERVE_KEYS = {"provider", "label", "start", "duration", "max_mb", "process", "options"}
# Where build artifacts land in the guest.
GUEST_IN = {"linux": "/tmp/desk", "openwrt": "/tmp/desk", "windows": "C:/Users/tester/desk"}


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
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except OSError as e:
        raise DeskError(f"{path}: {e.strerror}") from None
    except tomllib.TOMLDecodeError as e:
        raise DeskError(f"{path}: not valid TOML: {e}") from None
    unknown = set(data) - {"image", "record", "build", "disks", "usb", "step", "observe"}
    if unknown:
        raise DeskError(f"{path}: unknown keys {sorted(unknown)}")
    if not _text(data.get("image")):
        raise DeskError(f"{path}: no image")
    if not isinstance(data.get("record", False), bool):
        raise DeskError(f"{path}: record is true or false")
    if not isinstance(data.get("disks", []), list) or not all(isinstance(d, str) for d in data.get("disks", [])):
        raise DeskError(f"{path}: disks is a list like [\"8G:data\"]")
    for d in data.get("disks", []):
        _checked(path, "disks", disks.parse, d)
    devices = data.get("usb", [])
    if not isinstance(devices, list) or not all(isinstance(d, str) and (usb.ID.fullmatch(d.lower()) or
                                                                        usb.ADDRESS.fullmatch(d))
                                                for d in devices):
        raise DeskError(f"{path}: usb is a list like [\"0fd9:0084\"] (VENDOR:PRODUCT or BUS.DEVICE)")
    build = data.get("build", {})
    if not isinstance(build, dict) or set(build) - {"run", "artifacts", "dir"}:
        raise DeskError(f"{path}: [build] takes run, artifacts and dir")
    if "run" in build and not _text(build["run"]):
        raise DeskError(f"{path}: [build] run is a command line")
    if "dir" in build and not (_text(build["dir"]) and (path.parent / build["dir"]).is_dir()):
        raise DeskError(f"{path}: [build] dir is a directory, relative to the scenario file (got {build['dir']!r})")
    if not isinstance(build.get("artifacts", []), list) or not all(_text(a) for a in build.get("artifacts", [])):
        raise DeskError(f"{path}: [build] artifacts is a list of file patterns")
    if not isinstance(data.get("step", []), list) or not all(isinstance(s, dict) for s in data.get("step", [])):
        raise DeskError(f"{path}: steps are [[step]] tables")
    for i, step in enumerate(data.get("step", []), 1):
        actions = set(step) & ACTIONS
        if len(actions) != 1 or set(step) - ACTIONS - OPTIONS:
            raise DeskError(f"{path}: step {i} needs exactly one of {sorted(ACTIONS)}, "
                            f"plus any of {sorted(OPTIONS)} (got {sorted(step)})")
        action = actions.pop()
        if "until" in step and action not in ("exec", "root"):
            raise DeskError(f"{path}: step {i}: until goes with exec or root")
        unread = set(step) - {action, "name", "soft"} - READS.get(action, set())
        if unread:
            takes = sorted(READS.get(action, set()) | {"name", "soft"})
            raise DeskError(f"{path}: step {i}: {action} does not use {', '.join(sorted(unread))} "
                            f"(it takes {', '.join(takes)})")
        for key, value in step.items():
            check, takes = VALUES[key]
            if not check(value):
                raise DeskError(f"{path}: step {i}: {key} takes {takes} (got {value!r})")
        if action == "disk_add":
            _checked(path, f"step {i}", disks.parse, step[action])
        if action == "clock" and step[action] != "reset":
            _checked(path, f"step {i}", clock.parse, step[action], 0.0, UTC)  # whether it reads, in any zone
        if "step" in step:
            _checked(path, f"step {i}", clock.duration, step["step"])
    observations(path, data)
    return data


def observations(path: Path, data: dict) -> dict[str, dict]:
    """The [[observe]] tables by label, checked as far as can be without a guest: the provider,
    its options, limits, and steps naming labels there are."""
    from . import observe

    tables = data.get("observe", [])
    if not isinstance(tables, list) or not all(isinstance(t, dict) for t in tables):
        raise DeskError(f"{path}: observations are [[observe]] tables")
    out: dict[str, dict] = {}
    for i, t in enumerate(tables, 1):
        where = f"{path}: [[observe]] {i}"
        unknown = set(t) - OBSERVE_KEYS
        if unknown:
            raise DeskError(f"{where}: unknown keys {sorted(unknown)} (it takes {', '.join(sorted(OBSERVE_KEYS))})")
        if t.get("provider") not in observe.PROVIDERS:
            raise DeskError(f"{where}: provider is one of {', '.join(observe.PROVIDERS)} (got {t.get('provider')!r})")
        label = t.get("label", t["provider"])
        if not (_text(label) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}", label)):
            raise DeskError(f"{where}: label is letters, digits, - _ . (got {label!r})")
        if label in out:
            raise DeskError(f"{where}: label {label!r} twice (give each a label of its own)")
        if t.get("start", "before") not in ("before", "step"):
            raise DeskError(f"{where}: start is \"before\" (the first step) or \"step\" (an observe_start step)")
        for key in ("duration", "max_mb"):
            if key in t and not (_number(t[key]) and t[key] > 0):
                raise DeskError(f"{where}: {key} takes a number above 0 (got {t[key]!r})")
        if "process" in t and not _text(t["process"]):
            raise DeskError(f"{where}: process is an image name, e.g. \"app.exe\"")
        if not isinstance(t.get("options", {}), dict):
            raise DeskError(f"{where}: options is a table like {{csv = true}}")
        _checked(path, f"[[observe]] {i}", observe.provider(t["provider"]).options, t.get("options", {}))
        out[label] = t
    started = set()
    for i, step in enumerate(data.get("step", []), 1):
        for key in ("observe_start", "observe_stop", "mark"):
            value = step.get(key)
            if key != "mark" and isinstance(value, str) and value not in out:
                raise DeskError(f"{path}: step {i}: {key} names no [[observe]] label (there are: "
                                f"{', '.join(out) or 'none'})")
            if key == "mark" and key in step and not out:
                raise DeskError(f"{path}: step {i}: mark needs an [[observe]] session to mark")
        if "observe_start" in step:
            started.add(step["observe_start"])
    for label, t in out.items():
        if t.get("start") == "step" and label not in started:
            raise DeskError(f"{path}: [[observe]] {label!r} starts at a step, and no observe_start step names it")
    return out


def check_observations(data: dict, spec) -> None:
    """What only the image tells: whether its guests are of a family the providers observe."""
    from . import observe

    for t in data.get("observe", []):
        p = observe.provider(t["provider"])
        if spec.family not in p.families:
            raise DeskError(f"[[observe]] {t['provider']}: observes {', '.join(p.families)} guests, and {spec.name} "
                            f"is a {spec.family} one")


def _observe_start(run, label: str, table: dict):
    from . import observe

    return observe.start(run, table["provider"], label=label, options=table.get("options"),
                         duration=table.get("duration"), max_mb=table.get("max_mb"),
                         target={"image": table["process"]} if table.get("process") else None,
                         owner=run.label)


def _observe_step(run, steps: Steps, action: str, value) -> str:
    """observe_start, observe_stop and mark. Whatever goes wrong is desk's (exit 2): a capture
    that could not be made says nothing about the product."""
    from . import observe

    try:
        if action == "observe_start":
            s = _observe_start(run, value, steps.observations[value])
            return f"session {s.id} ({s.provider})"
        if action == "mark":
            m = observe.mark(run, value)
            return f"mark {m['seq']} in {', '.join(m['sessions'])}"
        refs = ([s.id for s in observe.listing(run) if s.state in observe.COLLECTABLE] if value is True else [value])
        done = []
        for ref in refs:
            s, problems = observe.stop(run, ref, reason="scenario step")
            if problems:
                raise observe.ObserveError(f"observation {s.name()} is not complete: " + "; ".join(problems))
            done.append(f"{s.name()}: " + ", ".join(a["name"] for a in s.artifacts
                                                    if a.get("verified") and a["kind"] != "text"))
        return "; ".join(done) or "no session open"
    except observe.ObserveError:
        raise
    except DeskError as e:
        raise observe.ObserveError(str(e)) from None


def _checked(path: Path, where: str, parse, *args) -> None:
    try:
        parse(*args)
    except DeskError as e:
        raise DeskError(f"{path}: {where}: {e}") from None


def fixed_waits(data: dict) -> list[str]:
    """Steps that wait a fixed time: too short on a busy host, wasted time on an idle one."""
    found = []
    for i, step in enumerate(data.get("step", []), 1):
        if "wait" in step and step["wait"] is not True:
            found.append(f"step {i}: wait = {step['wait']} waits a fixed time; expect what should appear "
                         "(or wait = true for a still screen)")
        for key in ("exec", "root"):
            command = step.get(key)
            if isinstance(command, str) and re.search(r"\bsleep\s+[\d.]", command) and not step.get("until"):
                found.append(f"step {i}: sleep in {key}; until = true (with rc or output) repeats the "
                             "check until it holds")
    return found


def describe(step: dict) -> tuple[str, str]:
    action = next(k for k in step if k in ACTIONS)
    value = step[action]
    return action, value if isinstance(value, str) else " ".join(map(str, value)) if isinstance(value, list) else ""


def _base(path: Path, data: dict) -> Path:
    """Where [build] runs and its artifacts are: `dir`, relative to the scenario file (a project in
    a repository of several), else the top of the scenario's git repository."""
    if "dir" in data.get("build", {}):
        return (path.parent / data["build"]["dir"]).resolve()
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
        # Stopped in the guest when its time is up, as `desk exec` does: one that hangs would
        # otherwise go on running there under the steps that follow.
        attempt = max(5, deadline - time.monotonic()) if step.get("until") else limit
        start = time.monotonic()
        try:
            r = guest.ssh_run(run.ssh_port, user, guest.command(spec, [command], limit=attempt), capture=True,
                              timeout=attempt + 30)
        except subprocess.TimeoutExpired as e:
            r = subprocess.CompletedProcess([], 124, e.stdout or b"", e.stderr or b"")
        # Windows ends its lines with \r\n, and an expected output of several lines has \n.
        out, err = (b.decode(errors="replace").replace("\r\n", "\n") for b in (r.stdout, r.stderr))
        problem = None
        if r.returncode != step.get("rc", 0) and time.monotonic() - start >= attempt:
            problem = f"still running after {attempt:.0f}s, stopped"
        elif r.returncode != step.get("rc", 0):
            problem = f"exit {r.returncode}"
        elif "output" in step and step["output"] not in out:
            problem = f"output lacks {step['output']!r}"
        if problem is None:
            return out.strip().splitlines()[-1][:120] if out.strip() else ""
        if not step.get("until") or time.monotonic() + 2 > deadline:
            if step.get("rc", 0) != 255:
                guest.lost(r, run.ssh_port, user, f"step {command[:60]!r}")
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
    if action in ("observe_start", "observe_stop", "mark"):
        return _observe_step(run, steps, action, value)
    if action == "gui":
        if isinstance(value, list):
            argv = value
        else:
            argv = split_windows(value) if spec.family == "windows" else shlex.split(value)
        steps.gui(*argv, env={k: str(v) for k, v in step.get("env", {}).items()})
    elif action == "focus":
        return steps.focus(value)
    elif action == "click":
        steps.click(value, role, step.get("exact", True), timeout or 60, step.get("button", "left"), step.get("window"))
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
    elif action == "open":
        # A page that did not load fails the step; one the site sent elsewhere (a login) says so.
        shown, asked = browser.open_url(run, value, timeout=timeout or 30, browser=step.get("browser"))
        return shown if browser.under(shown, asked) else f"{shown} (opened {asked})"
    elif action == "viewport":
        return browser.viewport(run, value, browser=step.get("browser"))
    elif action == "disk_add":
        return f"disk {disks.add(run, *disks.parse(value))} plugged in"
    elif action == "disk_rm":
        disks.remove(run, value, timeout=timeout or 30)
        return f"disk {value} unplugged"
    elif action == "console_errors":
        # New errors in the web console since the scenario started or the previous check.
        lines = browser.console(run)
        found, unknown = browser.errors(lines), browser.unlevelled(lines)
        new, steps.console_seen = found[steps.console_seen:], len(found)
        maybe, steps.console_unlevelled = unknown[steps.console_unlevelled:], len(unknown)
        if len(new) > value:
            raise StepFailed(f"{len(new)} console errors (at most {value}): " + " | ".join(new[:3])[:300])
        if maybe:
            raise browser.Unchecked(f"{len(new)} console errors and {browser.unlevelled_note(len(maybe))}: "
                                    + " | ".join(maybe[:3])[:300])
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
    check_observations(data, spec)
    _warn(data)
    base = _base(path, data)
    artifacts = _build(data, base)
    agent, _ = runs.agent_process()
    run = runs.up(spec, label=f"scenario {base.name}/{path.name}" + (f" ({agent})" if agent else ""))
    print(f"run {run.id} ({spec.name})", flush=True)
    ok = True
    try:
        if data.get("record"):
            record.start(run)
        run.log_event(scenario=str(path.resolve()), steps=len(data.get("step", [])))
        if artifacts:
            dest = GUEST_IN[spec.family]
            guest.ssh_run(run.ssh_port, run.user, spec.cmd(f"mkdir -p {dest}" if spec.family != "windows"
                          else f"New-Item -ItemType Directory -Force {dest} | Out-Null"), capture=True)
            for f in artifacts:
                if guest.scp(run.ssh_port, str(f), f"{run.user}@127.0.0.1:{dest}/{f.name}") != 0:
                    raise DeskError(f"copying {f.name} to the guest failed; {runs.host_state(run)}")
        for d in data.get("disks", []):
            disks.add(run, *disks.parse(d))
        for device in data.get("usb", []):
            usb.add(run, device)
        # Started before the first step, so they see the product from its start; the run's end
        # (evidence.finish) stops and collects whatever is still open, failure or not.
        for label, table in observations(path, data).items():
            if table.get("start", "before") == "before":
                s = _observe_start(run, label, table)
                print(f"observing: {label} (session {s.id}, {s.provider})", flush=True)
        ok = _run_steps(run, runs.spec_for(run), data, observed=observations(path, data))
    finally:
        # Also when the VM is gone: the steps and why the guest was lost are in its directory.
        report, videos = evidence.finish(run, keep=keep, running=keep, stopped=True)
        if report:
            print(f"report: {report / 'report.md'}")
        for v in videos:
            print(f"video: {v}")
    print("scenario passed" if ok else "scenario FAILED")
    if keep and run.live_domain() is not None:
        print(f"run {run.id} left running: desk -r {run.id} COMMAND; desk down {run.id} when done; "
              f"desk -r {run.id} scenario run FILE --from N goes on in it")
    return ok


def continue_scenario(path: Path, run, first: int, image: str | None = None) -> bool:
    """Steps `first`.. of the file in a run that is already there (left by --keep): no build, no
    new run. While a scenario is being written, a fixed step is tried again without the ones before
    it; the run stays, and its report (desk down) counts each step's last attempt."""
    data = load(path)
    spec = load_image(image or data["image"])
    check_observations(data, spec)
    if run.image != spec.name:
        raise DeskError(f"run {run.id} is of {run.image}, the scenario of {spec.name}")
    total = len(data.get("step", []))
    if not 1 <= first <= total:
        raise DeskError(f"--from {first}: the scenario has steps 1 to {total}")
    _warn(data)
    run.log_event(scenario=str(path.resolve()), steps=total, first=first)
    ok = _run_steps(run, runs.spec_for(run), data, first, observed=observations(path, data))
    print(f"steps {first}-{total} passed" if ok else "scenario FAILED")
    print(f"run {run.id} still running: desk down {run.id} writes the report")
    return ok


def _warn(data: dict) -> None:
    for w in fixed_waits(data):
        print(f"warning: {w}", flush=True)


def _run_steps(run, spec, data: dict, first: int = 1, observed: dict | None = None) -> bool:
    """Steps `first`.. in order, stopping at the first failure that is not soft; whether all passed."""
    from .observe import ObserveError

    ok = True
    steps = Steps(spec, run)
    steps.observations = observed or {}
    for i, step in enumerate(data.get("step", []), 1):
        if i < first:
            continue
        action, arg = describe(step)
        label = step.get("name") or f"{action} {arg}".strip()
        t0 = time.monotonic()
        lost = None
        try:
            detail = _step(run, spec, steps, step)
            passed = True
        except (StepFailed, DeskError, subprocess.TimeoutExpired) as e:
            detail, passed = str(e), False
            # Without the guest no step says anything about the product: desk's failure (exit 2).
            if isinstance(e, guest.NoConnection):
                lost = guest.NoConnection(f"{e}; {runs.host_state(run)}")
                detail = str(lost)
            elif isinstance(e, (browser.Unchecked, ObserveError)):
                lost = e  # a check desk could not make says nothing about the product either
        run.log_event(step=i, action=label, ok=passed, detail=detail, seconds=round(time.monotonic() - t0, 1))
        head, *rest = detail.splitlines() or [""]
        print(f"{'PASS' if passed else 'FAIL'}  {i:>2}. {label[:60]:<60} {time.monotonic() - t0:5.1f}s  "
              f"{head[:100]}", flush=True)
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
            if lost:
                raise lost
            # soft: an independent check whose failure should not hide the ones after it.
            if not step.get("soft"):
                break
    return ok
