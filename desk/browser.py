"""Firefox's web console, as the images send it to the journal (images/_shared/gnome/desk-firefox.js):
console.* calls, uncaught exceptions and rejected promises of every page."""

import json
import re
import time
from datetime import datetime
from urllib.parse import urlsplit

from . import a11y, display, guest
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


def _linux(run):
    spec = load_image(run.image)
    if spec.family == "windows":
        raise DeskError("desk browser drives Firefox on Linux guests")
    return spec


def open_url(run, target: str, timeout: float = 30) -> str:
    """Open an address in the guest's Firefox the way another program would (`firefox URL`),
    which replaces the page in the current tab (desk-firefox.js); a path (/system/services) is
    taken relative to the page shown now. Returns the address the page ended up at."""
    _linux(run)
    before = a11y.page_url(run).get("url") if not target.startswith(("http://", "https://", "file:")) else None
    if target.startswith("/"):
        if not before:
            raise DeskError(f"no page in a focused Firefox window to open {target} on; give the whole address")
        parts = urlsplit(before)
        url = f"{parts.scheme}://{parts.netloc}{target}"
    elif "://" in target or target.startswith(("about:", "file:", "data:")):
        url = target
    else:
        raise DeskError(f"give a whole address (https://{target}) or a path starting with /: "
                        "Firefox would guess the scheme, http:// for most hosts")
    dom = run.dom()
    r = guest.ssh_run(run.ssh_port, run.user, guest.remote_command(["firefox", url], gui=True),
                      capture=True, timeout=60)
    if r.returncode != 0:
        raise DeskError(f"starting firefox failed: {r.stderr.decode(errors='replace').strip()[-300:]}")
    # A link opened from another program does not take the focus on Wayland.
    deadline = time.monotonic() + timeout
    while True:
        try:
            a11y.focus(run, "Firefox")
            break
        except DeskError:
            if time.monotonic() > deadline:
                raise
            time.sleep(1)
    want = url.split("#")[0].rstrip("/")
    current = None
    while time.monotonic() < deadline:
        current = a11y.page_url(run).get("url")
        if current and current.split("#")[0].rstrip("/").startswith(want):
            break
        time.sleep(0.5)
    display.wait_stable(dom, timeout=10)
    return a11y.page_url(run).get("url") or current or url
