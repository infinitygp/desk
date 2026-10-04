"""End-to-end check of an image and the harness: run it after rebuilding images or changing desk."""

import hashlib
import secrets
import time
from pathlib import Path

from . import a11y, display, guest, runs
from .config import DeskError, ImageSpec, load_image


class Failed(Exception):
    pass


# What the checks drive on each kind of guest. `window` matches the a11y window's application
# name (Linux) or title (Windows, where packaged apps run under ApplicationFrameHost).
SCENARIOS = {
    "linux": {
        "calc": "gnome-calculator", "calc_window": "gnome-calculator",
        "buttons": ["7", "×", "6", "="], "result": ("text", "value", "42"),
        "editor": "gnome-text-editor", "editor_window": "gnome-text-editor", "editor_role": "text",
        "agent_check": ["id", "-u"], "agent_expect": b"0",
    },
    "windows": {
        "calc": "calc.exe", "calc_window": "Calculator",
        "buttons": ["Seven", "Multiply by", "Six", "Equals"], "result": ("label", "name", "Display is 42"),
        "editor": "notepad.exe", "editor_window": "Notepad", "editor_role": "document",
        "agent_check": ["cmd.exe", "/c", "whoami"], "agent_expect": b"nt authority\\system",
    },
}


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise Failed(msg)


def _ssh(run, command: str) -> str:
    r = guest.ssh_run(run.ssh_port, run.user, runs.spec_for(run).cmd(command), capture=True, timeout=120)
    _check(r.returncode == 0, f"{command!r} exited {r.returncode}: {r.stderr.decode(errors='replace')[-300:]}")
    return r.stdout.decode()


def _ensure(run, binary: str, package: str) -> None:
    """Some installs may lack the apps the selftest drives."""
    install = (f"if command -v dnf; then dnf install -y -q {package}; "
               f"else pacman -S --noconfirm --needed {package}; fi")
    cmd = f"command -v {binary} >/dev/null || ({install}) >/dev/null 2>&1; command -v {binary}"
    r = guest.ssh_run(run.ssh_port, runs.spec_for(run).root_user, cmd, capture=True, timeout=600)
    _check(r.returncode == 0, f"{binary} is not available and could not be installed")


def _launch(run, app: str, window: str) -> None:
    """Start `app` and wait for its focused window, matched by application or title rather than
    by text that localized images translate."""
    spec = runs.spec_for(run)
    dom = run.dom()
    before = display.screen_hash(dom)
    if spec.family == "windows":
        from . import winagent

        winagent.request(run, cmd="run", file=app)
    else:
        _ensure(run, app, app)
        _ssh(run, guest.remote_command([app], gui=True))
    _check(display.wait_change(dom, timeout=30, base=before), f"{app} never appeared on screen")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        snap = a11y.snapshot(run)
        if any(t["focus"] and (t["app"] == window or window in t["title"]) for t in snap["tops"]):
            return
        time.sleep(1)
    raise Failed(f"no focused {window} window in the a11y tree")


def _node(run, role: str) -> dict:
    hits = [n for n in a11y.snapshot(run)["nodes"] if n["role"] == role and n.get("value") is not None]
    _check(bool(hits), f"no {role} with a value in the focused window")
    return hits[0]


def run_selftest(image: str, reboot: bool = False, keep: bool = False, spec: ImageSpec | None = None,
                 shots_dir: Path | None = None) -> bool:
    """`spec` picks specific builds (desk maintain tests new ones); `shots_dir` gets a screenshot
    of every failed step."""
    spec = spec or load_image(image)
    sc = SCENARIOS[spec.family]
    run = None
    results: list[tuple[str, bool, str]] = []

    def step(name: str, fn) -> bool:
        t0 = time.monotonic()
        try:
            detail = fn() or ""
            ok = True
        except (Failed, DeskError) as e:
            detail, ok = str(e), False
            if shots_dir and run is not None and run.live_domain() is not None:
                shots_dir.mkdir(parents=True, exist_ok=True)
                shot = shots_dir / f"{len(results) + 1:02d}-{name.split(' ')[0]}.png"
                shot.write_bytes(display.screenshot(run.live_domain()))
                detail += f" (screen: {shot})"
        results.append((name, ok, detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name:<34} {time.monotonic() - t0:5.1f}s  {detail}", flush=True)
        return ok

    def start():
        nonlocal run
        run = runs.up(spec, run_id=f"selftest-{secrets.token_hex(3)}")
        # A new build under test must really restore its warm state, not fall back to a cold boot.
        _check(not spec.warm_version or run.domain != f"desk-{run.id}", "booted cold instead of restoring")
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
        code, out, _ = guest.agent_exec(run.dom(), [*(spec.agent_wrapper or []), *sc["agent_check"]], timeout=60)
        _check(code == 0 and out.strip().lower() == sc["agent_expect"], f"exit {code}, output {out!r}")

    def calculator():
        _launch(run, sc["calc"], sc["calc_window"])
        for label in sc["buttons"]:
            x, y = a11y.locate_name(run, label, role="button", exact=True)
            display.click(run.dom(), x, y)
        display.wait_stable(run.dom())
        role, field, want = sc["result"]
        nodes = a11y.snapshot(run)["nodes"]
        got = [n.get(field) for n in nodes if n["role"] == role]
        _check(want in got, f"no {role} with {field} {want!r} (saw {got[:5]})")
        display.key(run.dom(), "alt+f4")
        return "7×6=42 through a11y clicks"

    def typing():
        text = "Zażółć gęślą jaźń ĄĆĘŁŃÓŚŹŻ 123 !@#"
        if spec.family == "windows":
            # Notepad opens a first-run teaching tip that blocks its UI Automation; Esc closes it.
            from . import winagent

            winagent.request(run, cmd="run", file=sc["editor"])
            time.sleep(3)
            display.key(run.dom(), "esc")
        _launch(run, sc["editor"], sc["editor_window"])
        display.type_text(run.dom(), text, layout=spec.layout)
        display.wait_stable(run.dom())
        value = _node(run, sc["editor_role"])["value"]
        _check(value == text, f"editor holds {value!r}")
        return f"{len(text)} characters"

    def do_reboot():
        took, inhibitors = guest.reboot(run.ssh_port, spec)
        _launch(run, sc["calc"], sc["calc_window"])
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
    from .maintain import record

    record(image, "selftest", ok=not failed, passed=len(results) - len(failed), total=len(results),
           reboot=reboot, warm=str(spec.warm_dir.resolve()), failed=[f"{n}: {d}" for n, _, d in failed])
    return not failed
