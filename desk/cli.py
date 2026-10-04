import argparse
import os
import sys
from pathlib import Path

from . import display, guest, images, runs
from .config import DeskError, list_images, load_image


def _emit_shot(run: runs.Run, png: bytes, out: str | None, label: str = "") -> None:
    path = Path(out) if out else run.next_shot_path(label)
    path.write_bytes(png)
    w, h = display.png_size(png)
    print(f"{path} {w}x{h}")


def _after_action(run: runs.Run, dom, args) -> None:
    if getattr(args, "shot", False):
        png, _ = display.wait_stable(dom, timeout=args.settle)
        _emit_shot(run, png, None, args.cmd)


def cmd_image(args) -> None:
    if args.image_cmd == "list":
        for name in list_images():
            spec = load_image(name)
            state = "warm" if (spec.warm_dir / "memory.save").exists() else (
                "base" if spec.base_xml.exists() else "not built")
            print(f"{name}\t{state}")
        return
    spec = load_image(args.name)
    if args.image_cmd == "build":
        images.build(spec, force=args.force)
    elif args.image_cmd == "warm":
        images.warm(spec, force=args.force)


def cmd_up(args) -> None:
    run = runs.up(load_image(args.image), run_id=args.name, cold=args.cold)
    print(f"run {run.id} up (image {run.image}, ssh port {run.ssh_port})")
    print(f"export DESK_RUN={run.id}  # fish: set -x DESK_RUN {run.id}")


def cmd_down(args) -> None:
    targets = runs.all_runs() if args.all else [runs.resolve(args.id)]
    for run in targets:
        runs.down(run, keep=args.keep)
        print(f"run {run.id} down" + (" (files kept)" if args.keep else ""))


def cmd_ls(args) -> None:
    for r in runs.all_runs():
        state = "running" if r.live_domain() is not None else "stopped"
        print(f"{r.id}\t{r.image}\t{state}\tssh:{r.ssh_port}\t{r.dir}")


def cmd_shot(args) -> None:
    run = runs.resolve(args.run)
    dom = run.dom()
    png = display.wait_stable(dom, timeout=args.settle)[0] if args.settle > 0 else display.screenshot(dom)
    _emit_shot(run, png, args.output, args.label or "")


def cmd_wait(args) -> None:
    run = runs.resolve(args.run)
    if args.ready:
        guest.wait_ssh(run.ssh_port, run.user, timeout=args.timeout)
        guest.wait_ready(run.ssh_port, run.user, runs.spec_for(run).ready, timeout=args.timeout)
    dom = run.dom()
    if args.change and not display.wait_change(dom, timeout=args.timeout):
        print("no change")
        sys.exit(1)
    _, stable = display.wait_stable(dom, timeout=args.timeout)
    print("stable" if stable else "still changing")
    if not stable:
        sys.exit(1)


def cmd_click(args) -> None:
    run = runs.resolve(args.run)
    dom = run.dom()
    display.click(dom, args.x, args.y, button=args.button, count=2 if args.double else 1)
    _after_action(run, dom, args)


def cmd_move(args) -> None:
    run = runs.resolve(args.run)
    dom = run.dom()
    display.move(dom, args.x, args.y)
    _after_action(run, dom, args)


def cmd_drag(args) -> None:
    run = runs.resolve(args.run)
    dom = run.dom()
    display.drag(dom, args.x1, args.y1, args.x2, args.y2)
    _after_action(run, dom, args)


def cmd_scroll(args) -> None:
    run = runs.resolve(args.run)
    dom = run.dom()
    display.scroll(dom, args.x, args.y, args.dy)
    _after_action(run, dom, args)


def cmd_type(args) -> None:
    run = runs.resolve(args.run)
    dom = run.dom()
    display.type_text(dom, args.text)
    _after_action(run, dom, args)


def cmd_key(args) -> None:
    run = runs.resolve(args.run)
    dom = run.dom()
    for combo in args.combo:
        display.key(dom, combo)
    _after_action(run, dom, args)


def cmd_exec(args) -> None:
    run = runs.resolve(args.run)
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
        sys.exit(code)
    command = guest.remote_command(argv, root=args.root, gui=args.gui)
    r = guest.ssh_run(run.ssh_port, run.user, command, timeout=args.timeout)
    sys.exit(r.returncode)


def cmd_push(args) -> None:
    run = runs.resolve(args.run)
    sys.exit(guest.scp(run.ssh_port, args.local, f"{run.user}@127.0.0.1:{args.remote}"))


def cmd_pull(args) -> None:
    run = runs.resolve(args.run)
    sys.exit(guest.scp(run.ssh_port, f"{run.user}@127.0.0.1:{args.remote}", args.local))


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
    p.set_defaults(func=cmd_image)

    p = sub.add_parser("up", help="start a disposable run")
    p.add_argument("image")
    p.add_argument("--name", help="run id (default: random)")
    p.add_argument("--cold", action="store_true", help="boot instead of restoring the warm state")
    p.set_defaults(func=cmd_up)

    p = sub.add_parser("down", help="destroy a run and delete its files")
    p.add_argument("id", nargs="?")
    p.add_argument("--all", action="store_true")
    p.add_argument("--keep", action="store_true", help="keep the run directory (disk, shots, logs)")
    p.set_defaults(func=cmd_down)

    sub.add_parser("ls", help="list runs").set_defaults(func=cmd_ls)

    p = sub.add_parser("shot", help="screenshot; prints the PNG path and size")
    p.add_argument("-o", "--output")
    p.add_argument("--label")
    p.add_argument("--settle", type=float, default=3.0, help="wait up to N s for a still screen (0: now)")
    p.set_defaults(func=cmd_shot)

    p = sub.add_parser("wait", help="wait until the screen stops changing")
    p.add_argument("--change", action="store_true", help="first wait for the screen to change at all")
    p.add_argument("--ready", action="store_true", help="first wait for SSH and the desktop session (after a reboot)")
    p.add_argument("--timeout", type=float, default=15.0, help="seconds per phase")
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser("click", help="click at screen pixel coordinates")
    p.add_argument("x", type=int)
    p.add_argument("y", type=int)
    p.add_argument("--button", choices=["left", "right", "middle"], default="left")
    p.add_argument("--double", action="store_true")
    _action_opts(p)
    p.set_defaults(func=cmd_click)

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

    p = sub.add_parser("type", help="type ASCII text on the US layout")
    p.add_argument("text")
    _action_opts(p)
    p.set_defaults(func=cmd_type)

    p = sub.add_parser("key", help="press key combos, e.g. super, ctrl+alt+t, enter")
    p.add_argument("combo", nargs="+")
    _action_opts(p)
    p.set_defaults(func=cmd_key)

    p = sub.add_parser("exec", help="run a command in the guest (desk exec [opts] -- cmd ...)")
    p.add_argument("--root", action="store_true", help="via sudo")
    p.add_argument("--gui", action="store_true", help="start detached inside the graphical session")
    p.add_argument("--agent", action="store_true", help="via qemu-guest-agent as root, no network needed")
    p.add_argument("--timeout", type=float, default=None)
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
        p.set_defaults(func=fn)

    sub.add_parser("ssh", help="interactive shell in the guest").set_defaults(func=cmd_ssh)
    return ap


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except DeskError as e:
        print(f"desk: {e}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)
