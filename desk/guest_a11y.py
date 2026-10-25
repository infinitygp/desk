"""Runs inside the guest (piped to `python3 -` over SSH): AT-SPI tree with screen coordinates.

Usage: snapshot JSON_OPTS | resolve JSON_SPEC | scroll JSON_SPEC [HOW] | action JSON_SPEC [NAME] |
set-text JSON_SPEC TEXT | edit JSON_SPEC | read JSON_SPEC | focused JSON | page JSON |
clipboard JSON_TEXT | rdm JSON | focus JSON_QUERY | shell JSON
Prints one JSON document. Needs DBUS_SESSION_BUS_ADDRESS of the graphical session, and
DESK_DESKTOP, the desktop it runs (where windows are on the screen comes from it).
"""

import json
import os
import sys
import tempfile
import time

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, Gio, GLib  # noqa: E402

INTERACTIVE = {
    "push button", "toggle button", "check box", "radio button", "menu item", "check menu item",
    "radio menu item", "entry", "password text", "text", "combo box", "link", "page tab",
    "list item", "tree item", "table cell", "slider", "spin button", "switch", "menu", "icon",
    "button", "toggle", "editbar", "terminal",
}
# Roles whose name is only a container caption and adds noise without children context: listed
# only for text of their own.
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
# A large web page answers slowly enough that two walks of it took minutes; past this a listing
# is cut short (and says so) rather than leaving the caller waiting.
WALK_SECONDS = 20
# Looking a node up again after the tree changed searches the application's whole tree.
SEARCH_SECONDS = 10
# Hit tests (what is really at a point) per listing; Firefox answers each in about a millisecond.
HIT_SECONDS = 3
# Text of a text element that find and expect search: a log in a <pre> is a single element.
TEXT_CHARS = 10000
DESKTOP = os.environ.get("DESK_DESKTOP", "gnome")


def _helper(method: str, args=None, reply: str | None = None):
    """A method of the GNOME Shell extension (org.desk.Helper)."""
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    res = bus.call_sync("org.desk.Helper", "/org/desk/Helper", "org.desk.Helper", method, args,
                        GLib.VariantType(reply) if reply else None, Gio.DBusCallFlags.NONE, 2000, None)
    return res.unpack()[0] if reply else None


# KWin runs scripts but answers none of their results over D-Bus: a script hands them back by
# calling the helper, which listens on its own connection meanwhile.
REPLY_IFACE = Gio.DBusNodeInfo.new_for_xml(
    '<node><interface name="org.desk.Reply"><method name="Reply"><arg type="s" direction="in"/></method>'
    '</interface></node>').interfaces[0]
KWIN_WINDOWS = """
function box(g) { return [Math.round(g.x), Math.round(g.y), Math.round(g.width), Math.round(g.height)]; }
const focus = workspace.activeWindow;
// Stacking order, bottom first. A window's accessible coordinates count from its client area,
// which is its frame unless KWin draws the decoration; GTK 3 and Chromium count from the buffer.
// Only the class tells an XWayland window ("KWin::X11Window(0x...)").
reply(JSON.stringify(workspace.stackingOrder.map(w => ({
    title: w.caption, wm_class: w.resourceClass, pid: w.pid, x11: String(w).startsWith("KWin::X11Window"),
    minimized: w.minimized,
    focus: w === focus, frame: box(w.clientGeometry), buffer: box(w.bufferGeometry)}))));
"""
KWIN_ACTIVATE = """
const q = QUERY.toLowerCase();
const hit = workspace.stackingOrder.slice().reverse().find(w => w.normalWindow &&
    ((w.resourceClass || "").toLowerCase().includes(q) || (w.caption || "").toLowerCase().includes(q)));
if (hit) { hit.minimized = false; workspace.activeWindow = hit; }
reply(hit ? hit.caption : "");
"""


def kwin(script: str, **values) -> str:
    """Run a KWin script, `reply(text)` in it handing back the result."""
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    got = []

    def on_call(_conn, _sender, _path, _iface, _method, params, invocation):
        got.append(params.unpack()[0])
        invocation.return_value(None)

    reg = bus.register_object("/org/desk/Reply", REPLY_IFACE, on_call, None, None)
    head = "".join(f"const {k} = {json.dumps(v)};\n" for k, v in values.items())
    head += (f"function reply(text) {{ callDBus({json.dumps(bus.get_unique_name())}, '/org/desk/Reply', "
             "'org.desk.Reply', 'Reply', text); }\n")
    name = f"desk-{os.getpid()}"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(head + script)
    try:
        sid = bus.call_sync("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "loadScript",
                            GLib.Variant("(ss)", (f.name, name)), GLib.VariantType("(i)"),
                            Gio.DBusCallFlags.NONE, 2000, None).unpack()[0]
        if sid < 0:
            raise LookupError(f"KWin did not load the script {f.name}")
        try:
            bus.call_sync("org.kde.KWin", f"/Scripting/Script{sid}", "org.kde.kwin.Script", "run", None, None,
                          Gio.DBusCallFlags.NONE, 2000, None)
            ctx, deadline = GLib.MainContext.default(), time.monotonic() + 3
            while not got and time.monotonic() < deadline:
                ctx.iteration(False) or time.sleep(0.01)
        finally:
            bus.call_sync("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "unloadScript",
                          GLib.Variant("(s)", (name,)), None, Gio.DBusCallFlags.NONE, 2000, None)
    except GLib.Error as e:
        raise LookupError(f"KWin's scripting does not answer, so windows cannot be placed on the screen: {e.message}. "
                          "The session may still be starting (desk wait --ready)") from None
    finally:
        bus.unregister_object(reg)
        os.unlink(f.name)
    if not got:
        raise LookupError("KWin ran desk's script but it did not answer within 3 s")
    return got[0]


def session_env(*names: str) -> None:
    """Variables of the graphical session the helper needs and SSH does not bring (DISPLAY, the
    compositor's socket), from the systemd user manager the session hands them to."""
    if all(n in os.environ for n in names):
        return
    import subprocess

    out = subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True, text=True,
                         timeout=10).stdout
    for line in out.splitlines():
        key, _, value = line.partition("=")
        if key in names:
            os.environ.setdefault(key, value)


def gtk_frame_extents(xids: list[int]) -> dict[int, tuple[int, int, int, int]]:
    """The shadow GTK draws around its own decoration on X11 (_GTK_FRAME_EXTENTS: left, right, top,
    bottom) of each window that has one, through libX11, which every X11 desktop has."""
    import ctypes

    x = ctypes.cdll.LoadLibrary("libX11.so.6")
    x.XOpenDisplay.restype, x.XOpenDisplay.argtypes = ctypes.c_void_p, [ctypes.c_char_p]
    x.XInternAtom.restype, x.XInternAtom.argtypes = ctypes.c_ulong, [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    out = ctypes.POINTER(ctypes.c_ulong)
    x.XGetWindowProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_long, ctypes.c_long,
                                     ctypes.c_int, ctypes.c_ulong, out, ctypes.POINTER(ctypes.c_int), out, out,
                                     ctypes.POINTER(ctypes.c_void_p)]
    x.XFree.argtypes = x.XCloseDisplay.argtypes = [ctypes.c_void_p]
    display = x.XOpenDisplay(None)
    if not display:
        raise LookupError(f"cannot open the X display {os.environ.get('DISPLAY')!r} to read window frames")
    try:
        atom = x.XInternAtom(display, b"_GTK_FRAME_EXTENTS", 1)
        found = {}
        for xid in xids if atom else []:
            kind, size, count, after = ctypes.c_ulong(), ctypes.c_int(), ctypes.c_ulong(), ctypes.c_ulong()
            data = ctypes.c_void_p()
            # 6: XA_CARDINAL.
            if x.XGetWindowProperty(display, xid, atom, 0, 4, 0, 6, ctypes.byref(kind), ctypes.byref(size),
                                    ctypes.byref(count), ctypes.byref(after), ctypes.byref(data)) == 0 and data:
                if count.value == 4 and size.value == 32:
                    found[xid] = tuple(ctypes.cast(data, ctypes.POINTER(ctypes.c_long))[:4])
                x.XFree(data)
        return found
    finally:
        x.XCloseDisplay(display)


def x11_windows() -> list[dict]:
    """Xfce and Cinnamon run on X11, where libwnck knows the windows. AT-SPI's screen coordinates
    would do for GTK 3 and Qt, but GTK 4 counts from its window there as well, so every toolkit is
    placed as on Wayland: the window as X has it (GTK's shadow included) is the buffer, the frame is
    what GTK draws inside its shadow."""
    session_env("DISPLAY", "XAUTHORITY")
    gi.require_version("Wnck", "3.0")
    from gi.repository import Wnck

    screen = Wnck.Screen.get_default()
    if screen is None:
        raise LookupError(f"no X display to list windows on (DISPLAY={os.environ.get('DISPLAY')!r})")
    screen.force_update()
    active = screen.get_active_window()
    windows = screen.get_windows_stacked()
    shadows = gtk_frame_extents([w.get_xid() for w in windows])
    listed = []
    for w in windows:
        x, y, width, height = w.get_client_window_geometry()
        left, right, top, bottom = shadows.get(w.get_xid(), (0, 0, 0, 0))
        listed.append({"title": w.get_name(), "wm_class": w.get_class_group_name(), "pid": w.get_pid(), "x11": False,
                       "minimized": w.is_minimized(), "focus": w == active, "buffer": [x, y, width, height],
                       "frame": [x + left, y + top, width - left - right, height - top - bottom], "wnck": w})
    return listed


def hyprctl(*args: str):
    """hyprctl's JSON, from the Hyprland instance of the session."""
    import subprocess

    session_env("HYPRLAND_INSTANCE_SIGNATURE", "XDG_RUNTIME_DIR", "WAYLAND_DISPLAY")
    r = subprocess.run(["hyprctl", "-j", *args], capture_output=True, text=True, timeout=10)
    # A dispatch answers "ok", or what it did not do (with exit code 0).
    if r.returncode != 0 or (args[0] == "dispatch" and r.stdout.strip() != "ok"):
        raise LookupError(f"hyprctl {' '.join(args)} failed: {(r.stderr or r.stdout).strip()}")
    return json.loads(r.stdout) if args[0] != "dispatch" else None


def hyprland_windows() -> list[dict]:
    # No stacking order to be had: the focus history, most recent last, stands in for it.
    clients = sorted(hyprctl("clients"), key=lambda c: -c["focusHistoryID"] if c["focusHistoryID"] >= 0 else -10**6)
    return [{"title": c["title"], "wm_class": c["class"], "pid": c["pid"], "x11": c["xwayland"],
             "minimized": not c["mapped"] or c["hidden"], "focus": c["focusHistoryID"] == 0,
             "frame": [*c["at"], *c["size"]], "buffer": [*c["at"], *c["size"]]} for c in clients]


def shell_windows() -> list[dict]:
    if DESKTOP == "kde":
        return json.loads(kwin(KWIN_WINDOWS))
    if DESKTOP in ("xfce", "cinnamon"):
        return [{k: v for k, v in w.items() if k != "wnck"} for w in x11_windows()]
    if DESKTOP == "hyprland":
        return hyprland_windows()
    try:
        return json.loads(_helper("GetWindows", reply="(s)"))
    except GLib.Error as e:
        # Without the window list nothing can be placed on the screen; an empty tree would read
        # as "no windows open".
        raise LookupError("the desk shell extension (org.desk.Helper) does not answer, so windows cannot be placed "
                          f"on the screen: {e.message}. The session may still be starting (desk wait --ready); "
                          "otherwise GNOME Shell did not load the extension") from None


def shell(_opts) -> dict:
    """The overview and the notification banner shown now ({} from an older shell extension, and
    on desktops without them)."""
    if DESKTOP != "gnome":
        return {}
    try:
        return json.loads(_helper("GetShell", reply="(s)"))
    except GLib.Error:
        return {}


def focus(query: str) -> dict:
    """Bring a window to the front by application or title, as the window manager would."""
    if DESKTOP == "kde":
        return {"title": kwin(KWIN_ACTIVATE, QUERY=query)}
    q = query.lower()
    if DESKTOP in ("xfce", "cinnamon"):
        hit = next((w for w in reversed(x11_windows()) if q in (w["wm_class"] or "").lower()
                    or q in (w["title"] or "").lower()), None)
        if hit:
            gi.require_version("GdkX11", "3.0")
            from gi.repository import GdkX11

            hit["wnck"].activate(GdkX11.x11_get_server_time(GdkX11.X11Window.lookup_for_display(
                GdkX11.X11Display.get_default(), GdkX11.x11_get_default_root_xwindow())))
        return {"title": hit["title"] if hit else ""}
    if DESKTOP == "hyprland":
        hit = next((w for w in reversed(hyprland_windows()) if q in (w["wm_class"] or "").lower()
                    or q in (w["title"] or "").lower()), None)
        if hit:
            hyprctl("dispatch", "focuswindow", f"pid:{hit['pid']}")
        return {"title": hit["title"] if hit else ""}
    return {"title": _helper("Activate", GLib.Variant("(s)", (query,)), "(s)")}


def clipboard(text: str) -> dict:
    """Set the clipboard for pasting: only the compositor (or Plasma's clipboard service) can
    without a focused window of its own."""
    if DESKTOP == "kde":
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync("org.kde.klipper", "/klipper", "org.kde.klipper.klipper", "setClipboardContents",
                      GLib.Variant("(s)", (text,)), None, Gio.DBusCallFlags.NONE, 2000, None)
    elif DESKTOP != "gnome":
        import subprocess

        # xclip and wl-copy stay in the background to serve the text until something else is copied.
        x11 = DESKTOP in ("xfce", "cinnamon")
        session_env(*(("DISPLAY", "XAUTHORITY") if x11 else ("WAYLAND_DISPLAY", "XDG_RUNTIME_DIR")))
        subprocess.run(["xclip", "-selection", "clipboard"] if x11 else ["wl-copy"], input=text.encode(),
                       check=True, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        _helper("SetClipboard", GLib.Variant("(s)", (text,)))
    return {"done": True}


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


def text_of(acc, limit: int = 400) -> str:
    try:
        if "Text" not in acc.get_interfaces():
            return ""
        n = Atspi.Text.get_character_count(acc)
        t = Atspi.Text.get_text(acc, 0, min(n, limit))
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
        # GTK 3 and Chromium report window coordinates from the surface, client-side shadow
        # included; GTK 4 and others from the visible frame (the xdg window geometry). Measured on
        # GNOME 50.
        kit = toolkit_major(app) if app is not None else ("", 0)
        if kit == ("gtk", 3) or kit[0] == "chromium":
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
    elif role in QUIET or (role in TEXT_ROLES and not name):
        # For containers this is only their own text: children appear as embedded objects,
        # which text_of drops, and are listed separately. A named one keeps it too: a toolbar
        # labelled "Selection" holds "2 tags selected" and nothing else does.
        node["text"] = text_of(acc, TEXT_CHARS)
    return node, st


def contains(box, point) -> bool:
    x, y, w, h = box
    return x <= point[0] < x + w and y <= point[1] < y + h


def center(box) -> tuple[int, int]:
    x, y, w, h = box
    return x + w // 2, y + h // 2


def hit_at(root, base, point):
    """The deepest element the application itself says is at a screen point: what a click there
    reaches. `root` is the top of the process's tree, `base` its coordinate base. None when the
    toolkit cannot tell."""
    kind, ox, oy = base
    coord = Atspi.CoordType.SCREEN if kind == "screen" else Atspi.CoordType.WINDOW
    proc = process_of(root)
    acc, hit = root, None
    for _ in range(40):
        try:
            ch = Atspi.Component.get_accessible_at_point(acc, point[0] - ox, point[1] - oy, coord)
            # Another process (an embedded web view) has coordinates of its own. Toolkits without
            # a hit test of their own answer with the first child whose box contains the point,
            # hidden ones too (an unmapped GtkStack page): stop at what is showing.
            if ch is None or ch == acc or process_of(ch) != proc or "showing" not in states(ch):
                break
        except (GLib.Error, AttributeError, TypeError):
            break
        acc = hit = ch
    return hit


def labelled_by(acc, hit) -> bool:
    """Whether `hit` or what holds it is a label of `acc`, so that a click there reaches `acc`:
    a PatternFly tile's label lies over its radio button and is the tile's place to click."""
    try:
        labels = [r.get_target(i) for r in acc.get_relation_set() or []
                  if r.get_relation_type() == Atspi.RelationType.LABELLED_BY for i in range(r.get_n_targets())]
        a = hit
        for _ in range(3):
            if a is None:
                return False
            if any(a == label for label in labels):
                return True
            a = a.get_parent()
    except GLib.Error:
        pass
    return False


def placement(acc, hit, chain, point) -> str | None:
    """Where `hit`, the element at `point`, is relative to `acc`: "self", "inside" (a descendant,
    such as the text inside a button), "around" (an ancestor, and every element between them
    contains the point: `acc` itself is not hit-testable there; Firefox leaves out the buttons of
    sticky footers that way), "label" (a label of `acc` lies over it), "other" (something else
    covers `acc` there) or None (unknown). `chain` holds the ancestors of `acc` with their boxes,
    the top-level first."""
    if hit is None:
        return None
    if hit == acc:
        return "self"
    for i, (anc, _) in enumerate(chain):
        if anc == hit:
            # An ancestor is what is there also when `acc` is scrolled out of a list or pane in
            # between, which then does not contain the point.
            return "around" if all(b is None or contains(b, point) for _, b in chain[i + 1:]) else "other"
    a = hit
    for _ in range(60):
        try:
            a = a.get_parent()
        except GLib.Error:
            break
        if a is None:
            break
        if a == acc:
            return "inside"
        if any(a == anc for anc, _ in chain):
            break
    return "label" if labelled_by(acc, hit) else "other"


def label_of(acc) -> str:
    try:
        name = (acc.get_name() or "").strip()
        text = name or text_of(acc)
        return f"{acc.get_role_name()} {json.dumps(text[:60], ensure_ascii=False)}" if text else acc.get_role_name()
    except GLib.Error:
        return "another element"


def grid(bbox) -> list[tuple[int, int]]:
    """Points of a box to click, the middle first."""
    x, y, w, h = bbox
    return [(x + w * fx // 4, y + h * fy // 4)
            for fx, fy in ((2, 2), (1, 2), (3, 2), (2, 1), (2, 3), (1, 1), (3, 1), (1, 3), (3, 3))]


def within(chain, point) -> bool:
    """Whether every ancestor's box holds the point. Firefox's hit test answers with an element
    also where a scrolling container cuts it off (a table row below the bottom edge of Cockpit's
    main area), and a click there reaches nothing. A menu may hang outside the box of what holds
    it, so this only orders the points tried."""
    return all(b is None or contains(b, point) for _, b in chain)


def visible_point(bbox, chain) -> tuple[int, int] | None:
    return next((p for p in grid(bbox) if within(chain, p)), None)


def clickable_point(acc, bbox, chain, root, showing: bool):
    """A point of `bbox` where a click reaches `acc` (the middle if possible, one inside every
    ancestor first), or None and what covers it; and whether that point lies outside an
    ancestor's box. When the toolkit cannot hit-test, the middle of what it reports as showing."""
    racc, rbase, rbox = root
    cover, tested = None, False
    for p in sorted(grid(bbox), key=lambda p: not within(chain, p)):
        if rbox is not None and not contains(rbox, p):
            continue
        tested = True
        hit = hit_at(racc, rbase, p)
        where = placement(acc, hit, chain, p)
        if where is None:
            break
        if where != "other":
            return p, None, not within(chain, p)
        cover = cover or label_of(hit)
    else:
        if tested:
            return None, cover, False
    # Outside its window's box (a menu hanging below a small window), or the toolkit cannot tell.
    point = (visible_point(bbox, chain) or center(bbox)) if showing else None
    return point, None, point is not None and not within(chain, point)


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
    start = time.monotonic()

    def probe(node, acc, chain, root):
        """What a click on the node would reach: Firefox does not mark the buttons of sticky
        footers as showing although they are on screen, and a toast or sticky bar can cover what
        it marks as showing."""
        if node["bbox"] is None or probe.spent > HIT_SECONDS:
            return
        point = center(node["bbox"])
        if root[2] is None or not contains(root[2], point):
            return
        t = time.monotonic()
        hit = hit_at(root[0], root[1], point)
        where = placement(acc, hit, chain, point)
        probe.spent += time.monotonic() - t
        if node.get("offscreen"):
            if where in ("self", "inside", "around", "label"):
                del node["offscreen"]
        elif where == "other":
            node["covered"] = label_of(hit)

    probe.spent = 0.0

    def walk(acc, base, pid, path, depth, parent_name, proc, gtk4, parent_box, chain, root):
        nonlocal visited, truncated, offscreen
        if visited >= MAX_VISIT or len(nodes) - offscreen >= limit:
            truncated = truncated or "count"
            return
        if time.monotonic() - start > WALK_SECONDS:
            truncated = "time"
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
        if root is None:
            root = (acc, base, node["bbox"])
        # Not "showing" covers content scrolled out of view or clipped by a scrolling list,
        # which the agent can bring into view, but also background tabs and hidden windows,
        # which are pruned. Structural containers can lack "showing" while their children are
        # on screen (Gecko marks a PatternFly tbody that way), so the state is judged per node.
        if "showing" not in st:
            if node["role"] in HIDDEN_ROOTS:
                return
            node["offscreen"] = True
        emit = wanted(node, st, parent_name)
        if emit:
            probe(node, acc, chain, root)
        if emit and node["bbox"] is not None and not node.get("offscreen"):
            point = visible_point(node["bbox"], chain)
            if point is not None and point != center(node["bbox"]):
                node["point"] = list(point)
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
        below = chain + [(acc, node["bbox"])]
        for i in range(n):
            try:
                ch = acc.get_child_at_index(i)
            except GLib.Error:
                continue
            if ch is None:
                continue
            ch_proc = process_of(ch)
            ch_base = embedded_base(acc, base, ch) if ch_proc != proc else base
            walk(ch, ch_base, pid, path + [i], child_depth, pname, ch_proc, gtk4, node["bbox"], below,
                 root if ch_proc == proc else None)

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
            walk(top, base, pid, [ti], 0, "", process_of(top), toolkit_major(app) == ("gtk", 4), None, [], None)
    return {"windows": windows, "tops": tops, "nodes": nodes, "truncated": truncated}


def node_at(spec: dict):
    """The application of a node from a snapshot, and whatever is at the node's path now."""
    app = None
    for _, a in app_children(Atspi.get_desktop(0)):
        try:
            if a.get_process_id() == spec["pid"]:
                app = a
                break
        except GLib.Error:
            continue
    if app is None:
        raise LookupError("application is gone")
    acc = app
    for i in spec["path"]:
        acc = acc.get_child_at_index(i) if acc is not None and i < acc.get_child_count() else None
    return app, acc


def find_node(spec: dict):
    """Re-find a node from a snapshot: same path if role and name still match, else the only node
    of the window with them (of two "Delete" buttons, the first is not necessarily the one meant)."""
    app, acc = node_at(spec)
    path = spec["path"]
    if acc is not None and acc.get_role_name() == spec["role"] and (acc.get_name() or "").strip() == spec["name"]:
        return app, path, acc

    # The tree changed (a web page builds it lazily and re-renders): search the window for it.
    deadline = time.monotonic() + SEARCH_SECONDS

    hits = []

    def search(a, at, depth=0):
        if depth > 60 or time.monotonic() > deadline:
            return
        for i in range(a.get_child_count()):
            if len(hits) > 1:
                return
            ch = a.get_child_at_index(i)
            if ch is None:
                continue
            if ch.get_role_name() == spec["role"] and (ch.get_name() or "").strip() == spec["name"]:
                hits.append((at + [i], ch))
            else:
                search(ch, at + [i], depth + 1)

    top = app.get_child_at_index(path[0]) if path[0] < app.get_child_count() else None
    if top is not None:
        search(top, [path[0]])
    if len(hits) > 1:
        raise LookupError(f"{spec['role']} {spec['name']!r} moved, and the window has more than one; look again "
                          "(desk a11y) for the @ref of the one meant")
    if not hits:
        if time.monotonic() > deadline:
            raise LookupError(f"{spec['role']} {spec['name']!r} not found again within {SEARCH_SECONDS}s "
                              "(the page changed); look again")
        raise LookupError(f"{spec['role']} {spec['name']!r} no longer exists")
    return app, hits[0][0], hits[0][1]


def along(app, path):
    """Coordinate base for the node at `path`, following embedded processes on the way down; its
    ancestors with their boxes; and the top of its process's tree for hit tests."""
    acc = app.get_child_at_index(path[0])
    base = Mapper(shell_windows()).for_toplevel(app.get_name() or "", app.get_process_id(), acc, app)
    if base is None:
        return None, [], None
    proc = process_of(acc)
    gtk4 = toolkit_major(app) == ("gtk", 4)
    box = Mapper.place(acc, base)
    root = (acc, base, box)
    chain = []
    for i in path[1:]:
        chain.append((acc, box))
        ch = acc.get_child_at_index(i)
        ch_proc = process_of(ch)
        embedded = ch_proc != proc
        if embedded:
            base, proc = embedded_base(acc, base, ch), ch_proc
        ch_box = Mapper.place(ch, base)
        if gtk4:
            fixed = stacked_base(box, ch_box, base)
            if fixed != base:
                base, ch_box = fixed, box
        if embedded:
            root = (ch, base, ch_box)
        acc, box = ch, ch_box
    return base, chain, root


def resolve(spec: dict) -> dict:
    """Where the element is now and a point where a click reaches it (None: scrolled away or
    covered, with what covers it)."""
    app, path, acc = find_node(spec)
    base, chain, root = along(app, path)
    if base is None:
        raise LookupError("window position unknown")
    bbox = Mapper.place(acc, base)
    if bbox is None:
        raise LookupError("element has no on-screen extents")
    showing = "showing" in states(acc)
    point, cover, clipped = clickable_point(acc, bbox, chain, root, showing)
    return {"bbox": bbox, "showing": showing, "point": list(point) if point else None, "covered": cover,
            "clipped": clipped}


SCROLL = {"anywhere": Atspi.ScrollType.ANYWHERE, "top": Atspi.ScrollType.TOP_EDGE,
          "bottom": Atspi.ScrollType.BOTTOM_EDGE}


def scroll(spec: dict, how: str = "anywhere") -> dict:
    """Bring an element into view the way the toolkit itself would (scrollIntoView and kin)."""
    _, _, acc = find_node(spec)
    done = False
    if "Component" in acc.get_interfaces():
        try:
            done = Atspi.Component.scroll_to(acc, SCROLL[how])
        except (GLib.Error, AttributeError, TypeError):
            done = False
    if not done and how == "anywhere":
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
    if not Atspi.Action.do_action(acc, idx):
        raise LookupError(f"the application answered that action {names[idx]!r} failed (available: "
                          f"{', '.join(names)}); look at the screen before trying again")
    return {"done": names[idx], "actions": names}


def set_text(spec: dict, text: str) -> dict:
    _, _, acc = find_node(spec)
    if "EditableText" not in acc.get_interfaces():
        raise LookupError("the element takes no text through AT-SPI (Chromium's fields do not): click into it and "
                          "use desk type")
    if not Atspi.EditableText.set_text_contents(acc, text):
        raise LookupError("the application refused to replace the field's text through AT-SPI")
    return {"done": True}


def takes_text(acc, st: set[str]) -> bool:
    """A field to type into: Chromium marks its fields editable, without the EditableText
    interface."""
    interfaces = acc.get_interfaces()
    return "EditableText" in interfaces or ("editable" in st and "Text" in interfaces)


def edit(spec: dict) -> dict:
    """Give an editable element the keyboard focus, its window in front, so that real key presses
    reach it; with its current text."""
    app, path, acc = find_node(spec)
    if not takes_text(acc, states(acc)):
        raise LookupError("element is not editable")
    front = next((w for w in shell_windows() if w["focus"]), None)
    title = app.get_child_at_index(path[0]).get_name() or ""
    if title and (front is None or front["pid"] != app.get_process_id()):
        focus(title)
    try:
        Atspi.Component.grab_focus(acc)
    except (GLib.Error, AttributeError, TypeError):
        pass
    deadline = time.monotonic() + 2
    while "focused" not in states(acc) and time.monotonic() < deadline:
        time.sleep(0.1)
    front = next((w for w in shell_windows() if w["focus"]), None)
    return {"focused": "focused" in states(acc) and front is not None and front["pid"] == app.get_process_id(),
            "text": text_of(acc) if "Text" in acc.get_interfaces() else ""}


def read(spec: dict) -> dict:
    """The text of a field desk has just typed into."""
    try:
        _, _, acc = find_node(spec)
    except LookupError:
        # A field named after what it holds (KWrite's document): the focused one at its place.
        _, acc = node_at(spec)
        if acc is None or acc.get_role_name() != spec["role"] or "focused" not in states(acc):
            raise
    return {"text": text_of(acc) if "Text" in acc.get_interfaces() else ""}


def front_toplevel():
    """The application and top-level accessible of the focused window, if any."""
    front = next((w for w in shell_windows() if w["focus"]), None)
    if front is None:
        return None, None, None
    for _, app in app_children(Atspi.get_desktop(0)):
        try:
            if app.get_process_id() != front["pid"]:
                continue
            for i in range(app.get_child_count()):
                top = app.get_child_at_index(i)
                if top is not None and ((top.get_name() or "") == front["title"] or "active" in states(top)):
                    return front, app, top
        except GLib.Error:
            continue
    return front, None, None


def showing_walk(acc, visit, deadline, depth=0) -> None:
    """Depth first through what is showing, until `visit` returns True; False leaves out what is
    inside the node. Raises when time runs out: what was not looked at is not missing (a
    responsive design mode taken for closed would be toggled off)."""
    if depth > 80:
        return
    if time.monotonic() > deadline:
        raise LookupError("the focused window did not answer in time while its accessibility tree was read")
    try:
        n = acc.get_child_count()
    except GLib.Error:
        return
    for i in range(n):
        try:
            ch = acc.get_child_at_index(i)
            if ch is None or "showing" not in states(ch):
                continue
        except GLib.Error:
            continue
        seen = visit(ch)
        if seen or (seen is not False and showing_walk(ch, visit, deadline, depth + 1)):
            return True
    return None


def focused(_opts) -> dict:
    """The element with the keyboard focus in the focused window."""
    front, app, top = front_toplevel()
    if top is None:
        return {"window": front["title"] if front else None}
    found = []

    def visit(a):
        st = states(a)
        if "focused" in st:
            found.append((a, st))

    showing_walk(top, visit, time.monotonic() + 5)
    out = {"window": front["title"], "app": app.get_name() or ""}
    if found:
        a, st = found[-1]
        out.update(role=a.get_role_name(), name=(a.get_name() or "").strip(),
                   editable="editable" in st and takes_text(a, st))
    return out


DEVTOOLS = "devtools://"
# Chromium's address bar (views class), the one place that shows where a tab is going while it
# still shows the page before.
OMNIBOX = "OmniboxViewViews"


def doc_url(doc) -> str | None:
    getter = getattr(Atspi.Document, "get_attribute_value", None) or Atspi.Document.get_document_attribute_value
    for name in ("DocURL", "URI"):  # Firefox, Chromium
        try:
            url = getter(doc, name)
        except GLib.Error:
            url = None
        if url:
            return url
    return None


def page(_opts) -> dict:
    """Address of the page shown in the focused browser window, and in Chromium what its address
    bar shows."""
    front, app, top = front_toplevel()
    if top is None:
        return {"url": None}
    docs, bar = [], []
    chromium = toolkit_major(app)[0] == "chromium"

    def visit(a):
        role = a.get_role_name()
        if role == "document web":
            # Chromium's docked DevTools are a page of their own, in front of the one inspected
            # (desk browser viewport opens them): passed over, the walk goes on to the page.
            if (doc_url(a) or "").startswith(DEVTOOLS):
                return False
            if not docs:
                docs.append(a)
            # Its address bar may come after the page.
            return bool(bar) or not chromium
        if role == "entry" and not bar and chromium and (a.get_attributes() or {}).get("class") == OMNIBOX:
            bar.append(text_of(a, 2000))
            return bool(docs) or None
        return None

    showing_walk(top, visit, time.monotonic() + 5)
    url = error = None
    if docs:
        url = doc_url(docs[0])
        error = browser_error(docs[0], url)
    out = {"url": url or None, "problem": error, "app": app.get_name() or "", "window": front["title"]}
    if bar:
        out["address"] = bar[0]
    return out


def browser_error(doc, url: str | None = None) -> str | None:
    """The heading of the browser's own page shown instead of the one asked for (server not found,
    connection refused, a certificate it does not trust). Firefox's carries the address of the
    page that did not load and is told by what only its pages are made of, the same in every
    language: a heading with the id error-title, next to its introduction, one of Firefox's own
    buttons (moz-button) or on its error pages' body. Chromium's has an address of its own. A page
    that changes while it is read is none."""
    if (url or "").startswith("chrome-error://"):
        return chromium_error(doc)
    title, signs, budget = [], set(), [300]

    def visit(a, depth):
        if budget[0] <= 0 or depth > 8:
            return
        budget[0] -= 1
        attrs = a.get_attributes() or {}
        if attrs.get("id") == "error-title":
            title.append(a.get_name() or "")
        if attrs.get("id") == "error-intro" or attrs.get("tag") == "moz-button" \
                or "felt-privacy-body" in (attrs.get("class") or ""):
            signs.add(True)
        for i in range(a.get_child_count()):
            child = a.get_child_at_index(i)
            if child is not None:
                visit(child, depth + 1)

    try:
        visit(doc, 0)
    except GLib.Error:
        return None
    return (title[0] or "an error page") if title and signs else None


def chromium_error(doc) -> str:
    """Chromium's error page and certificate warning: the heading of its main message and the
    error code (ERR_CONNECTION_REFUSED, NET::ERR_CERT_AUTHORITY_INVALID), found by their ids and
    classes, the same in every language."""
    heading, code, budget = [], [], [300]

    def visit(a, depth, message):
        if budget[0] <= 0 or depth > 10:
            return
        budget[0] -= 1
        attrs = a.get_attributes() or {}
        message = message or attrs.get("id") == "main-message"
        if message and not heading and a.get_role_name() == "heading":
            heading.append((a.get_name() or "").strip())
        if not code and "error-code" in (attrs.get("class") or "").split():
            code.append((a.get_name() or "").strip() or text_of(a))
        for i in range(a.get_child_count()):
            child = a.get_child_at_index(i)
            if child is not None:
                visit(child, depth + 1, message)

    try:
        visit(doc, 0, False)
    except GLib.Error:
        pass
    text = (heading or [""])[0] or "an error page"
    return f"{text} ({code[0]})" if code and code[0] else text


RDM_TOOLBAR = "chrome://devtools/content/responsive/toolbar"
# The width and height fields of the device toolbar in Chromium's DevTools.
DEVICE_SIZE = "device-mode-size-input"


def rdm(opts) -> dict:
    """Firefox's responsive design mode in the focused window: where its page area (the browser
    framed below the mode's toolbar) is on screen, and with {"focus": true} the keyboard focus
    moved there from the mode's toolbar. {"open": False} when the mode is off. Chromium's device
    mode likewise (chromium_device)."""
    _, app, top = front_toplevel()
    if top is None:
        return {"open": False}
    base = Mapper(shell_windows()).for_toplevel(app.get_name() or "", app.get_process_id(), top, app)
    if toolkit_major(app)[0] == "chromium":
        return chromium_device(app, top, base, opts)
    found = []

    def visit(a):
        if a.get_role_name() == "embedded" and (a.get_name() or "").startswith(RDM_TOOLBAR):
            found.append(a)
            return True
        # The toolbar is the browser's (frame > panel > scroll pane > internal frame), never inside
        # a page: a large page would otherwise take the walk past its time.
        return False if a.get_role_name() == "document web" else None

    showing_walk(top, visit, time.monotonic() + 5)
    if not found or base is None:
        return {"open": False}
    # embedded toolbar < internal frame < scroll pane, whose other internal frame holds the page.
    holder = found[0].get_parent()
    pane = holder.get_parent() if holder is not None else None
    frames = [pane.get_child_at_index(i) for i in range(pane.get_child_count())] if pane is not None else []
    page_frames = [f for f in frames if f is not None and f != holder and f.get_role_name() == "internal frame"]
    if not page_frames:
        return {"open": True, "bbox": None}
    if opts.get("focus"):
        doc = page_frames[0].get_child_at_index(0) if page_frames[0].get_child_count() else None
        try:
            Atspi.Component.grab_focus(doc or page_frames[0])
        except (GLib.Error, AttributeError, TypeError):
            pass
    return {"open": True, "bbox": Mapper.place(page_frames[0], base)}


def chromium_device(app, top, base, opts: dict) -> dict:
    """Chromium's device mode, the toolbar of the DevTools docked in the window: its width and
    height fields (as snapshot lists them) and where the page area is on screen; with focus
    "devtools" or "page" the keyboard focus moved there, with {"zoom": true} the page shown at
    100 % (the toolbar's zoom; DevTools fit it to the window at first, which AT-SPI's boxes do not
    follow), "zoomed" once it is. {"devtools": False} while DevTools are closed, {"open": False}
    while they show no device toolbar; a window that cannot be looked through is an error."""
    index = next((i for i in range(app.get_child_count()) if app.get_child_at_index(i) == top), 0)
    pid, deadline = app.get_process_id(), time.monotonic() + 5
    docs, fields, zoom, budget = [], [], [], [400]

    def find_docs(acc, path, depth):
        if depth > 80:
            return
        if time.monotonic() > deadline:
            # What is not found here would read as DevTools being closed.
            raise LookupError("Chromium's window did not answer within 5 s")
        for i in range(acc.get_child_count()):
            ch = acc.get_child_at_index(i)
            if ch is None or "showing" not in states(ch):
                continue
            if ch.get_role_name() == "document web":
                docs.append((ch, path + [i], (doc_url(ch) or "").startswith(DEVTOOLS)))
            else:
                find_docs(ch, path + [i], depth + 1)

    def find_fields(acc, path, depth):
        # The device toolbar comes first in the DevTools page; the panels below it are large.
        for i in range(acc.get_child_count()):
            if (len(fields) == 2 and zoom) or budget[0] <= 0 or depth > 40:
                return
            budget[0] -= 1
            ch = acc.get_child_at_index(i)
            if ch is None or "showing" not in states(ch):
                continue
            role = ch.get_role_name()
            if role == "spin button" and DEVICE_SIZE in ((ch.get_attributes() or {}).get("class") or "").split():
                fields.append({"pid": pid, "path": path + [i], "role": "spin button",
                               "name": (ch.get_name() or "").strip()})
            elif role == "combo box":
                # The zoom's choices, told by the one named in digits, the same in every language.
                menu = ch.get_child_at_index(0) if ch.get_child_count() == 1 else None
                options = [menu.get_child_at_index(j) for j in range(menu.get_child_count())] if menu else []
                zoom.extend(o for o in options if o is not None and o.get_name() == "100%")
            else:
                find_fields(ch, path + [i], depth + 1)

    find_docs(top, [index], 0)
    tools = next(((d, p) for d, p, dev in docs if dev), None)
    shown = next((d for d, _, dev in docs if not dev), None)
    if tools:
        find_fields(*tools, 0)
    if opts.get("zoom") and zoom and "selected" not in states(zoom[0]):
        Atspi.Action.do_action(zoom[0], 0)  # its only action but the context menu: select
    target = {"devtools": tools[0] if tools else None, "page": shown}.get(opts.get("focus"))
    if target is not None:
        try:
            Atspi.Component.grab_focus(target)
        except (GLib.Error, AttributeError, TypeError):
            pass
    return {"open": len(fields) == 2, "devtools": tools is not None, "fields": fields,
            "zoomed": bool(zoom) and "selected" in states(zoom[0]),
            "bbox": Mapper.place(shown, base) if shown is not None and base is not None else None}


def main() -> None:
    cmd, arg = sys.argv[1], json.loads(sys.argv[2])
    extra = sys.argv[3] if len(sys.argv) > 3 else None
    try:
        if cmd == "snapshot":
            out = snapshot(arg)
        elif cmd == "focus":
            out = focus(arg)
        elif cmd == "resolve":
            out = resolve(arg)
        elif cmd == "scroll":
            out = scroll(arg, extra or "anywhere")
        elif cmd == "action":
            out = action(arg, extra)
        elif cmd == "set-text":
            out = set_text(arg, extra)
        elif cmd == "edit":
            out = edit(arg)
        elif cmd == "read":
            out = read(arg)
        elif cmd == "focused":
            out = focused(arg)
        elif cmd == "page":
            out = page(arg)
        elif cmd == "clipboard":
            out = clipboard(arg)
        elif cmd == "rdm":
            out = rdm(arg)
        elif cmd == "shell":
            out = shell(arg)
        else:
            out = {"error": f"unknown command {cmd}"}
    except (LookupError, GLib.Error) as e:
        out = {"error": str(e)}
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()
