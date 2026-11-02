"""Out-of-band screen and input: what a physical monitor, mouse and keyboard would see and do."""

import hashlib
import io
import time
import xml.etree.ElementTree as ET

import libvirt

from .config import DeskError, NotDone
from .virt import conn, qmp

ABS_MAX = 0x7FFF

KEY_ALIASES = {
    "enter": "ret", "return": "ret", "escape": "esc", "space": "spc", "del": "delete",
    "super": "meta_l", "win": "meta_l", "meta": "meta_l", "cmd": "meta_l",
    "control": "ctrl", "pageup": "pgup", "pagedown": "pgdn", "printscreen": "print",
    "backtick": "grave_accent", "capslock": "caps_lock",
    "-": "minus", "=": "equal", "[": "bracket_left", "]": "bracket_right", "\\": "backslash",
    ";": "semicolon", "'": "apostrophe", "`": "grave_accent", ",": "comma", ".": "dot", "/": "slash",
}

# US layout; the golden images use it so qcodes map 1:1 to characters.
_UNSHIFTED = {
    " ": "spc", "\n": "ret", "\t": "tab", "-": "minus", "=": "equal", "[": "bracket_left",
    "]": "bracket_right", "\\": "backslash", ";": "semicolon", "'": "apostrophe",
    "`": "grave_accent", ",": "comma", ".": "dot", "/": "slash",
}
_SHIFTED = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9",
    ")": "0", "_": "minus", "+": "equal", "{": "bracket_left", "}": "bracket_right",
    "|": "backslash", ":": "semicolon", '"': "apostrophe", "~": "grave_accent", "<": "comma",
    ">": "dot", "?": "slash",
}


# Polish (programmer's) layout: AltGr (right Alt) plus the base letter.
_ALTGR = {"ą": "a", "ć": "c", "ę": "e", "ł": "l", "ń": "n", "ó": "o", "ś": "s", "ź": "x", "ż": "z"}


def _heads(dom: libvirt.virDomain) -> int:
    """Monitors: one virtio GPU each (see virt.prepare_domain_xml)."""
    return max(1, len(ET.fromstring(dom.XMLDesc(0)).findall("devices/video")))


def screen_sizes(dom: libvirt.virDomain) -> list[tuple[int, int]]:
    return [png_size(_head_png(dom, i)) for i in range(_heads(dom))]


def screenshot(dom: libvirt.virDomain) -> bytes:
    """The screen as PNG; several monitors side by side, left to right, top-aligned, which is
    how desk arranges them in the guest (monitors.apply), so guest coordinates match."""
    heads = _heads(dom)
    if heads == 1:
        return _head_png(dom, 0)
    try:
        from PIL import Image
    except ImportError:
        raise DeskError("several monitors need Pillow on the host to put the pictures together "
                        "(dnf install python3-pillow)") from None
    images = [Image.open(io.BytesIO(_head_png(dom, i))).convert("RGB") for i in range(heads)]
    canvas = Image.new("RGB", (sum(i.width for i in images), max(i.height for i in images)))
    x = 0
    for image in images:
        canvas.paste(image, (x, 0))
        x += image.width
    out = io.BytesIO()
    canvas.save(out, "PNG", compress_level=1)
    return out.getvalue()


def _head_png(dom: libvirt.virDomain, head: int) -> bytes:
    stream = conn().newStream(0)
    mime = dom.screenshot(stream, head, 0)
    chunks: list[bytes] = []
    stream.recvAll(lambda _st, data, buf: buf.append(data), chunks)
    stream.finish()
    if mime != "image/png":
        raise DeskError(f"unexpected screenshot type {mime}")
    return b"".join(chunks)


def crop(png: bytes, box: tuple[int, int, int, int], zoom: float = 1.0) -> bytes:
    """Part of a screenshot (x, y, width, height), optionally enlarged so details stay legible."""
    try:
        from PIL import Image
    except ImportError:
        raise DeskError("cutting screenshots needs Pillow on the host (dnf install python3-pillow)") from None
    image = Image.open(io.BytesIO(png))
    x, y, w, h = box
    x, y = max(x, 0), max(y, 0)
    w, h = min(w, image.width - x), min(h, image.height - y)
    if w <= 0 or h <= 0:
        raise DeskError(f"region {box} is outside the {image.width}x{image.height} screen")
    part = image.crop((x, y, x + w, y + h))
    if zoom != 1:
        part = part.resize((round(w * zoom), round(h * zoom)), Image.LANCZOS)
    out = io.BytesIO()
    part.save(out, "PNG")
    return out.getvalue()


def png_size(png: bytes) -> tuple[int, int]:
    return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")


def wait_stable(dom: libvirt.virDomain, timeout: float = 10.0, frames: int = 3,
                interval: float = 0.25) -> tuple[bytes, bool]:
    """Return the latest frame once `frames` consecutive screenshots are identical."""
    deadline = time.monotonic() + timeout
    last, same = None, 0
    while True:
        png = screenshot(dom)
        digest = hashlib.sha256(png).digest()
        same = same + 1 if digest == last else 1
        last = digest
        if same >= frames:
            return png, True
        if time.monotonic() >= deadline:
            return png, False
        time.sleep(interval)


def screen_hash(dom: libvirt.virDomain) -> bytes:
    return hashlib.sha256(screenshot(dom)).digest()


def wait_change(dom: libvirt.virDomain, timeout: float = 30.0, interval: float = 0.25,
                base: bytes | None = None) -> bool:
    """Wait until the screen differs from `base`, by default how it looks now (an app window
    appearing). Pass a `base` taken before starting something that may already have drawn."""
    if base is None:
        base = screen_hash(dom)
    if screen_hash(dom) != base:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(interval)
        if hashlib.sha256(screenshot(dom)).digest() != base:
            return True
    return False


def _abs(v: int, size: int) -> int:
    if not 0 <= v < size:
        raise DeskError(f"coordinate {v} outside screen (0..{size - 1})")
    return round(v * ABS_MAX / max(size - 1, 1))


def _btn(button: str, down: bool) -> dict:
    return {"type": "btn", "data": {"down": down, "button": button}}


class OffScreen(NotDone):
    """A point a step gives is not on the guest's screen: nothing was clicked, which says nothing
    about the product."""


def check_points(dom, points) -> None:
    """All of `points` on a monitor of the guest, before any event is sent for them."""
    sizes = screen_sizes(dom)
    width = sum(w for w, _ in sizes)
    for x, y in points:
        left = 0
        for i, (w, h) in enumerate(sizes):
            if left <= x < left + w:
                if not 0 <= y < h:
                    raise OffScreen(f"({x},{y}) is below monitor {i + 1}, which is {h} pixels high")
                break
            left += w
        else:
            raise OffScreen(f"({x},{y}) is outside the screen, which is {width} pixels wide")


class _Pointer:
    """Absolute pointer events in screen coordinates. With several monitors a point belongs to
    one of them, and QEMU takes its events for that GPU's console in the console's own pixels.
    Windows maps the absolute tablet onto its primary monitor only, so an operation that touches
    another monitor there goes through the in-session agent instead (SetCursorPos, SendInput)."""

    def __init__(self, dom, run=None, points=()):
        self.dom = dom
        self.sizes = screen_sizes(dom)
        self.width = sum(w for w, _ in self.sizes)
        self.target: dict = {}
        primary = self.sizes[0]
        self.agent = run if (run is not None and getattr(run, "agent_port", 0) and len(self.sizes) > 1 and
                             any(not (x < primary[0] and y < primary[1]) for x, y in points)) else None

    def move(self, x: int, y: int) -> None:
        if self.agent:
            if not 0 <= x < self.width:
                raise DeskError(f"coordinate {x} outside the screen (0..{self.width - 1})")
            self._agent(action="move", x=x, y=y)
            return
        left = 0
        for i, (w, h) in enumerate(self.sizes):
            if left <= x < left + w:
                if not 0 <= y < h:
                    raise DeskError(f"({x},{y}) is below monitor {i + 1}, which is {h} pixels high")
                self.target = {"device": f"video{i}", "head": 0} if len(self.sizes) > 1 else {}
                self.send([{"type": "abs", "data": {"axis": "x", "value": _abs(x - left, w)}},
                           {"type": "abs", "data": {"axis": "y", "value": _abs(y, h)}}])
                return
            left += w
        raise DeskError(f"coordinate {x} outside the screen (0..{self.width - 1})")

    def send(self, events: list[dict]) -> None:
        qmp(self.dom, "input-send-event", events=events, **self.target)

    def button(self, button: str, down: bool) -> None:
        if self.agent:
            if button.startswith("wheel"):
                if down:
                    self._agent(action="wheel", delta=-1 if button == "wheel-down" else 1)
            else:
                self._agent(action="down" if down else "up", button=button)
            return
        self.send([_btn(button, down)])

    def _agent(self, **req) -> None:
        from . import winagent

        winagent.request(self.agent, cmd="pointer", **req)


def move(dom, x: int, y: int, run=None) -> None:
    _Pointer(dom, run, [(x, y)]).move(x, y)


def click(dom, x: int, y: int, button: str = "left", count: int = 1, run=None) -> None:
    p = _Pointer(dom, run, [(x, y)])
    p.move(x, y)
    # Windows drops a right click whose press comes 50 ms after the move and lasts 50 ms (no
    # context menu); 100 ms each work, and two clicks stay well inside the double-click time.
    time.sleep(0.1)
    for _ in range(count):
        p.button(button, True)
        try:
            time.sleep(0.1)
        finally:
            p.button(button, False)
        time.sleep(0.08)


def press(dom, x: int, y: int, button: str = "left", run=None) -> None:
    """Move to (x, y) and press the button, which stays down until release()."""
    p = _Pointer(dom, run, [(x, y)])
    p.move(x, y)
    time.sleep(0.1)
    p.button(button, True)
    time.sleep(0.15)


def glide(dom, x1: int, y1: int, x2: int, y2: int, steps: int = 30, run=None) -> None:
    """Move from (x1, y1) to (x2, y2) as a hand would, and wiggle over the target.

    Toolkit drag-and-drop (GTK, WebKit, Qt on Wayland) only starts after the pointer has moved a
    few pixels with the button held, and only accepts the drop once the target has seen motion
    over it; jumping there and letting go at once leaves the drag hanging."""
    p = _Pointer(dom, run, [(x1, y1), (x2, y2)])
    for i in range(1, steps + 1):
        p.move(x1 + (x2 - x1) * i // steps, y1 + (y2 - y1) * i // steps)
        time.sleep(0.02)
    for dx in (2, -2, 0):
        p.move(min(max(x2 + dx, 0), p.width - 1), y2)
        time.sleep(0.06)
    time.sleep(0.2)


def release(dom, x: int, y: int, button: str = "left", run=None) -> None:
    """Let go of a button pressed at or moved to (x, y), which tells a second monitor's agent."""
    _Pointer(dom, run, [(x, y)]).button(button, False)
    time.sleep(0.1)


def drag(dom, x1: int, y1: int, x2: int, y2: int, steps: int = 30, run=None) -> None:
    """Press, move like a hand would and release over the target."""
    try:
        press(dom, x1, y1, run=run)
        glide(dom, x1, y1, x2, y2, steps, run=run)
    finally:
        # Also when a point on the way is off the screen or the command is interrupted: a
        # button left down would turn every later pointer move into a drag.
        release(dom, x2, y2, run=run)


def scroll(dom, x: int, y: int, dy: int, run=None) -> None:
    """Positive dy scrolls down, negative up; one unit is one wheel notch."""
    p = _Pointer(dom, run, [(x, y)])
    p.move(x, y)
    button = "wheel-down" if dy > 0 else "wheel-up"
    for _ in range(abs(dy)):
        p.button(button, True)
        p.button(button, False)
        time.sleep(0.03)


def _qcode(name: str) -> str:
    n = name.strip().lower()
    return KEY_ALIASES.get(n, n)


def _key_event(code: str, down: bool) -> dict:
    return {"type": "key", "data": {"down": down, "key": {"type": "qcode", "data": code}}}


def chord(dom, codes: list[str], hold_ms: int = 40, gap_ms: int = 40) -> None:
    """Press keys in order, release in reverse.

    Not faster: QEMU's keyboards queue 16 scancodes, and a guest busy for a moment that fell behind
    lost the rest of a word, a key release among it, which left AltGr held for the letters after
    it. Windows' USB keyboard did, taking one scancode per poll; runs type through PS/2
    (virt.spare_usb_keyboards).

    input-send-event is applied before the command returns, unlike send-key, which only
    queues timed events; so once this returns the guest has every key and the next action
    (a click moving focus, say) cannot overtake the typing.
    """
    try:
        qmp(dom, "input-send-event", events=[_key_event(c, True) for c in codes])
        time.sleep(hold_ms / 1000)
    finally:
        # Released whatever happened to the press (an unknown key among them, an interrupt): a
        # modifier left down would change every key and click after it.
        qmp(dom, "input-send-event", events=[_key_event(c, False) for c in reversed(codes)])
    time.sleep(gap_ms / 1000)


def key(dom, combo: str) -> None:
    """Press a combo like `ctrl+alt+t`, `super`, `shift+tab` (QEMU qcode names or aliases)."""
    parts = combo.split("+") if combo != "+" else ["+"]
    chord(dom, [_qcode(p) for p in parts], hold_ms=60, gap_ms=40)


def key_plan(text: str, layout: str = "us") -> list[list[str]]:
    """qcode chords that produce `text` on the guest layout."""
    plan = []
    for ch in text:
        if ch.isascii() and ch.isalnum():
            codes = ["shift", ch.lower()] if ch.isupper() else [ch]
        elif ch in _UNSHIFTED:
            codes = [_UNSHIFTED[ch]]
        elif ch in _SHIFTED:
            codes = ["shift", _SHIFTED[ch]]
        elif layout == "pl" and ch.lower() in _ALTGR:
            codes = ["alt_r", *(["shift"] if ch.isupper() else []), _ALTGR[ch.lower()]]
        else:
            raise DeskError(f"cannot type {ch!r} on the {layout} layout; use `desk set-text` on the field instead")
        plan.append(codes)
    return plan


def type_text(dom, text: str, layout: str = "us") -> None:
    for codes in key_plan(text, layout):
        chord(dom, codes)
