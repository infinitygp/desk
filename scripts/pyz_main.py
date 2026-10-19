"""desk as one file (desk.pyz, a release): unpacks itself once per build into the cache and runs
from there, so that the code, the image definitions and the helpers desk starts in the background
or copies into guests are ordinary files with their modes. Becomes the archive's __main__.py."""

import json
import os
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

# Builds unpacked earlier and not started for this long are removed (their background helpers
# live no longer than a run).
KEEP_DAYS = 7


def unpack(archive: Path, cache: Path) -> Path:
    with zipfile.ZipFile(archive) as z:
        build = json.loads(z.read("BUILD.json"))
        target = cache / build["id"]
        if (target / "BUILD.json").exists():
            os.utime(target)  # in use: not one to prune
            return target
        cache.mkdir(parents=True, exist_ok=True)
        # Unpacked next to its place and moved there whole: another desk may be starting too.
        tmp = Path(tempfile.mkdtemp(prefix=f".{build['id']}.", dir=cache))
        for info in z.infolist():
            path = Path(z.extract(info, tmp))
            mode = info.external_attr >> 16
            if mode and not info.is_dir():
                path.chmod(mode & 0o777)
    try:
        tmp.rename(target)
    except OSError:
        shutil.rmtree(tmp, ignore_errors=True)  # the other one was quicker
    cutoff = time.time() - KEEP_DAYS * 86400
    for old in cache.iterdir():
        try:
            if old != target and old.is_dir() and old.stat().st_mtime < cutoff:
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            continue  # moved or removed meanwhile by another desk that starts
    return target


def main() -> None:
    if sys.version_info < (3, 11):
        sys.exit(f"desk: needs Python 3.11 or newer as /usr/bin/python3 (this is {sys.version.split()[0]})")
    archive = Path(__file__).resolve().parent
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "desk"
    target = unpack(archive, cache)
    # For desk update and the maintenance timer: the file to replace and to run.
    os.environ["DESK_LAUNCHER"] = str(archive)
    sys.path.insert(0, str(target))
    try:
        from desk.cli import main as desk
    except ModuleNotFoundError as e:
        if e.name not in ("libvirt", "libvirt_qemu"):
            raise
        # Not even `desk doctor` runs without them (bin/desk says the same in a checkout).
        print("desk: Python's libvirt bindings are missing for /usr/bin/python3: sudo pacman -S libvirt-python (Arch) "
              "or sudo dnf install python3-libvirt (Fedora); then `desk doctor` lists the rest", file=sys.stderr)
        sys.exit(2)
    desk()


if __name__ == "__main__":
    main()
