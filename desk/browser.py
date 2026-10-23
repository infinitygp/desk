"""The browsers of the Linux images, Firefox and Chromium: their web consoles as the images send
them to the journal (images/_shared/gnome/desk-firefox.js, desk-chromium-flags), console.* calls,
uncaught exceptions and rejected promises of every page; opening pages; pages at a phone's size."""

import json
import re
import time
from datetime import datetime
from urllib.parse import unquote, urlsplit

from . import a11y, display, guest
from .config import DeskError, load_image

# As AT-SPI and the shell name them; the keys are what --browser and scenarios take.
FIREFOX, CHROMIUM = "Firefox", "Chromium"
NAMES = {"firefox": FIREFOX, "chromium": CHROMIUM}

MESSAGE = re.compile(r"(console\.[a-z]+|JavaScript (?:error|warning)): .*")
# Firefox's own components log with their name in front, unquoted ("console.warn: RSLoader: ...";
# a page's console.log("text") is printed quoted, objects and numbers as values), and their
# exceptions come from its internal URLs, or name its actors (after a restart with the devtools
# open, e.g. desk browser viewport's responsive design mode).
FIREFOX_OWN = re.compile(r"console\.[a-z]+: [A-Za-z][\w.-]*: |JavaScript (error|warning): (resource|chrome)://|"
                         r"JavaScript error: , line 0: NotFoundError: No such JS(Process|Window)Actor ")
ERROR = re.compile(r"(console\.(error|assert)|JavaScript error): ")
# Chromium (--enable-logging=stderr) logs every console call of a page at one level, so
# console.error cannot be told from console.log; uncaught exceptions and rejected promises start
# with "Uncaught". A message of several lines leaves its first one here: the rest of the lines
# carry no mark to find them by.
CHROMIUM_LINE = re.compile(r':CONSOLE:(\d+)\] "(.*?)(?:", source: (\S*) \(\d+\))?$')
CHROMIUM_OWN = ("chrome://", "chrome-extension://", "chrome-untrusted://", "chrome-error://", "devtools://")
# In front of a Chromium message that may be an error as well as not.
UNLEVELLED = "Chromium console: "
# Bash with the start time in $S, also part of the report's guest script; as root, journalctl
# reads the desktop user's journal too.
JOURNAL = r"""journalctl --since "@$S" --no-pager -o short-iso-precise </dev/null 2>/dev/null \
    | grep -E ': (console\.[a-z]+|JavaScript (error|warning|error detail)): |:CONSOLE:[0-9]+\] ' | tail -n 2000
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


def _chromium(m: re.Match, everything: bool) -> str | None:
    """A Chromium console line in the shape of Firefox's: uncaught ones as JavaScript errors."""
    line, text, source = m.groups()
    if source is None:
        text += " …"
    elif not everything and source.startswith(CHROMIUM_OWN):
        return None
    if text.startswith("Uncaught "):
        return f"JavaScript error: {source or ''}, line {line}: {text}"
    return f'{UNLEVELLED}"{text}"' + (f" ({source}, line {line})" if source else "")


def stamped(text: str, everything: bool = False) -> list[tuple[float, str]]:
    """(guest time, `TIME console.error: ...`) from journalctl short-iso-precise output."""
    out: list[tuple[float, str]] = []
    detailed: set[int] = set()
    for line in text.splitlines():
        d = DETAIL.search(line)
        if d:
            _attach(out, d.group(1), detailed)
            continue
        c = CHROMIUM_LINE.search(line)
        if c:
            message = _chromium(c, everything)
        else:
            m = MESSAGE.search(line)
            message = m.group(0) if m and (everything or not FIREFOX_OWN.match(m.group(0))) else None
        if message is None:
            continue
        stamp = line.split(" ", 1)[0]
        try:
            when = datetime.fromisoformat(stamp).timestamp()
        except ValueError:
            when = 0.0
        out.append((when, f"{stamp[11:19]} {message}"))
    return out


def parse(text: str, everything: bool = False) -> list[str]:
    return [line for _, line in stamped(text, everything)]


def errors(lines: list[str]) -> list[str]:
    return [ln for ln in lines if ERROR.match(ln.split(" ", 1)[1])]


def unlevelled(lines: list[str]) -> list[str]:
    """Chromium's messages, each of which may be console.error as well as console.log."""
    return [ln for ln in lines if ln.split(" ", 1)[1].startswith(UNLEVELLED)]


def unlevelled_note(count: int) -> str:
    return (f"{count} Chromium console messages that could be errors or not: Chromium logs console.error at "
            "the level of console.log")


class Unchecked(DeskError):
    """Errors in the web console that desk cannot count."""


def console(run, everything: bool = False) -> list[str]:
    from .clock import guest_since

    spec = load_image(run.image)
    if spec.family == "windows":
        raise DeskError("desk browser console reads the browsers of Linux guests")
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
        raise DeskError("desk browser drives Firefox and Chromium on Linux guests")
    spec.need_desktop("desk browser")
    return spec


def _which(page: dict, choice: str | None) -> str:
    """The browser asked for, else the one in the focused window, else Firefox."""
    if choice:
        return NAMES[choice]
    return page["app"] if page.get("app") in (FIREFOX, CHROMIUM) else FIREFOX


def address(target: str) -> bool:
    """A path on the site shown (/system/services) or a whole address, which open_url takes."""
    return target.startswith("/") or "://" in target or target.startswith(("about:", "file:", "data:"))


def open_url(run, target: str, timeout: float = 30, browser: str | None = None) -> tuple[str, str]:
    """Open an address in the guest's browser the way another program would (`firefox URL`),
    which replaces the page in the current tab (desk-firefox.js, desk-chromium-flags); a path
    (/system/services) is taken relative to the page shown now. Returns the address the page ended
    up at and the one that was opened."""
    _linux(run)
    if not address(target):
        raise DeskError(f"give a whole address (https://{target}) or a path starting with /: "
                        "the browser would guess the scheme, http:// for most hosts")
    page = a11y.page_url(run)
    name = _which(page, browser)
    before = page.get("url") if page.get("app", name) == name else None
    if target.startswith("/"):
        if not before:
            raise DeskError(f"no page in a focused {name} window to open {target} on; give the whole address")
        parts = urlsplit(before)
        url = f"{parts.scheme}://{parts.netloc}{target}"
    else:
        url = target
    dom = run.dom()
    deadline = time.monotonic() + timeout
    if urlsplit(url).fragment and before and _address(before, False) == _address(url, False):
        # Both browsers take an address from another program that differs from the page shown
        # only after "#" for no new page at all (Firefox leaves an empty "New Tab", the old
        # picture on screen); from another page they load it whole. Quickly: Cockpit ends its
        # session when none of its pages is open for a few seconds.
        _start(run, name, "about:blank")
        # Chromium gives about:blank no address at all.
        blank = ("about:blank",) if name == FIREFOX else ("about:blank", None)
        while a11y.page_url(run).get("url") not in blank:
            if time.monotonic() > deadline:
                raise DeskError(f"{name} did not leave the page shown for about:blank to open the page again with its "
                                f"new part after #: {url}")
            time.sleep(0.2)
    _start(run, name, url)
    # A link opened from another program does not take the focus on Wayland.
    while True:
        try:
            a11y.focus(run, name)
            break
        except DeskError:
            if time.monotonic() > deadline:
                raise
            time.sleep(1)
    arrived = seen = None
    while time.monotonic() < deadline and not arrived:
        shown = a11y.page_url(run)
        current = shown.get("url")
        seen = current or seen
        # The document is there; what follows "#" a page may rewrite itself (its own routes),
        # which the caller sees in the address returned.
        arrived = bool(current) and under(current, url, fragment=False)
        if not arrived and shown.get("problem") and current != before:
            break  # Chromium's error pages have an address of their own
        if not arrived:
            time.sleep(0.5)
    display.wait_stable(dom, timeout=10)
    page = a11y.page_url(run)
    if page.get("problem"):
        raise NotLoaded(f"{url} did not load: {name} shows {page['problem']!r} instead")
    if not page.get("url") and not arrived:
        # While it waits for a server that does not answer, Firefox shows no page at all.
        raise NotLoaded(f"{url} did not load within {timeout:.0f}s: {name} shows no page (still waiting for the "
                        "server?)")
    going = page.get("address")
    if not arrived and going and _bare(going) != _bare(page["url"]):
        # Chromium keeps the page before while it waits; its address bar has the one it waits for.
        raise NotLoaded(f"{url} did not load within {timeout:.0f}s: {name} still shows {page['url']} while its "
                        f"address bar has {going} (still waiting for the server?)")
    # Somewhere else than asked for is where the site sent the browser (a login, https, back to
    # the page from before); the caller sees both addresses.
    return page.get("url") or seen, url


def _start(run, name: str, url: str) -> None:
    # Fedora calls Chromium chromium-browser, Arch chromium.
    argv = ["firefox", url] if name == FIREFOX else \
        ["bash", "-c", 'exec "$(command -v chromium-browser || command -v chromium)" "$1"', "chromium", url]
    have = "" if name == FIREFOX else "{ command -v chromium-browser || command -v chromium; } >/dev/null || exit 127; "
    r = guest.ssh_run(run.ssh_port, run.user, have + guest.remote_command(argv, gui=True), capture=True, timeout=60)
    if r.returncode == 127 and name == CHROMIUM:
        raise DeskError(f"{run.image} has no Chromium: it was built before its images came with one "
                        f"(desk maintain --only {run.image})")
    if r.returncode != 0:
        raise DeskError(f"starting {name.lower()} failed: {r.stderr.decode(errors='replace').strip()[-300:]}")


def _address(url: str, fragment: bool = True) -> tuple[str, str, str]:
    parts = urlsplit(url)
    scheme = "http" if parts.scheme.lower() in ("http", "https") else parts.scheme.lower()
    return scheme, parts.netloc.lower(), parts.path.rstrip("/") + ("?" + parts.query if parts.query else "") + \
        ("#" + parts.fragment if fragment and parts.fragment else "")


def _bare(url: str) -> str:
    """An address the way Chromium's address bar writes it: decoded, without http(s):// or
    file://, www. or the slash at the end."""
    return re.sub(r"^(https?://|file://)?(www\.)?", "", unquote(url.strip())).rstrip("/")


def under(shown: str, asked: str, fragment: bool = True) -> bool:
    """`shown` is the page `asked` for or one below it, as the browsers write addresses: the host
    in lower case, https where the site upgrades http, file:/x as file:///x; what follows "#" too
    unless `fragment` is false."""
    (scheme, host, path), (wanted_scheme, wanted_host, wanted_path) = \
        _address(shown, fragment), _address(asked, fragment)
    return (scheme, host) == (wanted_scheme, wanted_host) and path.startswith(wanted_path)


RDM_CLOSE = "Close Responsive Design Mode"


def _rdm(run, focus: str | None = None, zoom: bool = False) -> dict:
    """Firefox's responsive design mode or Chromium's device mode in the focused window; focus
    "page" or (Chromium) "devtools" moves the keyboard focus there, zoom sets Chromium's to 100 %.
    _helper has turned its box into screen pixels already."""
    return a11y._helper(run, "rdm", json.dumps({"focus": focus, "zoom": zoom}))


def _rdm_fields(run, name: str = FIREFOX) -> list[dict]:
    if name == CHROMIUM:
        return _rdm(run).get("fields") or []
    snap = a11y.snapshot(run, app="Firefox", limit=400, save=False)
    bar = next((n for n in snap["nodes"] if n["role"] == "embedded" and n["name"].startswith("chrome://devtools/"
                                                                                            "content/responsive")), None)
    return [n for n in snap["nodes"] if bar is not None and n["role"] == "spin button"
            and n["pid"] == bar["pid"] and n["path"][:len(bar["path"])] == bar["path"]]


def _until(run, ready, failure: str) -> dict:
    deadline = time.monotonic() + 15
    while not ready(state := _rdm(run)):
        if time.monotonic() > deadline:
            raise DeskError(failure)
        time.sleep(0.5)
    return state


def viewport(run, size: str, browser: str | None = None) -> str:
    """Pages at WIDTHxHEIGHT CSS pixels, a phone's layout, through Firefox's responsive design mode
    or Chromium's device mode (a window cannot get that narrow), with its sizes typed into the
    mode's toolbar like a person would; `reset` closes the mode. Returns the size and where the
    page area is on screen."""
    spec = _linux(run)
    name = _which({} if browser else a11y.page_url(run), browser)
    chromium = name == CHROMIUM
    mode = "Chromium's device mode" if chromium else "Firefox's responsive design mode"
    a11y.focus(run, name)
    state = _rdm(run)
    if size == "reset":
        if chromium and state.get("devtools"):
            # The device mode is the DevTools' and ends with them.
            _rdm(run, focus="devtools")
            display.key(run.dom(), "f12")
            _until(run, lambda s: not s.get("devtools"), "Chromium's DevTools did not close (F12)")
            display.wait_stable(run.dom(), timeout=5)
        elif not chromium and state["open"]:
            display.click(run.dom(), *a11y.locate_name(run, RDM_CLOSE, exact=True), run=run)
            display.wait_stable(run.dom(), timeout=5)
        return f"{'DevTools' if chromium else 'responsive design mode'} closed: pages get the whole window again"
    try:
        width, height = (int(v) for v in size.lower().split("x"))
    except ValueError:
        raise DeskError(f"viewport is WIDTHxHEIGHT, e.g. 390x700, or reset (got {size!r})") from None
    if chromium and not state.get("devtools"):
        display.key(run.dom(), "ctrl+shift+i")
        state = _until(run, lambda s: s.get("devtools"), "Chromium's DevTools did not open (ctrl+shift+i)")
    if not state["open"]:
        if chromium:
            # DevTools take ctrl+shift+m only while they have the keyboard focus.
            _rdm(run, focus="devtools")
        display.key(run.dom(), "ctrl+shift+m")
        state = _until(run, lambda s: s["open"], f"{mode} did not open (ctrl+shift+m)")
    if chromium:
        # A page fitted to the window would be smaller than its CSS pixels on screen, and clicks
        # by the accessibility tree would miss. The toolbar is drawn anew afterwards, which empties
        # a size typed meanwhile.
        if not _rdm(run, zoom=True).get("zoomed"):
            _until(run, lambda s: s.get("zoomed"), f"{mode} did not take the zoom 100 % (its toolbar's Zoom)")
        display.wait_stable(run.dom(), timeout=5)
    # The first time in a Firefox session the mode's toolbar is there a moment before its fields.
    deadline = time.monotonic() + 15
    while len(fields := _rdm_fields(run, name)) < 2:
        if time.monotonic() > deadline:
            raise DeskError(f"cannot find the width and height fields of {mode}")
        time.sleep(0.5)
    specs = [{k: node[k] for k in ("pid", "path", "role", "name")} for node in fields[:2]]
    for node, value in zip(specs, (width, height), strict=True):
        note = a11y.replace_text(run, node, str(value), spec.layout)
        display.key(run.dom(), "ret")
        if note:
            raise DeskError(f"setting the viewport size: {note}")
    display.wait_stable(run.dom(), timeout=5)
    if chromium:
        typed = [a11y._helper(run, "read", json.dumps(node))["text"] for node in specs]
        if typed != [str(width), str(height)]:
            raise DeskError(f"setting the viewport size: {mode}'s fields show {' x '.join(typed)} afterwards")
    # Keys typed next are for the page, not the mode's height field.
    box = _rdm(run, focus="page").get("bbox")
    if not box:
        return f"page area {width}x{height} CSS pixels (where it is on screen is unknown; look at a screenshot)"
    # Firefox's page sits inside a 1 px frame.
    x, y, w, h = box if chromium else (box[0] + 1, box[1] + 1, box[2] - 2, box[3] - 2)
    f = (run.scale or 100) / 100
    shown = (round(w / f), round(h / f))
    taller = "(desk display --resolution 1280x1024 for a taller one)"
    clipped = ""
    if chromium and shown[0] == width and shown[1] < height:
        # Chromium's box is the part of the page its window shows.
        shown, clipped = (width, height), f" and runs past the bottom of the window {taller}"
    note = "" if shown == (width, height) else f" (asked for {width}x{height}; {name} shows {shown[0]}x{shown[1]})"
    if y + h > display.png_size(display.screenshot(run.dom()))[1]:
        clipped = f" and runs past the bottom of the screen {taller}"
    return (f"page area {shown[0]}x{shown[1]} CSS pixels{note}{clipped}; on screen: "
            f"desk shot --region {x},{y},{w},{h}; desk browser viewport reset to close the mode")
