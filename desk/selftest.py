"""End-to-end check of an image and the harness: run it after rebuilding images or changing desk."""

import hashlib
import secrets
import time

from . import a11y, display, guest, runs
from .config import DeskError, load_image


class Failed(Exception):
    pass


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise Failed(msg)


def _ssh(run, command: str) -> str:
    r = guest.ssh_run(run.ssh_port, run.user, command, capture=True, timeout=120)
    _check(r.returncode == 0, f"{command!r} exited {r.returncode}: {r.stderr.decode(errors='replace')[-300:]}")
    return r.stdout.decode()


def _ensure(run, binary: str, package: str) -> None:
    """Minimal installs (Ubuntu's desktop-minimal) may lack the apps the selftest drives."""
    install = f"apt-get install -y -q {package} || dnf install -y -q {package}"
    cmd = f"command -v {binary} >/dev/null || ({install}) >/dev/null 2>&1; command -v {binary}"
    r = guest.ssh_run(run.ssh_port, guest.ROOT, cmd, capture=True, timeout=600)
    _check(r.returncode == 0, f"{binary} is not available and could not be installed")


def _launch(run, app: str) -> None:
    """Start `app` and wait for its focused window (matched by application, not by the
    window title, which is translated in localized images)."""
    _ensure(run, app, app)
    dom = run.dom()
    _ssh(run, guest.remote_command([app], gui=True))
    _check(display.wait_change(dom, timeout=30), f"{app} never appeared on screen")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        snap = a11y.snapshot(run)
        if any(t["focus"] and t["app"] == app for t in snap["tops"]):
            return
        time.sleep(1)
    raise Failed(f"no focused {app} window in the a11y tree")


def _value_of(run, role: str) -> str:
    hits = [n for n in a11y.snapshot(run)["nodes"] if n["role"] == role and n.get("value") is not None]
    _check(bool(hits), f"no {role} with a value in the focused window")
    return hits[0]["value"]


def run_selftest(image: str, reboot: bool = False, keep: bool = False) -> bool:
    spec = load_image(image)
    run = None
    results: list[tuple[str, bool, str]] = []

    def step(name: str, fn) -> bool:
        t0 = time.monotonic()
        try:
            detail = fn() or ""
            ok = True
        except (Failed, DeskError) as e:
            detail, ok = str(e), False
        results.append((name, ok, detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name:<34} {time.monotonic() - t0:5.1f}s  {detail}", flush=True)
        return ok

    def start():
        nonlocal run
        run = runs.up(spec, run_id=f"selftest-{secrets.token_hex(3)}")
        return f"run {run.id}"

    def screen():
        png = display.screenshot(run.dom())
        w, h = display.png_size(png)
        _check(w >= 800 and h >= 600, f"screen only {w}x{h}")
        shots = {hashlib.sha256(display.screenshot(run.dom())).digest() for _ in range(2)}
        return f"{w}x{h}, {'still' if len(shots) == 1 else 'changing'}"

    def ssh():
        _check(_ssh(run, "echo desk-ok").strip() == "desk-ok", "unexpected echo output")

    def agent():
        code, out, _ = guest.agent_exec(run.dom(), [*(spec.agent_wrapper or []), "id", "-u"], timeout=30)
        _check(code == 0 and out.strip() == b"0", f"exit {code}, output {out!r}")

    def calculator():
        _launch(run, "gnome-calculator")
        for label in ("7", "×", "6", "="):
            x, y = a11y.locate_name(run, label, role="button", exact=True)
            display.click(run.dom(), x, y)
        display.wait_stable(run.dom())
        value = _value_of(run, "text")
        _check(value == "42", f"display shows {value!r}, expected '42'")
        display.key(run.dom(), "ctrl+q")
        return "7×6=42 through a11y clicks"

    def typing():
        text = "Zażółć gęślą jaźń ĄĆĘŁŃÓŚŹŻ 123 !@#"
        _launch(run, "gnome-text-editor")
        display.type_text(run.dom(), text, layout=spec.layout)
        display.wait_stable(run.dom())
        value = _value_of(run, "text")
        _check(value == text, f"editor holds {value!r}")
        return f"{len(text)} characters"

    def do_reboot():
        took, inhibitors = guest.reboot(run.ssh_port, run.user, spec.ready)
        _launch(run, "gnome-calculator")
        note = f" (overrode {len(inhibitors)} shutdown inhibitor(s))" if inhibitors else ""
        return f"new boot ready in {took:.0f}s, a11y works{note}"

    try:
        if step("up (warm restore)", start):
            step("screenshot", screen)
            step("exec over SSH", ssh)
            step("exec through guest agent", agent)
            step("a11y + click by name", calculator)
            step("type (incl. Polish letters)", typing)
            if reboot:
                step("reboot", do_reboot)
    finally:
        if run is not None and not keep:
            runs.down(run)
    failed = [r for r in results if not r[1]]
    print(f"{len(results) - len(failed)}/{len(results)} passed" + (f"; run kept: {run.id}" if keep and run else ""))
    return not failed
