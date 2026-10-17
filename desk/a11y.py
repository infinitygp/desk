"""Accessibility tree of the guest desktop, with refs (@e1...) that map to screen pixels."""

import difflib
import json
import os
import shlex
import subprocess
from pathlib import Path

from . import display, guest, winagent
from .config import DeskError, load_image

HELPER = Path(__file__).with_name("guest_a11y.py")
SESSION_ENV = "env DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus"


def _helper(run, *args: str) -> dict:
    if run.agent_port:
        # Windows: UI Automation through the in-session agent, same protocol as guest_a11y.py.
        op, arg, *rest = args
        return winagent.request(run, cmd="uia", op=op, arg=json.loads(arg), text=rest[0] if rest else None)
    if getattr(run, "image", None):
        load_image(run.image).need_desktop("the accessibility tree (a11y, find, clicks by name, focus)")
    cmd = f"{SESSION_ENV} python3 - " + " ".join(shlex.quote(a) for a in args)
    try:
        r = guest.ssh_run(run.ssh_port, run.user, cmd, input=HELPER.read_bytes(), capture=True, timeout=60)
    except subprocess.TimeoutExpired as e:
        raise DeskError("a11y helper timed out (an application may not be answering on the a11y bus)") from e
    if r.returncode != 0:
        raise DeskError(f"a11y helper failed: {r.stderr.decode(errors='replace').strip()[-500:]}")
    out = json.loads(r.stdout)
    if "error" in out:
        raise DeskError(out["error"])
    if getattr(run, "scale", None) and run.scale != 100:
        # GNOME's logical layout: toolkits and the shell speak logical pixels; the screen has more.
        f = run.scale / 100
        for box in [n.get("bbox") for n in out.get("nodes", [])] + [out.get("bbox"), out.get("point")]:
            if box:
                box[:] = [round(v * f) for v in box]
    return out


def _on_screen(bbox: list[int], size: tuple[int, int]) -> bool:
    cx, cy = center(bbox)
    return 0 <= cx < size[0] and 0 <= cy < size[1]


def snapshot(run, app: str | None = None, all_windows: bool = False, limit: int = 300,
             save: bool = True) -> dict:
    snap = _helper(run, "snapshot", json.dumps({"app": app, "all": all_windows, "max": limit}))
    size = display.png_size(display.screenshot(run.dom()))
    # Scrolled-away content keeps its extents; mark it so it is listed apart from what can be
    # clicked right now.
    for i, node in enumerate(snap["nodes"], 1):
        node["ref"] = f"e{i}"
        if not _on_screen(node["bbox"], size):
            node["offscreen"] = True
    if save:
        # Refs printed to the agent must keep pointing at this snapshot. Whole or not at all:
        # another desk command may be reading the refs right now.
        tmp = run.dir / f".a11y.json.{os.getpid()}"
        tmp.write_text(json.dumps(snap))
        os.replace(tmp, run.dir / "a11y.json")
    return snap


def center(bbox: list[int]) -> tuple[int, int]:
    x, y, w, h = bbox
    return x + w // 2, y + h // 2


def format_node(node: dict, indent: bool = True) -> str:
    pad = "  " * node["depth"] if indent else ""
    parts = [f"{pad}@{node['ref']} {node['role']}"]
    if node["name"]:
        parts.append(json.dumps(node["name"], ensure_ascii=False))
    if node.get("value") is not None:
        parts.append(f"value={json.dumps(node['value'], ensure_ascii=False)}")
    if node.get("text"):
        parts.append(f"text={json.dumps(node['text'], ensure_ascii=False)}")
    states = node["states"] + (["offscreen"] if node.get("offscreen") else []) + \
        (["covered"] if node.get("covered") else [])
    if states:
        parts.append("[" + ",".join(states) + "]")
    if not node.get("offscreen"):
        cx, cy = center(node["bbox"])
        parts.append(f"({cx},{cy})")
    if node.get("covered"):
        parts.append(f"under {node['covered']}")
    return " ".join(parts)


def format_snapshot(snap: dict, offscreen: bool = False) -> str:
    lines = []
    hidden = 0
    tops = snap["tops"] + [{"start": len(snap["nodes"])}]
    for top, nxt in zip(tops, tops[1:], strict=False):
        mark = " [focused]" if top["focus"] else ""
        lines.append(f"window {json.dumps(top['title'], ensure_ascii=False)} ({top['app']}){mark}")
        if top.get("unavailable"):
            lines.append("  (contents not available through UI Automation right now: "
                         f"{top['unavailable'].strip()[:120]}; a popup may be in the way, look at a screenshot)")
        for n in snap["nodes"][top["start"]:nxt["start"]]:
            if n.get("offscreen") and not offscreen:
                hidden += 1
            else:
                lines.append("  " + format_node(n))
    if not snap["tops"]:
        lines.append("(no accessible windows in scope; try --all)")
    if hidden:
        lines.append(f"... {hidden} more outside the visible area (desk a11y --offscreen); "
                     "clicking their @ref scrolls them into view")
    if snap["truncated"] == "time":
        lines.append("... cut short: the application took too long to answer; narrow with --app")
    elif snap["truncated"]:
        lines.append("... truncated; narrow with --app or raise --max")
    return "\n".join(lines)


def _spec(run, ref: str) -> dict:
    f = run.dir / "a11y.json"
    if not f.exists():
        raise DeskError("no a11y snapshot yet (desk a11y)")
    ref = ref.lstrip("@")
    node = next((n for n in json.loads(f.read_text())["nodes"] if n["ref"] == ref), None)
    if node is None:
        raise DeskError(f"no {ref} in the last snapshot")
    return {k: node[k] for k in ("pid", "path", "role", "name")}


def _point(run, spec: dict, size: tuple[int, int]) -> tuple[tuple[int, int] | None, str | None]:
    res = _helper(run, "resolve", json.dumps(spec))
    # The guest finds a point where a click reaches the element (hit tests); Windows' agent only
    # says whether it is showing.
    point = res["point"] if "point" in res else (center(res["bbox"]) if res["showing"] else None)
    if point and not _on_screen([*point, 1, 1], size):
        point = None
    return (tuple(point) if point else None), res.get("covered")


def _visible_point(run, spec: dict) -> tuple[int, int]:
    """Fresh screen position (layouts move) where a click reaches the element itself, not a sticky
    bar or toast over it; scrolls it into view first if needed."""
    # A page still building moves what it has already drawn; wait for it, briefly.
    display.wait_stable(run.dom(), timeout=2, frames=2)
    size = display.png_size(display.screenshot(run.dom()))
    point, covered = _point(run, spec, size)
    # Scrolled away: the toolkit scrolls it into view. Covered by a sticky bar: to the top, then
    # the bottom edge of the view, away from it.
    for how in ("anywhere", "top", "bottom"):
        if point or (how != "anywhere" and not covered):
            break
        _helper(run, "scroll", json.dumps(spec), *([] if how == "anywhere" else [how]))
        display.wait_stable(run.dom(), timeout=3)
        point, covered = _point(run, spec, size)
    if point:
        return point
    if covered:
        raise DeskError(f"{spec['role']} {spec['name']!r} is covered by {covered}, also after scrolling; "
                        "close or move what is in the way, or click by pixels from a screenshot")
    raise DeskError(f"{spec['role']} {spec['name']!r} is outside the visible area and could not be "
                    "scrolled into view; scroll with `desk scroll` and look again")


def locate_ref(run, ref: str) -> tuple[int, int]:
    return _visible_point(run, _spec(run, ref))


# ARIA's names for roles, which agents testing web pages think in, for AT-SPI's.
ROLE_ALIASES = {
    "tab": "page tab", "tablist": "page tab list", "tabpanel": "panel", "checkbox": "check box",
    "radio": "radio button", "textbox": "entry", "searchbox": "entry", "combobox": "combo box",
    "menuitem": "menu item", "listitem": "list item", "treeitem": "tree item", "spinbutton": "spin button",
    "img": "image", "row": "table row", "cell": "table cell", "gridcell": "table cell",
    "columnheader": "column header", "rowheader": "row header",
}


def role_name(role: str | None) -> str | None:
    return ROLE_ALIASES.get(role, role) if role else role


def _matches(snap: dict, query: str, role: str | None, exact: bool) -> list[dict]:
    q = query.casefold()
    role = role_name(role)

    def match(n: dict) -> bool:
        if role and n["role"] != role:
            return False
        hay = [n["name"], n.get("value") or "", n.get("text") or ""]
        return any((h.casefold() == q) if exact else (q in h.casefold()) for h in hay)

    return [n for n in snap["nodes"] if match(n)]


def explain_missing(run, snap: dict | None, query: str) -> str:
    """For a failure message: the focused window, names close to the one looked for, and what of
    the desktop lies over the windows (the overview, a notification), one line each."""
    lines = []
    if snap is not None:
        top = next((t for t in snap["tops"] if t["focus"]), None)
        lines.append(f"focused window: {json.dumps(top['title'], ensure_ascii=False)} ({top['app']})"
                     if top else "no window has the focus")
        names = {}
        for n in snap["nodes"]:
            for h in (n["name"], n.get("text"), n.get("value")):
                if h and len(h) <= 2 * len(query) + 20:
                    names.setdefault(h, n["role"])
        close = difflib.get_close_matches(query, list(names), n=3, cutoff=0.6)
        if close:
            lines.append("similar: " + ", ".join(f"{names[c]} {json.dumps(c, ensure_ascii=False)}" for c in close))
    if not run.agent_port:
        try:
            shell = _helper(run, "shell", "{}")
        except DeskError:
            shell = {}
        if shell.get("overview"):
            lines.append("the overview is open over the windows")
        if banner := shell.get("banner"):
            lines.append(f"a notification of {banner['source'] or 'the system'} is on screen: "
                         f"{json.dumps(banner['title'], ensure_ascii=False)}")
    return "".join(f"\n{line}" for line in lines)


def find(run, query: str, role: str | None = None, exact: bool = False,
         all_windows: bool = False, save: bool = True) -> list[dict]:
    return _matches(snapshot(run, all_windows=all_windows, limit=2000, save=save), query, role, exact)


# A dialog that is open has the user's attention; a modal one blocks everything behind it.
DIALOG_ROLES = {"dialog", "alert", "file chooser"}


def _ranker(snap: dict):
    dialogs = [(n["pid"], n["path"]) for n in snap["nodes"] if n["role"] in DIALOG_ROLES]

    def in_dialog(n: dict) -> bool:
        return any(n["pid"] == pid and n["path"][:len(path)] == path and n["path"] != path for pid, path in dialogs)

    def rank(n: dict) -> tuple[bool, bool, bool, bool]:
        # Visible first, then not covered by something else, then inside an open dialog (its
        # "Close" over the browser window's), then controls over the text describing them.
        return (bool(n.get("offscreen")), bool(n.get("covered")), not in_dialog(n),
                n["role"] in ("label", "heading", "paragraph", "static", "section"))

    return rank


def best_match(snap: dict, hits: list[dict]) -> dict:
    return min(hits, key=_ranker(snap))


def pick(snap: dict, hits: list[dict], query: str) -> dict:
    """The element a click by name means: the whole name over a part of one, and no guessing
    between different names that merely contain the query ("Save" vs "Save as...")."""
    q = query.casefold()
    whole = [n for n in hits if q in {(n.get(k) or "").casefold() for k in ("name", "value", "text")}]
    if whole:
        return best_match(snap, whole)
    rank = _ranker(snap)
    best = min(rank(n) for n in hits)
    top = [n for n in hits if rank(n) == best]
    names = sorted({n["name"] or n.get("text") or n.get("value") or "" for n in top})
    if len(names) > 1:
        shown = ", ".join(f"{n!r}" for n in names[:8]) + (" ..." if len(names) > 8 else "")
        raise DeskError(f"{query!r} is part of several names: {shown}; give the whole name "
                        "(or --role to narrow it down)")
    return top[0] if len(top) == 1 else best_match(snap, top)


def role_hint(snap: dict, query: str, role: str | None, exact: bool) -> str:
    """When nothing of a role matches: what the name belongs to instead."""
    if not role:
        return ""
    others = sorted({f"{n['role']} {n['name'] or n.get('text') or n.get('value')!r}"
                     for n in _matches(snap, query, None, exact)})
    return f" with role {role!r}; there is: {', '.join(others[:6])}" if others else ""


def locate_name(run, query: str, role: str | None = None, exact: bool = False) -> tuple[int, int]:
    snap = snapshot(run, limit=2000, save=False)
    hits = _matches(snap, query, role, exact)
    if not hits:
        snap = snapshot(run, all_windows=True, limit=2000, save=False)
        hits = _matches(snap, query, role, exact)
    if not hits:
        cut = " (the listing was cut short: the application answers slowly)" if snap["truncated"] == "time" else ""
        raise DeskError(f"nothing named {query!r}{role_hint(snap, query, role, exact)} on screen{cut}")
    best = pick(snap, hits, query)
    return _visible_point(run, {k: best[k] for k in ("pid", "path", "role", "name")})


def focus(run, query: str) -> str:
    """Bring the topmost window whose application or title contains `query` to the front, as a
    user clicking it would; returns its title."""
    if run.agent_port:
        reply = winagent.request(run, cmd="focus", query=query)
        if reply.get("title") and reply.get("focused") is False:
            reply = winagent.request(run, cmd="focus", query=query)  # Windows refuses now and then
        title = reply.get("title", "")
        if title and reply.get("focused") is False:
            raise DeskError(f"Windows kept {title!r} behind the window in front; keys would go to that one "
                            "(click the window, or close what is in front)")
    else:
        title = _helper(run, "focus", json.dumps(query))["title"]
    if not title:
        raise DeskError(f"no window of an application or with a title containing {query!r}")
    return title


def action(run, ref: str, name: str | None = None) -> dict:
    args = ["action", json.dumps(_spec(run, ref))] + ([name] if name else [])
    return _helper(run, *args)


def set_text(run, ref: str, text: str, layout: str = "us") -> str:
    """Replace a field's text the way a user would, so that web pages notice (React reads the
    input events of real typing, and AT-SPI's text replacement sends none): focus the field,
    select everything, then type, or paste what the keyboard cannot produce. Returns a note when
    only AT-SPI could do it or the field shows something else afterwards."""
    return replace_text(run, _spec(run, ref), text, layout)


def _shown(text: str) -> str:
    """Text as the guests read it back: whitespace runs as one space, at most 400 characters."""
    return " ".join(text.replace("\ufffc", " ").split())[:400]


def replace_text(run, spec: dict, text: str, layout: str = "us") -> str:
    if run.agent_port:
        shown = _helper(run, "set-text", json.dumps(spec), text).get("text")
        if shown is None or spec["role"] == "password text" or _shown(shown) == _shown(text):
            return ""
        return f"the field shows {_shown(shown)!r} afterwards"
    if not _helper(run, "edit", json.dumps(spec))["focused"]:
        _helper(run, "set-text", json.dumps(spec), text)
        return ("the field did not take the keyboard focus, so its text was replaced through AT-SPI only, "
                "which a web page does not notice; click into it and use desk type")
    dom = run.dom()
    display.key(dom, "ctrl+a")
    try:
        # Newlines and tabs typed would be Return and Tab: a form submitted, the focus moved.
        plan = None if any(c in text for c in "\n\t\r") else display.key_plan(text, layout)
    except DeskError:
        plan = None
    if not text:
        display.key(dom, "delete")
    elif plan is not None:
        for codes in plan:
            display.chord(dom, codes)
    else:
        try:
            _helper(run, "clipboard", json.dumps(text))
        except DeskError:
            _helper(run, "set-text", json.dumps(spec), text)
            return ("the text has characters the keyboard cannot type and this image cannot paste "
                    "(desk image warm), so it was replaced through AT-SPI only, which a web page does not notice")
        display.key(dom, "ctrl+v")
    if spec["role"] == "password text":
        return ""  # it shows bullets, not the text
    shown = _helper(run, "read", json.dumps(spec))["text"]
    return "" if shown == _shown(text) else f"the field shows {shown!r} afterwards"


# Web browsers as AT-SPI names them: their pages take keys pressed outside a text field as
# shortcuts (Firefox's quick find on "/" and "'").
BROWSERS = {"Firefox", "Chromium", "Google Chrome", "Epiphany"}


def check_typing_target(run, spec) -> None:
    """Refuse to type into a browser window whose focus is not in a text field: a click that
    missed its field (the layout moved) would otherwise turn the text into page shortcuts."""
    if spec.family != "linux":
        return
    try:
        f = focused_field(run)
    except DeskError:
        return  # where the focus is cannot be told; typing never depended on it before
    # Only a focused element that was found and takes no text: a walk that ran out of time or
    # missed an element Firefox does not mark as showing proves nothing.
    if f.get("app") not in BROWSERS or not f.get("role") or f.get("editable"):
        return
    where = f"{f['role']} {f['name']!r}" if f.get("role") else "nothing"
    raise DeskError(f"the keyboard focus in {f.get('window')!r} is on {where}, not in a text field, so the page "
                    "would take the keys as shortcuts; click into the field first (desk type --anywhere to "
                    "type there anyway)")


def focused_field(run) -> dict:
    """The element with the keyboard focus in the focused window (Linux guests)."""
    return _helper(run, "focused", "{}")


def page_url(run) -> dict:
    """The address of the page in the focused browser window (Linux guests)."""
    return _helper(run, "page", "{}")
