import base64
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
    # "cloudimg": import the cloud image at `cloud_image_url`, whose first boot (cloud-init
    # NoCloud seed from `user_data`) sets the system up and powers off.
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
    # Always the latest build; checked against the .SHA256 file published next to it.
    cloud_image_url: str | None = None
    # Off for systems whose boot loader is not signed for UEFI Secure Boot (Arch).
    secure_boot: bool = True
    # One script or several, run in order as root.
    provision: str | list[str] | None = None
    # Directories (relative to the image) copied to /tmp/desk-provision/files before provisioning.
    files: list[str] | None = None
    # Python file with prepare(steps) (desk/steps.py), run on the host with real input once the
    # provisioned session is up: what a user does before the tests start (log in somewhere).
    prepare: str | None = None
    # A variant reuses another image's installed disk and differs only in provisioning, so it
    # has its own warm state (e.g. the same system in another language) without a second install.
    base: str | None = None
    agent_wrapper: list[str] | None = None
    relogin: str | None = None
    layout: str = "us"
    # "windows": boot `iso` with an answer-file CD built from `unattend` and `unattend_files`.
    unattend: str | None = None
    unattend_files: list[str] | None = None
    image_index: int = 1
    virtio_win_iso: str | None = None
    openssh_msi: str | None = None
    # Guest port of an in-session agent, forwarded from the host like SSH (Windows).
    agent_port: int | None = None
    # Emulated sound card model (e.g. "ich9") for guests that have none.
    sound: str | None = None
    # How the harness talks to this kind of guest.
    family: str = "linux"
    # Account for `exec --root` and system work: Linux has its own root key, a Windows
    # administrator gets a full token over SSH anyway.
    root_user: str = "root"
    sync: str = "sync"
    # Run before saving the warm state; returns once the guest has settled down.
    idle: str | None = None
    reboot: str = "systemctl reboot --check-inhibitors=no"
    boot_id: str = "cat /proc/sys/kernel/random/boot_id"
    # `desk maintain` reinstalls the image once its base is this many days old (none: only when
    # its definition changes).
    rebuild_days: int | None = None
    # Prints the minutes left until the guest's licence runs out (Windows evaluation); read at
    # every warm-up, so `desk maintain` reinstalls in time and `desk up` warns.
    expiry: str | None = None
    # Not from image.toml: a specific build instead of the current one (`desk maintain` tests a
    # new build before making it current).
    base_version: str | None = None
    warm_version: str | None = None

    @property
    def base_name(self) -> str:
        return self.base or self.name

    @property
    def base_dir(self) -> Path:
        """One installed build; BASE/<name> itself is a symlink to the current one."""
        return BASE / (f"{self.base_name}.{self.base_version}" if self.base_version else self.base_name)

    @property
    def base_disk(self) -> Path:
        return self.base_dir / "disk.qcow2"

    @property
    def base_nvram(self) -> Path:
        return self.base_dir / "VARS.qcow2"

    @property
    def base_xml(self) -> Path:
        return self.base_dir / "domain.xml"

    def cmd(self, command: str) -> str:
        """`command` as it must be sent over SSH."""
        if self.family != "windows":
            return command
        # Windows OpenSSH hands the command to powershell.exe through the Windows command line,
        # which mangles quotes; an encoded command arrives intact. Errors become plain text rather
        # than CLIXML, output UTF-8 so Polish letters survive the console code page, and the
        # login shell (PowerShell, whose -c would exit 1 for any failure) passes the code on.
        # An SSH subsystem would not save the outer PowerShell: Windows OpenSSH starts
        # subsystems through the default shell too, and loses their exit codes and output.
        script = (
            "$ProgressPreference = 'SilentlyContinue'; [Console]::OutputEncoding = [Text.Encoding]::UTF8\n"
            "$global:LASTEXITCODE = 0; $script:deskFailed = $false\n"
            f"& {{\n{command}\n}} 2>&1 | ForEach-Object {{\n"
            "  if ($_ -is [System.Management.Automation.ErrorRecord]) {\n"
            "    [Console]::Error.WriteLine($_.ToString()); $script:deskFailed = $true\n"
            "  } else { $_ }\n"
            "} | Out-String -Stream\n"
            "if ($LASTEXITCODE) { exit $LASTEXITCODE }; if ($script:deskFailed) { exit 1 }\n"
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode()
        return (f"powershell.exe -NoProfile -NonInteractive -OutputFormat Text -EncodedCommand {encoded}; "
                "exit $LASTEXITCODE")

    @property
    def provision_scripts(self) -> list[str]:
        if not self.provision:
            return []
        return [self.provision] if isinstance(self.provision, str) else list(self.provision)

    @property
    def warm_dir(self) -> Path:
        """One saved desktop; WARM/<name> itself is a symlink to the current one."""
        return WARM / (f"{self.name}.{self.warm_version}" if self.warm_version else self.name)


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
        for key in ("kickstart", "user_data", "prepare"):
            if parent.get(key):
                parent[key] = str(rel / parent[key])
        if parent.get("files"):
            parent["files"] = [str(rel / f) for f in parent["files"]]
        if parent.get("provision"):
            pv = parent["provision"]
            parent["provision"] = [str(rel / x) for x in ([pv] if isinstance(pv, str) else pv)]
        data = {**parent, **data}
    for key in ("base_version", "warm_version"):
        data.pop(key, None)
    return ImageSpec(name=name, dir=d, **data)


def list_images() -> list[str]:
    return sorted(p.name for p in IMAGES_DIR.iterdir() if (p / "image.toml").is_file())
