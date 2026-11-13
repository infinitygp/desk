import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import fields, replace
from datetime import datetime
from pathlib import Path

from . import a11y, browser, config, display, evidence, guest, images, record, runs
from .config import BASE, WARM, DeskError, list_images, load_image, version


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
            # Once the installer is done, the install scripts run in a domain of their own.
            import libvirt

            from .virt import conn

            # By its version too: fedora-44-gnome- begins the install of fedora-44-gnome-cockpit as well.
            install = re.compile(rf"desk-install-{re.escape(spec.name)}-\d{{8}}-\d{{6}}")
            dom = next((d for d in conn().listAllDomains(libvirt.VIR_CONNECT_LIST_DOMAINS_ACTIVE)
                        if install.fullmatch(d.name())), None)
        if dom is None or not dom.isActive():
            raise DeskError(f"no install of {spec.name} is running")
        out = Path(images.LOGS) / f"{images.build_domain_name(spec)}-{int(time.time())}.png"
        out.write_bytes(display.screenshot(dom))
        print(out)
        return
    if args.image_cmd == "list":
        _image_list()
        return
    if args.image_cmd == "blocks":
        _image_blocks()
        return
    if args.image_cmd == "add":
        names = config.add_image(args.distro, args.desktop, args.addon or [], _image_settings(args))
        for n in names[:-1]:
            print(f"added {n}, whose install {names[-1]} runs on")
        print(f"added {names[-1]}; build, warm and selftest it with: desk maintain --only {names[-1]}")
        return
    if args.image_cmd == "set":
        settings = _image_settings(args)
        if not settings:
            raise DeskError("desk image set takes --memory, --cpus or --disk")
        config.set_image(args.name, settings)
        print(f"{args.name}: {', '.join(f'{k} = {v}' for k, v in settings.items())} "
              "(desk maintain makes the builds it needs)")
        return
    if args.image_cmd == "save":
        from . import derive

        derive.check_name(args.name)
        run = runs.resolve(args.source_run)
        derive.save(args.name, run)
        _down(run)
        return
    if args.image_cmd == "rm":
        variant = config.base_of(args.name) is not None
        derived = config.derived_from(args.name) is not None
        users = [n for n in list_images() if config.derived_from(n) == args.name]
        config.remove_image(args.name)
        # NAME and NAME.VERSION: NAME* is also the builds of the images whose names begin with it.
        roots = (WARM,) if variant else (BASE,) if derived else (BASE, WARM)
        builds = ", ".join(f"{d / args.name}, {d / args.name}.*" for d in roots)
        print(f"removed {args.name}; its builds stay until deleted by hand ({builds})")
        if users:
            # Their disks are whole, but what a run of them is set up with comes from the source's definition.
            command = config.add_command(args.name)
            print(f"{', '.join(users)} derived from {args.name}: a run of {'it' if len(users) == 1 else 'them'} takes "
                  f"its settings from that definition, so define it again before one starts"
                  + (f" ({command})" if command else ""))
        return
    spec = load_image(args.name)
    if args.image_cmd == "show":
        _image_show(spec)
        return
    images.refuse_derived(spec, args.image_cmd)
    # Builds are versioned (images.promote), so runs of the current build may go on meanwhile.
    with runs.image_lock(spec.name):
        if args.image_cmd == "build":
            images.build(spec, force=args.force)
        elif args.image_cmd == "warm":
            images.warm(spec, force=args.force)
    with runs.state_lock():
        runs.prune_versions()


def _image_settings(args) -> dict:
    """What `desk image add/set` sets besides the blocks, as image settings."""
    given = {"memory_mib": args.memory, "vcpus": args.cpus, "disk_gb": args.disk}
    return {k: v for k, v in given.items() if v is not None}


def _shown(value, files: bool = False) -> str:
    """A setting as `desk image show` prints it: files within their root, a script by its first line."""
    if isinstance(value, list):
        return "[" + ", ".join(_shown(v, files) for v in value) + "]"
    if files and isinstance(value, str):
        p = Path(value)
        return str(p.relative_to(config.IMAGES_DIR)) if p.is_relative_to(config.IMAGES_DIR) else value
    if isinstance(value, str) and "\n" in value.strip():
        return config._toml_value(value.strip().partition("\n")[0] + " ...")
    return config._toml_value(value)


def _image_show(spec) -> None:
    """The settings an image's blocks add up to, those that differ from ImageSpec's defaults."""
    if spec.derived_from:
        print(f"# {config._image_file(spec.name)}: derived from {spec.derived_from}, a disk saved from a run "
              "(desk image save); the settings are its")
    else:
        data = config.read_image(spec.name)
        print(f"# {config._image_file(spec.name)}: " + " + ".join(
            [data["distro"], *([data["desktop"]] if data["desktop"] else []), *data["addons"]]))
    for f in fields(spec):
        value = getattr(spec, f.name)
        if f.name not in ("name", "dir", "base_version", "warm_version") and value != f.default:
            print(f"{f.name} = {_shown(value, f.name in config.PATH_KEYS + config.LIST_KEYS)}")


def _image_blocks() -> None:
    for kind in config.KINDS:
        print(kind)
        found, refused = config.scan_blocks(kind)
        for name, d in found.items():
            try:
                data = config._toml(d / "block.toml")
            except DeskError as e:
                print(f"  {name:<16}broken: {e}")
                continue
            also = config.BLOCK_ONLY[kind]
            note = f" ({also}: {', '.join(data[also])})" if also and also in data else ""
            print(f"  {name:<16}{config.describe(d)}{note}")
        for name, why in refused.items():
            print(f"  {name:<16}refused: {why}")


def _image_list() -> None:
    from .maintain import days_left, load_status

    status = load_status()
    # Builds stay when their definition goes (older desks shipped definitions of their own).
    current = {p.name for d in (BASE, WARM) if d.is_dir() for p in d.iterdir() if p.is_symlink()}
    orphans = sorted(current - set(list_images()))
    w = max([22, *(len(n) + 2 for n in [*list_images(), *orphans])])
    print(f"{'IMAGE':<{w}}{'STATE':<8}{'BUILT':<12}{'SELFTEST':<20}NOTES")
    for name in list_images():
        try:
            spec = load_image(name)
        except DeskError as e:
            # One definition its blocks no longer make (a block gone with an update) hides no other.
            print(f"{name:<{w}}{'-':<8}{'-':<12}{'-':<20}broken definition: {e}")
            continue
        notes = [f"variant of {spec.base}"] if spec.base else []
        if spec.derived_from:
            notes.append(_derived_note(spec))
        if (spec.warm_dir / "memory.save").exists():
            state = "warm"
            stale = runs.warm_stale(spec)
            if stale:
                notes.append(f"warm state {stale} (desk image warm --force)")
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
        print(f"{name:<{w}}{state:<8}{built:<12}{test:<20}{'; '.join(notes)}")
    for name in orphans:
        command = config.add_command(name)
        print(f"{name:<{w}}{'-':<8}{'-':<12}{'-':<20}builds without a definition: "
              + (f"use them again after {command}" if command else "delete them by hand"))


def _derived_note(spec) -> str:
    """Where a derived image comes from, as its build says: the run it was saved from."""
    try:
        run = json.loads((spec.base_dir / "meta.json").read_text()).get("source_run")
    except (OSError, ValueError):
        run = None
    return f"derived from {spec.derived_from}" + (f" (run {run})" if run else "")


def _resolution(text: str | None) -> tuple[int, int] | None:
    if not text:
        return None
    try:
        width, height = (int(v) for v in text.lower().split("x"))
    except ValueError:
        raise DeskError(f"--resolution takes WIDTHxHEIGHT, e.g. 1920x1080 (got {text!r})") from None
    return width, height


def cmd_up(args) -> None:
    from . import disks, forward, monitors, usb, usbvfiod

    resolution = _resolution(args.resolution)
    spec = load_image(args.image)
    if args.memory or args.cpus:
        spec = replace(spec, memory_mib=args.memory or spec.memory_mib, vcpus=args.cpus or spec.vcpus)
    # Whatever can be refused without a VM is refused before one is started.
    monitors.check(spec, args.displays, resolution, args.scale)
    extra_disks = [disks.parse(item) for item in args.disk or []]
    if args.usbvfiod:
        usbvfiod.check()
    for query in args.usb or []:
        # With the controller of usbvfiod the guest needs no usb-host: the device node is all it takes.
        usb.openable(query) if args.usbvfiod else usb.usable(query)
    run = runs.up(spec, run_id=args.name, cold=args.cold, force=args.force, label=args.label,
                  displays=args.displays, resolution=resolution, shared_memory=args.shared_memory,
                  usbvfiod=args.usbvfiod)
    try:
        if args.displays != 1 or resolution or args.scale:
            monitors.apply(run, spec, resolution=resolution, scale=args.scale)
        run.log_event(argv=sys.argv[1:])
        for size, name in extra_disks:
            disks.add(run, size, name)
        for query in args.usb or []:
            d = usb.add(run, query)
            print(f"USB device {d['id']} {d['name']} handed to the guest")
        if args.record:
            record.start(run)
        forwards = [(port, forward.start(run, port)) for port in args.forward or []]
    except BaseException:
        # Its id has not been printed yet, so nobody could take a half set up run down.
        print(f"[desk] run {run.id} started, but setting it up failed; taking it down again", file=sys.stderr)
        runs.down(run)
        raise
    print(f"run {run.id} up (image {run.image}, ssh port {run.ssh_port})" + (", recording" if args.record else "")
          + (f", USB controller of usbvfiod on {run.usbvfiod['bus']}" if run.usbvfiod else ""))
    if run.usbvfiod:
        print(f"hotplug socket: {run.usbvfiod['hotplug']} (desk -r {run.id} usb socket)")
    for port, local in forwards:
        print(_forward_line(port, local))
    # Not DESK_RUN: exported, it points every command of the shell at this run, whichever run
    # the agent meant next.
    print(f"next: desk -r {run.id} COMMAND (desk down {run.id} when done)")


def cmd_down(args) -> None:
    if args.id and args.run and args.id != args.run:
        raise DeskError(f"two different runs given: -r {args.run} and {args.id}")
    targets = runs.all_runs() if args.all else [runs.resolve(args.id or args.run)]
    for run in targets:
        _down(run, keep=args.keep, diff=args.diff, report=not args.no_report)


def _down(run: runs.Run, keep: bool = False, diff: bool = False, report: bool = True) -> None:
    gone = _run_files(run)
    # A VM that is gone (QEMU ended) still has its screenshots and timeline to report.
    written, videos = evidence.finish(run, keep=keep, diff=diff, report=report, stopped=not evidence.reported(run))
    if keep or run.dir.exists():
        print(f"run {run.id} down (files kept in {run.dir})")
    else:
        print(f"run {run.id} down; deleted {run.dir}{gone} (--keep keeps it)"
              + ("; screenshots and logs are in the report" if written else ""))
    if written:
        print(f"report: {written / 'report.md'}")
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


def _memory_arg(text: str) -> int:
    m = re.fullmatch(r"([1-9][0-9]*)([MG])", text)
    if not m:
        raise argparse.ArgumentTypeError(f"memory is a number with M or G, e.g. 8G (got {text!r})")
    return int(m[1]) * (1024 if m[2] == "G" else 1)


def _disk_arg(text: str) -> int:
    m = re.fullmatch(r"([1-9][0-9]*)G", text)
    if not m:
        raise argparse.ArgumentTypeError(f"a disk size is a number of GB with G, e.g. 60G (got {text!r})")
    return int(m[1])


def _cpus_arg(text: str) -> int:
    # More vCPUs than the host has threads only makes them wait for each other.
    most = os.cpu_count() or 1
    if not text.isdigit() or not 1 <= int(text) <= most:
        raise argparse.ArgumentTypeError(f"vCPUs from 1 to {most}, the host's (got {text!r})")
    return int(text)


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
    from . import usbvfiod

    for r in runs.all_runs():
        state = "running" if r.live_domain() is not None else "stopped"
        if r.usbvfiod and not usbvfiod.alive(r):
            state += ", usbvfiod ended"
        held = (r.label or "-") + ("" if not r.holder or runs.holder_alive(r) else " (agent gone)")
        print(f"{r.id}\t{r.image}\t{state}\tidle {r.idle() / 60:.0f} min\t{held}\tssh:{r.ssh_port}\t{r.dir}")


def cmd_gc(args) -> None:
    removed = runs.gc_idle()
    with runs.state_lock():
        removed += runs.gc() + runs.prune_versions()
    if not removed:
        print(f"nothing to clean (idle limit {runs.idle_ttl() / 3600:g} h, DESK_TTL to change)")


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
    start, out, r = time.monotonic(), b"", None
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
            r, out = None, b"(the command itself did not finish)"
        if time.monotonic() + 2 > deadline:
            break
        time.sleep(2)
    if r is not None:
        user = spec.root_user if args.root else run.user
        guest.lost(r, run.ssh_port, user, f"still no answer after {timeout:.0f}s")  # not a command that kept failing
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
    print(f"desk: still running after {guest._seconds(limit)}s; stopped it "
          "(--timeout SECONDS to allow longer, 0: no limit)", file=sys.stderr)
    sys.exit(124)


def _exit_status(code: int) -> int:
    """A guest's exit code as an exit status, which holds 0-255: Windows' -196608 would end as its
    last byte, 0, and read as success. The guest's own wrapper does the same over SSH."""
    if 0 <= code <= 255:
        return code
    print(f"desk: exit code {code} (0x{code & 0xFFFFFFFF:08X}) does not fit an exit status: exit 1", file=sys.stderr)
    return 1


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
        sys.exit(_exit_status(code))
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
            guest.lost(r, run.ssh_port, run.user, f"exec {argv[0][:60]!r}")
            code = r.returncode
        run.log_event(rc=code)
        sys.exit(_exit_status(code))
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
            guest.lost(r, run.ssh_port, run.user, f"exec {argv[0][:60]!r}")
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
    # ssh's own 255 is no answer from the guest, not the command's code.
    guest.lost(r, run.ssh_port, spec.root_user if args.root else run.user, f"exec {argv[0][:60]!r}")
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
    from .observe.session import Store

    if any(s.state == "running" for s in Store(run).all()):
        from . import observe

        for s in observe.listing(run):
            if s.state == "interrupted" and s.error.startswith("the guest rebooted"):
                print(f"note: observation {s.name()} ended with the reboot; desk observe stop {s.id} collects what it "
                      "left")
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
        print(f"prepared again ({images._named(spec.path(spec.prepare))})")
    elif spec.prepare:
        print(f"note: programs open before the reboot are gone; `desk prepare` sets up again what {spec.name} "
              "prepares (fedora-44-gnome-cockpit: Firefox logged in to Cockpit), or reboot with --prepare")
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
        build = scenario.for_image(data, image)["build"]
        print(f"{path}: image {image}, {len(data.get('step', []))} steps"
              + (f"; build: {build['run']}" if build.get("run") else ""))
        for i, step in enumerate(data.get("step", []), 1):
            print(f"  {i:>2}. {scenario.step_label(step)}"
                  + (f"  [only {', '.join(step['only'])}]" if "only" in step else ""))
        scenario.check_observations(data, load_image(image))
        if not args.image:
            for other in data.get("images", []):
                try:
                    scenario.check_observations(data, load_image(other))
                except DeskError as e:
                    # An image this host has not defined is not an error here; it is not checked either.
                    print(f"  also for {other}: NOT CHECKED here ({e})")
                    continue
                skipped = sum(not scenario.is_for(st, other) for st in data.get("step", []))
                print(f"  also for {other}" + (f", {skipped} step{'s' * (skipped > 1)} skipped" if skipped else ""))
        if scenario.browser_machine(data):
            print(f"  browser machine: {data['browser']} (the steps that are not commands go to it)")
        for label, t in scenario.observations(path, data).items():
            print(f"  observe {label}: {t['provider']}, "
                  + ("before the first step" if t.get("start", "before") == "before" else "at its observe_start step"))
        for w in scenario.fixed_waits(data):
            print(f"warning: {w}")
        return
    if args.repeat is not None:
        if args.repeat < 2:
            raise DeskError("--repeat takes a number of runs, 2 or more")
        for flag, given, why in (
                ("--keep", args.keep, "the runs would all be left running"),
                ("--all", args.all, "it runs the file's images, --repeat one image; use one of them"),
                ("--from", args.from_step is not None, "it goes on in one existing run; --repeat starts new ones")):
            if given:
                raise DeskError(f"--repeat takes no {flag}: {why}")
        sys.exit(scenario.run_repeat(path, args.repeat, image=args.image, no_build=args.no_build))
    if args.all:
        if args.image or args.from_step is not None:
            raise DeskError("--all runs the file's image and every one of its images; it takes no --image or --from")
        sys.exit(scenario.run_all(path, keep=args.keep, no_build=args.no_build))
    if args.from_step is not None:
        run = runs.resolve(args.run)
        sys.exit(0 if scenario.continue_scenario(path, run, args.from_step, image=args.image) else 1)
    sys.exit(0 if scenario.run_scenario(path, keep=args.keep, image=args.image, no_build=args.no_build) else 1)


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


def _mib(n) -> str:
    return f"{(n or 0) / 2**20:.1f} MiB"


def _since(run: runs.Run, t: float | None) -> str:
    if not t:
        return "-"
    offset = t - run.created
    return f"+{int(offset // 60):02d}:{int(offset % 60):02d}"


def _observe_target(args) -> dict | None:
    if args.process and args.pid:
        raise DeskError("give --process or --pid, not both")
    return {"image": args.process} if args.process else {"pid": args.pid} if args.pid else None


def _observe_options(items: list[str] | None) -> dict:
    out = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise DeskError(f"--set takes KEY=VALUE (got {item!r})")
        out[key.strip()] = value
    return out


def _print_session(run: runs.Run, s, live: dict | None = None) -> None:
    from . import observe

    p = observe.PROVIDERS.get(s.provider)
    print(f"session {s.name()}: {s.provider} ({p.title if p else '?'}), {s.state}"
          + (", complete" if s.complete else ", NOT complete" if s.complete is False else ""))
    print(f"  started {_since(run, s.started or s.created)}, stopped {_since(run, s.stopped)}"
          + (f"; limits: {s.limits.get('duration', 0):.0f} s" if s.limits.get("duration") else "")
          + (f", {_mib(s.limits['max_bytes'])}" if s.limits.get("max_bytes") else ""))
    if s.target:
        print(f"  following {s.target.get('image') or 'pid ' + str(s.target.get('pid'))}: "
              + (", ".join(f"{x['name']} pid {x['pid']}" for x in list(s.processes.values())[:10]) or "not seen yet"))
    for k, v in (live or {}).items():
        print(f"  {k}: {_mib(v) if k == 'bytes' else v}")
    shown = ("ended", "interrupted", "snapshot restored", "stop failed", "watchdog gone")
    for e in [e for e in s.events if e["event"] in shown][-3:]:
        print(f"  {e['event']}: {e.get('detail', '')}")
    if s.stats.get("endpoint"):
        print(f"  endpoint: {s.stats['endpoint']} on the host ({p.connect_hint(s) if hasattr(p, 'connect_hint') else ''})")
    for a in s.artifacts:
        state = "ok" if a.get("verified") else "NOT VERIFIED" if a.get("transferred") else "NOT COPIED"
        size = _mib(a.get("bytes") or a.get("guest_bytes"))
        print(f"  {a['name']}: {state}, {size}" + (f", sha256 {a['sha256'][:16]}" if a.get("sha256") else "")
              + (f": {a.get('detail')}" if a.get("detail") else "") + (f"\n    {a['path']}" if a.get("path") else ""))
    if p and s.state == "stopped":
        for line in p.summary(s):
            print(f"  {line}")
    if s.error and s.state in ("failed", "interrupted", "stopped"):
        print(f"  problem: {s.error}")


def cmd_observe(args) -> None:
    from . import observe

    if args.observe_cmd == "setup":
        info = observe.setup(args.provider, Path(args.file), accept_licence=args.accept_eula,
                             who=runs.default_label()[0])
        print(f"{args.provider}: set up {', '.join(f['name'] for f in info['files'])} in "
              f"{observe.Host.setup_dir(args.provider)}" + ("; licence terms accepted" if info["licence_accepted"] else ""))
        p = observe.provider(args.provider)
        if p.licence and not info["licence_accepted"]:
            print(f"note: the {p.licence} are not accepted, so desk starts it only where the guest has accepted them "
                  "(read them, then set up again with --accept-eula)")
        return
    if args.observe_cmd == "providers":
        run = None
        if not args.offline:
            try:
                run = runs.resolve(args.run)
            except DeskError as e:
                if args.run or os.environ.get("DESK_RUN"):
                    raise
                print(f"(no guest asked: {e})")
        for p, cap in observe.capabilities(run, args.provider or None):
            if isinstance(cap, str):
                state = cap
            elif cap.available:
                state = "available" + (f" ({cap.version})" if cap.version else "")
            else:
                state = "NOT available: " + "; ".join(cap.problems)
            print(f"{p.name:<9}{p.title}: {state}")
            if args.verbose:
                for k, o in p.OPTIONS.items():
                    print(f"    --set {k}=  {o.help} (default {o.default!r})")
                for n in p.notes:
                    print(f"    note: {n}")
                if not isinstance(cap, str):
                    for k, v in cap.details.items():
                        print(f"    {k}: {v}")
        return
    run = runs.resolve(args.run)
    if args.observe_cmd == "start":
        s = observe.start(run, args.provider, label=args.label, options=_observe_options(args.set),
                          duration=args.duration, max_mb=args.max_mb, target=_observe_target(args))
        limits = []
        if s.limits.get("duration"):
            limits.append(f"{s.limits['duration'] / 60:g} min")
        if s.limits.get("max_bytes"):
            limits.append(_mib(s.limits["max_bytes"]))
        print(f"observing: session {s.name()} ({s.provider})" + (f"; it stops itself after {' or '.join(limits)}"
                                                                 if limits else "")
              + f"; desk observe stop {s.id}")
        if s.stats.get("endpoint"):
            p = observe.provider(s.provider)
            print(f"endpoint: {s.stats['endpoint']} on the host: {p.connect_hint(s)}")
        if s.tool.get("password_file") and not s.options.get("password"):
            print(f"password: {Path(s.tool['password_file']).read_text().strip()}")
        return
    if args.observe_cmd == "status":
        s, live = observe.status(run, args.session)
        _print_session(run, s, live)
        return
    if args.observe_cmd == "list":
        sessions = observe.listing(run)
        for s in sessions:
            print(f"{s.id}\t{s.label or '-'}\t{s.provider}\t{s.state}"
                  + ("\tcomplete" if s.complete else "\tNOT complete" if s.complete is False else "\t-")
                  + f"\t{_since(run, s.started or s.created)}\t{len(s.artifacts)} artifact(s)")
        if not sessions:
            print(f"no observation sessions in run {run.id} (desk observe start PROVIDER)")
        return
    if args.observe_cmd == "mark":
        m = observe.mark(run, args.text, ref=args.session)
        print(f"mark {m['seq']} in {', '.join(m['sessions'])}: guest time {m['guest']:.3f}, host time "
              f"{m['host']:.3f} (clocks {m['offset']:+.3f} s apart, ±{m['uncertainty']:.3f} s)"
              + ("" if m.get("event") else f"; no ETW marker event ({m.get('event_error') or 'not emitted'})"))
        for problem in m.get("problems") or []:
            print(f"warning: {problem}", file=sys.stderr)
        if args.shot:
            png, _ = display.wait_stable(run.dom(), timeout=3)
            _emit_shot(run, png, None, f"mark-{m['seq']}")
        return
    if args.observe_cmd == "pipe":
        from .observe import ipc

        reply = ipc.probe_pipe(observe.guest_for(run), args.name, send=(args.send or "").encode(), read=args.read,
                               timeout=args.timeout)
        if not reply.get("connected"):
            raise DeskError(f"could not connect to pipe {args.name}: {reply.get('error') or 'no answer'}")
        print(f"connected to \\\\.\\pipe\\{args.name}; server pid {reply.get('server_pid', 'unknown')}")
        if reply.get("sent"):
            print(f"sent {reply['sent']} bytes")
        if "received" in reply:
            print(f"received {len(reply['received'])} bytes: {reply['received']!r}")
        elif reply.get("read_timed_out"):
            print(f"nothing received within {args.timeout:g} s")
        if reply.get("error"):
            raise DeskError(f"pipe {args.name}: {reply['error']}")
        return
    # stop
    sessions = ([s.id for s in observe.listing(run) if s.state in observe.COLLECTABLE] if args.all
                else [args.session])
    if args.all and not sessions:
        print("nothing to stop")
        return
    failed = []
    for ref, done in zip(sessions, observe.stop_many(run, sessions, force=args.force, keep_guest=args.keep_guest,
                                                     reason="desk observe stop")):
        if isinstance(done, BaseException):
            if len(sessions) == 1:
                raise done
            print(f"session {ref}: not stopped: {done}")
            failed.append(f"{ref}: not stopped ({done})")
            continue
        s, problems = done
        _print_session(run, s)
        if problems:
            failed.append(f"{s.name()}: " + "; ".join(problems))
    if failed:
        raise DeskError("observation not complete: " + " | ".join(failed))


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
    print(winsettings.apply(run, runs.spec_for(run), args))
    _after_action(run, run.dom(), args)


def cmd_offline(args) -> None:
    from . import offline

    run = runs.resolve(args.run)
    spec = runs.spec_for(run)
    if args.state == "on":
        print(offline.on(run, spec, args.ports))
    elif args.ports:
        raise DeskError("--ports goes with desk offline on")
    elif args.state == "off":
        print(offline.off(run, spec))
    else:
        print("offline: desk's firewall rules are in the guest" if offline.state(run, spec) else "online")


def _ports(text: str) -> list[int]:
    ports = [int(p) for p in text.split(",") if p.strip()]
    if not ports or not all(0 < p < 65536 for p in ports):
        raise argparse.ArgumentTypeError(f"ports are numbers 1-65535, comma-separated (got {text!r})")
    return ports


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


def cmd_usb(args) -> None:
    from . import usb, usbvfiod

    if args.usb_cmd == "ls":
        for d in usb.listing():
            if d.get("unread"):
                print(f"-\t-\t-\t-\t{d['unread']}")
                continue
            if d.get("gone"):
                print(f"-\t{d['bus']}.{d['dev']}\t-\t-\tgone, still in run {d['run']} "
                      f"(desk -r {d['run']} usb rm {d['bus']}.{d['dev']}, or the next usb add takes it out)")
                continue
            if d.get("tree"):
                print(f"{d['id']}\t-\tat:{d['at']}\t{d['name']}\tin run {d['run']} at {d['at']}")
                continue
            where = (f"in run {d['run']}" + (f" at {d['at']}" if d.get("at") else "") if d["run"] else
                     "free" if d["access"] else "free, but not yours to open (desk usb add says how)")
            print(f"{d['id']}\t{d['bus']}.{d['dev']}\tport:{d['port']}\t{d['name'] or '-'}\t{where}")
        return
    run = runs.resolve(args.run)
    if args.usb_cmd == "socket":
        print(usbvfiod.hotplug_socket(run))
    elif args.usb_cmd == "add":
        d = usb.add(run, args.device, at=args.at, hub_options=args.hub)
        made = f", with the hubs {', '.join(d['hubs'])}" if d.get("hubs") else ""
        print(f"USB device {d['id']} {d['name']} handed to the guest"
              + (f" at port path {d['at']}{made}" if d.get("at") else "")
              + " (the host does without it until desk usb rm or desk down)")
    else:
        d = usb.remove(run, args.device)
        if d.get("tree"):
            below = f", with {', '.join(d['below'])} below it" if d.get("below") else ""
            print(f"{d['id']} removed ({d['name']}{below})")
        else:
            print(f"USB device {d['id']} back with the host"
                  + (f" (it was at port path {d['at']})" if d.get("at") else ""))


def cmd_clock(args) -> None:
    from . import clock

    run = runs.resolve(args.run)
    if args.step and (not args.value or args.value == "reset"):
        raise DeskError("--step goes with a time to move to, e.g. desk clock +3d --step 1d")
    if args.value == "reset":
        clock.reset(run)
    elif args.step:
        tz = clock.zone(run)
        for t, started in clock.advance(run, args.value, args.step, timeout=args.timeout):
            stamp = datetime.fromtimestamp(t, tz).strftime("%Y-%m-%d %H:%M")
            print(f"{stamp}: " + (", ".join(started) if started else "nothing started"), flush=True)
    elif args.value:
        clock.set_time(run, args.value)
    print(clock.describe(run))


def cmd_browser(args) -> None:
    run = runs.resolve(args.run)
    if args.browser_cmd == "open":
        try:
            shown, asked = browser.open_url(run, args.url, browser=args.browser)
            print(f"showing {shown}" + ("" if browser.under(shown, asked) else f" (opened {asked}: the site sent the "
                                        "browser there, or back to where it was)"))
        except browser.NotLoaded as e:
            # What the page did, not what desk could not do: exit 1, with the screenshot asked for.
            print(f"desk: {e}", file=sys.stderr)
            _after_action(run, run.dom(), args)
            run.log_event(rc=1)
            sys.exit(1)
        _after_action(run, run.dom(), args)
        return
    if args.browser_cmd == "viewport":
        print(browser.viewport(run, args.size, browser=args.browser))
        _after_action(run, run.dom(), args)
        return
    lines = browser.console(run, everything=args.all)
    unknown = browser.unlevelled(lines) if args.errors else []
    if args.errors:
        lines = browser.errors(lines)
    print("\n".join(lines) if lines else "(no errors in the web console since the run started)" if args.errors
          else "(nothing in the web console since the run started)")
    if unknown:
        print(browser.unlevelled_note(len(unknown)) + " (desk browser console lists them)")


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


def cmd_install(args) -> None:
    from . import install

    for line in install.install():
        print(line)
    if not args.quiet:
        print()
        cmd_doctor(args)


def cmd_update(args) -> None:
    from . import install

    print(install.update())


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
    run.log_event(rc=rc if rc in (0, 124) else 2)  # what desk exits with, for the report's timeline
    if rc == 124:
        print(f"desk: still copying after {args.timeout:.0f}s; stopped it (--timeout SECONDS to allow longer, "
              "0: no limit)", file=sys.stderr)
        sys.exit(rc)
    if rc != 0:
        # scp's own 1 would read as a failed check: copying is desk's work. What scp says above
        # names the cause; the folder missing is the usual one, and --root does nothing on Windows.
        where = "its folder may not exist"
        if not args.root and runs.spec_for(run).family != "windows":
            where += f", or {args.remote} be outside what {run.user} can access: --root"
        raise DeskError(f"copying failed (scp exit {rc}); {where}")


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
    ap.add_argument("--version", action="version", version=f"desk {version()}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("image", help="golden images")
    isub = p.add_subparsers(dest="image_cmd", required=True)
    isub.add_parser("list")
    isub.add_parser("blocks", help="the distros, desktops and addons images are made of")
    ip = isub.add_parser("add", help="define an image of this host: a distro, its desktop, addons")
    ip.add_argument("distro")
    ip.add_argument("desktop", nargs="?")
    ip.add_argument("--addon", action="append", metavar="NAME", help="repeatable, in the order they apply")
    image_sizes = [ip]
    ip = isub.add_parser("set", help="change an image's sizes")
    ip.add_argument("name")
    image_sizes.append(ip)
    for ip in image_sizes:
        ip.add_argument("--memory", type=_memory_arg, metavar="SIZE", help="e.g. 8G (a new warm state)")
        ip.add_argument("--cpus", type=_cpus_arg, metavar="N", help="vCPUs (a new warm state)")
        ip.add_argument("--disk", type=_disk_arg, metavar="SIZE", help="e.g. 60G (a new install)")
    for name, hlp in (("rm", "remove an image's definition (its builds stay)"),
                      ("show", "the settings an image's blocks add up to")):
        isub.add_parser(name, help=hlp).add_argument("name")
    for name, hlp in (("build", "unattended install of the base image"),
                      ("warm", "save a logged-in desktop for instant runs")):
        ip = isub.add_parser(name, help=hlp)
        ip.add_argument("name")
        ip.add_argument("--force", action="store_true")
    ip = isub.add_parser("save", help="keep the disk of a prepared run as a new image (shuts the guest down, "
                                      "takes the run down)")
    ip.add_argument("name", help="the new image: lower-case letters, digits and -")
    ip.add_argument("source_run", metavar="RUN")
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
    p.add_argument("--memory", type=_memory_arg, metavar="SIZE",
                   help="memory of this run, e.g. 8G (default: the image's; other than its warm state's: boots cold)")
    p.add_argument("--cpus", type=_cpus_arg, metavar="N",
                   help="vCPUs of this run (default: the image's; other than its warm state's: boots cold)")
    p.add_argument("--disk", action="append", metavar="SIZE[:NAME]", help="extra empty disk, e.g. 10G:backup "
                   "(repeatable; desk disk)")
    p.add_argument("--usb", action="append", metavar="DEVICE",
                   help="hand a host USB device to the guest: VENDOR:PRODUCT, BUS.DEVICE, port:BUS-PORT or "
                        "serial:SERIAL (repeatable; desk usb)")
    p.add_argument("--forward", action="append", type=_port_arg, metavar="PORT",
                   help="reach this guest port from the host (repeatable; desk forward)")
    p.add_argument("--shared-memory", action="store_true",
                   help="keep the guest's RAM in shared memory that host processes may map, as a vfio-user device "
                        "needs (boots cold)")
    p.add_argument("--usbvfiod", action="store_true",
                   help="give the guest a second USB controller, served by usbvfiod, where hubs and devices stand "
                        "at port paths (desk usb add --at; implies --shared-memory, so it boots cold)")
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
                   help="then redo the image's preparation (fedora-44-gnome-cockpit: Firefox logged in to Cockpit)")
    p.set_defaults(func=cmd_reboot)

    p = sub.add_parser("selftest", help="end-to-end check of an image and the harness")
    p.add_argument("image")
    p.add_argument("--reboot", action="store_true", help="also reboot the guest and check it comes back")
    p.add_argument("--keep", action="store_true", help="keep the run afterwards")
    p.set_defaults(func=cmd_selftest)

    sub.add_parser("doctor", help="what desk needs on this host, what is missing and how to install it; "
                   "where images and runs are kept").set_defaults(func=cmd_doctor)

    p = sub.add_parser("install", help="put desk on PATH (~/.local/bin) and its skill where coding agents look "
                       "(~/.agents/skills, ~/.claude/skills), then check the host")
    p.add_argument("--quiet", action="store_true", help="without the host check")
    p.set_defaults(func=cmd_install)
    sub.add_parser("update", help="replace this release of desk with the newest one (gh), skill included"
                   ).set_defaults(func=cmd_update)

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
    p.add_argument("--all", action="store_true",
                   help="run on the file's image and then on each of its images, one after the other")
    p.add_argument("--no-build", action="store_true",
                   help="skip [build] run and take the newest files matching artifacts as they are")
    p.add_argument("--repeat", type=int, metavar="N",
                   help="run the scenario N times (2 or more), each in a new run ([build] runs once), and "
                        "say which steps passed in one run and failed in another")
    p.add_argument("--from", dest="from_step", type=int, metavar="N",
                   help="run steps N.. in the run already there (-r / $DESK_RUN, e.g. left by --keep): "
                        "no build, no new run; it stays running")
    p.set_defaults(func=cmd_scenario)

    p = sub.add_parser("display", help="show or change the run's monitors: resolution, scaling")
    p.add_argument("--resolution", help="mode for every monitor, e.g. 1920x1080 (one the monitors offer)")
    p.add_argument("--scale", type=int, help="scaling in percent, e.g. 150 (Windows: signs in again)")
    p.set_defaults(func=cmd_display)

    from . import winsettings

    p = sub.add_parser("win", help="Windows settings without the Settings app: theme, taskbar, transparency, "
                                   "contrast, text size, wallpaper")
    winsettings.add_commands(p.add_subparsers(dest="win_cmd", required=True), _action_opts)
    p.set_defaults(func=cmd_win)

    p = sub.add_parser("offline", help="refuse the guest's outgoing connections but DNS, or only those to --ports, "
                                       "with its own firewall; desk's SSH and agent keep working")
    p.add_argument("state", choices=["on", "off", "status"])
    p.add_argument("--ports", type=_ports, help="only these, e.g. 443 or 80,443")
    p.set_defaults(func=cmd_offline)

    p = sub.add_parser("disk", help="extra empty disks plugged into the running guest")
    dsub = p.add_subparsers(dest="disk_cmd", required=True)
    dp = dsub.add_parser("add", help="create an empty disk and plug it in")
    dp.add_argument("size", help="e.g. 10G (thin: takes host space only as the guest writes)")
    dp.add_argument("--name", help="a-z, 0-9, - (default disk1, disk2, ...): the guest sees it as the serial")
    dp = dsub.add_parser("rm", help="unplug a disk (the guest must release it) and delete it")
    dp.add_argument("name")
    dsub.add_parser("ls", help="the run's extra disks")
    p.set_defaults(func=cmd_disk)

    p = sub.add_parser("usb", help="hand host USB devices to the running guest")
    usub = p.add_subparsers(dest="usb_cmd", required=True)
    up_ = usub.add_parser("add", help="plug a host USB device into the guest (the host does without it)")
    up_.add_argument("device", help="VENDOR:PRODUCT (1209:0001), BUS.DEVICE (3.7), port:BUS-PORT (3-1.2) or "
                                      "serial:SERIAL, as desk usb ls shows")
    up_.add_argument("--at", metavar="PORTPATH",
                     help="on a run started with --usbvfiod: plug it in at this port path of the controller, dotted "
                          "from the root port (5.1.4: port 4 of the hub on port 1 of the hub on root port 5), with "
                          "hubs where none is; without it the device takes the first free root port that fits")
    up_.add_argument("--hub", action="append", metavar="PORTPATH=[VENDOR:PRODUCT][/PORTS][/hs|/fs]",
                     help="the hub at a port path on the way, e.g. 5=067b:2586/4/hs (default 0409:55aa, 8 ports, "
                          "high speed; one that exists keeps what it is; repeatable)")
    up_ = usub.add_parser("rm", help="give a USB device back to the host")
    up_.add_argument("device", help="as for add, or at:PORTPATH for what is at a port path of the usbvfiod "
                                      "controller (a hub, and everything below it, too)")
    usub.add_parser("ls", help="host USB devices and the runs they are in, and the hubs and devices of the "
                    "usbvfiod controllers")
    usub.add_parser("socket", help="the hotplug socket of the run's usbvfiod, for usbvfiod's remote and for "
                    "a process that serves emulated devices on it")
    p.set_defaults(func=cmd_usb)

    p = sub.add_parser("clock", help="show or move the guest's clock (schedules, expiry, retention)")
    p.add_argument("value", nargs="?", help="+3d, -2h30m (s m h d w), '2026-12-24 23:59' (guest local time), "
                   "or reset (back to the host's time, time sync on)")
    p.add_argument("--step", help="get there in steps of this size (1d, 6h), each once what it set off has "
                   "finished, so timers run for every time they were due")
    p.add_argument("--timeout", type=float, default=600, help="seconds to wait for each step to finish")
    p.set_defaults(func=cmd_clock)

    p = sub.add_parser("prepare", help="redo the image's preparation where it is missing (fedora-44-gnome-cockpit: "
                       "Firefox logged in to Cockpit with administrative access), e.g. after a reboot")
    p.add_argument("hook", nargs="?", default="prepare",
                   help="another step of the image's prepare script (fedora-44-gnome-cockpit: admin)")
    _action_opts(p)
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("browser", help="Firefox and Chromium in the guest: their web console, opening pages, page "
                       "size")
    bsub = p.add_subparsers(dest="browser_cmd", required=True)
    bp = bsub.add_parser("console", help="console messages, uncaught exceptions and rejected promises of every "
                         "page since the run started")
    bp.add_argument("--errors", action="store_true", help="only errors (console.error, uncaught exceptions)")
    bp.add_argument("--all", action="store_true", help="also the browsers' own components and pages")
    bp = bsub.add_parser("open", help="open an address in the current tab, or a path (/system/services) on the "
                         "site shown now")
    bp.add_argument("url")
    bp.add_argument("--browser", choices=sorted(browser.NAMES), help="which browser (default: the one in the "
                    "focused window, else Firefox)")
    _action_opts(bp)
    bp = bsub.add_parser("viewport", help="pages at WIDTHxHEIGHT CSS pixels (a phone: 390x700) through Firefox's "
                         "responsive design mode or Chromium's device mode, or reset")
    bp.add_argument("size", metavar="WxH|reset")
    bp.add_argument("--browser", choices=sorted(browser.NAMES), help="which browser (default: the one in the "
                    "focused window, else Firefox)")
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

    p = sub.add_parser("observe", help="capture what happens in a Windows guest with its own tools (Process Monitor, "
                       "ETW, packet and USB captures, IPC endpoints, debugger servers)")
    osub = p.add_subparsers(dest="observe_cmd", required=True)
    op = osub.add_parser("providers", help="the providers and whether the run's guest can use them")
    op.add_argument("provider", nargs="*")
    op.add_argument("-v", "--verbose", action="store_true", help="options, notes and what the guest said")
    op.add_argument("--offline", action="store_true", help="without asking a guest")
    op = osub.add_parser("setup", help="keep the host's copy of a tool desk does not ship (ProcMon, IDA's server)")
    op.add_argument("provider")
    op.add_argument("file", help="the tool, its zip or its folder")
    op.add_argument("--accept-eula", action="store_true",
                    help="you have read and accept the tool's licence terms; desk passes that on (ProcMon /AcceptEula)")
    op = osub.add_parser("start", help="start a capture in the run's guest")
    op.add_argument("provider")
    op.add_argument("--label", help="a name for the session (letters, digits, - _ .)")
    op.add_argument("--duration", type=float, help="seconds after which it stops itself (default 1800)")
    op.add_argument("--max-mb", type=float, help="MiB its files may grow to (default 4096)")
    op.add_argument("--process", metavar="IMAGE", help="follow processes of this image (app.exe) and their children")
    op.add_argument("--pid", type=int, help="follow this process and its children")
    op.add_argument("--set", action="append", metavar="KEY=VALUE", help="a provider option (desk observe providers -v)")
    op = osub.add_parser("status", help="a session and how its capture is doing")
    op.add_argument("session", nargs="?")
    osub.add_parser("list", help="the run's sessions")
    op = osub.add_parser("stop", help="stop a capture, copy its files to the host and check them")
    op.add_argument("session", nargs="?")
    op.add_argument("--all", action="store_true", help="every open session of the run")
    op.add_argument("--force", action="store_true", help="end desk's own tool by its PID where its stop command would "
                    "end another's too")
    op.add_argument("--keep-guest", action="store_true", help="leave the files in the guest as well")
    op = osub.add_parser("mark", help="mark this moment in the running captures and the timeline")
    op.add_argument("text")
    op.add_argument("--session")
    op.add_argument("--shot", action="store_true", help="also take a screenshot")
    op = osub.add_parser("pipe", help="connect to a named pipe of the guest: its server's pid, an answer to what is sent")
    op.add_argument("name", help="the pipe's name without \\\\.\\pipe\\")
    op.add_argument("--send", help="text to write after connecting")
    op.add_argument("--read", type=int, default=0, help="bytes to read back")
    op.add_argument("--timeout", type=float, default=5.0)
    p.set_defaults(func=cmd_observe)

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
        try:
            args.func(args)
        finally:
            sys.stdout.flush()  # here, where a reader that has left can still be handled
    except DeskError as e:
        print(f"desk: {e}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:
        # Whoever read the output has left (desk ... | head): nothing more to say, and nothing
        # left to flush into the closed pipe when Python exits.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(141)
    except Exception as e:  # noqa: BLE001 - never exit 1, which callers read as a failed test
        message = explain(e)
        if message is None or os.environ.get("DESK_DEBUG"):
            import traceback

            traceback.print_exc()
        print(f"desk: {message or f'internal error ({type(e).__name__}: {e})'}", file=sys.stderr)
        sys.exit(2)
