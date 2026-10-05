"""Runs inside the guest (piped to `python3 -` over SSH): AT-SPI tree with screen coordinates.

Usage: snapshot JSON_OPTS | resolve JSON_SPEC | action JSON_SPEC | set-text JSON_SPEC TEXT
Prints one JSON document. Needs DBUS_SESSION_BUS_ADDRESS of the graphical session.
"""

import json
import sys

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, Gio, GLib  # noqa: E402

INTERACTIVE = {
    "push button", "toggle button", "check box", "radio button", "menu item", "check menu item",
    "radio menu item", "entry", "password text", "text", "combo box", "link", "page tab",
    "list item", "tree item", "table cell", "slider", "spin button", "switch", "menu", "icon",
    "button", "toggle", "editbar", "terminal",
}
# Roles whose name is only a container caption and adds noise without children context.
QUIET = {"filler", "panel", "section", "scroll pane", "viewport", "layered pane", "split pane",
         "redundant object", "unknown", "grouping"}
TEXT_ROLES = {"label", "heading", "paragraph", "static", "caption", "document web",
              "alert", "notification", "status bar", "block quote", "list item"}
# Hidden roots: a background tab's document or an unmapped window is not "off-screen
# content", it is simply not there for the user.
HIDDEN_ROOTS = {"document web", "frame", "window", "dialog", "internal frame", "alert", "file chooser"}
MAX_VISIT = 8000
MAX_OFFSCREEN = 300
OBJ = "￼"


def shell_windows() -> list[dict]:
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        res = bus.call_sync("org.desk.Helper", "/org/desk/Helper", "org.desk.Helper", "GetWindows",
                            None, GLib.VariantType("(s)"), Gio.DBusCallFlags.NONE, 2000, None)
        return json.loads(res.unpack()[0])
    except GLib.Error:
        return []


def focus(query: str) -> dict:
    """Bring a window to the front by application or title (the shell extension does it)."""
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    res = bus.call_sync("org.desk.Helper", "/org/desk/Helper", "org.desk.Helper", "Activate",
                        GLib.Variant("(s)", (query,)), GLib.VariantType("(s)"), Gio.DBusCallFlags.NONE, 2000, None)
    return {"title": res.unpack()[0]}


def states(acc) -> set[str]:
    ss = acc.get_state_set()
    return {s.value_nick for s in Atspi.StateType.__enum_values__.values() if ss.contains(s)}


def extents(acc, coord):
    try:
        r = acc.get_extents(coord)
    except (GLib.Error, AttributeError, TypeError):
        return None
    if r.width <= 0 or r.height <= 0:
        return None
    return [r.x, r.y, r.width, r.height]


def text_of(acc) -> str:
    try:
        if "Text" not in acc.get_interfaces():
            return ""
        n = Atspi.Text.get_character_count(acc)
        t = Atspi.Text.get_text(acc, 0, min(n, 400))
    except GLib.Error:
        return ""
    return " ".join(t.replace(OBJ, " ").split())


def app_children(desktop):
    for i in range(desktop.get_child_count()):
        try:
            app = desktop.get_child_at_index(i)
        except GLib.Error:
            continue
        if app is not None:
            yield i, app


def toolkit_major(app) -> tuple[str, int]:
    try:
        name = (app.get_toolkit_name() or "").lower()
        major = int((app.get_toolkit_version() or "0").split(".")[0])
    except (GLib.Error, ValueError):
        return ("", 0)
    return (name, major)


class Mapper:
    """Maps a top-level window's AT-SPI coordinates to screen pixels."""

    def __init__(self, windows: list[dict]):
        self.windows = windows

    def for_toplevel(self, app_name: str, pid: int, top, app=None) -> tuple[str, int, int] | None:
        if app_name == "gnome-shell":
            return ("screen", 0, 0)
        name = top.get_name() or ""
        cands = [w for w in self.windows if w["pid"] == pid and not w["minimized"]]
        if not cands:
            # Sandboxed (Flatpak) apps reach the a11y bus through a proxy with another pid.
            cands = [w for w in self.windows if w["title"] == name and not w["minimized"]]
        named = [w for w in cands if w["title"] == name]
        win = named[-1] if named else (cands[-1] if len(cands) == 1 else None)
        if win is None:
            return None
        if win["x11"]:
            return ("screen", 0, 0)
        # GTK 3 reports window coordinates from the surface, client-side shadow included; GTK 4
        # and others from the visible frame (the xdg window geometry). Measured on GNOME 50.
        if app is not None and toolkit_major(app) == ("gtk", 3):
            return ("window", win["buffer"][0], win["buffer"][1])
        return ("window", win["frame"][0], win["frame"][1])

    @staticmethod
    def place(acc, base):
        kind, ox, oy = base
        coord = Atspi.CoordType.SCREEN if kind == "screen" else Atspi.CoordType.WINDOW
        e = extents(acc, coord)
        if e is None:
            return None
        return [e[0] + ox, e[1] + oy, e[2], e[3]]


def process_of(acc) -> int:
    try:
        return acc.get_process_id()
    except GLib.Error:
        return -1


def embedded_base(parent, parent_base, child):
    """`child` roots another process's tree (WebKit's web process under a Tauri or Epiphany
    window). Its window coordinates start at its own origin, which sits where `parent`, the
    embedding element, is on screen."""
    pbox = Mapper.place(parent, parent_base)
    cext = extents(child, Atspi.CoordType.WINDOW)
    if pbox is None or cext is None:
        return parent_base
    return ("window", pbox[0] - cext[0], pbox[1] - cext[1])


def stacked_base(parent_box, child_box, base):
    """GTK 4 reports a widget's bounds relative to its parent widget, and a GtkStack page (an
    accessible that is no widget) reports its child's bounds, so the child's offset counts twice
    (12 px off in gnome-calculator's keypad, GTK 4.22). A child as large as its parent cannot
    sit at an offset inside it: take that offset back for the child and everything below."""
    if parent_box is None or child_box is None or parent_box[2:] != child_box[2:]:
        return base
    dx, dy = child_box[0] - parent_box[0], child_box[1] - parent_box[1]
    if dx == 0 and dy == 0:
        return base
    kind, ox, oy = base
    return (kind, ox - dx, oy - dy)


def describe(acc, base, pid, path, depth):
    role = acc.get_role_name()
    st = states(acc)
    bbox = Mapper.place(acc, base)
    name = (acc.get_name() or "").strip()
    node = {"pid": pid, "path": path, "role": role, "name": name, "depth": depth, "bbox": bbox,
            "states": sorted(st & {"focused", "checked", "selected", "expanded", "pressed", "editable",
                                   "active"})}
    if "sensitive" not in st and "enabled" not in st:
        node["states"].append("disabled")
    if role in INTERACTIVE and ("Text" in acc.get_interfaces()):
        value = text_of(acc)[:120]
        if "editable" in st or (value and value != name):
            node["value"] = value
    elif (role in TEXT_ROLES or role in QUIET) and not name:
        # For containers this is only their own text: children appear as embedded objects,
        # which text_of drops, and are listed separately.
        node["text"] = text_of(acc)[:160]
    return node, st


def wanted(node, st, parent_name) -> bool:
    if node["bbox"] is None:
        return False
    role, name = node["role"], node["name"]
    if role in INTERACTIVE:
        return True
    if role in QUIET:
        return bool(node.get("text")) and node["text"] != parent_name
    if role == "label" and name and name == parent_name:
        return False
    return bool(name or node.get("text"))


def snapshot(opts: dict) -> dict:
    windows = shell_windows()
    mapper = Mapper(windows)
    focus = next((w for w in windows if w["focus"]), None)
    scope_app = opts.get("app")
    want_all = opts.get("all", False)
    limit = opts.get("max", 300)
    desktop = Atspi.get_desktop(0)
    nodes, tops, visited, offscreen = [], [], 0, 0
    truncated = False

    def walk(acc, base, pid, path, depth, parent_name, proc, gtk4, parent_box):
        nonlocal visited, truncated, offscreen
        if visited >= MAX_VISIT or len(nodes) - offscreen >= limit:
            truncated = True
            return
        visited += 1
        try:
            node, st = describe(acc, base, pid, path, depth)
        except GLib.Error:
            return
        if gtk4:
            fixed = stacked_base(parent_box, node["bbox"], base)
            if fixed != base:
                base, node["bbox"] = fixed, list(parent_box)
        # Not "showing" covers content scrolled out of view or clipped by a scrolling list,
        # which the agent can bring into view, but also background tabs and hidden windows,
        # which are pruned. Structural containers can lack "showing" while their children are
        # on screen (Gecko marks a PatternFly tbody that way), so the state is judged per node.
        if "showing" not in st:
            if node["role"] in HIDDEN_ROOTS:
                return
            node["offscreen"] = True
        emit = wanted(node, st, parent_name)
        if emit and node.get("offscreen"):
            emit = offscreen < MAX_OFFSCREEN
            offscreen += emit
        if emit:
            nodes.append(node)
        child_depth = depth + 1 if emit else depth
        pname = node["name"] if emit else parent_name
        try:
            n = acc.get_child_count()
        except GLib.Error:
            return
        for i in range(n):
            try:
                ch = acc.get_child_at_index(i)
            except GLib.Error:
                continue
            if ch is None:
                continue
            ch_proc = process_of(ch)
            ch_base = embedded_base(acc, base, ch) if ch_proc != proc else base
            walk(ch, ch_base, pid, path + [i], child_depth, pname, ch_proc, gtk4, node["bbox"])

    for _, app in app_children(desktop):
        # An application that is exiting or not answering must not break the whole listing.
        try:
            app_name = app.get_name() or ""
            pid = app.get_process_id()
            n_tops = app.get_child_count()
        except GLib.Error:
            continue
        if scope_app and scope_app.lower() not in app_name.lower():
            continue
        for ti in range(n_tops):
            try:
                top = app.get_child_at_index(ti)
                if top is None:
                    continue
                st = states(top)
            except GLib.Error:
                continue
            if "showing" not in st and app_name != "gnome-shell":
                continue
            base = mapper.for_toplevel(app_name, pid, top, app)
            if base is None:
                continue
            is_focus = focus is not None and focus["pid"] == pid and (
                (top.get_name() or "") == focus["title"] or "active" in st)
            if not (want_all or scope_app):
                if focus is not None and not is_focus:
                    continue
                if focus is None and app_name != "gnome-shell":
                    continue
            tops.append({"app": app_name, "pid": pid, "title": top.get_name() or "",
                         "focus": bool(is_focus), "start": len(nodes)})
            walk(top, base, pid, [ti], 0, "", process_of(top), toolkit_major(app) == ("gtk", 4), None)
    return {"windows": windows, "tops": tops, "nodes": nodes, "truncated": truncated}


def find_node(spec: dict):
    """Re-find a node from a snapshot: same path if role and name still match, else first match."""
    desktop = Atspi.get_desktop(0)
    app = None
    for _, a in app_children(desktop):
        try:
            if a.get_process_id() == spec["pid"]:
                app = a
                break
        except GLib.Error:
            continue
    if app is None:
        raise LookupError("application is gone")
    path = spec["path"]
    acc = app
    for i in path:
        acc = acc.get_child_at_index(i) if acc is not None and i < acc.get_child_count() else None
    if acc is not None and acc.get_role_name() == spec["role"] and (acc.get_name() or "").strip() == spec["name"]:
        return app, path, acc

    def search(a, at, depth=0):
        if depth > 60:
            return None
        for i in range(a.get_child_count()):
            ch = a.get_child_at_index(i)
            if ch is None:
                continue
            if ch.get_role_name() == spec["role"] and (ch.get_name() or "").strip() == spec["name"]:
                return at + [i], ch
            hit = search(ch, at + [i], depth + 1)
            if hit is not None:
                return hit
        return None

    top = app.get_child_at_index(path[0]) if path[0] < app.get_child_count() else None
    hit = search(top, [path[0]]) if top is not None else None
    if hit is None:
        raise LookupError(f"{spec['role']} {spec['name']!r} no longer exists")
    return app, hit[0], hit[1]


def base_along(app, path):
    """Coordinate base for the node at `path`, following embedded processes on the way down."""
    acc = app.get_child_at_index(path[0])
    base = Mapper(shell_windows()).for_toplevel(app.get_name() or "", app.get_process_id(), acc, app)
    if base is None:
        return None
    proc = process_of(acc)
    gtk4 = toolkit_major(app) == ("gtk", 4)
    box = Mapper.place(acc, base) if gtk4 else None
    for i in path[1:]:
        ch = acc.get_child_at_index(i)
        ch_proc = process_of(ch)
        if ch_proc != proc:
            base, proc = embedded_base(acc, base, ch), ch_proc
        if gtk4:
            ch_box = Mapper.place(ch, base)
            fixed = stacked_base(box, ch_box, base)
            if fixed != base:
                base, ch_box = fixed, box
            box = ch_box
        acc = ch
    return base


def resolve(spec: dict) -> dict:
    app, path, acc = find_node(spec)
    base = base_along(app, path)
    if base is None:
        raise LookupError("window position unknown")
    bbox = Mapper.place(acc, base)
    if bbox is None:
        raise LookupError("element has no on-screen extents")
    return {"bbox": bbox, "showing": "showing" in states(acc)}


def scroll(spec: dict) -> dict:
    """Bring an element into view the way the toolkit itself would (scrollIntoView and kin)."""
    _, _, acc = find_node(spec)
    done = False
    if "Component" in acc.get_interfaces():
        try:
            done = Atspi.Component.scroll_to(acc, Atspi.ScrollType.ANYWHERE)
        except (GLib.Error, AttributeError, TypeError):
            done = False
    if not done:
        try:
            done = Atspi.Component.grab_focus(acc)
        except (GLib.Error, AttributeError, TypeError):
            done = False
    return {"done": bool(done)}


def action(spec: dict, name: str | None) -> dict:
    _, _, acc = find_node(spec)
    if "Action" not in acc.get_interfaces():
        raise LookupError("element has no actions")
    names = [Atspi.Action.get_action_name(acc, i) for i in range(Atspi.Action.get_n_actions(acc))]
    if not names:
        raise LookupError("element has no actions")
    if name is not None and name not in names:
        raise LookupError(f"no action {name!r}; available: {', '.join(names)}")
    idx = names.index(name) if name is not None else 0
    Atspi.Action.do_action(acc, idx)
    return {"done": names[idx], "actions": names}


def set_text(spec: dict, text: str) -> dict:
    _, _, acc = find_node(spec)
    if "EditableText" not in acc.get_interfaces():
        raise LookupError("element is not editable")
    Atspi.EditableText.set_text_contents(acc, text)
    return {"done": True}


def main() -> None:
    cmd, arg = sys.argv[1], json.loads(sys.argv[2])
    try:
        if cmd == "snapshot":
            out = snapshot(arg)
        elif cmd == "focus":
            out = focus(arg)
        elif cmd == "resolve":
            out = resolve(arg)
        elif cmd == "scroll":
            out = scroll(arg)
        elif cmd == "action":
            out = action(arg, sys.argv[3] if len(sys.argv) > 3 else None)
        elif cmd == "set-text":
            out = set_text(arg, sys.argv[3])
        else:
            out = {"error": f"unknown command {cmd}"}
    except (LookupError, GLib.Error) as e:
        out = {"error": str(e)}
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()
