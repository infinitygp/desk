"""Firefox's web console, as the images send it to the journal (images/_shared/gnome/desk-firefox.js):
console.* calls, uncaught exceptions and rejected promises of every page."""

import json
import re
from datetime import datetime

from . import guest
from .config import DeskError, load_image

MESSAGE = re.compile(r"(console\.[a-z]+|JavaScript (?:error|warning)): .*")
# Firefox's own components log with their name in front, unquoted ("console.warn: RSLoader: ...";
# a page's console.log("text") is printed quoted, objects and numbers as values), and their
# exceptions come from its internal URLs.
FIREFOX_OWN = re.compile(r"console\.[a-z]+: [A-Za-z][\w.-]*: |JavaScript (error|warning): (resource|chrome)://")
ERROR = re.compile(r"(console\.(error|assert)|JavaScript error): ")
# Bash with the start time in $S, also part of the report's guest script; as root, journalctl
# reads the desktop user's journal too.
JOURNAL = r"""journalctl --since "@$S" --no-pager -o short-iso-precise </dev/null 2>/dev/null \
    | grep -E ': (console\.[a-z]+|JavaScript (error|warning|error detail)): ' | tail -n 2000
"""
# What images/_shared/gnome/desk.cfg adds after an uncaught exception: the thrown value and stack.
DETAIL = re.compile(r"JavaScript error detail: (\{.*\})\s*$")


def _attach(out: list[tuple[float, str]], raw: str, done: set[int]) -> None:
    """Add a detail line's value and stack to the error it belongs to, the latest one before it
    with the same message (Firefox prints the detail right after the error)."""
    try:
        info = json.loads(raw)
    except ValueError:
        return
    extra = (f" = {info['value']}" if info.get("value") else "") + \
        (f" (at {' < '.join(info['stack'][:3])})" if info.get("stack") else "")
    if not extra:
        return
    for i in range(len(out) - 1, max(len(out) - 20, -1), -1):
        if i not in done and "JavaScript error: " in out[i][1] and out[i][1].endswith(f": {info.get('message')}"):
            out[i] = (out[i][0], out[i][1] + extra)
            done.add(i)
            return


def stamped(text: str, everything: bool = False) -> list[tuple[float, str]]:
    """(guest time, `TIME console.error: ...`) from journalctl short-iso-precise output."""
    out: list[tuple[float, str]] = []
    detailed: set[int] = set()
    for line in text.splitlines():
        d = DETAIL.search(line)
        if d:
            _attach(out, d.group(1), detailed)
            continue
        m = MESSAGE.search(line)
        if not m or (not everything and FIREFOX_OWN.match(m.group(0))):
            continue
        stamp = line.split(" ", 1)[0]
        try:
            when = datetime.fromisoformat(stamp).timestamp()
        except ValueError:
            when = 0.0
        out.append((when, f"{stamp[11:19]} {m.group(0)}"))
    return out


def parse(text: str, everything: bool = False) -> list[str]:
    return [line for _, line in stamped(text, everything)]


def errors(lines: list[str]) -> list[str]:
    return [ln for ln in lines if ERROR.match(ln.split(" ", 1)[1])]


def console(run, everything: bool = False) -> list[str]:
    spec = load_image(run.image)
    if spec.family == "windows":
        raise DeskError("desk browser console reads Firefox on Linux guests")
    r = guest.ssh_run(run.ssh_port, spec.root_user, f"S={int(run.since)} bash -s", input=JOURNAL.encode(),
                      capture=True, timeout=60)
    return parse(r.stdout.decode(errors="replace"), everything)
