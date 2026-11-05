"""Derived images (`desk image save NAME RUN`): the disk of a prepared run kept as an image that runs
start from, cold, with everything installed. A run with the usbvfiod controller cannot be
snapshotted (snapshot._refuse_controller) and always boots cold, so software installed in it would
otherwise be installed again in every run.

The disk is flattened into a build of its own (BASE/NAME.<version>, promoted like any build), so it
never depends on the builds of the image the run came from, which `desk maintain` replaces and
deletes. The definition (images/NAME.toml) names that image only for its settings."""

import os
import re
import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path

from . import config, guest, images, runs
from .config import BASE, WARM, DeskError
from .virt import backing_files, reflink_copy

NAME = re.compile(r"[a-z0-9]([a-z0-9-]{0,46}[a-z0-9])?")
# Flattening ~13 GiB (a Windows guest) takes about a minute; this only ends a copy that hangs.
CONVERT_TIMEOUT = 3600
SYNC_TIMEOUT = 120
SHUTDOWN_TIMEOUT = 600


def _log(msg: str) -> None:
    print(f"[desk] {msg}", flush=True)


def _defined(name: str) -> None:
    f = config._image_file(name)
    if f.exists():
        raise DeskError(f"{name} is defined already ({f}); pick another name, or remove it first with "
                        f"desk image rm {name}")


def check_name(name: str) -> None:
    """Refuse a name before anything is touched: it must be free as a definition and as builds."""
    if not NAME.fullmatch(name):
        raise DeskError(f"image names are lower-case letters, digits and - (at most 48, starting and ending with "
                        f"a letter or digit); got {name!r}")
    _defined(name)
    command = config.add_command(name)
    if command:
        raise DeskError(f"{name} is the name of an image that blocks make ({command}); a derived image needs "
                        "a name of its own")
    left = [p for p in (BASE / name, WARM / name, *images.versions(BASE, name), *images.versions(WARM, name))
            if p.exists() or p.is_symlink()]
    if left:
        raise DeskError(f"builds of an image called {name} are left in {left[0]} (an image removed with its builds "
                        "kept); delete them by hand or pick another name")


def _stop_guest(run: runs.Run, spec: config.ImageSpec) -> None:
    """Shut the guest down through the guest, so that the disk is whole: a copy of a disk that a
    running Windows writes to is not a disk that boots. The guest is not destroyed when it does
    not answer: the run stays as it is."""
    dom = run.live_domain()
    if dom is None:
        return  # off already: nothing has the disk open
    _log(f"shutting down the guest of run {run.id}")
    try:
        r = guest.ssh_run(run.ssh_port, spec.root_user, spec.cmd(spec.sync), capture=True, timeout=SYNC_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise DeskError(f"the guest of run {run.id} did not answer over SSH within {SYNC_TIMEOUT} s, so it cannot be "
                        "shut down cleanly; its disk is not copied while it runs") from None
    guest.connected(r, f"run {run.id} cannot be shut down cleanly")
    images._power_off(dom, spec, run.ssh_port, timeout=SHUTDOWN_TIMEOUT, after="after the shutdown was asked for")


def _source_base(disk: Path, spec: config.ImageSpec) -> Path:
    """The base build the run's disk stands on, the bottom of its backing chain: it holds the domain
    definition the run was started from. The image's current build when the chain cannot be read."""
    chain = backing_files(disk)
    for build in ([chain[-1].parent] if len(chain) > 1 else []) + [spec.base_dir.resolve()]:
        if (build / "domain.xml").is_file():
            return build
    raise DeskError(f"the domain definition the run was started from is gone: {disk} stands on no build with a "
                    f"domain.xml, and {spec.base_dir} has none")


def save(name: str, run: runs.Run) -> Path:
    """Save the disk of `run` as the image `name` and return its build. The run is shut down if it
    runs, and stays (off) for the caller to take down once this has succeeded. The caller holds
    the run (runs.resolve), which keeps gc_idle from taking it down meanwhile."""
    spec = runs.spec_for(run)
    disk, nvram = run.dir / "disk.qcow2", run.dir / "VARS.qcow2"
    for f in (disk, nvram):
        if not f.is_file():
            raise DeskError(f"run {run.id} has no {f.name} ({f}); there is nothing to save")
    began = time.monotonic()
    link = BASE / name
    build = None
    published = False
    # The whole save: the image's builds are not pruned while it is made, and two saves to one name
    # follow each other.
    with runs.image_lock(name):
        check_name(name)
        # Before the guest goes down: what can be refused is refused while the run is as it was.
        source = _source_base(disk, spec)
        _stop_guest(run, spec)
        version = images.new_version()
        derived = replace(spec, name=name, base=None, derived_from=run.image, base_version=version,
                          warm_version=None)
        build = derived.base_dir
        try:
            build.mkdir(parents=True)
            _log(f"flattening the disk of run {run.id} into {derived.base_disk}")
            subprocess.run(["qemu-img", "convert", "-O", "qcow2", str(disk), str(derived.base_disk)],
                           capture_output=True, check=True, timeout=CONVERT_TIMEOUT)
            shutil.copyfile(source / "domain.xml", derived.base_xml)
            reflink_copy(nvram, derived.base_nvram)
            extra = {"derived_from": run.image, "source_run": run.id, "source_base": str(source),
                     "timezone": spec.timezone, "layout": spec.layout}
            # The licence clock of the image it came from goes on running in the copy.
            expires = runs.warm_meta(spec).get("expires")
            if expires is not None:
                extra["expires"] = expires
            images._seal(derived, extra)
            with runs.state_lock():
                _defined(name)
                images.promote(link, build)
                config.write_image(name, {"derived_from": run.image})
                published = True
                config.load_image(name)
        except BaseException:
            if published:
                config._image_file(name).unlink(missing_ok=True)
            if link.is_symlink() and os.readlink(link) == build.name:
                link.unlink()
            shutil.rmtree(build, ignore_errors=True)
            raise
    used = (build / "disk.qcow2").stat().st_blocks * 512
    print(f"saved image {name} from run {run.id} ({run.image}) in {time.monotonic() - began:.0f} s")
    print(f"  disk: {build / 'disk.qcow2'} ({used / 2**30:.1f} GiB, no backing file)")
    print(f"  also: domain.xml of {source}, the run's VARS.qcow2, meta.json")
    print(f"  definition: {config._image_file(name)}; current build: {link}")
    extra_disks = list((run.dir / "disks").glob("*.qcow2")) if (run.dir / "disks").is_dir() else []
    if extra_disks:
        print(f"  not in the image: the run's extra disks ({', '.join(f.stem for f in extra_disks)})")
    return build
