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

    def click(self, name: str, role: str | None = None, exact: bool = True) -> None:
        self.wait_for(name, role, exact)
        x, y = a11y.locate_name(self.target, name, role, exact)
        display.click(self.target.dom(), x, y, run=self.target)

    def focus(self, query: str) -> str:
        """Bring a window to the front by application or title, as clicking it would."""
        title = a11y.focus(self.target, query)
        display.wait_stable(self.target.dom(), timeout=5)
        return title

    def type(self, text: str) -> None:
        display.type_text(self.target.dom(), text, layout=self.spec.layout)

    def key(self, *combos: str) -> None:
        for combo in combos:
            display.key(self.target.dom(), combo)

    def settle(self, timeout: float = 10) -> None:
        display.wait_stable(self.target.dom(), timeout=timeout)


def run_prepare(spec: ImageSpec, target) -> None:
    """The image's `prepare` script: a Python file whose prepare(steps) runs on the host once the
    provisioned session is up, before the warm state is saved (and after cold boots)."""
    if not spec.prepare:
        return
    script = runpy.run_path(str(spec.dir / spec.prepare))
    script["prepare"](Steps(spec, target))
