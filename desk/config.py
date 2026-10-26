import base64
import json
import os
import subprocess
import sys
import tomllib
import types
import typing
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
# (`home`), which libvirt to use (`uri`), the guests' time zone and keyboard layout. The
# environment wins over the file.
CONFIG_FILE = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "desk" / "config.toml"
CONFIG_DIR = CONFIG_FILE.parent
# Images are made of blocks: a distro (how it is installed and talked to), the desktop it runs and
# addons on top, each a directory with a block.toml of ImageSpec settings and the files these
# name. The shipped ones are in images/, the host's own (one in its language, one for its
# hardware) beside config.toml, laid out alike.
BLOCK_ROOTS = (IMAGES_DIR, CONFIG_DIR)
KINDS = ("distros", "desktops", "addons")
# The images this host has: a file each, written by `desk image add`, naming its blocks.
IMAGES = CONFIG_DIR / "images"


# The layouts `desk type` knows: US, and Polish (programmer's), which is US plus AltGr letters.
# Any other layout moves keys (y and z, digits), which typing by key codes cannot know.
LAYOUTS = ("us", "pl")


def _zone(name) -> bool:
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return False


def _local() -> dict:
    try:
        with CONFIG_FILE.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        _refuse(str(e))
    unknown = set(data) - {"home", "uri", "timezone", "layout"}
    if unknown or not all(isinstance(v, str) for v in data.values()):
        _refuse(f"it takes home = \"/path\", uri = \"qemu:///session\", timezone = \"Europe/Berlin\" and "
                f"layout = \"us\" (got {sorted(data)})")
    if "timezone" in data and not _zone(data["timezone"]):
        _refuse(f"timezone {data['timezone']!r} is no time zone this host knows (timedatectl list-timezones)")
    if data.get("layout", "us") not in LAYOUTS:
        _refuse(f"layout is one of {', '.join(LAYOUTS)} (got {data['layout']!r})")
    return data


def _refuse(why: str) -> None:
    # Exit code 2, as for everything desk cannot do; 1 would read as a failed check.
    print(f"desk: {CONFIG_FILE}: {why}", file=sys.stderr)
    raise SystemExit(2)


LOCAL = _local()
HOME = Path(os.environ.get("DESK_HOME") or LOCAL.get("home")
            or Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "desk").expanduser()
URI = os.environ.get("DESK_URI") or LOCAL.get("uri") or "qemu:///session"


def host_timezone(localtime: Path = Path("/etc/localtime")) -> str | None:
    """The host's time zone, None when it cannot be told: /etc/localtime links into the zone files."""
    try:
        name = os.readlink(localtime).partition("zoneinfo/")[2].removeprefix("posix/")
    except OSError:
        return None
    # right/: clocks counting leap seconds, 27 s off what guests and Windows keep.
    return name if _zone(name) and not name.startswith("right/") else None


# What images get unless they set their own: provisioning puts them into the guest.
TIMEZONE = LOCAL.get("timezone") or host_timezone() or "UTC"
LAYOUT = LOCAL.get("layout", "us")

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
    # Always the latest build; checked against the .SHA256 file published next to it, or against
    # the checksum file of this name in its directory (Debian's SHA512SUMS, Ubuntu's SHA256SUMS).
    cloud_image_url: str | None = None
    cloud_image_sums: str | None = None
    # A gzipped raw disk image, checked against the sha256sums file published next to it.
    disk_image_url: str | None = None
    # Directory (relative to the image) whose tree is written onto the disk image's root file
    # system before its first boot; @SSH_PUBKEY@ and @PASSWORD@ in its files are filled in.
    disk_files: str | None = None
    # Off for systems whose boot loader is not signed for UEFI Secure Boot (Arch).
    secure_boot: bool = True
    # Scripts run as root over SSH on the installed system before it becomes the base image
    # (`desk image build`), so a change is a new install: what a desktop block installs.
    install: list[str] | None = None
    # One script or several, run in order as root before the warm state is saved.
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
    # The guest's keyboard layout (one of LAYOUTS), which `desk type` maps characters to keys for,
    # and its time zone, in which desk clock and the report read times without a zone. Both are
    # config.toml's or the host's unless the image's file sets them; provisioning applies them.
    layout: str = "us"
    timezone: str = "UTC"
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
    # The graphical session to drive (a11y, browser, programs started in it): the desktop block's
    # name, "windows" for Windows'. "": the screen is a text console, where screenshots, keys and
    # typing still work.
    desktop: str = ""
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
    # Not from a definition: a specific build instead of the current one (`desk maintain` tests a
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

    def path(self, rel: str) -> Path:
        """A file the definition names: blocks name theirs relative to themselves, which composing
        the image turns into absolute paths."""
        return self.dir / rel

    @property
    def provision_scripts(self) -> list[str]:
        if not self.provision:
            return []
        return [self.provision] if isinstance(self.provision, str) else list(self.provision)

    @property
    def warm_dir(self) -> Path:
        """One saved desktop; WARM/<name> itself is a symlink to the current one."""
        return WARM / (f"{self.name}.{self.warm_version}" if self.warm_version else self.name)


# What decides how the base disk gets installed; any change means a new install. Memory and
# vCPUs are not among them: the warm-up and every run set them, so a change is a new warm state.
INSTALL_KEYS = ("installer", "location", "kickstart", "iso", "iso_url", "iso_sha256", "kernel", "initrd",
                "extra_args", "user_data", "cloud_image_url", "secure_boot", "release", "unattend",
                "unattend_files", "image_index", "virtio_win_iso", "openssh_msi", "os_variant",
                "disk_gb", "disk_image_url", "disk_files", "install_marker", "install")
# Settings that name files of their block.
PATH_KEYS = ("kickstart", "user_data", "unattend", "disk_files", "prepare")
# Lists of files that the blocks of an image add up to, in the order distro, desktop, addons.
LIST_KEYS = ("unattend_files", "install", "provision", "files")
# What a block says about the others besides its settings: the desktops a distro was tried with
# (none: it comes with its own, as Windows does, or has none), the distros an addon works on.
BLOCK_ONLY = {"distros": "desktops", "desktops": None, "addons": "distros"}
# What an image's file sets besides naming its blocks: settings that name no files.
NOT_OVERRIDES = {"name", "dir", "base", "base_version", "warm_version", *PATH_KEYS, *LIST_KEYS}


def scan_blocks(kind: str) -> tuple[dict[str, Path], dict[str, str]]:
    """The blocks of a kind by name, shipped and the host's own, and why the other names cannot be
    used: those fail only the images that use them."""
    found: dict[str, Path] = {}
    refused: dict[str, str] = {}
    for root in BLOCK_ROOTS:
        d = root / kind
        for p in sorted(d.iterdir()) if d.is_dir() else []:
            if not (p / "block.toml").is_file() or p.name in refused:
                continue
            if p.name in found:
                refused[p.name] = (f"{kind[:-1]} {p.name!r} is defined twice, in {found.pop(p.name)} and in {p}: "
                                   "rename the host's own")
            elif kind != "distros" and "-" in p.name:
                refused[p.name] = (f"{p}: the name of a desktop or an addon has no '-', which separates the "
                                   "blocks in image names")
            elif kind == "distros" and (other := next(
                    (n for n in found if n.startswith(f"{p.name}-") or p.name.startswith(f"{n}-")), None)):
                refused[p.name] = (f"{p}: the names of the images of {p.name} and of {other} can be the same "
                                   "(one name and '-' begins the other): rename the host's own")
            else:
                found[p.name] = p
    return found, refused


def blocks(kind: str) -> dict[str, Path]:
    """The blocks of a kind by name, shipped and the host's own."""
    return scan_blocks(kind)[0]


def describe(block: Path) -> str:
    """What a block is: the first sentence of the comment its block.toml starts with."""
    words = []
    for line in (block / "block.toml").read_text().splitlines():
        if not line.startswith("#"):
            break
        words.append(line.lstrip("#").strip())
    text = " ".join(words)
    return text[:text.index(". ") + 1] if ". " in text else text


def _block(kind: str, name: str) -> tuple[Path, dict]:
    """A block's settings, its files as absolute paths."""
    found, refused = scan_blocks(kind)
    d = found.get(name)
    if d is None:
        raise DeskError(refused.get(name) or f"no {kind[:-1]} {name!r} (there are: {', '.join(found) or 'none'})")
    data = _toml(d / "block.toml")

    def resolve(rel: str) -> str:
        p = Path(os.path.normpath(d / rel))
        if not p.exists():
            raise DeskError(f"{d / 'block.toml'}: {rel} does not exist")
        return str(p)

    for key in PATH_KEYS:
        if key in data:
            data[key] = resolve(data[key])
    for key in LIST_KEYS:
        if key in data:
            data[key] = [resolve(x) for x in ([data[key]] if isinstance(data[key], str) else data[key])]
    return d, data


def image_name(distro: str, desktop: str | None, addons: list[str]) -> str:
    return "-".join([distro, *([desktop] if desktop else []), *addons])


def parse_name(name: str) -> tuple[str, str | None, list[str]] | None:
    """The blocks an image name stands for, None when it names no distro."""
    distros = blocks("distros")
    for distro in sorted(distros, key=len, reverse=True):
        if name == distro:
            return distro, None, []
        if name.startswith(f"{distro}-"):
            rest = name[len(distro) + 1:].split("-")
            # A distro that comes with its desktop or has none is followed by addons only.
            if "desktops" not in _toml(distros[distro] / "block.toml"):
                return distro, None, rest
            return distro, rest[0], rest[1:]
    return None


def _layers(distro: str, desktop: str | None, addons: list[str]) -> tuple[Path, list[dict], list[dict]]:
    """The distro's directory, its and the desktop's settings, the addons'; refuses what does not go
    together."""
    d_dir, d = _block("distros", distro)
    desktops = d.pop("desktops", None)
    if desktops is None and desktop:
        raise DeskError(f"{distro} comes with its desktop or has none; it takes no other (got {desktop!r})")
    if desktops is not None and desktop not in desktops:
        raise DeskError(f"{distro} needs one of the desktops {', '.join(desktops)}"
                        + (f" (got {desktop!r})" if desktop else ""))
    base = [d]
    if desktop:
        base.append({**_block("desktops", desktop)[1], "desktop": desktop})
    if len(set(addons)) != len(addons):
        raise DeskError(f"an addon named twice: {', '.join(addons)}")
    extra = []
    for a in addons:
        data = _block("addons", a)[1]
        allowed = data.pop("distros", None)
        if allowed is not None and distro not in allowed:
            raise DeskError(f"the addon {a} works on {', '.join(allowed)}, not on {distro}")
        extra.append(data)
    return d_dir, base, extra


def variant_of(distro: str, desktop: str | None, addons: list[str]) -> str | None:
    """The image whose install an image with these blocks shares: the one without its addons, when
    these only add to the warm state."""
    _, _, extra = _layers(distro, desktop, addons)
    if not addons or any(k in INSTALL_KEYS for layer in extra for k in layer):
        return None
    return image_name(distro, desktop, [])


def _image_file(name: str) -> Path:
    return IMAGES / f"{name}.toml"


def add_command(name: str) -> str | None:
    """The `desk image add` that defines an image of this name, None when no blocks make it."""
    parts = parse_name(name)
    if parts is None:
        return None
    try:
        _layers(*parts)
    except DeskError:
        return None
    distro, desktop, addons = parts
    return " ".join(["desk image add", distro, *([desktop] if desktop else []), *(f"--addon {a}" for a in addons)])


def _unknown(name: str) -> str:
    known = f"defined here: {', '.join(list_images()) or 'none'}"
    command = add_command(name)
    if command is None:
        return f"unknown image {name!r} ({known}; desk image blocks lists what images are made of)"
    return f"image {name!r} is not defined on this host ({known}); define it with: {command}"


def read_image(name: str) -> dict:
    """What an image's file says: its blocks and the settings it overrides."""
    f = _image_file(name)
    if not f.is_file():
        raise DeskError(_unknown(name))
    data = _toml(f)
    if not isinstance(data.get("distro"), str):
        raise DeskError(f"{f}: no distro")
    data.setdefault("desktop", None)
    data.setdefault("addons", [])
    if not _fits(data["desktop"], str | None) or not _fits(data["addons"], list[str]):
        raise DeskError(f"{f}: desktop is the name of a block, addons a list of names")
    want = image_name(data["distro"], data["desktop"], data["addons"])
    if want != name:
        raise DeskError(f"{f}: its blocks make the image {want}, so the file is {_image_file(want).name}")
    wrong = sorted(k for k in data if k in NOT_OVERRIDES)
    if wrong:
        raise DeskError(f"{f}: an image's file names its blocks and sets sizes and the like, not {', '.join(wrong)}"
                        " (a block of the host's own holds files)")
    # The file may be edited by hand: a value of another type would fail only once a build or run uses it.
    hints = typing.get_type_hints(ImageSpec)
    for key, value in data.items():
        if key not in ("distro", "desktop", "addons") and key in hints and not _fits(value, hints[key]):
            shown = json.dumps(value, ensure_ascii=False, default=str)
            raise DeskError(f"{f}: {key} = {shown} is not {_type_name(hints[key])}")
    return data


def _fits(value, hint) -> bool:
    """Whether a value read from TOML is of a setting's type; true is no number here."""
    if isinstance(hint, types.UnionType):
        return any(_fits(value, h) for h in typing.get_args(hint))
    if typing.get_origin(hint) is list:
        return isinstance(value, list) and all(_fits(v, typing.get_args(hint)[0]) for v in value)
    return type(value) is hint


def _type_name(hint) -> str:
    return hint.__name__ if isinstance(hint, type) else str(hint)


def load_image(name: str) -> ImageSpec:
    data = read_image(name)
    distro, desktop, addons = data.pop("distro"), data.pop("desktop"), data.pop("addons")
    d_dir, layers, extra = _layers(distro, desktop, addons)
    base = variant_of(distro, desktop, addons)
    if base:
        own = sorted(k for k in data if k in INSTALL_KEYS)
        if own:
            raise DeskError(f"{name} runs on the install of {base}: set {', '.join(own)} there")
        # A variant runs on its base's install, so it has what the base's file sets too, under
        # what its addons and its own file set.
        layers.append({k: v for k, v in read_image(base).items() if k not in ("distro", "desktop", "addons")})
    spec: dict = {}
    for layer in layers + extra:
        for key, value in layer.items():
            if key in LIST_KEYS:
                spec[key] = spec.get(key, []) + value
            elif key == "prepare" and "prepare" in spec:
                raise DeskError(f"{name}: two of its blocks bring a prepare script")
            else:
                spec[key] = value
    if base:
        spec["base"] = base
    spec.update(data)
    spec.setdefault("layout", LAYOUT)
    spec.setdefault("timezone", TIMEZONE)
    f = _image_file(name)
    if spec["layout"] not in LAYOUTS:
        raise DeskError(f"{f}: layout is one of {', '.join(LAYOUTS)} (got {spec['layout']!r})")
    if not _zone(spec["timezone"]):
        raise DeskError(f"{f}: timezone {spec['timezone']!r} is no time zone this host knows")
    try:
        return ImageSpec(name=name, dir=d_dir, **spec)
    except TypeError as e:
        # An unknown or a missing setting.
        raise DeskError(f"{f}: {str(e).replace('ImageSpec.__init__() ', '')}") from None


def _toml(path: Path) -> dict:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise DeskError(f"{path}: not valid TOML: {e}") from None


def list_images() -> list[str]:
    if not IMAGES.is_dir():
        return []
    old = sorted(p for p in IMAGES.iterdir() if (p / "image.toml").is_file())
    if old:
        raise DeskError(f"{old[0]} is the definition of an older desk: images are made of blocks now. Move its "
                        f"files into a block of the host's own ({CONFIG_DIR}/addons/NAME/block.toml), add the "
                        "image with desk image add and delete the directory")
    return sorted(p.stem for p in IMAGES.glob("*.toml"))


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    # A JSON string is a TOML basic string.
    return json.dumps(str(v), ensure_ascii=False)


def write_image(name: str, data: dict) -> Path:
    """An image's file: its blocks first, then the settings it overrides."""
    IMAGES.mkdir(parents=True, exist_ok=True)
    lines = ["# Written by desk image add and desk image set, which keep no comments; editing it by hand is fine."]
    for key in ("distro", "desktop", "addons", *sorted(set(data) - {"distro", "desktop", "addons"})):
        if data.get(key) not in (None, []):
            lines.append(f"{key} = {_toml_value(data[key])}")
    f = _image_file(name)
    tmp = f.with_name(f".{f.name}.{os.getpid()}")
    tmp.write_text("\n".join(lines) + "\n")
    os.replace(tmp, f)
    return f


def add_image(distro: str, desktop: str | None, addons: list[str], settings: dict) -> list[str]:
    """Define an image, and the one whose install it runs on when that is missing; the names
    written, that one first."""
    name = image_name(distro, desktop, addons)
    if _image_file(name).exists():
        raise DeskError(f"{name} is defined already ({_image_file(name)}); desk image set {name} changes it")
    written = []
    base = variant_of(distro, desktop, addons)
    if base and not _image_file(base).exists():
        write_image(base, {"distro": distro, "desktop": desktop})
        written.append(base)
    write_image(name, {"distro": distro, "desktop": desktop, "addons": addons, **settings})
    try:
        load_image(name)
    except DeskError:
        for n in [name, *written]:
            _image_file(n).unlink()
        raise
    return [*written, name]


def set_image(name: str, settings: dict) -> None:
    data = read_image(name)
    before = _image_file(name).read_bytes()
    write_image(name, {**data, **settings})
    try:
        load_image(name)
    except DeskError:
        _image_file(name).write_bytes(before)
        raise


def base_of(name: str) -> str | None:
    """The image whose install an image runs on; of one whose file does not load, what its name says."""
    try:
        return load_image(name).base
    except DeskError:
        pass
    try:
        parts = parse_name(name)
    except DeskError:
        return None
    if not parts or not parts[2]:
        return None
    try:
        return variant_of(*parts)
    except DeskError:
        # Its addon is gone: a variant it may have been still has its builds on that install.
        return image_name(parts[0], parts[1], [])


def remove_image(name: str) -> None:
    """Remove an image's file, a broken one too, unless another image runs on its install."""
    if not _image_file(name).is_file():
        raise DeskError(_unknown(name))
    users = [other for other in list_images() if other != name and base_of(other) == name]
    if users:
        raise DeskError(f"{', '.join(users)} runs on the install of {name}; "
                        f"remove {'it' if len(users) == 1 else 'them'} first")
    _image_file(name).unlink()
