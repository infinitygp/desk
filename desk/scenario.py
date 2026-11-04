"""Repeatable end-to-end tests: a TOML file in the project's repository builds the product on the
host, starts a run and drives it step by step with the same real input as the desk commands.
The result is an exit code and the usual report, with the steps in its timeline."""

import glob
import hashlib
import json
import os
import re
import shlex
import subprocess
import time
import tomllib
from datetime import UTC
from pathlib import Path, PurePosixPath, PureWindowsPath

from . import a11y, browser, clock, disks, display, evidence, guest, record, runs, usb, winagent
from .config import DeskError, NotDone, load_image
from .steps import Steps

# Exactly one per step; the value is the action's argument.
ACTIONS = {"root", "exec", "gui", "focus", "click", "type", "key", "expect", "expect_absent", "shot",
           "reboot", "wait", "clock", "disk_add", "disk_rm", "console_errors", "prepare", "open", "viewport",
           "observe_start", "observe_stop", "mark", "set_text", "move", "drag", "scroll", "press", "release", "win",
           "pull", "offline", "usb_add", "usb_rm", "host"}
OPTIONS = {"role", "exact", "timeout", "output", "rc", "name", "soft", "env", "step", "until", "anywhere", "browser",
           "button", "window", "state", "text", "double", "to", "save"}
# The options each action reads, besides name and soft; any other would be ignored without a word.
READS = {
    "root": {"timeout", "output", "rc", "until"}, "exec": {"timeout", "output", "rc", "until"},
    "gui": {"env", "rc", "output", "timeout", "until"}, "type": {"anywhere"},
    "click": {"role", "exact", "timeout", "button", "window", "double"},
    "set_text": {"text", "role", "exact", "timeout", "window"}, "pull": {"to", "timeout"},
    # By name; a point [x, y] takes none of these (load says so), press takes button either way.
    "move": {"role", "exact", "timeout", "window"}, "drag": {"role", "exact", "timeout", "window"},
    "press": {"role", "exact", "timeout", "window", "button"}, "release": {"role", "exact", "timeout", "window"},
    "expect": {"role", "exact", "timeout", "window", "state"}, "expect_absent": {"role", "exact", "timeout", "window"},
    "reboot": {"timeout"}, "wait": {"timeout"}, "clock": {"step", "timeout"}, "disk_rm": {"timeout"},
    "open": {"timeout", "browser"}, "viewport": {"browser"}, "usb_add": {"timeout"},
    "host": {"timeout", "rc", "output", "env", "until", "save"},
}
# {NAME} in a step: the stdout a host step before it kept with save = "NAME".
SAVED = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _text(v) -> bool:
    return isinstance(v, str) and bool(v)


def _pixels(v, n: int) -> bool:
    return isinstance(v, list) and len(v) == n and all(isinstance(x, int) and not isinstance(x, bool) and x >= 0
                                                       for x in v)


def _texts(v) -> bool:
    return _text(v) or (isinstance(v, list) and bool(v) and all(_text(x) for x in v))


# What each action and option takes, as (check, description).
VALUES = {
    **{a: (_text, "a text") for a in ("root", "exec", "focus", "type", "expect", "expect_absent", "shot",
                                      "clock", "disk_add", "disk_rm", "mark", "set_text")},
    "click": (lambda v: _text(v) or _pixels(v, 2), "a name, or [x, y] in screen pixels"),
    "win": (_text, "what follows desk win, e.g. \"taskbar --align left\""),
    "pull": (_text, "a file in the guest"),
    **{a: (lambda v: _text(v) and usb.is_query(v), f"a USB device, {usb.QUERY}") for a in ("usb_add", "usb_rm")},
    "offline": (lambda v: isinstance(v, bool) or (isinstance(v, list) and bool(v) and all(
        isinstance(p, int) and not isinstance(p, bool) and 0 < p < 65536 for p in v)),
                "true (every outgoing connection but DNS refused), a list of ports ([443]) or false"),
    "to": (_text, "a folder (ending in /) or file on the host, relative to where [build] runs"),
    "move": (lambda v: _text(v) or _pixels(v, 2), "an element's name, or [x, y] in screen pixels"),
    "drag": (lambda v: _pixels(v, 4) or (isinstance(v, list) and len(v) == 2 and all(_text(x) for x in v)),
             "[x1, y1, x2, y2] in screen pixels or [\"from name\", \"to name\"]"),
    "press": (lambda v: _text(v) or _pixels(v, 2), "an element's name, or [x, y] in screen pixels"),
    "release": (lambda v: v is True or _text(v) or _pixels(v, 2),
                "true (where the pointer is), an element's name or [x, y]"),
    "scroll": (lambda v: _pixels(v[:2], 2) and len(v) == 3 and _number(v[2]) and isinstance(v[2], int) and v[2] != 0
               if isinstance(v, list) else False, "[x, y, notches] (positive: down)"),
    "text": (lambda v: isinstance(v, str), "a text (\"\" empties the field)"),
    "observe_start": (_text, "the label of an [[observe]] table"),
    "observe_stop": (lambda v: v is True or _text(v), "true (every open session) or an [[observe]] label"),
    "gui": (_texts, "a command line or a list of arguments"),
    "key": (_texts, "a key combo or a list of them"),
    "reboot": (lambda v: v is True or _text(v), "true or the command that reboots"),
    "prepare": (lambda v: v is True or _text(v), "true or the name of a prepare step"),
    "wait": (lambda v: v is True or (_number(v) and v > 0), "true (until the screen is still) or seconds"),
    "console_errors": (lambda v: v == "reset" or (isinstance(v, int) and not isinstance(v, bool) and v >= 0),
                       "a count, e.g. 0, or \"reset\" (the count starts here)"),
    "open": (lambda v: _text(v) and browser.address(v), "a path on the site shown (/system) or a whole address"),
    "viewport": (lambda v: isinstance(v, str) and (v == "reset" or re.fullmatch(r"[1-9]\d*x[1-9]\d*", v)),
                 "WIDTHxHEIGHT (390x700) or reset"),
    "role": (_text, "a text"), "output": (_text, "a text"), "name": (_text, "a text"), "step": (_text, "a text"),
    "window": (_text, "a text"),
    "state": (lambda v: (_text(v) and v in a11y.STATES) or (isinstance(v, list) and bool(v) and set(v) <= a11y.STATES
                                                            and all(_text(x) for x in v)),
              "one of " + ", ".join(sorted(a11y.STATES)) + " or a list of them"),
    "button": (lambda v: v in ("left", "right", "middle"), "left, right or middle"),
    "browser": (lambda v: isinstance(v, str) and v in browser.NAMES, " or ".join(sorted(browser.NAMES))),
    "timeout": (lambda v: _number(v) and v > 0, "seconds"),
    "rc": (lambda v: isinstance(v, int) and not isinstance(v, bool), "an exit code"),
    **{o: (lambda v: isinstance(v, bool), "true or false") for o in ("exact", "soft", "until", "anywhere", "double")},
    "env": (lambda v: isinstance(v, dict) and all(isinstance(x, (str, int, float)) for x in v.values()),
            "a table like {NAME = \"value\"}"),
    "host": (lambda v: isinstance(v, list) and bool(v) and all(_text(x) for x in v),
             "a program and its arguments, [\"program\", \"arg\"] (no shell)"),
    "save": (lambda v: isinstance(v, str) and re.fullmatch(r"[A-Z][A-Z0-9_]*", v) is not None,
             "a NAME in capitals, used as {NAME} by later steps"),
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
    unknown = set(data) - {"image", "record", "build", "disks", "usb", "step", "observe", "browser"}
    if unknown:
        raise DeskError(f"{path}: unknown keys {sorted(unknown)}")
    if not _text(data.get("image")):
        raise DeskError(f"{path}: no image")
    if "browser" in data and not _text(data["browser"]):
        raise DeskError(f"{path}: browser is the image of the machine whose browser opens the guest's pages")
    if not isinstance(data.get("record", False), bool):
        raise DeskError(f"{path}: record is true or false")
    if not isinstance(data.get("disks", []), list) or not all(isinstance(d, str) for d in data.get("disks", [])):
        raise DeskError(f"{path}: disks is a list like [\"8G:data\"]")
    for d in data.get("disks", []):
        _checked(path, "disks", disks.parse, d)
    devices = data.get("usb", [])
    if not isinstance(devices, list) or not all(isinstance(d, str) and usb.is_query(d) for d in devices):
        raise DeskError(f"{path}: usb is a list like [\"0fd9:0084\"] ({usb.QUERY})")
    build = data.get("build", {})
    _check_build(path, build, "[build]")
    variants = build.get("image", {})
    if not isinstance(variants, dict) or not all(isinstance(v, dict) for v in variants.values()):
        raise DeskError(f"{path}: [build.image.NAME] tables give the build of another image")
    for name, variant in variants.items():
        _check_build(path, variant, f"[build.image.{name}]")
    if not isinstance(data.get("step", []), list) or not all(isinstance(s, dict) for s in data.get("step", [])):
        raise DeskError(f"{path}: steps are [[step]] tables")
    held = 0  # the step whose press is not released yet
    saved: set[str] = set()  # names host steps before this one keep
    for i, step in enumerate(data.get("step", []), 1):
        unknown = sorted(set(SAVED.findall(json.dumps(step))) - saved)
        if unknown:
            raise DeskError(f"{path}: step {i}: no step before it saves {', '.join(unknown)} "
                            "(a host step with save = \"NAME\")")
        if "save" in step:
            saved.add(step["save"])
        actions = set(step) & ACTIONS
        if len(actions) != 1 or set(step) - ACTIONS - OPTIONS:
            raise DeskError(f"{path}: step {i} needs exactly one of {sorted(ACTIONS)}, "
                            f"plus any of {sorted(OPTIONS)} (got {sorted(step)})")
        action = actions.pop()
        if "until" in step and action not in ("exec", "root", "gui", "host"):
            raise DeskError(f"{path}: step {i}: until goes with exec, root, gui or host")
        by_name = {"role", "exact", "timeout", "window"} & set(step)
        if action in ("click", "move", "drag", "press", "release") and by_name and not (
                isinstance(step[action], str) or (isinstance(step[action], list) and isinstance(step[action][0], str))):
            raise DeskError(f"{path}: step {i}: {action} at pixels takes no {', '.join(sorted(by_name))}, "
                            "which find an element by name")
        if action == "press" and held:
            raise DeskError(f"{path}: step {i}: press while the button of step {held} is still held; release first")
        if action == "release" and not held:
            raise DeskError(f"{path}: step {i}: release without a press before it")
        if action in ("press", "release"):
            held = i if action == "press" else 0
        if action == "win":
            from . import winsettings

            try:
                winsettings.parse(split_windows(step["win"]))
            except DeskError as e:
                raise DeskError(f"{path}: step {i}: {e}") from None
        if action == "open" and any(not 0 < int(p) < 65536 for p in GUEST_PORT.findall(step["open"])):
            raise DeskError(f"{path}: step {i}: {{guest:PORT}} takes a port 1-65535")
        if action == "pull" and "to" not in step:
            raise DeskError(f"{path}: step {i}: pull names the file in the guest; to = \"dir/\" is where it goes")
        if action == "set_text" and "text" not in step:
            raise DeskError(f"{path}: step {i}: set_text names the field; text = \"...\" is what it gets")
        if action == "gui" and not {"rc", "output"} & set(step) and {"timeout", "until"} & set(step):
            raise DeskError(f"{path}: step {i}: a gui step waits for the program only with rc or output, "
                            "and timeout and until go with them")
        unread = set(step) - {action, "name", "soft"} - READS.get(action, set())
        if unread:
            takes = sorted(READS.get(action, set()) | {"name", "soft"})
            raise DeskError(f"{path}: step {i}: {action} does not use {', '.join(sorted(unread))} "
                            f"(it takes {', '.join(takes)})")
        for key, value in step.items():
            check, takes = VALUES[key]
            if isinstance(value, str) and SAVED.search(value):
                continue  # known only when the step runs, where what reads it checks it
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


def _check_build(path: Path, build, where: str) -> None:
    keys = {"run", "artifacts", "dir"} | ({"image"} if where == "[build]" else set())
    if not isinstance(build, dict) or set(build) - keys:
        raise DeskError(f"{path}: {where} takes {', '.join(sorted(keys))}")
    if "run" in build and not _text(build["run"]):
        raise DeskError(f"{path}: {where} run is a command line")
    if "dir" in build and not (_text(build["dir"]) and (path.parent / build["dir"]).is_dir()):
        raise DeskError(f"{path}: {where} dir is a directory, relative to the scenario file (got {build['dir']!r})")
    if not isinstance(build.get("artifacts", []), list) or not all(_text(a) for a in build.get("artifacts", [])):
        raise DeskError(f"{path}: {where} artifacts is a list of file patterns")


def for_image(data: dict, image: str) -> dict:
    """The scenario with the build of `image`: [build] for the file's own image, or its
    [build.image.NAME] table over it (a package for each distribution). A file that has such tables
    has none for an image it does not name, rather than the artifacts of another."""
    build = dict(data.get("build", {}))
    variants = build.pop("image", {})
    if variants and image != data["image"]:
        if image not in variants:
            raise DeskError(f"[build] is for {data['image']} and {', '.join(sorted(variants))}, not {image}: "
                            f"add a [build.image.{image}] table")
        build.update(variants[image])
    return {**data, "build": build}


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
    """What only the image tells: whether its guests are of a family the providers observe, and
    win steps are for Windows."""
    from . import observe

    if spec.family != "windows" and any("win" in st for st in data.get("step", [])):
        i = next(i for i, st in enumerate(data["step"], 1) if "win" in st)
        raise DeskError(f"step {i}: win changes Windows settings, and {spec.name} is a {spec.family} guest")

    for t in data.get("observe", []):
        p = observe.provider(t["provider"])
        if spec.family not in p.families:
            raise DeskError(f"[[observe]] {t['provider']}: observes {', '.join(p.families)} guests, and {spec.name} "
                            f"is a {spec.family} one")


def _observe_start(run, label: str, table: dict):
    from . import observe

    try:
        return observe.start(run, table["provider"], label=label, options=table.get("options"),
                             duration=table.get("duration"), max_mb=table.get("max_mb"),
                             target={"image": table["process"]} if table.get("process") else None,
                             owner=run.label)
    except observe.ObserveError:
        raise
    except Exception as e:
        raise observe.ObserveError(f"starting observation {label}: {e}") from e


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
        done, wrong = [], []
        for ref, result in zip(refs, observe.stop_many(run, refs, reason="scenario step") if refs else []):
            if isinstance(result, BaseException):
                wrong.append(f"observation {ref} not stopped: {result}")
                continue
            s, problems = result
            if problems:
                wrong.append(f"observation {s.name()} is not complete: " + "; ".join(problems))
            done.append(f"{s.name()}: " + ", ".join(a["name"] for a in s.artifacts
                                                    if a.get("verified") and a["kind"] != "text"))
        if wrong:
            raise observe.ObserveError("; ".join(wrong))
        return "; ".join(done) or "no session open"
    except observe.ObserveError:
        raise
    except Exception as e:
        raise observe.ObserveError(str(e) or type(e).__name__) from e


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


def _build(data: dict, repo: Path, skip: bool = False) -> tuple[list[Path], str]:
    """The artifacts, built first unless `skip` (desk scenario run --no-build); and what the
    report says of the build when it was skipped."""
    build = data.get("build", {})
    if build.get("run") and not skip:
        print(f"build: {build['run']}", flush=True)
        if subprocess.run(build["run"], shell=True, cwd=repo).returncode != 0:
            raise DeskError(f"build failed: {build['run']}")
    files = []
    for pattern in build.get("artifacts", []):
        hits = [Path(p) for p in glob.glob(str(repo / pattern))]
        if not hits:
            raise DeskError(f"no build artifact matches {pattern}"
                            + (" (--no-build: build it, or run without --no-build)" if skip else ""))
        # Builds leave older versions around; the one just built is the newest.
        files.append(max(hits, key=lambda p: p.stat().st_mtime))
    if not (skip and build.get("run")):
        return files, ""
    said = "skipped (--no-build); artifacts: " + (", ".join(
        f"{f.relative_to(repo) if f.is_relative_to(repo) else f} "
        f"({time.strftime('%Y-%m-%d %H:%M', time.localtime(f.stat().st_mtime))})" for f in files) or "none")
    print(f"build: {said}", flush=True)
    return files, said


def tail(text: str, lines: int = 15, chars: int = 1500) -> str:
    """The end of a command's output, where the cause of a failure usually is."""
    return "\n".join(text.strip().splitlines()[-lines:])[-chars:]


def _shell(run, spec, step: dict, as_root: bool) -> str:
    command = step["root" if as_root else "exec"]
    user = spec.root_user if as_root else run.user

    def once(attempt: float) -> tuple[subprocess.CompletedProcess, bool]:
        # Stopped in the guest when its time is up, as `desk exec` does: one that hangs would
        # otherwise go on running there under the steps that follow.
        try:
            return guest.ssh_run(run.ssh_port, user, guest.command(spec, [command], limit=attempt), capture=True,
                                 timeout=attempt + 30), False
        except subprocess.TimeoutExpired as e:
            return subprocess.CompletedProcess([], 124, e.stdout or b"", e.stderr or b""), True

    return _checked_command(step, once, lambda r: guest.lost(r, run.ssh_port, user, f"step {command[:60]!r}"))


def _host(run, step: dict, saved: dict[str, str], base: Path) -> str:
    """A program on the host, as desk's user and without a shell, checked like an exec step; its
    environment names the run, so a host tool (a device emulator, a power switch) can aim at it."""
    argv = step["host"]
    env = {**os.environ, "DESK_RUN": run.id, "DESK_RUN_DIR": str(run.dir), "DESK_SSH_PORT": str(run.ssh_port),
           **{k: str(v) for k, v in step.get("env", {}).items()}}
    last = {}

    def once(attempt: float) -> tuple[subprocess.CompletedProcess, bool]:
        stopped = False
        try:
            r = subprocess.run(argv, capture_output=True, timeout=attempt, env=env, cwd=base)
        except subprocess.TimeoutExpired as e:
            r, stopped = subprocess.CompletedProcess(argv, 124, e.stdout or b"", e.stderr or b""), True
        except OSError as e:
            raise DeskError(f"host step: cannot run {argv[0]}: {e.strerror}") from None
        last["r"] = r
        return r, stopped

    shown = _checked_command(step, once, None)
    out = last["r"].stdout.decode(errors="replace").strip()
    if "save" in step:
        saved[step["save"]] = out[:4096]
    return f"exit {last['r'].returncode}" + (f": {shown}" if shown else "")


def _substituted(step: dict, saved: dict[str, str]) -> dict:
    """The step with each {NAME} replaced by what a host step saved under NAME."""
    def one(v):
        if isinstance(v, str):
            def value(m):
                if m[1] not in saved:
                    raise DeskError(f"{{{m[1]}}} has no value: the host step that saves it did not run here")
                return saved[m[1]]
            return SAVED.sub(value, v)
        return [one(x) for x in v] if isinstance(v, list) else v
    return {k: one(v) for k, v in step.items()}


def _gui_waited(run, spec, step: dict, argv: list[str], env: dict[str, str]) -> str:
    """A program in the desktop session, waited for and checked like an exec step (exec --gui --wait):
    what it sees there (the display, UI Automation, the session's environment) SSH does not."""
    spec.need_desktop("a program in the session (gui)")

    def once(attempt: float) -> tuple[subprocess.CompletedProcess, bool]:
        if run.agent_port:
            # The agent stops the program when its time is up.
            reply = winagent.request(run, timeout=attempt + 30, cmd="run", file=argv[0], args=argv[1:] or None,
                                     env=env or None, wait=True, timeout_s=attempt)
            out, err = ((reply.get(k) or "").encode() for k in ("stdout", "stderr"))
            stopped = bool(reply.get("timeout"))
            return subprocess.CompletedProcess([], 124 if stopped else int(reply.get("code", 1)), out, err), stopped
        try:
            # systemd stops it (RuntimeMaxSec) when its time is up.
            return guest.ssh_run(run.ssh_port, run.user,
                                 guest.remote_command(argv, gui=True, wait=True, env=env, limit=attempt),
                                 capture=True, timeout=attempt + 30), False
        except subprocess.TimeoutExpired as e:
            return subprocess.CompletedProcess([], 124, e.stdout or b"", e.stderr or b""), True

    return _checked_command(step, once, None if run.agent_port else
                            lambda r: guest.lost(r, run.ssh_port, run.user, f"step {argv[0][:60]!r}"))


def _checked_command(step: dict, once, lost) -> str:
    """Runs a command (`once`, given the seconds it may take; it says whether it was stopped) until it
    gives the step's rc and output, once or (until) every 2 s for the step's time; `lost` tells ssh's
    255 from the command's."""
    # until: the same command every 2 s until it does what the step expects, e.g. a service is up.
    limit = step.get("timeout", 120 if step.get("until") else 600)
    deadline = time.monotonic() + limit
    while True:
        attempt = max(5, deadline - time.monotonic()) if step.get("until") else limit
        start = time.monotonic()
        r, stopped = once(attempt)
        # Windows ends its lines with \r\n, and an expected output of several lines has \n.
        out, err = (b.decode(errors="replace").replace("\r\n", "\n") for b in (r.stdout, r.stderr))
        problem = None
        if r.returncode != step.get("rc", 0) and (stopped or time.monotonic() - start >= attempt):
            problem = f"still running after {attempt:.0f}s, stopped"
        elif r.returncode != step.get("rc", 0):
            problem = f"exit {r.returncode}"
        elif "output" in step and step["output"] not in out:
            problem = f"output lacks {step['output']!r}"
        if problem is None:
            return out.strip().splitlines()[-1][:120] if out.strip() else ""
        if not step.get("until") or time.monotonic() + 2 > deadline:
            if step.get("rc", 0) != 255 and lost:
                lost(r)
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


# With a browser machine (browser = IMAGE) these steps go to the guest under test, and the rest (its
# screen, the browser, clicks, keys) to the browser machine.
GUEST_ACTIONS = {"root", "exec", "reboot", "pull", "clock", "disk_add", "disk_rm", "prepare", "offline",
                 "observe_start", "observe_stop", "mark", "usb_add", "usb_rm", "host"}
# A port of the guest under test in an address to open: {guest:80}.
GUEST_PORT = re.compile(r"\{guest:(\d{1,5})\}")


class _Browser:
    """The browser machine as the steps see it (its screen, guest and browser), while what they log,
    events and screenshots, goes into the scenario's run and its one report."""

    def __init__(self, run, owner):
        self._run, self._owner, self.gateway = run, owner, None

    def __getattr__(self, name):
        return getattr(self._run, name)

    def log_event(self, **event) -> None:
        self._owner.log_event(**event)

    def next_shot_path(self, label: str) -> Path:
        return self._owner.next_shot_path(label)


def browser_machine(data: dict):
    """The image of the scenario's browser machine, if it has one: a Linux desktop, whose browser
    desk drives."""
    if not data.get("browser"):
        return None
    spec = load_image(data["browser"])
    spec.need_desktop("the browser machine of a scenario (browser = ...)")
    if spec.family != "linux":
        raise DeskError(f"browser = {spec.name!r}: desk drives the browsers of Linux desktops only")
    return spec


def _guest_url(value: str, run, machine) -> str:
    """{guest:PORT} as the browser reaches that port of the guest under test: a tunnel to the host
    (desk forward), which the browser machine reaches at its gateway (passt maps it to the host); in
    the guest itself, localhost."""
    if machine is None or not GUEST_PORT.search(value):
        return GUEST_PORT.sub(lambda m: f"localhost:{m.group(1)}", value)
    from . import forward

    if machine.gateway is None:
        r = guest.ssh_run(machine.ssh_port, machine.user, "ip -4 route show default", capture=True, timeout=30)
        guest.lost(r, machine.ssh_port, machine.user, "reading the browser machine's gateway")
        found = re.search(r"default via (\S+)", r.stdout.decode(errors="replace"))
        if not found:
            raise NotDone(f"the browser machine {machine.id} has no default route to reach the host through")
        machine.gateway = found.group(1)
    return GUEST_PORT.sub(lambda m: f"{machine.gateway}:{forward.start(run, int(m.group(1)))}", value)


def _pull(run, spec, base: Path, step: dict) -> str:
    """A file of the guest copied to the host for a later scenario to take as an artifact, with the
    SHA-256 the guest computes checked against the copy's."""
    src = step["pull"]
    dest = base / step["to"]
    if step["to"].endswith(("/", "\\")) or dest.is_dir():
        dest = dest / (PureWindowsPath(src) if spec.family == "windows" else PurePosixPath(src)).name
    if spec.family == "windows":
        quoted = src.replace("'", "''")
        script = (f"if (-not (Test-Path -LiteralPath '{quoted}' -PathType Leaf)) {{ exit 3 }}; "
                  f"(Get-FileHash -Algorithm SHA256 -LiteralPath '{quoted}').Hash.ToLower()")
    else:
        script = f"test -f {shlex.quote(src)} || exit 3; sha256sum < {shlex.quote(src)}"
    r = guest.ssh_run(run.ssh_port, run.user, guest.command(spec, [script], limit=600), capture=True, timeout=630)
    guest.lost(r, run.ssh_port, run.user, f"step pull {src[:60]!r}")
    if r.returncode == 3:
        raise StepFailed(f"no file {src} in the guest")
    said = r.stdout.decode(errors="replace").split()
    if r.returncode != 0 or not said or not re.fullmatch(r"[0-9a-f]{64}", said[0]):
        why = r.stderr.decode(errors="replace").strip()[-300:]
        raise NotDone(f"the guest did not give the SHA-256 of {src}: {why or f'exit {r.returncode}'}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    rc = guest.scp(run.ssh_port, f"{run.user}@127.0.0.1:{src}", str(dest), timeout=step.get("timeout", 600))
    if rc != 0:
        raise NotDone(f"copying {src} to {dest} " + ("ran out of time" if rc == 124 else f"failed (scp exit {rc})"))
    digest = hashlib.sha256()
    with dest.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    if digest.hexdigest() != said[0]:
        raise NotDone(f"{dest} has SHA-256 {digest.hexdigest()}, the guest's {src} {said[0]}")
    size = dest.stat().st_size
    run.log_event(pulled=str(dest), bytes=size, sha256=said[0])
    return f"{dest} ({size} bytes, sha256 {said[0]})"


def _find(step: dict) -> dict:
    """How a step that takes an element's name finds it, as click does."""
    return {"role": step.get("role"), "exact": step.get("exact", True), "timeout": step.get("timeout", 60),
            "window": step.get("window")}


def _step(run, spec, steps: Steps, step: dict) -> str:
    action, _ = describe(step)
    value, role, timeout = step[action], step.get("role"), step.get("timeout")
    if action == "host":
        return _host(run, step, steps.saved, steps.base)
    if action in ("root", "exec"):
        return _shell(run, spec, step, action == "root")
    if action in ("observe_start", "observe_stop", "mark"):
        return _observe_step(run, steps, action, value)
    if action == "gui":
        if isinstance(value, list):
            argv = value
        else:
            argv = split_windows(value) if spec.family == "windows" else shlex.split(value)
        env = {k: str(v) for k, v in step.get("env", {}).items()}
        if {"rc", "output"} & set(step):
            return _gui_waited(run, spec, step, argv, env)
        steps.gui(*argv, env=env)
    elif action == "focus":
        return steps.focus(value)
    elif action == "click" and isinstance(value, list):
        steps.pointer("click", value, step.get("button", "left"), 2 if step.get("double") else 1)
    elif action == "click":
        steps.click(value, role, step.get("exact", True), timeout or 60, step.get("button", "left"), step.get("window"),
                    2 if step.get("double") else 1)
    elif action in ("move", "drag", "scroll"):
        steps.pointer(action, value, **_find(step))
    elif action == "press":
        steps.press(value, step.get("button", "left"), **_find(step))
    elif action == "release":
        steps.release(value, **_find(step))
    elif action == "win":
        from . import winsettings

        return winsettings.apply(run, spec, winsettings.parse(split_windows(value)))
    elif action == "offline":
        from . import offline

        return offline.off(run, spec) if value is False else offline.on(run, spec, None if value is True else value)
    elif action == "pull":
        return _pull(run, spec, steps.base, step)
    elif action == "set_text":
        return steps.set_text(value, step["text"], role, step.get("exact", True), timeout or 60, step.get("window"))
    elif action == "type":
        steps.type(value, anywhere=step.get("anywhere", False))
    elif action == "key":
        steps.key(*([value] if isinstance(value, str) else value))
    elif action == "expect":
        states = step.get("state")
        node = steps.wait_for(value, role, step.get("exact", False), timeout or 30, step.get("window"),
                              [states] if isinstance(states, str) else states)
        return a11y.format_node(node, indent=False)
    elif action == "expect_absent":
        # Absent while the page is still loading proves nothing; at least let the screen settle.
        steps.settle()
        steps.wait_gone(value, role, step.get("exact", False), timeout or 30, step.get("window"))
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
    elif action == "usb_add":
        # A device that is still coming back (re-enumerating, a reset) gets the step's timeout.
        usb.wait_present(value, 10 if timeout is None else timeout)
        d = usb.add(run, value)
        return f"USB device {d['id']} {d['name']} ({d['bus']}.{d['dev']}) handed to the guest"
    elif action == "usb_rm":
        d = usb.remove(run, value)
        return f"USB device {d['id']} ({d['bus']}.{d['dev']}) back with the host"
    elif action == "disk_rm":
        disks.remove(run, value, timeout=timeout or 30)
        return f"disk {value} unplugged"
    elif action == "console_errors":
        # New errors in the web console since the scenario started or the previous check.
        lines = browser.console(run)
        found, unknown = browser.errors(lines), browser.unlevelled(lines)
        new, steps.console_seen = found[steps.console_seen:], len(found)
        maybe, steps.console_unlevelled = unknown[steps.console_unlevelled:], len(unknown)
        if value == "reset":
            # Left out, not hidden: the step says how many and which.
            left = new + maybe
            return f"{len(left)} console messages before this point left out" + (
                ": " + " | ".join(left[:3])[:300] if left else "")
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


def run_scenario(path: Path, keep: bool = False, image: str | None = None, no_build: bool = False) -> bool:
    data = load(path)
    spec = load_image(image or data["image"])
    data = for_image(data, spec.name)
    check_observations(data, spec)
    bspec = browser_machine(data)
    _warn(data)
    base = _base(path, data)
    artifacts, built = _build(data, base, skip=no_build)
    agent, _ = runs.agent_process()
    run = runs.up(spec, label=f"scenario {base.name}/{path.name}" + (f" ({agent})" if agent else ""))
    print(f"run {run.id} ({spec.name})", flush=True)
    ok, brun = True, None
    try:
        if bspec:
            brun = runs.up(bspec, label=f"scenario {base.name}/{path.name}: browser for {run.id}")
            run.log_event(browser_run=brun.id)
            print(f"browser run {brun.id} ({bspec.name})", flush=True)
        if data.get("record"):
            record.start(run)
        run.log_event(scenario=str(path.resolve()), steps=len(data.get("step", [])),
                      **({"build": built} if built else {}))
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
        # The run's end (evidence.finish) stops and collects whatever is still open, failure or not.
        _observe_before(run, path, data)
        ok = _run_steps(run, runs.spec_for(run), data, observed=observations(path, data), base=base,
                        machine=_Browser(brun, run) if brun else None)
    finally:
        if brun:
            # Its screenshots and steps are in the scenario's report.
            evidence.finish(brun, keep=keep, report=False, running=keep, stopped=True)
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
        if brun:
            print(f"its browser machine {brun.id} too: desk down {brun.id} when done")
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
    machine = None
    if browser_machine(data):
        events = [json.loads(line) for line in (run.dir / "log.jsonl").read_text().splitlines() if line.strip()]
        bid = next((ev["browser_run"] for ev in reversed(events) if "browser_run" in ev), None)
        if bid is None:
            raise DeskError(f"run {run.id} has no browser machine; run the scenario whole (it starts one)")
        machine = _Browser(runs.resolve(bid), run)
    run.log_event(scenario=str(path.resolve()), steps=total, first=first)
    if first == 1:
        _observe_before(run, path, data)
    ok = _run_steps(run, runs.spec_for(run), data, first, observed=observations(path, data),
                    base=_base(path, for_image(data, spec.name)), machine=machine)
    print(f"steps {first}-{total} passed" if ok else "scenario FAILED")
    print(f"run {run.id} still running: desk down {run.id} writes the report")
    return ok


def _observe_before(run, path: Path, data: dict) -> None:
    """The [[observe]] sessions that start before the first step, so they see the product from its start."""
    for label, table in observations(path, data).items():
        if table.get("start", "before") == "before":
            s = _observe_start(run, label, table)
            print(f"observing: {label} (session {s.id}, {s.provider})", flush=True)


def _warn(data: dict) -> None:
    for w in fixed_waits(data):
        print(f"warning: {w}", flush=True)


def _run_steps(run, spec, data: dict, first: int = 1, observed: dict | None = None,
               base: Path | None = None, machine: _Browser | None = None) -> bool:
    """Steps `first`.. in order, stopping at the first failure that is not soft; whether all passed.
    With a browser machine, the steps that are not GUEST_ACTIONS go to it."""
    from .observe import ObserveError

    ok = True
    steps = Steps(spec, run)
    steps.observations = observed or {}
    steps.base = base or Path.cwd()  # where pull's `to` is, as [build]'s artifacts are
    steps.saved = {}  # what host steps kept with save
    on_browser = None
    if machine is not None:
        on_browser = (machine, runs.spec_for(machine._run))
        on_browser += (Steps(on_browser[1], machine),)
    try:
        for i, step in enumerate(data.get("step", []), 1):
            if i < first:
                continue
            action, arg = describe(step)
            label = step.get("name") or f"{action} {arg}".strip()
            t0 = time.monotonic()
            lost = None
            target, tspec, tsteps = on_browser if on_browser and action not in GUEST_ACTIONS else (run, spec, steps)
            try:
                step = _substituted(step, steps.saved)
                if action == "open":
                    step = {**step, "open": _guest_url(step["open"], run, machine)}
                detail = _step(target, tspec, tsteps, step)
                passed = True
            except (StepFailed, DeskError, subprocess.TimeoutExpired) as e:
                detail, passed = str(e), False
                # Without the guest no step says anything about the product: desk's failure (exit 2).
                if isinstance(e, guest.NoConnection):
                    lost = guest.NoConnection(f"{e}; {runs.host_state(run)}")
                    detail = str(lost)
                elif isinstance(e, (browser.Unchecked, ObserveError, winagent.AgentGone, NotDone)):
                    lost = e  # a check desk could not make (or ask the session) says nothing about the product either
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
                    shot.write_bytes(display.screenshot(target.dom()))
                    run.log_event(shot=str(shot))
                except DeskError:
                    pass  # the VM itself is gone; the report says so
                if lost:
                    raise lost
                # soft: an independent check whose failure should not hide the ones after it.
                if not step.get("soft"):
                    break
    finally:
        for each in (steps, on_browser[2] if on_browser else None):
            if each is None or not each.held:
                continue
            # A button left down would turn every later pointer move in the guest into a drag.
            held = each.held
            try:
                each.release()
                print(f"[desk] the {held} mouse button, held since a press step, was let go", flush=True)
            except DeskError as e:
                print(f"[desk] warning: the mouse button held since a press step could not be let go: {e}",
                      flush=True)
    return ok
