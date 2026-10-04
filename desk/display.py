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


def key(dom, combo: str, hold_ms: int = 60) -> None:
    """Press a combo like `ctrl+alt+t`, `super`, `shift+tab` (QEMU qcode names or aliases)."""
    parts = combo.split("+") if combo != "+" else ["+"]
    keys = [{"type": "qcode", "data": _qcode(p)} for p in parts]
    qmp(dom, "send-key", keys=keys, **{"hold-time": hold_ms})


def type_text(dom, text: str, hold_ms: int = 25) -> None:
    for ch in text:
        if ch.isascii() and ch.isalnum():
            codes = ["shift", ch.lower()] if ch.isupper() else [ch]
        elif ch in _UNSHIFTED:
            codes = [_UNSHIFTED[ch]]
        elif ch in _SHIFTED:
            codes = ["shift", _SHIFTED[ch]]
        else:
            raise DeskError(f"cannot type {ch!r} on the US layout; put it on the clipboard in the guest instead")
        qmp(dom, "send-key", keys=[{"type": "qcode", "data": c} for c in codes], **{"hold-time": hold_ms})
        time.sleep(0.01)
