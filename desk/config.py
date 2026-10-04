import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
IMAGES_DIR = PROJECT / "images"

HOME = Path(os.environ.get("DESK_HOME", "/var/lib/vmlab"))
URI = os.environ.get("DESK_URI", "qemu:///session")

BASE = HOME / "base"
WARM = HOME / "warm"
RUNS = HOME / "runs"
CACHE = HOME / "cache"
LOGS = HOME / "logs"
SSH_KEY = HOME / "keys" / "id_ed25519"


class DeskError(Exception):
    pass


@dataclass
class ImageSpec:
    name: str
    dir: Path
    os_variant: str
    memory_mib: int
    vcpus: int
    disk_gb: int
    location: str
    kickstart: str
    user: str
    ready: str
    provision: str | None = None
    agent_wrapper: list[str] | None = None
    relogin: str | None = None
    layout: str = "us"

    @property
    def base_disk(self) -> Path:
        return BASE / f"{self.name}.qcow2"

    @property
    def base_nvram(self) -> Path:
        return BASE / f"{self.name}.VARS.qcow2"

    @property
    def base_xml(self) -> Path:
        return BASE / f"{self.name}.xml"

    @property
    def warm_dir(self) -> Path:
        return WARM / self.name


def load_image(name: str) -> ImageSpec:
    d = IMAGES_DIR / name
    spec = d / "image.toml"
    if not spec.is_file():
        known = ", ".join(sorted(p.name for p in IMAGES_DIR.iterdir() if (p / "image.toml").is_file()))
        raise DeskError(f"unknown image {name!r} (known: {known or 'none'})")
    with spec.open("rb") as f:
        data = tomllib.load(f)
    return ImageSpec(name=name, dir=d, **data)


def list_images() -> list[str]:
    return sorted(p.name for p in IMAGES_DIR.iterdir() if (p / "image.toml").is_file())
