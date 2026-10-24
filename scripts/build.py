#!/usr/bin/python3
"""Builds desk.pyz, the single-file release, from the files of this checkout that git does not
ignore, as they are in the working tree: the code, the image definitions, the skill and the
licence, with scripts/pyz_main.py as its entry point. Run by the release workflow, and before a tag by hand:

    /usr/bin/python3 scripts/build.py [--version vX.Y.Z] [--output dist/desk.pyz]
"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import time
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# LICENSE: the MIT licence asks for its notice in every copy, and a release is one.
PARTS = ["desk", "images", "skill", "LICENSE"]


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True).stdout.strip()


def repo_slug() -> str:
    """owner/name of the GitHub repository the releases come from (desk update)."""
    url = git("remote", "get-url", "origin")
    m = re.search(r"github\.com[:/](.+?)(\.git)?$", url)
    if not m:
        raise SystemExit(f"build: origin is not a GitHub repository: {url}")
    return m.group(1)


def build_id(root: Path, files: list[str]) -> str:
    """What desk update compares: the contents of the release, its entry point included (a release
    that changes only how it starts is a new one), not its name."""
    digest = hashlib.sha256()
    for f in [*files, "scripts/pyz_main.py"]:
        digest.update(f.encode() + b"\0" + (root / f).read_bytes() + b"\0")
    return digest.hexdigest()[:16]


def build(version: str | None, output: Path) -> dict:
    # Untracked files too (not ignored ones): a trial build before the commit has them.
    files = sorted(f for f in git("ls-files", "-co", "--exclude-standard", *PARTS).splitlines() if (ROOT / f).is_file())
    info = {
        "version": version or git("describe", "--tags", "--always", "--dirty"),
        "commit": git("rev-parse", "--short", "HEAD"),
        "id": build_id(ROOT, files),
        "repo": repo_slug(),
        "built": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
    }
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "desk"
        for f in files:
            (staging / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / f, staging / f)
        shutil.copy2(ROOT / "scripts" / "pyz_main.py", staging / "__main__.py")
        (staging / "BUILD.json").write_text(json.dumps(info, indent=2) + "\n")
        output.parent.mkdir(parents=True, exist_ok=True)
        zipapp.create_archive(staging, output, interpreter="/usr/bin/python3", compressed=True)
    return info


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--version", help="release name, e.g. v0.1.0 (default: git describe)")
    ap.add_argument("--output", type=Path, default=ROOT / "dist" / "desk.pyz")
    args = ap.parse_args()
    info = build(args.version, args.output)
    print(f"{args.output} {info['version']} ({info['commit']}, build {info['id']}, "
          f"{args.output.stat().st_size // 1024} KiB)")


if __name__ == "__main__":
    main()
