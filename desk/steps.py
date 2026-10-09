"""What a person does at the desktop, as steps a script can run: start a program, wait for an
element, click it by name, type, press keys. Images use it to prepare their warm state
(`prepare` in image.toml, e.g. logging in to Cockpit), with the same real input as `desk click`."""

import runpy
import time
from dataclasses import dataclass
from pathlib import Path

import libvirt

from . import a11y, display, guest
from .config import DeskError, ImageSpec


@dataclass
class Target:
    """The parts of a run that steps need, also available for the warm-up domain."""
    id: str
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
        # Web console errors already checked by a scenario's console_errors step.
        self.console_seen = 0

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
            if r.returncode != 0:
                raise DeskError(f"starting {argv[0]} failed: {r.stderr.decode(errors='replace').strip()}")
        display.wait_change(dom, timeout=30, base=before)

    def wait_for(self, name: str, role: str | None = None, exact: bool = True, timeout: float = 60) -> dict:
        """Until an element with this name (or value or text) is in the focused window."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                hits = a11y.find(self.target, name, role, exact, save=False)
            except DeskError:
                hits = []  # the window is still coming up
            if hits:
                return hits[0]
            if time.monotonic() > deadline:
                raise DeskError(f"{role or 'element'} {name!r} did not appear within {timeout:.0f}s")
            time.sleep(1)

    def first(self, *candidates: tuple[str, str | None], timeout: float = 60, exact: bool = True) -> int:
        """Until one of these (name, role) elements is in the focused window; which one."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                snap = a11y.snapshot(self.target, limit=2000, save=False)
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

    def click(self, name: str, role: str | None = None, exact: bool = True) -> None:
        self.wait_for(name, role, exact)
        x, y = a11y.locate_name(self.target, name, role, exact)
        display.click(self.target.dom(), x, y, run=self.target)

    def focus(self, query: str) -> str:
        """Bring a window to the front by application or title, as clicking it would."""
        title = a11y.focus(self.target, query)
        display.wait_stable(self.target.dom(), timeout=5)
        return title

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
    script = runpy.run_path(str(spec.dir / spec.prepare))
    hooks = {k: v for k, v in script.items() if callable(v) and not k.startswith("_") and k.islower()}
    if hook not in hooks:
        raise DeskError(f"{spec.name} has no prepare step {hook!r} (has: {', '.join(sorted(hooks))})")
    hooks[hook](Steps(spec, target))
