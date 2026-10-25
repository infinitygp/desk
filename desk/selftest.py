"""End-to-end check of an image and the harness: run it after rebuilding images or changing desk."""

import hashlib
import re
import secrets
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import a11y, display, guest, runs
from .config import Busy, DeskError, ImageSpec, load_image


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
    # Headless: the checks of a desktop that apply, plus what OpenWrt is tested for (packages
    # installed from a file, LuCI).
    "openwrt": {
        "agent_check": ["id", "-u"], "agent_expect": b"0",
        "package": "htop", "web_port": 80,
    },
    "windows": {
        "calc": "calc.exe", "calc_window": "Calculator",
        "buttons": ["Seven", "Multiply by", "Six", "Equals"], "result": ("label", "name", "Display is 42"),
        "editor": "notepad.exe", "editor_window": "Notepad", "editor_role": "document",
        "agent_check": ["cmd.exe", "/c", "whoami"], "agent_expect": b"nt authority\\system",
    },
}


# A desktop's own calculator and editor where it has them (desktops without them get GNOME's, which
# their block installs).
DESKTOP_APPS = {
    "kde": {"calc": "kcalc", "calc_window": "kcalc", "buttons": ["Seven", "Multiply", "Six", "Equals"],
            "editor": "kwrite", "editor_window": "kwrite"},
    # GTK 3, next to GNOME's calculator in GTK 4.
    "xfce": {"editor": "mousepad", "editor_window": "Mousepad"},
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


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
               f"elif command -v apt-get; then DEBIAN_FRONTEND=noninteractive apt-get install -y -q {package}; "
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
    _wait_focused(run, app, window)


def _wait_focused(run, app: str, window: str) -> None:
    deadline = time.monotonic() + 30
    problem = ""
    while time.monotonic() < deadline:
        try:
            snap = a11y.snapshot(run)
        except DeskError as e:
            snap, problem = {"tops": []}, f": {e}"  # a session still starting answers a moment later
        if any(t["focus"] and (t["app"] == window or window in t["title"]) for t in snap["tops"]):
            return
        time.sleep(1)
    raise Failed(f"no focused {window} window in the a11y tree{problem}")


def _node(run, role: str) -> dict:
    hits = [n for n in a11y.snapshot(run)["nodes"] if n["role"] == role and n.get("value") is not None]
    _check(bool(hits), f"no {role} with a value in the focused window")
    return hits[0]


def run_selftest(image: str, reboot: bool = False, keep: bool = False, spec: ImageSpec | None = None,
                 shots_dir: Path | None = None) -> bool:
    """`spec` picks specific builds (desk maintain tests new ones); `shots_dir` gets a screenshot
    of every failed step."""
    spec = spec or load_image(image)
    sc = {**SCENARIOS[spec.family], **DESKTOP_APPS.get(spec.desktop, {})}
    run = None
    results: list[tuple[str, bool, str]] = []

    def step(name: str, fn) -> bool:
        t0 = time.monotonic()
        try:
            detail = fn() or ""
            ok = True
        except Busy:
            raise  # the host's state, not the image's: no result rather than a failed one
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

    # A build under test (desk maintain) must restore its warm state; so must any image that has
    # a usable one. Only where none is to be had is a cold boot what the step checks.
    cold_reason = runs.cold_reason(spec) if spec.base_xml.exists() else None
    usable = (spec.warm_dir / "memory.save").exists() and runs.warm_is_current(spec)
    if cold_reason and usable and not spec.warm_version:
        # Held by another run: a cold boot would pass without the restore having been tried.
        raise Busy(f"{cold_reason}; the selftest restores it, so it runs once that run is down")
    expect_cold = bool(cold_reason) and not spec.warm_version

    def start():
        nonlocal run
        run = runs.up(spec, run_id=f"selftest-{secrets.token_hex(3)}", label="selftest")
        cold = run.domain == f"desk-{run.id}"
        if cold and usable and not spec.warm_version:
            # The only reason left: another run restored it while this one waited for memory.
            raise Busy(f"warm state of {spec.name} was taken by another run while the selftest waited to start")
        _check(expect_cold or not cold, "booted cold instead of restoring" + (f": {cold_reason}" if cold_reason else ""))
        return f"run {run.id}" + (f"; {cold_reason}" if cold else "")

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
            display.click(run.dom(), x, y, run=run)
        display.wait_stable(run.dom())
        role, field, want = sc["result"]
        nodes = a11y.snapshot(run)["nodes"]
        got = [n.get(field) for n in nodes if n["role"] == role]
        _check(want in got, f"no {role} with {field} {want!r} (saw {got[:5]})")
        display.key(run.dom(), "alt+f4")
        return "7×6=42 through a11y clicks"

    def typing():
        layout = runs.spec_for(run).layout  # the guest's, which a new config.toml does not change
        text = ("Zażółć gęślą jaźń ĄĆĘŁŃÓŚŹŻ 123 !@#" if layout == "pl"
                else "Pack my box with five dozen liquor jugs 123 !@#")
        if spec.family == "windows":
            # Started once only: a second start opens another window, which takes the focus
            # whenever it comes up, in the middle of typing too. Notepad's first-run tip, which
            # would block its UI Automation, is marked closed by the image.
            from . import winagent

            winagent.request(run, cmd="run", file=sc["editor"])
            _wait_focused(run, sc["editor"], sc["editor_window"])
        else:
            _launch(run, sc["editor"], sc["editor_window"])
        display.type_text(run.dom(), text, layout=layout)
        display.wait_stable(run.dom())
        value = _node(run, sc["editor_role"])["value"]
        _check(value == text, f"editor holds {value!r}")
        return f"{len(text)} characters"

    def do_reboot():
        took, inhibitors = guest.reboot(run.ssh_port, spec)
        if not spec.desktop:
            agent()
            return f"new boot ready in {took:.0f}s, guest agent answers"
        _launch(run, sc["calc"], sc["calc_window"])
        note = f" (overrode {len(inhibitors)} shutdown inhibitor(s))" if inhibitors else ""
        return f"new boot ready in {took:.0f}s, a11y works{note}"

    def package():
        # A package file through the host, the way a project tests its own build: fetched from
        # the feed in the guest, pulled, pushed back and installed from the file. The package
        # index lives in RAM (/var is /tmp), so a booted router has none until apk update.
        pkg = sc["package"]
        _ssh(run, f"apk update >/dev/null && cd /tmp && apk fetch {pkg} >/dev/null")
        name = _ssh(run, f"cd /tmp && ls {pkg}-*.apk").split()[0]
        with tempfile.TemporaryDirectory() as tmp:
            _check(guest.scp(run.ssh_port, f"{run.user}@127.0.0.1:/tmp/{name}", tmp) == 0, "pulling the package failed")
            _ssh(run, f"rm /tmp/{name}")
            _check(guest.scp(run.ssh_port, str(Path(tmp) / name), f"{run.user}@127.0.0.1:/tmp/") == 0,
                   "pushing the package failed")
        _ssh(run, f"apk add --allow-untrusted /tmp/{name} >/dev/null && command -v {pkg}")
        return f"{name} pulled, pushed and installed"

    def web():
        from . import forward

        local = forward.start(run, sc["web_port"])
        try:
            return _web(local)
        finally:
            forward.stop(run, sc["web_port"])

    def _web(local: int) -> str:
        try:
            page = urllib.request.urlopen(f"http://127.0.0.1:{local}/cgi-bin/luci/", timeout=30)
        except urllib.error.HTTPError as e:
            page = e  # the login form comes with 403
        title = re.search(rb"<title>([^<]*)", page.read())
        _check(title is not None, f"no LuCI page on guest port {sc['web_port']} (HTTP {page.status})")
        login = urllib.request.Request(f"http://127.0.0.1:{local}/cgi-bin/luci/",
                                       data=f"luci_username={spec.user}&luci_password={spec.user}".encode())
        try:
            urllib.request.build_opener(_NoRedirect).open(login, timeout=30)
            answer = None
        except urllib.error.HTTPError as e:
            answer = e
        _check(answer is not None and answer.status == 302 and "sysauth" in (answer.headers.get("Set-Cookie") or ""),
               "logging in to LuCI failed")
        return f"{title.group(1).decode(errors='replace')!r} through desk forward, logged in"

    try:
        if step("up (cold boot)" if expect_cold else "up (warm restore)", start):
            step("screenshot", screen)
            step("exec over SSH", ssh)
            step("exec through guest agent", agent)
            if spec.desktop:
                step("a11y + click by name", calculator)
                step("type with real key presses", typing)
            else:
                step("package from a file (push/pull)", package)
                step("web interface (desk forward)", web)
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
