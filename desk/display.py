"""Out-of-band screen and input: what a physical monitor, mouse and keyboard would see and do."""

import hashlib
import time

import libvirt

from .config import DeskError
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


def screenshot(dom: libvirt.virDomain) -> bytes:
    stream = conn().newStream(0)
    mime = dom.screenshot(stream, 0, 0)
    chunks: list[bytes] = []
    stream.recvAll(lambda _st, data, buf: buf.append(data), chunks)
    stream.finish()
    if mime != "image/png":
        raise DeskError(f"unexpected screenshot type {mime}")
    return b"".join(chunks)


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


def wait_change(dom: libvirt.virDomain, timeout: float = 30.0, interval: float = 0.25) -> bool:
    """Wait until the screen differs from how it looks now (e.g. an app window appears)."""
    base = hashlib.sha256(screenshot(dom)).digest()
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


def _move_events(x: int, y: int, size: tuple[int, int]) -> list[dict]:
    return [
        {"type": "abs", "data": {"axis": "x", "value": _abs(x, size[0])}},
        {"type": "abs", "data": {"axis": "y", "value": _abs(y, size[1])}},
    ]


def _btn(button: str, down: bool) -> dict:
    return {"type": "btn", "data": {"down": down, "button": button}}


def move(dom, x: int, y: int) -> None:
    size = png_size(screenshot(dom))
    qmp(dom, "input-send-event", events=_move_events(x, y, size))


def click(dom, x: int, y: int, button: str = "left", count: int = 1) -> None:
    size = png_size(screenshot(dom))
    qmp(dom, "input-send-event", events=_move_events(x, y, size))
    time.sleep(0.05)
    for _ in range(count):
        qmp(dom, "input-send-event", events=[_btn(button, True)])
        time.sleep(0.05)
        qmp(dom, "input-send-event", events=[_btn(button, False)])
        time.sleep(0.08)


def drag(dom, x1: int, y1: int, x2: int, y2: int, steps: int = 12) -> None:
    size = png_size(screenshot(dom))
    qmp(dom, "input-send-event", events=_move_events(x1, y1, size))
    time.sleep(0.05)
    qmp(dom, "input-send-event", events=[_btn("left", True)])
    for i in range(1, steps + 1):
        x = x1 + (x2 - x1) * i // steps
        y = y1 + (y2 - y1) * i // steps
        qmp(dom, "input-send-event", events=_move_events(x, y, size))
        time.sleep(0.02)
    qmp(dom, "input-send-event", events=[_btn("left", False)])


def scroll(dom, x: int, y: int, dy: int) -> None:
    """Positive dy scrolls down, negative up; one unit is one wheel notch."""
    size = png_size(screenshot(dom))
    qmp(dom, "input-send-event", events=_move_events(x, y, size))
    button = "wheel-down" if dy > 0 else "wheel-up"
    for _ in range(abs(dy)):
        qmp(dom, "input-send-event", events=[_btn(button, True)])
        qmp(dom, "input-send-event", events=[_btn(button, False)])
        time.sleep(0.03)


def _qcode(name: str) -> str:
    n = name.strip().lower()
    return KEY_ALIASES.get(n, n)


def _key_event(code: str, down: bool) -> dict:
    return {"type": "key", "data": {"down": down, "key": {"type": "qcode", "data": code}}}


def chord(dom, codes: list[str], hold_ms: int = 30, gap_ms: int = 20) -> None:
    """Press keys in order, release in reverse.

    input-send-event is applied before the command returns, unlike send-key, which only
    queues timed events; so once this returns the guest has every key and the next action
    (a click moving focus, say) cannot overtake the typing.
    """
    qmp(dom, "input-send-event", events=[_key_event(c, True) for c in codes])
    time.sleep(hold_ms / 1000)
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
