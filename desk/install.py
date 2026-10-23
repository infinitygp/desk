"""Putting desk where agents find it, and keeping a release current. A host gets desk either as
a checkout of its repository (bin/desk) or as a release, the single file desk.pyz; `desk install`
links the command into ~/.local/bin and puts the skill where coding agents look for skills,
`desk update` replaces a release with the newest one."""

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from .config import BUILD, LAUNCHER, PROJECT, DeskError, version

BIN = Path.home() / ".local" / "bin" / "desk"
# Coding agents' skill directories: the shared one holds the skill, the others link to it.
SKILLS = Path.home() / ".agents" / "skills" / "desk"
LINKED = [Path.home() / ".claude" / "skills" / "desk"]


def _same(path: Path, target: Path) -> bool:
    try:
        return path.resolve() == target.resolve()
    except OSError:
        return False


def _command() -> str:
    if _same(BIN, LAUNCHER):
        return f"{BIN} is this desk"
    if BIN.exists() or BIN.is_symlink():
        # Another desk (a checkout, an older download): the user's to replace, not desk's.
        return f"{BIN} is another desk ({BIN.resolve()}), left as it is"
    BIN.parent.mkdir(parents=True, exist_ok=True)
    BIN.symlink_to(LAUNCHER)
    on_path = str(BIN.parent) in os.environ.get("PATH", "").split(":")
    return f"{BIN} -> {LAUNCHER}" + ("" if on_path else f" (add {BIN.parent} to PATH)")


def _skill() -> list[str]:
    source = PROJECT / "skill"
    done = []
    if SKILLS.is_symlink():
        # A checkout links its skill directory, so that edits show at once.
        done.append(f"{SKILLS} is this desk's skill" if _same(SKILLS, source)
                    else f"{SKILLS} links to {SKILLS.resolve()}, left as it is")
    elif not BUILD and not SKILLS.exists():
        SKILLS.parent.mkdir(parents=True, exist_ok=True)
        SKILLS.symlink_to(source)
        done.append(f"{SKILLS} -> {source}")
    else:
        # A release unpacks into the cache, which is pruned: the skill is copied out of it, whole,
        # so that a file the release no longer has does not stay behind for agents to read.
        shutil.rmtree(SKILLS, ignore_errors=True)
        shutil.copytree(source, SKILLS)
        done.append(f"{SKILLS}: skill of desk {version()}")
    for link in LINKED:
        if link.exists() or link.is_symlink():
            if not _same(link, SKILLS):
                done.append(f"{link} is another skill directory, left as it is")
            continue
        if link.parent.parent.is_dir():  # only for agents that are installed (~/.claude)
            link.parent.mkdir(exist_ok=True)
            link.symlink_to(os.path.relpath(SKILLS, link.parent))
            done.append(f"{link} -> {SKILLS}")
    return done


def install() -> list[str]:
    return [_command(), *_skill()]


def _build_of(pyz: Path) -> dict:
    try:
        with zipfile.ZipFile(pyz) as z:
            return json.loads(z.read("BUILD.json"))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile) as e:
        raise DeskError(f"{pyz} is not a desk release: {e}") from None


def update() -> str:
    if not BUILD:
        raise DeskError(f"this desk runs from a checkout of its repository ({PROJECT}): update it with git pull there")
    new = LAUNCHER.with_name(f".{LAUNCHER.name}.new")
    r = subprocess.run(["gh", "release", "download", "--repo", BUILD["repo"], "--pattern", "desk.pyz",
                        "--output", str(new), "--clobber"], capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        new.unlink(missing_ok=True)
        raise DeskError(f"downloading the newest release of {BUILD['repo']} failed: {r.stderr.strip()[-300:]} "
                        "(gh auth status)")
    build = _build_of(new)
    if build["id"] == BUILD["id"]:
        new.unlink()
        return f"desk {version()} is the newest release"
    new.chmod(0o755)
    os.replace(new, LAUNCHER)
    # The new release puts its own skill in place.
    subprocess.run([sys.executable, str(LAUNCHER), "install", "--quiet"], check=True, timeout=120)
    return f"desk {version()} -> {build['version']} ({build['commit']})"
