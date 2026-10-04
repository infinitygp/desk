"""Accessibility tree of the guest desktop, with refs (@e1...) that map to screen pixels."""

import json
import shlex
import subprocess
from pathlib import Path

from . import display, guest
from .config import DeskError

HELPER = Path(__file__).with_name("guest_a11y.py")
SESSION_ENV = "env DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus"


def _helper(run, *args: str) -> dict:
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
    return out


def _on_screen(bbox: list[int], size: tuple[int, int]) -> bool:
    cx, cy = center(bbox)
    return 0 <= cx < size[0] and 0 <= cy < size[1]


def snapshot(run, app: str | None = None, all_windows: bool = False, limit: int = 300,
             save: bool = True) -> dict:
    snap = _helper(run, "snapshot", json.dumps({"app": app, "all": all_windows, "max": limit}))
    size = display.png_size(display.screenshot(run.dom()))
    # Scrolled-away content keeps its extents; only what can be clicked is useful.
    kept, starts = [], []
    for top_i, top in enumerate(snap["tops"]):
        end = snap["tops"][top_i + 1]["start"] if top_i + 1 < len(snap["tops"]) else len(snap["nodes"])
        starts.append(len(kept))
        kept.extend(n for n in snap["nodes"][top["start"]:end] if _on_screen(n["bbox"], size))
    for top, start in zip(snap["tops"], starts, strict=True):
        top["start"] = start
    snap["nodes"] = kept
    for i, node in enumerate(snap["nodes"], 1):
        node["ref"] = f"e{i}"
    if save:
        # Refs printed to the agent must keep pointing at this snapshot.
        (run.dir / "a11y.json").write_text(json.dumps(snap))
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
    if node["states"]:
        parts.append("[" + ",".join(node["states"]) + "]")
    cx, cy = center(node["bbox"])
    parts.append(f"({cx},{cy})")
    return " ".join(parts)


def format_snapshot(snap: dict) -> str:
    lines = []
    tops = snap["tops"] + [{"start": len(snap["nodes"])}]
    for top, nxt in zip(tops, tops[1:], strict=False):
        mark = " [focused]" if top["focus"] else ""
        lines.append(f"window {json.dumps(top['title'], ensure_ascii=False)} ({top['app']}){mark}")
        lines.extend("  " + format_node(n) for n in snap["nodes"][top["start"]:nxt["start"]])
    if not snap["tops"]:
        lines.append("(no accessible windows in scope; try --all)")
    if snap["truncated"]:
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


def locate_ref(run, ref: str) -> tuple[int, int]:
    """Fresh screen position of a ref from the last snapshot (layouts move)."""
    return center(_helper(run, "resolve", json.dumps(_spec(run, ref)))["bbox"])


def find(run, query: str, role: str | None = None, exact: bool = False,
         all_windows: bool = False, save: bool = True) -> list[dict]:
    snap = snapshot(run, all_windows=all_windows, limit=2000, save=save)
    q = query.casefold()

    def match(n: dict) -> bool:
        if role and n["role"] != role:
            return False
        hay = [n["name"], n.get("value") or "", n.get("text") or ""]
        return any((h.casefold() == q) if exact else (q in h.casefold()) for h in hay)

    return [n for n in snap["nodes"] if match(n)]


def locate_name(run, query: str, role: str | None = None, exact: bool = False) -> tuple[int, int]:
    hits = find(run, query, role, exact, save=False)
    if not hits:
        hits = find(run, query, role, exact, all_windows=True, save=False)
    if not hits:
        raise DeskError(f"nothing named {query!r} on screen")
    interactive = [n for n in hits if n["role"] not in ("label", "heading", "paragraph", "static")]
    best = (interactive or hits)[0]
    return center(best["bbox"])


def action(run, ref: str, name: str | None = None) -> dict:
    args = ["action", json.dumps(_spec(run, ref))] + ([name] if name else [])
    return _helper(run, *args)


def set_text(run, ref: str, text: str) -> None:
    _helper(run, "set-text", json.dumps(_spec(run, ref)), text)
