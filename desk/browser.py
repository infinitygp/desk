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
# exceptions come from its internal URLs, or name its actors (after a restart with the devtools
# open, e.g. desk browser viewport's responsive design mode).
FIREFOX_OWN = re.compile(r"console\.[a-z]+: [A-Za-z][\w.-]*: |JavaScript (error|warning): (resource|chrome)://|"
                         r"JavaScript error: , line 0: NotFoundError: No such JS(Process|Window)Actor ")
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
    from .clock import guest_since

    spec = load_image(run.image)
    if spec.family == "windows":
        raise DeskError("desk browser console reads Firefox on Linux guests")
    spec.need_desktop("desk browser")
    r = guest.ssh_run(run.ssh_port, spec.root_user, f"S={int(guest_since(run))} bash -s", input=JOURNAL.encode(),
                      capture=True, timeout=60)
    guest.connected(r, "reading the web console")
    return parse(r.stdout.decode(errors="replace"), everything)


class NotLoaded(DeskError):
    """The browser does not show the page that was to be opened."""


def _linux(run):
    spec = load_image(run.image)
    if spec.family == "windows":
        raise DeskError("desk browser drives Firefox on Linux guests")
    spec.need_desktop("desk browser")
    return spec


def open_url(run, target: str, timeout: float = 30) -> tuple[str, str]:
    """Open an address in the guest's Firefox the way another program would (`firefox URL`),
    which replaces the page in the current tab (desk-firefox.js); a path (/system/services) is
    taken relative to the page shown now. Returns the address the page ended up at and the one
    that was opened."""
    _linux(run)
    before = a11y.page_url(run).get("url")
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
    deadline = time.monotonic() + timeout
    if urlsplit(url).fragment and before and _address(before, False) == _address(url, False):
        # Firefox takes an address from another program that differs from the page shown only
        # after "#" for a new page that never loads (an empty "New Tab", the old picture left on
        # screen); from another page it loads it whole. Quickly: Cockpit ends its session when
        # none of its pages is open for a few seconds.
        _firefox(run, "about:blank")
        while a11y.page_url(run).get("url") != "about:blank":
            if time.monotonic() > deadline:
                raise DeskError("Firefox did not leave the page shown for about:blank to open the page again with its "
                                f"new part after #: {url}")
            time.sleep(0.2)
    _firefox(run, url)
    # A link opened from another program does not take the focus on Wayland.
    while True:
        try:
            a11y.focus(run, "Firefox")
            break
        except DeskError:
            if time.monotonic() > deadline:
                raise
            time.sleep(1)
    arrived = seen = None
    while time.monotonic() < deadline and not arrived:
        current = a11y.page_url(run).get("url")
        seen = current or seen
        # The document is there; what follows "#" a page may rewrite itself (its own routes),
        # which the caller sees in the address returned.
        arrived = bool(current) and under(current, url, fragment=False)
        if not arrived:
            time.sleep(0.5)
    display.wait_stable(dom, timeout=10)
    page = a11y.page_url(run)
    if page.get("problem"):
        raise NotLoaded(f"{url} did not load: Firefox shows {page['problem']!r} instead")
    if not page.get("url") and not arrived:
        # While it waits for a server that does not answer, Firefox shows no page at all.
        raise NotLoaded(f"{url} did not load within {timeout:.0f}s: Firefox shows no page (still waiting for the "
                        "server?)")
    # Somewhere else than asked for is where the site sent the browser (a login, https, back to
    # the page from before); the caller sees both addresses.
    return page.get("url") or seen, url


def _firefox(run, url: str) -> None:
    r = guest.ssh_run(run.ssh_port, run.user, guest.remote_command(["firefox", url], gui=True),
                      capture=True, timeout=60)
    if r.returncode != 0:
        raise DeskError(f"starting firefox failed: {r.stderr.decode(errors='replace').strip()[-300:]}")


def _address(url: str, fragment: bool = True) -> tuple[str, str, str]:
    parts = urlsplit(url)
    scheme = "http" if parts.scheme.lower() in ("http", "https") else parts.scheme.lower()
    return scheme, parts.netloc.lower(), parts.path.rstrip("/") + ("?" + parts.query if parts.query else "") + \
        ("#" + parts.fragment if fragment and parts.fragment else "")


def under(shown: str, asked: str, fragment: bool = True) -> bool:
    """`shown` is the page `asked` for or one below it, as Firefox writes addresses: the host in
    lower case, https where the site upgrades http, file:/x as file:///x; what follows "#" too
    unless `fragment` is false."""
    (scheme, host, path), (wanted_scheme, wanted_host, wanted_path) = \
        _address(shown, fragment), _address(asked, fragment)
    return (scheme, host) == (wanted_scheme, wanted_host) and path.startswith(wanted_path)


RDM_CLOSE = "Close Responsive Design Mode"


def _rdm(run, focus: bool = False) -> dict:
    # _helper has turned its box into screen pixels already.
    return a11y._helper(run, "rdm", json.dumps({"focus": focus}))


def viewport(run, size: str) -> str:
    """Pages at WIDTHxHEIGHT CSS pixels, a phone's layout, through Firefox's responsive design mode
    (a window cannot get that narrow), with its sizes typed into the mode's toolbar like a person
    would; `reset` closes the mode. Returns the size and where the page area is on screen."""
    spec = _linux(run)
    a11y.focus(run, "Firefox")
    state = _rdm(run)
    if size == "reset":
        if state["open"]:
            display.click(run.dom(), *a11y.locate_name(run, RDM_CLOSE, exact=True), run=run)
            display.wait_stable(run.dom(), timeout=5)
        return "responsive design mode closed: pages get the whole window again"
    try:
        width, height = (int(v) for v in size.lower().split("x"))
    except ValueError:
        raise DeskError(f"viewport is WIDTHxHEIGHT, e.g. 390x700, or reset (got {size!r})") from None
    if not state["open"]:
        display.key(run.dom(), "ctrl+shift+m")
        deadline = time.monotonic() + 15
        while not state["open"]:
            if time.monotonic() > deadline:
                raise DeskError("Firefox's responsive design mode did not open (ctrl+shift+m)")
            time.sleep(0.5)
            state = _rdm(run)
    snap = a11y.snapshot(run, app="Firefox", limit=400, save=False)
    bar = next((n for n in snap["nodes"] if n["role"] == "embedded" and n["name"].startswith("chrome://devtools/"
                                                                                            "content/responsive")), None)
    fields = [n for n in snap["nodes"] if bar is not None and n["role"] == "spin button"
              and n["pid"] == bar["pid"] and n["path"][:len(bar["path"])] == bar["path"]]
    if len(fields) < 2:
        raise DeskError("cannot find the width and height fields of Firefox's responsive design mode")
    for node, value in zip(fields[:2], (width, height), strict=True):
        note = a11y.replace_text(run, {k: node[k] for k in ("pid", "path", "role", "name")}, str(value), spec.layout)
        display.key(run.dom(), "ret")
        if note:
            raise DeskError(f"setting the viewport size: {note}")
    display.wait_stable(run.dom(), timeout=5)
    # Keys typed next are for the page, not the mode's height field.
    box = _rdm(run, focus=True).get("bbox")
    if not box:
        return f"page area {width}x{height} CSS pixels (where it is on screen is unknown; look at a screenshot)"
    # The page sits inside a 1 px frame.
    x, y, w, h = box[0] + 1, box[1] + 1, box[2] - 2, box[3] - 2
    f = (run.scale or 100) / 100
    shown = (round(w / f), round(h / f))
    note = "" if shown == (width, height) else f" (asked for {width}x{height}; Firefox shows {shown[0]}x{shown[1]})"
    clipped = " and runs past the bottom of the screen (desk display --resolution for a taller one)" \
        if y + h > display.png_size(display.screenshot(run.dom()))[1] else ""
    return (f"page area {shown[0]}x{shown[1]} CSS pixels{note}{clipped}; on screen: "
            f"desk shot --region {x},{y},{w},{h}; desk browser viewport reset to close the mode")
