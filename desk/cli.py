import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import a11y, display, evidence, guest, images, record, runs
from .config import DeskError, list_images, load_image


def _emit_shot(run: runs.Run, png: bytes, out: str | None, label: str = "") -> None:
    path = Path(out) if out else run.next_shot_path(label)
    path.write_bytes(png)
    run.log_event(shot=str(path))
    w, h = display.png_size(png)
    print(f"{path} {w}x{h}")


def _after_action(run: runs.Run, dom, args) -> None:
    if getattr(args, "shot", False):
        png, _ = display.wait_stable(dom, timeout=args.settle)
        _emit_shot(run, png, None, args.cmd)


def cmd_image(args) -> None:
    if args.image_cmd == "shot":
        spec = load_image(args.name)
        from .virt import lookup

        dom = lookup(images.build_domain_name(spec))
        if dom is None or not dom.isActive():
            raise DeskError(f"no install of {spec.name} is running")
        out = Path(images.LOGS) / f"{images.build_domain_name(spec)}-{int(time.time())}.png"
        out.write_bytes(display.screenshot(dom))
        print(out)
        return
    if args.image_cmd == "list":
        _image_list()
        return
    spec = load_image(args.name)
    # Builds are versioned (images.promote), so runs of the current build may go on meanwhile.
    with runs.image_lock(spec.name):
        if args.image_cmd == "build":
            images.build(spec, force=args.force)
        elif args.image_cmd == "warm":
            images.warm(spec, force=args.force)
    with runs.state_lock():
        runs.prune_versions()


def _image_list() -> None:
    from .maintain import days_left, load_status

    status = load_status()
    print(f"{'IMAGE':<22}{'STATE':<8}{'BUILT':<12}{'SELFTEST':<20}NOTES")
    for name in list_images():
        spec = load_image(name)
        notes = [f"variant of {spec.base}"] if spec.base else []
        if (spec.warm_dir / "memory.save").exists():
            state = "warm"
            if not runs.warm_is_current(spec):
                notes.append("warm state predates the base build (desk image warm --force)")
        else:
            state = "base" if spec.base_xml.exists() else "-"
        meta = spec.base_dir / "meta.json"
        built = time.strftime("%Y-%m-%d", time.localtime(json.loads(meta.read_text())["created"])) \
            if meta.exists() else "-"
        st = status.get(name, {})
        test = "-"
        if "selftest" in st:
            s = st["selftest"]
            test = f"{s['passed']}/{s['total']} {time.strftime('%m-%d %H:%M', time.localtime(s['t']))}"
            if spec.warm_dir.exists() and s.get("warm") != str(spec.warm_dir.resolve()):
                notes.append("last selftest was of another build")
        left = days_left(spec)
        if left is not None:
            notes.append(f"licence ends in {left:.0f} days")
        m = st.get("maintain")
        if m and not m["ok"]:
            what = "skipped" if m["ok"] is None else "failed"
            notes.append(f"maintenance {what} {time.strftime('%m-%d', time.localtime(m['t']))}: {m['note']}")
        print(f"{name:<22}{state:<8}{built:<12}{test:<20}{'; '.join(notes)}")


def _resolution(text: str | None) -> tuple[int, int] | None:
    if not text:
        return None
    try:
        width, height = (int(v) for v in text.lower().split("x"))
    except ValueError:
        raise DeskError(f"--resolution takes WIDTHxHEIGHT, e.g. 1920x1080 (got {text!r})") from None
    return width, height


def cmd_up(args) -> None:
    from . import disks, forward, monitors

    resolution = _resolution(args.resolution)
    spec = load_image(args.image)
    # Whatever can be refused without a VM is refused before one is started.
    monitors.check(spec, args.displays, resolution, args.scale)
    extra_disks = [disks.parse(item) for item in args.disk or []]
    run = runs.up(spec, run_id=args.name, cold=args.cold, force=args.force, label=args.label,
                  displays=args.displays, resolution=resolution)
    try:
        if args.displays != 1 or resolution or args.scale:
            monitors.apply(run, spec, resolution=resolution, scale=args.scale)
        run.log_event(argv=sys.argv[1:])
        for size, name in extra_disks:
            disks.add(run, size, name)
        if args.record:
            record.start(run)
        forwards = [(port, forward.start(run, port)) for port in args.forward or []]
    except BaseException:
        # Its id has not been printed yet, so nobody could take a half set up run down.
        print(f"[desk] run {run.id} started, but setting it up failed; taking it down again", file=sys.stderr)
        runs.down(run)
        raise
    print(f"run {run.id} up (image {run.image}, ssh port {run.ssh_port})" + (", recording" if args.record else ""))
    for port, local in forwards:
        print(_forward_line(port, local))
    print(f"export DESK_RUN={run.id}  # fish: set -x DESK_RUN {run.id}")


def cmd_down(args) -> None:
    if args.id and args.run and args.id != args.run:
        raise DeskError(f"two different runs given: -r {args.run} and {args.id}")
    targets = runs.all_runs() if args.all else [runs.resolve(args.id or args.run)]
    for run in targets:
        gone = _run_files(run)
        report, videos = evidence.finish(run, keep=args.keep, diff=args.diff, report=not args.no_report)
        if args.keep:
            print(f"run {run.id} down (files kept in {run.dir})")
        else:
            print(f"run {run.id} down; deleted {run.dir}{gone} (--keep keeps it)"
                  + ("; screenshots and logs are in the report" if report else ""))
        if report:
            print(f"report: {report / 'report.md'}")
        for v in videos:
            print(f"video: {v}")


def _run_files(run: runs.Run) -> str:
    """What `desk down` deletes beyond the disk, worth naming: snapshots, extra disks, kept files."""
    parts = []
    snaps = [d for d in (run.dir / "snapshots").iterdir() if d.is_dir()] if (run.dir / "snapshots").is_dir() else []
    disks = list((run.dir / "disks").glob("*.qcow2")) if (run.dir / "disks").is_dir() else []
    if snaps:
        parts.append(f"{len(snaps)} snapshot(s)")
    if disks:
        parts.append("disk(s) " + ", ".join(d.stem for d in disks))
    return f" with {' and '.join(parts)}" if parts else ""


def _forward_line(port: int, local: int) -> str:
    scheme = "https://" if port in (443, 8443, 9090) else "http://" if port in (80, 8080) else ""
    return (f"guest port {port} -> {scheme}127.0.0.1:{local}{'/' if scheme else ''} on the host, until the run goes "
            f"down (desk forward --stop {port})")


def cmd_forward(args) -> None:
    from . import forward

    run = runs.resolve(args.run)
    if args.stop is not None:
        stopped = forward.stop(run, None if args.stop == "all" else _port(args.stop))
        print("stopped " + (", ".join(map(str, stopped)) if stopped else "nothing"))
        return
    if not args.port:
        rows = forward.listing(run)
        for f in rows:
            print(f"{f['port']}\t127.0.0.1:{f['local']}\t" + ("up" if f["alive"] else "gone"))
        if not rows:
            print("no ports forwarded (desk forward PORT)")
        return
    port, _, local = args.port.partition(":")
    run.dom()
    print(_forward_line(_port(port), forward.start(run, _port(port), _port(local) if local else None)))


def _port_arg(text: str) -> int:
    try:
        return _port(text)
    except DeskError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def _port(text: str) -> int:
    if not text.isdigit() or not 0 < int(text) < 65536:
        raise DeskError(f"not a port number: {text!r}")
    return int(text)


def cmd_report(args) -> None:
    run = runs.resolve(args.run)
    print(evidence.collect(run, diff=args.diff) / "report.md")


def cmd_diff(args) -> None:
    run = runs.resolve(args.run)
    print(evidence.format_changes(evidence.file_changes(run, all_paths=args.all), limit=args.limit))


def cmd_ls(args) -> None:
    for r in runs.all_runs():
        state = "running" if r.live_domain() is not None else "stopped"
        held = (r.label or "-") + ("" if not r.holder or runs.holder_alive(r) else " (agent gone)")
        print(f"{r.id}\t{r.image}\t{state}\tidle {r.idle() / 60:.0f} min\t{held}\tssh:{r.ssh_port}\t{r.dir}")


def cmd_gc(args) -> None:
    evidence.prune_reports()
    removed = runs.gc_idle()
    with runs.state_lock():
        removed += runs.gc() + runs.prune_versions()
    if not removed:
        print(f"nothing to clean (idle limit {runs.IDLE_TTL / 3600:g} h, DESK_TTL to change)")


def cmd_shot(args) -> None:
    run = runs.resolve(args.run)
    dom = run.dom()
    png = display.wait_stable(dom, timeout=args.settle)[0] if args.settle > 0 else display.screenshot(dom)
    box = None
    if args.region:
        try:
            box = tuple(int(v) for v in args.region.split(","))
            assert len(box) == 4
        except (ValueError, AssertionError):
            raise DeskError(f"--region takes X,Y,WIDTH,HEIGHT (got {args.region!r})") from None
    elif args.window:
        box = _window_box(run, args.window)
    if box or args.zoom != 1:
        png = display.crop(png, box or (0, 0, *display.png_size(png)), zoom=args.zoom)
    _emit_shot(run, png, args.output, args.label or "")


def _window_box(run: runs.Run, query: str) -> tuple[int, int, int, int]:
    """The frame of the topmost window whose title or application contains `query`."""
    snap = a11y.snapshot(run, all_windows=True, limit=2000, save=False)
    q = query.casefold()
    # GNOME: the shell's own window list (frames in logical pixels), topmost last.
    for w in reversed(snap.get("windows") or []):
        if not w.get("minimized") and (q in (w.get("title") or "").casefold() or q in (w.get("wm_class") or "").casefold()):
            f = (run.scale or 100) / 100
            return tuple(round(v * f) for v in w["frame"])
    for top in reversed(snap["tops"]):
        if q in top["title"].casefold() or q in top["app"].casefold():
            nodes = snap["nodes"][top["start"]:]
            if nodes and nodes[0].get("bbox") and nodes[0]["role"] in ("frame", "window", "dialog"):
                return tuple(nodes[0]["bbox"])
    raise DeskError(f"no window with {query!r} in its title or application name")


def _wait_exec(run: runs.Run, args) -> None:
    """Run a command in the guest every 2 s until it succeeds: a service up, a file written."""
    spec = runs.spec_for(run)
    timeout = args.timeout if args.timeout is not None else 120
    deadline = time.monotonic() + timeout
    start, out = time.monotonic(), b""
    while True:
        try:
            limit = max(5, min(60, deadline - time.monotonic()))
            r = guest.ssh_run(run.ssh_port, spec.root_user if args.root else run.user,
                              guest.command(spec, [args.until_exec], limit=limit), capture=True, timeout=limit + 30)
            out = r.stdout + r.stderr
            if r.returncode == 0:
                run.log_event(rc=0)
                last = out.decode(errors="replace").strip().splitlines()
                print(f"succeeded after {time.monotonic() - start:.0f}s" + (f": {last[-1][:200]}" if last else ""))
                return
        except subprocess.TimeoutExpired:
            out = b"(the command itself did not finish)"
        if time.monotonic() + 2 > deadline:
            break
        time.sleep(2)
    run.log_event(rc=1)
    tail = out.decode(errors="replace").strip()[-500:]
    print(f"still failing after {timeout:.0f}s" + (f"; last output:\n{tail}" if tail else ""))
    sys.exit(1)


def cmd_wait(args) -> None:
    run = runs.resolve(args.run)
    if args.until_exec:
        _wait_exec(run, args)
        return
    if args.root:
        raise DeskError("--root goes with --until-exec")
    if args.timeout is None:
        args.timeout = 15.0
    if args.ready:
        guest.wait_ssh(run.ssh_port, run.user, timeout=args.timeout)
        spec = runs.spec_for(run)
        guest.wait_ready(run.ssh_port, run.user, spec.cmd(spec.ready), timeout=args.timeout)
    dom = run.dom()
    if args.change and not display.wait_change(dom, timeout=args.timeout):
        print("no change")
        sys.exit(1)
    _, stable = display.wait_stable(dom, timeout=args.timeout)
    print("stable" if stable else "still changing")
    if not stable:
        sys.exit(1)


def _target_point(run, args) -> tuple[int, int]:
    """Pixel coordinates, an @ref from the last `desk a11y`, or --name lookup."""
    t = args.target
    if args.name:
        if t:
            raise DeskError("give either a target or --name, not both")
        return a11y.locate_name(run, args.name, role=args.role, exact=args.exact)
    if len(t) == 1 and t[0].startswith("@"):
        return a11y.locate_ref(run, t[0])
    if len(t) == 2 and all(v.lstrip("-").isdigit() for v in t):
        return int(t[0]), int(t[1])
    raise DeskError("target must be X Y, @ref or --name TEXT")


def cmd_click(args) -> None:
    run = runs.resolve(args.run)
    dom = run.dom()
    x, y = _target_point(run, args)
    display.click(dom, x, y, button=args.button, count=2 if args.double else 1, run=run)
    if not args.shot:
        print(f"clicked ({x},{y})")
    _after_action(run, dom, args)


def cmd_a11y(args) -> None:
    run = runs.resolve(args.run)
    run.dom()
    snap = a11y.snapshot(run, app=args.app, all_windows=args.all, limit=args.max)
    print(a11y.format_snapshot(snap, offscreen=args.offscreen))


def cmd_find(args) -> None:
    run = runs.resolve(args.run)
    run.dom()
    hits = a11y.find(run, args.query, role=args.role, exact=args.exact, all_windows=args.all)
    for n in hits:
        print(a11y.format_node(n, indent=False))
    if not hits:
        print(f"nothing matches {args.query!r}" + ("" if args.all else " in the focused window (try --all)"))
        sys.exit(1)


def cmd_action(args) -> None:
    run = runs.resolve(args.run)
    dom = run.dom()
    res = a11y.action(run, args.ref, args.action)
    if not args.shot:
        print(f"did {res['done']!r} (available: {', '.join(res['actions'])})")
    _after_action(run, dom, args)


def cmd_set_text(args) -> None:
    run = runs.resolve(args.run)
    dom = run.dom()
    note = a11y.set_text(run, args.ref, args.text, layout=runs.spec_for(run).layout)
    if note:
        print(f"note: {note}")
    _after_action(run, dom, args)


def cmd_move(args) -> None:
    run = runs.resolve(args.run)
    dom = run.dom()
    display.move(dom, args.x, args.y, run=run)
    _after_action(run, dom, args)


def cmd_drag(args) -> None:
    run = runs.resolve(args.run)
    dom = run.dom()
    display.drag(dom, args.x1, args.y1, args.x2, args.y2, run=run)
    _after_action(run, dom, args)


def cmd_scroll(args) -> None:
    run = runs.resolve(args.run)
    dom = run.dom()
    display.scroll(dom, args.x, args.y, args.dy, run=run)
    _after_action(run, dom, args)


def cmd_type(args) -> None:
    run = runs.resolve(args.run)
    dom = run.dom()
    spec = runs.spec_for(run)
    if not args.anywhere:
        a11y.check_typing_target(run, spec)
    display.type_text(dom, args.text, layout=spec.layout)
    _after_action(run, dom, args)


def cmd_key(args) -> None:
    run = runs.resolve(args.run)
    dom = run.dom()
    for combo in args.combo:
        display.key(dom, combo)
    _after_action(run, dom, args)


# Plain `desk exec` and `exec --gui --wait`: long enough for a package build, short enough that a
# hung command does not block the caller indefinitely.
EXEC_TIMEOUT = 600.0


def _exec_stopped(run, limit: float) -> None:
    run.log_event(rc=124)
    print(f"desk: still running after {limit:.0f}s; stopped it (--timeout SECONDS to allow longer, 0: no limit)",
          file=sys.stderr)
    sys.exit(124)


def cmd_exec(args) -> None:
    run = runs.resolve(args.run)
    limit = EXEC_TIMEOUT if args.timeout is None else (args.timeout or None)
    argv = args.command
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        raise DeskError("nothing to run")
    if args.agent:
        if args.gui:
            raise DeskError("--gui needs SSH (the agent runs outside the user session)")
        wrapper = runs.spec_for(run).agent_wrapper or []
        code, out, err = guest.agent_exec(run.dom(), [*wrapper, *argv], timeout=args.timeout or 120)
        sys.stdout.buffer.write(out)
        sys.stderr.buffer.write(err)
        run.log_event(rc=code)
        sys.exit(code)
    if args.root and args.gui:
        raise DeskError("--gui starts programs in the desktop user's session; drop --root")
    if args.gui:
        runs.spec_for(run).need_desktop("desk exec --gui")
    if args.wait and not args.gui:
        raise DeskError("--wait goes with --gui (plain exec waits anyway)")
    env = {}
    for item in args.env or []:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise DeskError(f"--env takes NAME=VALUE (got {item!r})")
        env[key] = value
    if args.gui and args.wait:
        # Runs in the desktop session and returns the program's output and exit code: what it
        # sees there (windows, UI Automation, the session's environment) SSH does not.
        if run.agent_port:
            from . import winagent

            timeout = limit or 7 * 86400
            reply = winagent.request(run, timeout=timeout + 30, cmd="run", file=argv[0], args=argv[1:] or None,
                                     env=env or None, wait=True, timeout_s=timeout)
            sys.stdout.write(reply.get("stdout") or "")
            sys.stderr.write(reply.get("stderr") or "")
            if reply.get("timeout"):
                _exec_stopped(run, timeout)
            code = int(reply.get("code", 1))
        else:
            start = time.monotonic()
            try:
                r = guest.ssh_run(run.ssh_port, run.user,
                                  guest.remote_command(argv, gui=True, wait=True, env=env, limit=limit),
                                  timeout=limit and limit + 30)
            except subprocess.TimeoutExpired:
                _exec_stopped(run, limit)
            if limit and r.returncode != 0 and time.monotonic() - start >= limit:
                _exec_stopped(run, limit)
            code = r.returncode
        run.log_event(rc=code)
        sys.exit(code)
    if args.gui:
        # Returns once the program has drawn something (or after the timeout), on every guest:
        # the screen is compared with how it looked before the start, not after.
        dom = run.dom()
        before = display.screen_hash(dom)
        if run.agent_port:
            from . import winagent

            reply = winagent.request(run, cmd="run", file=argv[0], args=argv[1:] or None, env=env or None)
            started = f"started pid {reply.get('pid')}"
        else:
            r = guest.ssh_run(run.ssh_port, run.user, guest.remote_command(argv, gui=True, env=env),
                              timeout=args.timeout)
            if r.returncode != 0:
                run.log_event(rc=r.returncode)
                sys.exit(r.returncode)
            started = "started"
        changed = display.wait_change(dom, timeout=args.timeout or 30, base=before)
        run.log_event(rc=0)
        print(started + ("" if changed else "; nothing new on screen yet (slow start? check with desk shot)"))
        return
    spec = runs.spec_for(run)
    command = guest.command(spec, argv, env=env, limit=limit)
    start = time.monotonic()
    try:
        # The guest stops the command first; SSH's timeout is the fallback.
        r = guest.ssh_run(run.ssh_port, spec.root_user if args.root else run.user, command,
                          timeout=limit and limit + 30)
    except subprocess.TimeoutExpired:
        _exec_stopped(run, limit)
    if limit and r.returncode != 0 and time.monotonic() - start >= limit:
        _exec_stopped(run, limit)
    run.log_event(rc=r.returncode)
    sys.exit(r.returncode)


def cmd_reboot(args) -> None:
    run = runs.resolve(args.run)
    took, inhibitors = guest.reboot(run.ssh_port, runs.spec_for(run), timeout=args.timeout, command=args.command)
    if run.clock_offset:
        from . import clock

        clock.apply(run)
    for line in inhibitors:
        print(f"note: shutdown was blocked by: {line}")
    dom = run.dom()
    png, _ = display.wait_stable(dom, timeout=30)
    print(f"rebooted in {took:.0f}s")
    spec = runs.spec_for(run)
    if args.prepare:
        from . import steps

        if not spec.prepare:
            raise DeskError(f"{spec.name} has no prepare step")
        steps.run_prepare(spec, run)
        run.log_event(prepare="prepare")
        png, _ = display.wait_stable(dom, timeout=10)
        print(f"prepared again ({spec.prepare})")
    elif spec.prepare:
        print(f"note: programs open before the reboot are gone; `desk prepare` sets up again what {spec.name} "
              "prepares (fedora-44-cockpit: Firefox logged in to Cockpit), or reboot with --prepare")
    if args.shot:
        _emit_shot(run, png, None, "reboot")


def cmd_view(args) -> None:
    from .virt import vnc_port

    run = runs.resolve(args.run)
    port = vnc_port(run.dom())
    if port is None or not run.vnc_password:
        raise DeskError(f"run {run.id} has no VNC display (started before desk view existed?)")
    host = os.uname().nodename
    print(f"VNC for run {run.id}: 127.0.0.1:{port} on {host}, password {run.vnc_password}")
    print("On your workstation (fish), keep this running:")
    print(f"  ssh -N -L {port}:127.0.0.1:{port} {host}")
    print(f"then open a VNC viewer on localhost:{port}, ideally in view-only mode so you do not")
    print(f"move the pointer under the agent (e.g. `vncviewer -ViewOnly localhost:{port}`).")


def cmd_record(args) -> None:
    run = runs.resolve(args.run)
    if args.action == "start":
        record.start(run, fps=args.fps, limit=args.limit * 60)
        print(f"recording run {run.id} at {args.fps} fps (desk record stop; at most {args.limit:g} min)")
    else:
        video = record.stop(run)
        print(video if video else "nothing was recorded")


def cmd_focus(args) -> None:
    run = runs.resolve(args.run)
    print(f"focused: {a11y.focus(run, args.query)}")
    _after_action(run, run.dom(), args)


def cmd_scenario(args) -> None:
    from . import scenario

    path = Path(args.file)
    if args.action == "check":
        data = scenario.load(path)
        image = load_image(args.image or data["image"]).name
        print(f"{path}: image {image}, {len(data.get('step', []))} steps")
        for i, step in enumerate(data.get("step", []), 1):
            action, arg = scenario.describe(step)
            print(f"  {i:>2}. {step.get('name') or (action + ' ' + arg).strip()}")
        return
    sys.exit(0 if scenario.run_scenario(path, keep=args.keep, image=args.image) else 1)


def cmd_snapshot(args) -> None:
    from . import snapshot

    run = runs.resolve(args.run)
    if args.action == "list":
        for s in snapshot.listing(run):
            print(f"{s['name']}\t{time.strftime('%H:%M:%S', time.localtime(s['created']))}\t"
                  f"{s['bytes'] / 2**30:.1f} GiB")
        return
    if not args.name:
        raise DeskError(f"desk snapshot {args.action} needs a NAME")
    if args.action == "delete":
        snapshot.delete(run, args.name)
        print(f"deleted {args.name}")
        return
    # Like up/down: no other desk command creates or destroys VMs meanwhile.
    with runs.state_lock():
        if args.action == "save":
            took = snapshot.save(run, args.name, force=args.force)
            print(f"saved {args.name} ({took:.1f}s; desk snapshot restore {args.name})")
        else:
            took = snapshot.restore(run, args.name)
            print(f"restored {args.name} ({took:.1f}s)")


def cmd_display(args) -> None:
    from . import monitors

    run = runs.resolve(args.run)
    resolution = _resolution(args.resolution)
    if resolution or args.scale:
        monitors.apply(run, runs.spec_for(run), resolution=resolution, scale=args.scale)
    sizes = display.screen_sizes(run.dom())
    print(f"{len(sizes)} display(s): " + ", ".join(f"{w}x{h}" for w, h in sizes)
          + f"; scale {run.scale or 100} %")


def cmd_win(args) -> None:
    from . import winsettings

    run = runs.resolve(args.run)
    spec = runs.spec_for(run)
    if spec.family != "windows":
        raise DeskError("desk win is for Windows guests")
    if args.win_cmd == "theme":
        winsettings.theme(run, spec, args.mode)
    else:
        winsettings.taskbar(run, spec, align=args.align, autohide=args.autohide, edge=args.edge, size=args.size)
    print("done")
    _after_action(run, run.dom(), args)


def cmd_disk(args) -> None:
    from . import disks

    run = runs.resolve(args.run)
    if args.disk_cmd == "add":
        name = disks.add(run, args.size, args.name)
        print(f"disk {name} plugged in ({args.size}; Linux: /dev/disk/by-id/virtio-{name}, "
              f"Windows: serial number {name}, not initialized)")
    elif args.disk_cmd == "rm":
        disks.remove(run, args.name)
        print(f"disk {args.name} unplugged and deleted")
    else:
        for d in disks.listing(run):
            print(f"{d['name']}\t{d['size'] / 2**30:.1f} GiB\t{d['used'] / 2**20:.0f} MiB used\t"
                  + ("plugged in" if d["attached"] else "not plugged in"))


def cmd_clock(args) -> None:
    from . import clock

    run = runs.resolve(args.run)
    if args.step and (not args.value or args.value == "reset"):
        raise DeskError("--step goes with a time to move to, e.g. desk clock +3d --step 1d")
    if args.value == "reset":
        clock.reset(run)
    elif args.step:
        for t, started in clock.advance(run, args.value, args.step, timeout=args.timeout):
            stamp = datetime.fromtimestamp(t, clock.GUEST_TZ).strftime("%Y-%m-%d %H:%M")
            print(f"{stamp}: " + (", ".join(started) if started else "nothing started"), flush=True)
    elif args.value:
        clock.set_time(run, args.value)
    print(clock.describe(run))


def cmd_browser(args) -> None:
    from . import browser

    run = runs.resolve(args.run)
    if args.browser_cmd == "open":
        try:
            print(f"showing {browser.open_url(run, args.url)}")
        except browser.NotLoaded as e:
            # What the page did, not what desk could not do: exit 1, with the screenshot asked for.
            print(f"desk: {e}", file=sys.stderr)
            _after_action(run, run.dom(), args)
            run.log_event(rc=1)
            sys.exit(1)
        _after_action(run, run.dom(), args)
        return
    if args.browser_cmd == "viewport":
        print(browser.viewport(run, args.size))
        _after_action(run, run.dom(), args)
        return
    lines = browser.console(run, everything=args.all)
    if args.errors:
        lines = browser.errors(lines)
    print("\n".join(lines) if lines else "(nothing in the web console since the run started)")


def cmd_prepare(args) -> None:
    from . import steps

    run = runs.resolve(args.run)
    spec = runs.spec_for(run)
    if not spec.prepare:
        raise DeskError(f"{spec.name} has no prepare step")
    steps.run_prepare(spec, run, args.hook)
    run.log_event(prepare=args.hook)
    print("done")
    _after_action(run, run.dom(), args)


def cmd_maintain(args) -> None:
    from . import maintain

    if args.install_timer:
        maintain.install_timer()
        return
    sys.exit(0 if maintain.maintain(only=args.only, dry_run=args.dry_run) else 1)


def cmd_doctor(args) -> None:
    from . import doctor

    sys.exit(0 if doctor.run() else 1)


def cmd_selftest(args) -> None:
    from .selftest import run_selftest

    sys.exit(0 if run_selftest(args.image, reboot=args.reboot, keep=args.keep) else 1)


def _copy(args, push: bool) -> None:
    run = runs.resolve(args.run)
    user = runs.spec_for(run).root_user if args.root else run.user
    remote = f"{user}@127.0.0.1:{args.remote}"
    src, dst = (args.local, remote) if push else (remote, args.local)
    rc = guest.scp(run.ssh_port, src, dst, timeout=args.timeout or None)
    run.log_event(rc=rc)
    if rc == 124:
        print(f"desk: still copying after {args.timeout:.0f}s; stopped it (--timeout SECONDS to allow longer, "
              "0: no limit)", file=sys.stderr)
    elif rc != 0:
        print(f"desk: copying failed (scp exit {rc})"
              + ("" if args.root else f"; {args.remote} may be outside what {run.user} can access: --root"),
              file=sys.stderr)
    sys.exit(rc)


def cmd_push(args) -> None:
    _copy(args, push=True)


def cmd_pull(args) -> None:
    _copy(args, push=False)


def cmd_ssh(args) -> None:
    run = runs.resolve(args.run)
    argv = guest.ssh_argv(run.ssh_port, run.user, tty=sys.stdin.isatty())
    os.execvp(argv[0], argv)


def _action_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--shot", action="store_true", help="wait for the screen to settle, then screenshot")
    p.add_argument("--settle", type=float, default=5.0, help="max seconds to wait for a still screen")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="desk", description="Drive disposable desktop VMs for testing.")
    ap.add_argument("-r", "--run", help="run id (default: $DESK_RUN or the only active run)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("image", help="golden images")
    isub = p.add_subparsers(dest="image_cmd", required=True)
    isub.add_parser("list")
    for name, hlp in (("build", "unattended install of the base image"),
                      ("warm", "save a logged-in desktop for instant runs")):
        ip = isub.add_parser(name, help=hlp)
        ip.add_argument("name")
        ip.add_argument("--force", action="store_true")
    ip = isub.add_parser("shot", help="screenshot of an image install in progress")
    ip.add_argument("name")
    p.set_defaults(func=cmd_image)

    p = sub.add_parser("up", help="start a disposable run")
    p.add_argument("image")
    p.add_argument("--name", help="run id (default: random)")
    p.add_argument("--label", help="who or what uses the run, shown by desk ls (default: $DESK_LABEL, "
                   "or the coding agent and its directory)")
    p.add_argument("--cold", action="store_true", help="boot instead of restoring the warm state")
    p.add_argument("--force", action="store_true", help="start even if free memory looks too low")
    p.add_argument("--record", action="store_true", help="record a video from the start (desk record)")
    p.add_argument("--displays", type=int, default=1, help="monitors (boots cold when not 1)")
    p.add_argument("--resolution", help="mode the monitors start in, e.g. 1920x1080 (boots cold)")
    p.add_argument("--scale", type=int, help="scaling in percent, e.g. 150 (Windows: signs in again)")
    p.add_argument("--disk", action="append", metavar="SIZE[:NAME]", help="extra empty disk, e.g. 10G:backup "
                   "(repeatable; desk disk)")
    p.add_argument("--forward", action="append", type=_port_arg, metavar="PORT",
                   help="reach this guest port from the host (repeatable; desk forward)")
    p.set_defaults(func=cmd_up)

    p = sub.add_parser("down", help="destroy a run and delete its files")
    p.add_argument("id", nargs="?")
    p.add_argument("--all", action="store_true")
    p.add_argument("--keep", action="store_true", help="keep the run directory (disk, shots, logs)")
    p.add_argument("--diff", action="store_true", help="add the files the run changed to the report (~30 s)")
    p.add_argument("--no-report", action="store_true", help="skip the evidence report")
    p.set_defaults(func=cmd_down)

    p = sub.add_parser("report", help="write the evidence report now (timeline, guest problems)")
    p.add_argument("--diff", action="store_true", help="include changed files (~30 s)")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("diff", help="files changed in the guest since the image it started from (~30 s)")
    p.add_argument("--all", action="store_true", help="include routine paths (logs, caches, package db)")
    p.add_argument("--limit", type=int, default=200, help="max paths per category")
    p.set_defaults(func=cmd_diff)

    sub.add_parser("ls", help="list runs").set_defaults(func=cmd_ls)
    sub.add_parser("gc", help="destroy runs idle longer than $DESK_TTL s (default 2 h) and orphaned VMs"
                   ).set_defaults(func=cmd_gc)

    p = sub.add_parser("shot", help="screenshot; prints the PNG path and size")
    p.add_argument("-o", "--output")
    p.add_argument("--label")
    p.add_argument("--settle", type=float, default=3.0, help="wait up to N s for a still screen (0: now)")
    p.add_argument("--region", metavar="X,Y,W,H", help="only this part of the screen")
    p.add_argument("--window", metavar="NAME", help="only the window with NAME in its title or application")
    p.add_argument("--zoom", type=float, default=1.0, help="enlarge by this factor, e.g. 2, to see details")
    p.set_defaults(func=cmd_shot)

    p = sub.add_parser("wait", help="wait until the screen stops changing, or until a command in the guest succeeds")
    p.add_argument("--change", action="store_true", help="first wait for the screen to change at all")
    p.add_argument("--ready", action="store_true", help="first wait for SSH and the desktop session (after a reboot)")
    p.add_argument("--until-exec", metavar="CMD",
                   help="instead run CMD in the guest every 2 s until it exits 0, e.g. 'systemctl is-active foo'")
    p.add_argument("--root", action="store_true", help="with --until-exec: as root")
    p.add_argument("--timeout", type=float, default=None,
                   help="seconds per phase (default 15; --until-exec: in all, default 120)")
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser("click", help="click at X Y, an @ref from `desk a11y`, or --name TEXT")
    p.add_argument("target", nargs="*", help="X Y | @eN")
    p.add_argument("--name", help="click the element with this name (substring, case-insensitive)")
    p.add_argument("--role", help="with --name: only this role, e.g. button, 'toggle button', link")
    p.add_argument("--exact", action="store_true", help="with --name: whole name must match")
    p.add_argument("--button", choices=["left", "right", "middle"], default="left")
    p.add_argument("--double", action="store_true")
    _action_opts(p)
    p.set_defaults(func=cmd_click)

    p = sub.add_parser("a11y", help="accessibility tree of the focused window with @refs and click points")
    p.add_argument("--app", help="only this application (substring of its name)")
    p.add_argument("--all", action="store_true", help="every visible window, the shell included")
    p.add_argument("--max", type=int, default=300, help="max elements to list")
    p.add_argument("--offscreen", action="store_true",
                   help="also list elements scrolled out of view or clipped by a scrolling list")
    p.set_defaults(func=cmd_a11y)

    p = sub.add_parser("find", help="find elements by name, value or text; refs are usable right away")
    p.add_argument("query")
    p.add_argument("--role")
    p.add_argument("--exact", action="store_true")
    p.add_argument("--all", action="store_true", help="search every visible window")
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("action", help="invoke an AT-SPI action on an @ref (when clicking is not possible)")
    p.add_argument("ref")
    p.add_argument("action", nargs="?", help="action name (default: the first one)")
    _action_opts(p)
    p.set_defaults(func=cmd_action)

    p = sub.add_parser("set-text", help="replace the text of an editable @ref through AT-SPI")
    p.add_argument("ref")
    p.add_argument("text")
    _action_opts(p)
    p.set_defaults(func=cmd_set_text)

    p = sub.add_parser("move", help="move the pointer")
    p.add_argument("x", type=int)
    p.add_argument("y", type=int)
    _action_opts(p)
    p.set_defaults(func=cmd_move)

    p = sub.add_parser("drag", help="drag with the left button")
    for a in ("x1", "y1", "x2", "y2"):
        p.add_argument(a, type=int)
    _action_opts(p)
    p.set_defaults(func=cmd_drag)

    p = sub.add_parser("scroll", help="scroll the wheel at a point (dy>0 down)")
    p.add_argument("x", type=int)
    p.add_argument("y", type=int)
    p.add_argument("dy", type=int)
    _action_opts(p)
    p.set_defaults(func=cmd_scroll)

    p = sub.add_parser("type", help="type text with real key presses (ASCII, plus Polish letters on the pl layout)")
    p.add_argument("text", help="text to type; one that starts with a dash too ('---')")
    p.add_argument("--anywhere", action="store_true",
                   help="type even when no text field of a web page has the focus (its keyboard shortcuts)")
    _action_opts(p)
    p.set_defaults(func=cmd_type)

    p = sub.add_parser("key", help="press key combos, e.g. super, ctrl+alt+t, enter")
    p.add_argument("combo", nargs="+")
    _action_opts(p)
    p.set_defaults(func=cmd_key)

    p = sub.add_parser("exec", help="run a command in the guest (desk exec [opts] -- cmd ...)")
    p.add_argument("--root", action="store_true", help="as root (the harness has its own root key)")
    p.add_argument("--gui", action="store_true", help="start detached inside the graphical session")
    p.add_argument("--agent", action="store_true", help="via qemu-guest-agent as root, no network needed")
    p.add_argument("--wait", action="store_true",
                   help="with --gui: wait for the program and return its output and exit code")
    p.add_argument("--env", action="append", metavar="NAME=VALUE", help="environment variable (repeatable)")
    p.add_argument("--timeout", type=float, default=None,
                   help=f"seconds before the command is stopped (default {EXEC_TIMEOUT:.0f}, 0: no limit; "
                   "--gui: how long to wait for its window, default 30)")
    p.add_argument("command", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_exec)

    for name, fn in (("push", cmd_push), ("pull", cmd_pull)):
        p = sub.add_parser(name, help=f"copy files {'to' if name == 'push' else 'from'} the guest")
        if name == "push":
            p.add_argument("local")
            p.add_argument("remote")
        else:
            p.add_argument("remote")
            p.add_argument("local")
        p.add_argument("--root", action="store_true", help="as root, for paths the desktop user cannot access")
        p.add_argument("--timeout", type=float, default=guest.COPY_TIMEOUT,
                       help=f"seconds before the copy is stopped (default {guest.COPY_TIMEOUT:.0f}, 0: no limit)")
        p.set_defaults(func=fn)

    sub.add_parser("ssh", help="interactive shell in the guest").set_defaults(func=cmd_ssh)

    sub.add_parser("view", help="how to watch a run live over VNC through an SSH tunnel").set_defaults(func=cmd_view)

    p = sub.add_parser("reboot", help="reboot the guest and wait for the new session")
    p.add_argument("--shot", action="store_true")
    p.add_argument("--timeout", type=float, default=300, help="seconds until the new session must be up")
    p.add_argument("--command", help="reboot with this (as root), e.g. 'dnf offline reboot -y' for an upgrade")
    p.add_argument("--prepare", action="store_true",
                   help="then redo the image's preparation (fedora-44-cockpit: Firefox logged in to Cockpit)")
    p.set_defaults(func=cmd_reboot)

    p = sub.add_parser("selftest", help="end-to-end check of an image and the harness")
    p.add_argument("image")
    p.add_argument("--reboot", action="store_true", help="also reboot the guest and check it comes back")
    p.add_argument("--keep", action="store_true", help="keep the run afterwards")
    p.set_defaults(func=cmd_selftest)

    sub.add_parser("doctor", help="what desk needs on this host, what is missing and how to install it; "
                   "where images and runs are kept").set_defaults(func=cmd_doctor)

    p = sub.add_parser("focus", help="bring a window to the front by application or title")
    p.add_argument("query")
    _action_opts(p)
    p.set_defaults(func=cmd_focus)

    p = sub.add_parser("scenario", help="run a repeatable end-to-end test from a TOML file")
    p.add_argument("action", choices=["run", "check"])
    p.add_argument("file")
    p.add_argument("--keep", action="store_true",
                   help="leave the run running afterwards (report written), to go on testing by hand")
    p.add_argument("--image", help="another image than the file names")
    p.set_defaults(func=cmd_scenario)

    p = sub.add_parser("display", help="show or change the run's monitors: resolution, scaling")
    p.add_argument("--resolution", help="mode for every monitor, e.g. 1920x1080 (one the monitors offer)")
    p.add_argument("--scale", type=int, help="scaling in percent, e.g. 150 (Windows: signs in again)")
    p.set_defaults(func=cmd_display)

    p = sub.add_parser("win", help="Windows settings without the Settings app: theme, taskbar")
    wsub = p.add_subparsers(dest="win_cmd", required=True)
    wp = wsub.add_parser("theme", help="light or dark mode for Windows and apps")
    wp.add_argument("mode", choices=["light", "dark"])
    _action_opts(wp)
    wp = wsub.add_parser("taskbar", help="taskbar edge, size, alignment and auto-hide (restarts Explorer)")
    wp.add_argument("--edge", choices=["bottom", "top", "left", "right"])
    wp.add_argument("--size", choices=["default", "small"])
    wp.add_argument("--align", choices=["left", "center"])
    wp.add_argument("--autohide", choices=["on", "off"])
    _action_opts(wp)
    p.set_defaults(func=cmd_win)

    p = sub.add_parser("disk", help="extra empty disks plugged into the running guest")
    dsub = p.add_subparsers(dest="disk_cmd", required=True)
    dp = dsub.add_parser("add", help="create an empty disk and plug it in")
    dp.add_argument("size", help="e.g. 10G (thin: takes host space only as the guest writes)")
    dp.add_argument("--name", help="a-z, 0-9, - (default disk1, disk2, ...): the guest sees it as the serial")
    dp = dsub.add_parser("rm", help="unplug a disk (the guest must release it) and delete it")
    dp.add_argument("name")
    dsub.add_parser("ls", help="the run's extra disks")
    p.set_defaults(func=cmd_disk)

    p = sub.add_parser("clock", help="show or move the guest's clock (schedules, expiry, retention)")
    p.add_argument("value", nargs="?", help="+3d, -2h30m (s m h d w), '2026-12-24 23:59' (guest local time), "
                   "or reset (back to the host's time, time sync on)")
    p.add_argument("--step", help="get there in steps of this size (1d, 6h), each once what it set off has "
                   "finished, so timers run for every time they were due")
    p.add_argument("--timeout", type=float, default=600, help="seconds to wait for each step to finish")
    p.set_defaults(func=cmd_clock)

    p = sub.add_parser("prepare", help="redo the image's preparation where it is missing (fedora-44-cockpit: "
                       "Firefox logged in to Cockpit with administrative access), e.g. after a reboot")
    p.add_argument("hook", nargs="?", default="prepare",
                   help="another step of the image's prepare script (fedora-44-cockpit: admin)")
    _action_opts(p)
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("browser", help="Firefox in the guest: its web console, opening pages, page size")
    bsub = p.add_subparsers(dest="browser_cmd", required=True)
    bp = bsub.add_parser("console", help="console messages, uncaught exceptions and rejected promises of every "
                         "page since the run started")
    bp.add_argument("--errors", action="store_true", help="only errors (console.error, uncaught exceptions)")
    bp.add_argument("--all", action="store_true", help="also Firefox's own components")
    bp = bsub.add_parser("open", help="open an address in the current tab, or a path (/system/services) on the "
                         "site shown now")
    bp.add_argument("url")
    _action_opts(bp)
    bp = bsub.add_parser("viewport", help="pages at WIDTHxHEIGHT CSS pixels (a phone: 390x700) through Firefox's "
                         "responsive design mode, or reset")
    bp.add_argument("size", metavar="WxH|reset")
    _action_opts(bp)
    p.set_defaults(func=cmd_browser)

    p = sub.add_parser("forward", help="reach a guest port from the host (an SSH tunnel for as long as the run "
                       "exists); without PORT: list")
    p.add_argument("port", nargs="?", metavar="PORT[:LOCAL]")
    p.add_argument("--stop", metavar="PORT|all", help="stop forwarding")
    p.set_defaults(func=cmd_forward)

    p = sub.add_parser("snapshot", help="save the running guest and go back to it later")
    p.add_argument("action", choices=["save", "restore", "list", "delete"])
    p.add_argument("name", nargs="?")
    p.add_argument("--force", action="store_true", help="replace a snapshot of that name")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("record", help="record the screen as a video (WebM), kept with the report")
    p.add_argument("action", choices=["start", "stop"])
    p.add_argument("--fps", type=int, default=record.DEFAULT_FPS)
    p.add_argument("--limit", type=float, default=record.MAX_SECONDS / 60, help="minutes before it stops itself")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("maintain", help="rebuild images that are due, selftest them, switch to the new builds")
    p.add_argument("--only", metavar="IMAGE", help="only the family of this image")
    p.add_argument("--dry-run", action="store_true", help="only say what is due")
    p.add_argument("--install-timer", action="store_true",
                   help="run it weekly (Monday 03:00) from a systemd user timer")
    p.set_defaults(func=cmd_maintain)
    return ap


# Commands whose last positional argument is free text, and their options with a value.
TEXT_COMMANDS = {"type": {"--settle"}, "set-text": {"--settle"}}


def _text_args(argv: list[str]) -> list[str]:
    """`desk type '---'`: text that starts with a dash is text, not an unknown option. Puts `--`
    before the first such argument of `type` and `set-text`."""
    i = 0
    while i < len(argv) and argv[i].startswith("-"):
        i += 2 if argv[i] in ("-r", "--run") else 1
    if i >= len(argv) or argv[i] not in TEXT_COMMANDS or "--" in argv[i + 1:]:
        return argv
    valued = TEXT_COMMANDS[argv[i]]
    j = i + 1
    while j < len(argv):
        a = argv[j]
        if a in valued:
            j += 2
            continue
        if a.startswith("-") and len(a) > 1 and not a.startswith(tuple(f"{v}=" for v in valued)) \
                and a not in ("--shot", "--anywhere", "-h", "--help"):
            return argv[:j] + ["--"] + argv[j:]
        j += 1
    return argv


def explain(e: Exception) -> str | None:
    """One line for a failure of something desk relies on (a host tool, libvirt, the disk); None
    for what desk does not expect, which is a bug and keeps its traceback."""
    import errno

    import libvirt

    if isinstance(e, subprocess.CalledProcessError):
        tool = Path(str(e.cmd[0] if isinstance(e.cmd, (list, tuple)) else e.cmd)).name
        said = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else e.stderr or ""
        return f"{tool} failed (exit {e.returncode})" + (f": {said.strip()[-500:]}" if said.strip() else "")
    if isinstance(e, subprocess.TimeoutExpired):
        tool = Path(str(e.cmd[0] if isinstance(e.cmd, (list, tuple)) else e.cmd)).name
        what = "the guest did not answer over SSH" if tool == "ssh" else f"{tool} did not finish"
        return f"{what} within {e.timeout:.0f}s"
    if isinstance(e, libvirt.libvirtError):
        return f"libvirt: {e}"
    if isinstance(e, FileNotFoundError) and e.filename and "/" not in str(e.filename):
        return f"{e.filename} is not installed on this host (desk doctor lists what is missing)"
    if isinstance(e, OSError) and e.errno == errno.ENOSPC:
        return f"no space left on the disk{f' ({e.filename})' if e.filename else ''}; desk gc frees what is unused"
    if isinstance(e, OSError) and e.strerror:
        return f"{e.strerror}{f': {e.filename}' if e.filename else ''}"
    return None


def main(argv: list[str] | None = None) -> None:
    """Exit codes: 0 done; 1 the check a command stands for failed (a scenario step, selftest,
    find, wait); 2 desk could not do what was asked; 130 interrupted. `desk exec` passes the
    guest command's own code on."""
    argv = _text_args(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except DeskError as e:
        print(f"desk: {e}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:  # noqa: BLE001 - never exit 1, which callers read as a failed test
        message = explain(e)
        if message is None or os.environ.get("DESK_DEBUG"):
            import traceback

            traceback.print_exc()
        print(f"desk: {message or f'internal error ({type(e).__name__}: {e})'}", file=sys.stderr)
        sys.exit(2)
