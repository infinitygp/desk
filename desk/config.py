import base64
import json
import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
IMAGES_DIR = PROJECT / "images"

# A release (desk.pyz) runs unpacked from the cache, with BUILD.json saying which build it is and
# DESK_LAUNCHER the file it was started as; a checkout of the repository has neither.
BUILD = json.loads((PROJECT / "BUILD.json").read_text()) if (PROJECT / "BUILD.json").is_file() else {}
LAUNCHER = Path(os.environ["DESK_LAUNCHER"]) if BUILD and os.environ.get("DESK_LAUNCHER") else PROJECT / "bin" / "desk"


def version() -> str:
    """The release and where it runs from, or for a checkout what git says of it and where it is."""
    if BUILD:
        return f"{BUILD['version']} (release {BUILD['commit']}, {LAUNCHER})"
    try:
        described = subprocess.run(["git", "-C", str(PROJECT), "describe", "--tags", "--always", "--dirty"],
                                   capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        described = ""
    return f"{described or 'unknown'} (checkout {PROJECT})"

# What differs from host to host lives outside the repository: where images and runs are kept
# (`home`), which libvirt to use (`uri`). The environment wins over the file.
CONFIG_FILE = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "desk" / "config.toml"


def _local() -> dict:
    try:
        with CONFIG_FILE.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        _refuse(str(e))
    unknown = set(data) - {"home", "uri"}
    if unknown or not all(isinstance(v, str) for v in data.values()):
        _refuse(f"it takes home = \"/path\" and uri = \"qemu:///session\" (got {sorted(data)})")
    return data


def _refuse(why: str) -> None:
    # Exit code 2, as for everything desk cannot do; 1 would read as a failed check.
    print(f"desk: {CONFIG_FILE}: {why}", file=sys.stderr)
    raise SystemExit(2)


LOCAL = _local()
HOME = Path(os.environ.get("DESK_HOME") or LOCAL.get("home")
            or Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "desk").expanduser()
URI = os.environ.get("DESK_URI") or LOCAL.get("uri") or "qemu:///session"

BASE = HOME / "base"
WARM = HOME / "warm"
RUNS = HOME / "runs"
CACHE = HOME / "cache"
LOGS = HOME / "logs"
SSH_KEY = HOME / "keys" / "id_ed25519"


class DeskError(Exception):
    pass


class Busy(DeskError):
    """The host could not give what was asked for in time (memory, a lock): nothing of the image
    or the product under test was tried."""


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
    # NoCloud seed from `user_data`) sets the system up and powers off;
    # "disk": the prebuilt disk image at `disk_image_url` (OpenWrt), its last partition grown to
    # `disk_gb` and `disk_files` copied onto it; its first boot must power off by itself.
    installer: str = "kickstart"
    location: str | None = None
    kickstart: str | None = None
    # Replaces @RELEASE@ in the kickstart, so one kickstart serves several releases.
    release: str | None = None
    iso: str | None = None
    iso_url: str | None = None
    iso_sha256: str | None = None
    kernel: str | None = None
    initrd: str | None = None
    extra_args: str = ""
    user_data: str | None = None
    # Always the latest build; checked against the .SHA256 file published next to it.
    cloud_image_url: str | None = None
    # A gzipped raw disk image, checked against the sha256sums file published next to it.
    disk_image_url: str | None = None
    # Directory (relative to the image) whose tree is written onto the disk image's root file
    # system before its first boot; @SSH_PUBKEY@ and @PASSWORD@ in its files are filled in.
    disk_files: str | None = None
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
    # A line the install writes to the serial console once its own setup has succeeded (Windows'
    # firstboot.ps1; anything else it writes there starting like this line says what failed).
    # A build whose console lacks it is not kept. None: powering off is all there is to check.
    install_marker: str | None = None
    virtio_win_iso: str | None = None
    openssh_msi: str | None = None
    # Where the two files above come from when the cache has none, and what they must hash to.
    virtio_win_url: str | None = None
    virtio_win_sha256: str | None = None
    openssh_msi_url: str | None = None
    openssh_msi_sha256: str | None = None
    # Guest port of an in-session agent, forwarded from the host like SSH (Windows).
    agent_port: int | None = None
    # Emulated sound card model (e.g. "ich9") for guests that have none.
    sound: str | None = None
    # How the harness talks to this kind of guest: "linux" (systemd, bash), "windows" or
    # "openwrt" (BusyBox ash, procd, apk).
    family: str = "linux"
    # A graphical session to drive (a11y, browser, programs started in it). Without one the
    # screen is a text console: screenshots, keys and typing still work.
    desktop: bool = True
    # Account for `exec --root` and system work: Linux has its own root key, a Windows
    # administrator gets a full token over SSH anyway.
    root_user: str = "root"
    sync: str = "sync"
    # Run before saving the warm state; returns once the guest has settled down.
    idle: str | None = None
    reboot: str = "systemctl reboot --check-inhibitors=no"
    # Ends the warm-up's first boot, after provisioning, with a clean shutdown.
    poweroff: str = "systemctl poweroff --check-inhibitors=no"
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
    def shell(self) -> str:
        """The shell that runs scripts piped to the guest (`SHELL -s`)."""
        return "sh" if self.family == "openwrt" else "bash"

    def need_desktop(self, what: str) -> None:
        if not self.desktop:
            raise DeskError(f"{what} needs a desktop session, and {self.name} has none (its screen is a text "
                            "console); use desk exec, desk forward PORT for a web interface, desk shot, key, type")

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
    data = _toml(spec)
    if "base" in data:
        parent_dir = IMAGES_DIR / data["base"]
        if not (parent_dir / "image.toml").is_file():
            raise DeskError(f"{name}: its base image {data['base']!r} does not exist")
        parent = _toml(parent_dir / "image.toml")
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
    try:
        return ImageSpec(name=name, dir=d, **data)
    except TypeError as e:
        # An unknown or a missing setting.
        raise DeskError(f"{spec}: {str(e).replace('ImageSpec.__init__() ', '')}") from None


def _toml(path: Path) -> dict:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise DeskError(f"{path}: not valid TOML: {e}") from None


def list_images() -> list[str]:
    return sorted(p.name for p in IMAGES_DIR.iterdir() if (p / "image.toml").is_file())
