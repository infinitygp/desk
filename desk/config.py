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
    user: str
    ready: str
    # "kickstart": network install from `location` with `kickstart`;
    # "autoinstall": boot `kernel`/`initrd` from `iso`, cloud-init NoCloud seed from `user_data`.
    installer: str = "kickstart"
    location: str | None = None
    kickstart: str | None = None
    iso: str | None = None
    iso_url: str | None = None
    iso_sha256: str | None = None
    kernel: str | None = None
    initrd: str | None = None
    extra_args: str = ""
    user_data: str | None = None
    # One script or several, run in order as root.
    provision: str | list[str] | None = None
    # Directories (relative to the image) copied to /tmp/desk-provision/files before provisioning.
    files: list[str] | None = None
    # A variant reuses another image's installed disk and differs only in provisioning, so it
    # has its own warm state (e.g. the same system in another language) without a second install.
    base: str | None = None
    agent_wrapper: list[str] | None = None
    relogin: str | None = None
    layout: str = "us"

    @property
    def base_name(self) -> str:
        return self.base or self.name

    @property
    def base_disk(self) -> Path:
        return BASE / f"{self.base_name}.qcow2"

    @property
    def base_nvram(self) -> Path:
        return BASE / f"{self.base_name}.VARS.qcow2"

    @property
    def base_xml(self) -> Path:
        return BASE / f"{self.base_name}.xml"

    @property
    def provision_scripts(self) -> list[str]:
        if not self.provision:
            return []
        return [self.provision] if isinstance(self.provision, str) else list(self.provision)

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
    if "base" in data:
        parent_dir = IMAGES_DIR / data["base"]
        with (parent_dir / "image.toml").open("rb") as f:
            parent = tomllib.load(f)
        if "base" in parent:
            raise DeskError(f"{name}: a variant must be based on an installed image, not on another variant")
        # Inherit everything; the parent's file references stay relative to the parent.
        rel = Path("..") / data["base"]
        for key in ("kickstart", "user_data"):
            if parent.get(key):
                parent[key] = str(rel / parent[key])
        if parent.get("files"):
            parent["files"] = [str(rel / f) for f in parent["files"]]
        if parent.get("provision"):
            pv = parent["provision"]
            parent["provision"] = [str(rel / x) for x in ([pv] if isinstance(pv, str) else pv)]
        data = {**parent, **data}
    return ImageSpec(name=name, dir=d, **data)


def list_images() -> list[str]:
    return sorted(p.name for p in IMAGES_DIR.iterdir() if (p / "image.toml").is_file())
