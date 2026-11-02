"""What a person does at the desktop, as steps a script can run: start a program, wait for an
element, click it by name, type, press keys. Images use it to prepare their warm state
(the `prepare` of an addon, e.g. logging in to Cockpit), with the same real input as `desk click`."""

import runpy
import time
from dataclasses import dataclass
from pathlib import Path

import libvirt

from . import a11y, display, guest
from .config import DeskError, ImageSpec, NotDone


@dataclass
class Target:
    """The parts of a run that steps need, also available for the warm-up domain."""
    id: str
    image: str
    dir: Path
    ssh_port: int
    user: str
    agent_port: int
    domain: libvirt.virDomain

    def dom(self) -> libvirt.virDomain:
        return self.domain


class Steps:
    def __init__(self, spec: ImageSpec, target):
        self.spec, self.target = spec, target
        # Web console errors (and Chromium messages that may be errors) already checked by a
        # scenario's console_errors step.
        self.console_seen = self.console_unlevelled = 0
        # The latest listing in which wait_for found what it waited for.
        self.snap: dict | None = None
        # The mouse button press() holds down, and where the pointer was sent last.
        self.held: str | None = None
        self.at: tuple[int, int] | None = None

    def gui(self, *argv: str, env: dict[str, str] | None = None) -> None:
        """Start a program in the desktop session and wait until the screen has changed."""
        self.spec.need_desktop("starting a program in the session (gui)")
        dom = self.target.dom()
        before = display.screen_hash(dom)
        if self.spec.family == "windows":
            from . import winagent

            winagent.request(self.target, cmd="run", file=argv[0], args=list(argv[1:]) or None, env=env or None)
        else:
            r = guest.ssh_run(self.target.ssh_port, self.target.user,
                              guest.remote_command(list(argv), gui=True, env=env), capture=True, timeout=60)
            guest.lost(r, self.target.ssh_port, self.target.user, f"starting {argv[0]}")
            if r.returncode != 0:
                raise DeskError(f"starting {argv[0]} failed: {r.stderr.decode(errors='replace').strip()}")
        display.wait_change(dom, timeout=30, base=before)

    def look(self, name: str, role: str | None, exact: bool, window: str | None) -> tuple[dict, dict, list[dict]]:
        """The elements with this name in the focused window, or in the windows whose title or
        application contains `window`; with the listing they come from, and the whole one."""
        full = a11y.snapshot(self.target, all_windows=bool(window), limit=2000, save=False)
        snap = a11y.in_windows(full, window) if window else full
        return full, snap, a11y._matches(snap, name, role, exact)

    def wait_for(self, name: str, role: str | None = None, exact: bool = True, timeout: float = 60,
                 window: str | None = None, states: list[str] | None = None) -> dict:
        """Until an element with this name (or value or text) is in the focused window, or in the
        windows whose title or application contains `window` (the taskbar, a tray's flyout), and
        has all of `states` (focused, checked, ...)."""
        deadline = time.monotonic() + timeout
        snap = full = error = None
        hits = []
        while True:
            try:
                full, snap, hits = self.look(name, role, exact, window)
            except guest.NoConnection:
                raise  # a guest that does not answer is not a window still coming up
            except DeskError as e:
                error = e  # the window is still coming up
            fit = [n for n in hits if set(states or ()) <= set(n["states"])]
            if fit:
                self.snap = snap
                return fit[0]
            if time.monotonic() > deadline:
                if full is None:
                    raise a11y.Unread(f"the accessibility tree could not be read within {timeout:.0f}s: {error}")
                if hits:
                    raise DeskError(f"{role or 'element'} {name!r} is there but not {' and '.join(states)} within "
                                    f"{timeout:.0f}s; it is {', '.join(hits[0]['states']) or 'in no such state'}")
                if window and not snap["tops"]:
                    there = ", ".join(f"{t['title']!r} ({t['app']})" for t in full["tops"])
                    raise DeskError(f"no window with {window!r} in its title or application name within "
                                    f"{timeout:.0f}s; there are: {there or 'none'}")
                raise DeskError(f"{role or 'element'} {name!r} did not appear"
                                + (f" in a window of {window!r}" if window else "") + f" within {timeout:.0f}s"
                                + a11y.explain_missing(self.target, full, name))
            time.sleep(1)

    def wait_gone(self, name: str, role: str | None = None, exact: bool = False, timeout: float = 30,
                  window: str | None = None) -> None:
        """Until no element with this name is in the focused window (or those of `window`), as read
        from a listing: one that could not be read shows nothing gone."""
        deadline = time.monotonic() + timeout
        error = None
        while True:
            try:
                if not self.look(name, role, exact, window)[2]:
                    return
                error = None
            except guest.NoConnection:
                raise
            except DeskError as e:
                error = e
            if time.monotonic() > deadline:
                if error:
                    raise a11y.Unread(f"the accessibility tree could not be read within {timeout:.0f}s: {error}")
                raise DeskError(f"{name!r} is still on screen")
            time.sleep(1)

    def first(self, *candidates: tuple[str, str | None], timeout: float = 60, exact: bool = True) -> int:
        """Until one of these (name, role) elements is in the focused window; which one."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                snap = a11y.snapshot(self.target, limit=2000, save=False)
            except guest.NoConnection:
                raise
            except DeskError:
                snap = None  # the window is still coming up
            for i, (name, role) in enumerate(candidates):
                if snap and a11y._matches(snap, name, role, exact):
                    return i
            if time.monotonic() > deadline:
                raise DeskError(f"none of {[c[0] for c in candidates]} appeared within {timeout:.0f}s")
            time.sleep(1)

    def running(self, program: str) -> bool:
        """Whether the desktop user runs a process of this name (Linux guests)."""
        r = guest.ssh_run(self.target.ssh_port, self.target.user, f"pgrep -x -u \"$(id -u)\" {program}",
                          capture=True, timeout=30)
        return r.returncode == 0

    def click(self, name: str, role: str | None = None, exact: bool = True, timeout: float = 60,
              button: str = "left", window: str | None = None, count: int = 1) -> None:
        self.wait_for(name, role, exact, timeout, window)
        # The listing that found it; _visible_point looks the position up again anyway.
        x, y = a11y.locate_name(self.target, name, role, exact, snap=self.snap)
        display.click(self.target.dom(), x, y, button=button, count=count, run=self.target)

    def point(self, where, role: str | None = None, exact: bool = True, timeout: float = 60,
              window: str | None = None) -> tuple[int, int]:
        """[x, y] as it is, or where the element with this name is now (found as click finds it)."""
        if isinstance(where, list):
            display.check_points(self.target.dom(), [tuple(where)])
            return where[0], where[1]
        self.wait_for(where, role, exact, timeout, window)
        return a11y.locate_name(self.target, where, role, exact, snap=self.snap)

    def pointer(self, action: str, values: list, button: str = "left", count: int = 1, **find) -> None:
        """The machine's mouse: click [x, y]; move to [x, y] or an element (hover; with a button
        held it glides there as a hand would, for drag-and-drop); drag [x1, y1, x2, y2] or [from,
        to] by name with the left button; scroll [x, y, notches] (positive: down)."""
        dom = self.target.dom()
        if action == "drag" and isinstance(values[0], str):
            values = [*self.point(values[0], **find), *self.point(values[1], **find)]
        elif action == "move" and isinstance(values, str):
            values = list(self.point(values, **find))
        points = [tuple(values[i:i + 2]) for i in range(0, 4 if action == "drag" else 2, 2)]
        display.check_points(dom, points)
        if action == "click":
            display.click(dom, *values, button=button, count=count, run=self.target)
        elif action == "move" and self.held:
            display.glide(dom, *self.at, *values, run=self.target)
        elif action == "move":
            display.move(dom, *values, run=self.target)
        elif action == "drag":
            display.drag(dom, *values, run=self.target)
        else:
            display.scroll(dom, *values[:2], values[2], run=self.target)
        self.at = tuple(values[-2:]) if action == "drag" else tuple(values[:2])

    def press(self, where, button: str = "left", **find) -> None:
        """Press a button at [x, y] or on an element and keep it down: steps in between see the
        drag in progress; release() or the end of the steps lets go."""
        x, y = self.point(where, **find)
        self.held = button
        display.press(self.target.dom(), x, y, button=button, run=self.target)
        self.at = (x, y)

    def release(self, where=True, **find) -> None:
        """Let go of the held button where the pointer is, or after gliding to [x, y] or an element."""
        if where is not True:
            self.pointer("move", where, **find)
        button, self.held = self.held, None
        display.release(self.target.dom(), *self.at, button=button, run=self.target)

    def focus(self, query: str) -> str:
        """Bring a window to the front by application or title, as clicking it would."""
        title = a11y.focus(self.target, query)
        display.wait_stable(self.target.dom(), timeout=5)
        return title

    def set_text(self, name: str, text: str, role: str | None = None, exact: bool = True, timeout: float = 60,
                 window: str | None = None) -> str:
        """Replace the text of the field with this name as a user would (desk set-text), and see that
        it holds it afterwards; a note when only the accessibility interface could set it."""
        node = self.wait_for(name, role, exact, timeout, window)
        if "editable" not in node["states"]:
            # The field rather than its label of the same name.
            node = next((n for n in a11y._matches(self.snap, name, role, exact) if "editable" in n["states"]), node)
        note = a11y.replace_text(self.target, a11y.ref_spec(node), text, layout=self.spec.layout)
        if note.startswith("the field shows"):
            raise DeskError(f"{node['role']} {name!r} did not take {text!r}: {note}")
        if note:
            # Set behind the page's back: what follows would fail as if the product had.
            raise NotDone(f"{node['role']} {name!r}: {note}")
        return note

    def type(self, text: str, anywhere: bool = False) -> None:
        if not anywhere:
            a11y.check_typing_target(self.target, self.spec)
        display.type_text(self.target.dom(), text, layout=self.spec.layout)

    def key(self, *combos: str) -> None:
        for combo in combos:
            display.key(self.target.dom(), combo)

    def settle(self, timeout: float = 10) -> None:
        display.wait_stable(self.target.dom(), timeout=timeout)


def run_prepare(spec: ImageSpec, target, hook: str = "prepare") -> None:
    """The image's `prepare` script: a Python file whose prepare(steps) runs on the host once the
    provisioned session is up, before the warm state is saved (and after cold boots). Other
    functions in it are further steps a test can call by name (desk prepare NAME)."""
    if not spec.prepare:
        return
    script = runpy.run_path(str(spec.path(spec.prepare)))
    hooks = {k: v for k, v in script.items() if callable(v) and not k.startswith("_") and k.islower()}
    if hook not in hooks:
        raise DeskError(f"{spec.name} has no prepare step {hook!r} (has: {', '.join(sorted(hooks))})")
    hooks[hook](Steps(spec, target))
