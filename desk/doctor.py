"""`desk doctor`: what desk needs on this host and what of it is there, with the packages that
bring the rest on Arch Linux and on Fedora (and the distributions built on them). Images, warm
states and the maintenance timer are listed for information: none of them is required, a host
keeps the images it tests on."""

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Callable

from . import config
from .config import HOME, URI, DeskError, list_images, load_image, version


def tool(name: str) -> Callable[[], bool]:
    return lambda: shutil.which(name) is not None


def module(name: str) -> Callable[[], bool]:
    return lambda: importlib.util.find_spec(name) is not None


@cache
def _qemu_devices() -> str:
    try:
        result = subprocess.run(["qemu-system-x86_64", "-device", "help"], capture_output=True, text=True,
                                timeout=30)
        return result.stdout if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def qemu_device(name: str) -> Callable[[], bool]:
    return lambda: f'name "{name}"' in _qemu_devices()


def gst_elements(*names: str) -> Callable[[], bool]:
    def present() -> bool:
        try:
            return all(subprocess.run(["gst-inspect-1.0", "--exists", n], capture_output=True, timeout=30).returncode == 0
                       for n in names)
        except (OSError, subprocess.TimeoutExpired):
            return False
    return present


def uefi_firmware() -> bool:
    """A UEFI firmware for x86-64 guests that libvirt can find (QEMU firmware descriptors)."""
    for f in Path("/usr/share/qemu/firmware").glob("*.json"):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if "uefi" in d.get("interface-types", []) and any(t.get("architecture") == "x86_64" for t in d.get("targets", [])):
            return True
    return False


@dataclass(frozen=True)
class Need:
    what: str
    # What desk uses it for, so that a missing one can be judged.
    why: str
    present: Callable[[], bool]
    arch: str
    fedora: str


# Without these no run starts.
RUNS = [
    Need("Python libvirt bindings", "talking to libvirt", module("libvirt"), "libvirt-python", "python3-libvirt"),
    Need("libvirt QEMU driver", "running the VMs", lambda: shutil.which("virtqemud") is not None
         or shutil.which("libvirtd") is not None, "libvirt", "libvirt-daemon-driver-qemu"),
    Need("qemu-system-x86_64", "the VMs", tool("qemu-system-x86_64"), "qemu-system-x86", "qemu-system-x86-core"),
    Need("QEMU virtio-vga", "the guests' screen", qemu_device("virtio-vga"), "qemu-hw-display-virtio-vga",
         "qemu-device-display-virtio-vga"),
    Need("UEFI firmware", "booting the guests", uefi_firmware, "edk2-ovmf", "edk2-ovmf"),
    Need("qemu-img", "run disks, snapshots", tool("qemu-img"), "qemu-img", "qemu-img"),
    Need("passt", "the guests' network", tool("passt"), "passt", "passt"),
    Need("ssh, scp", "commands and files in the guest", lambda: tool("ssh")() and tool("scp")(), "openssh",
         "openssh-clients"),
]
# Without these no image is built (desk image build, desk maintain).
BUILDS = [
    Need("virt-install", "installing images", tool("virt-install"), "virt-install", "virt-install"),
    Need("ssh-keygen", "the key the guests trust", tool("ssh-keygen"), "openssh", "openssh"),
]
CURL = Need("curl", "downloading images and installers", tool("curl"), "curl", "curl")
# What only some images need to be built, by the `installer` of their distro (a kickstart install
# fetches its files through virt-install).
INSTALLERS = {
    "cloudimg": [
        CURL,
        Need("openssl", "the guest user's password for cloud-init", tool("openssl"), "openssl", "openssl"),
    ],
    "disk": [
        CURL,
        Need("gzip", "unpacking the disk image", tool("gzip"), "gzip", "gzip"),
        Need("sgdisk", "growing the disk image", tool("sgdisk"), "gptfdisk", "gdisk"),
        Need("parted", "growing the disk image", tool("parted"), "parted", "parted"),
        Need("guestfish", "writing files onto the disk image", tool("guestfish"), "libguestfs", "libguestfs"),
    ],
    "windows": [
        CURL,
        Need("7z", "drivers from the virtio-win ISO", tool("7z"), "7zip", "7zip"),
        Need("xorriso", "the answer-file CD", tool("xorriso"), "libisoburn", "xorriso"),
    ],
}
# Features that say so themselves when what they need is missing.
USB_HOST = Need("QEMU usb-host", "desk usb add, desk up --usb", qemu_device("usb-host"),
                "qemu-hw-usb-host", "qemu-device-usb-host")
FEATURES = [
    Need("Pillow", "desk shot --region/--window/--zoom, several monitors", module("PIL"), "python-pillow",
         "python3-pillow"),
    Need("QEMU virtio-gpu-pci", "desk up --resolution/--displays", qemu_device("virtio-gpu-pci"),
         "qemu-hw-display-virtio-gpu-pci", "qemu-device-display-virtio-gpu-pci"),
    USB_HOST,
    Need("virt-diff", "desk diff, desk down --diff", tool("virt-diff"), "guestfs-tools", "guestfs-tools"),
    Need("gh", "desk update from a private fork", tool("gh"), "github-cli", "gh"),
    Need("GStreamer with VP9, WebM, PNG and text overlay", "desk record",
         lambda: tool("gst-launch-1.0")() and gst_elements("multifilesrc", "pngdec", "videobox", "timeoverlay", "vp9enc",
                                                         "webmmux")(),
         "gstreamer gst-plugins-base gst-plugins-good", "gstreamer1 gstreamer1-plugins-base gstreamer1-plugins-good"),
]


def host_family(os_release: str) -> str | None:
    """"arch" or "fedora" for those and the distributions built on them, from /etc/os-release."""
    fields = dict(line.split("=", 1) for line in os_release.splitlines() if "=" in line)
    ids = [fields.get("ID", "").strip('"')] + fields.get("ID_LIKE", "").strip('"').split()
    return next((family for family in ("arch", "fedora") if family in ids), None)


def install_command(family: str | None, needs: list[Need]) -> str:
    """The command that installs what `needs` lack, as the user types it (fish and bash alike)."""
    def packages(of: str) -> str:
        return " ".join(sorted({p for n in needs for p in getattr(n, of).split()}))
    if family == "arch":
        return f"sudo pacman -S --needed {packages('arch')}"
    if family == "fedora":
        return f"sudo dnf install {packages('fedora')}"
    return f"Arch: sudo pacman -S --needed {packages('arch')}; Fedora: sudo dnf install {packages('fedora')}"


def _reflink(directory: Path) -> bool:
    """Whether files in `directory` can be copied without copying their data (btrfs, XFS)."""
    with tempfile.TemporaryDirectory(dir=directory, prefix=".desk-doctor-") as tmp:
        (Path(tmp) / "a").write_bytes(b"desk")
        return subprocess.run(["cp", "--reflink=always", f"{tmp}/a", f"{tmp}/b"], capture_output=True).returncode == 0


def _fstype(path: Path) -> str:
    best, fstype = "", "?"
    for line in Path("/proc/self/mounts").read_text().splitlines():
        _dev, mount, kind, *_ = line.split()
        if (str(path) == mount or str(path).startswith(mount.rstrip("/") + "/")) and len(mount) >= len(best):
            best, fstype = mount, kind
    return fstype


def storage() -> tuple[list[str], list[str]]:
    """(what there is to say about DESK_HOME, problems with it)."""
    there = HOME
    while not there.exists():
        there = there.parent
    source = ("$DESK_HOME" if os.environ.get("DESK_HOME") else f"home in {config.CONFIG_FILE}"
              if config.LOCAL.get("home") else "the default; `home = \"/path\"` in "
              f"{config.CONFIG_FILE} or $DESK_HOME for another place")
    lines = [f"home       {HOME} ({source})" + ("" if HOME.exists() else "; made by the first image build")]
    problems = []
    if not os.access(there, os.W_OK | os.X_OK):
        problems.append(f"{there} is not writable by {os.environ.get('USER', 'this user')}")
        return lines, problems
    free = shutil.disk_usage(there).free / 2**30
    reflink = _reflink(there)
    lines.append(f"           {_fstype(there.resolve())}, {free:.0f} GiB free, "
                 + ("copies share their data (reflink)" if reflink else "no reflink"))
    if not reflink:
        problems.append("this file system copies files in full: snapshots and warm-ups copy the whole disk each time, "
                        "and desk diff does not work. btrfs or XFS do it without copying")
    if free < 20:
        problems.append(f"only {free:.0f} GiB free: an image takes 5-25 GiB, a snapshot 3-4 GiB (desk gc frees what "
                        "nothing uses)")
    return lines, problems


def kvm() -> str | None:
    dev = Path("/dev/kvm")
    if not dev.exists():
        return "no /dev/kvm: virtualisation is off in the firmware, or this host is a VM without nested KVM"
    if not os.access(dev, os.R_OK | os.W_OK):
        return "/dev/kvm is not open to this user: sudo usermod -aG kvm $USER, then log in again"
    return None


def libvirt_connection() -> tuple[str, str | None]:
    try:
        import libvirt
    except ModuleNotFoundError:
        return f"libvirt    {URI}", None  # listed among the missing packages
    try:
        conn = libvirt.open(URI)
    except libvirt.libvirtError as e:
        return f"libvirt    {URI}", f"cannot connect to {URI}: {e}"
    def dotted(v: int) -> str:
        return f"{v // 1000000}.{v // 1000 % 1000}.{v % 1000}"
    try:
        versions = f", libvirt {dotted(conn.getLibVersion())}, QEMU {dotted(conn.getVersion())}"
    except libvirt.libvirtError:
        versions = ""  # no QEMU to ask: it is listed among the missing packages
    return f"libvirt    {URI}{versions}", None


def images() -> tuple[list[str], dict[str, list[str]]]:
    """(a line per image, the images each missing build tool is for)."""
    from . import runs

    lines, wanted = [], {}
    try:
        names = list_images()
    except DeskError as e:
        return [f"  {e}"], wanted
    if not names:
        lines.append("  none defined here (desk image blocks lists what images are made of, "
                     "desk image add defines one)")
    for name in names:
        try:
            spec = load_image(name)
        except DeskError as e:
            lines.append(f"  {name:<25}broken definition: {e}")
            continue
        missing = [] if spec.base or spec.derived_from else [
            n.what for n in INSTALLERS.get(spec.installer, []) if not n.present()]
        for what in missing:
            wanted.setdefault(what, []).append(name)
        if (spec.warm_dir / "memory.save").exists():
            stale = runs.warm_stale(spec)
            state = f"warm state that {stale} (boots cold)" if stale else "warm"
        elif spec.derived_from:
            state = (f"derived from {spec.derived_from}, boots cold" if spec.base_xml.exists()
                     else f"derived from {spec.derived_from}, its build is missing (desk image rm {name})")
        elif spec.base_xml.exists():
            state = f"installed, no warm state (boots cold; desk image warm {name})"
        elif spec.base:
            state = f"not built (variant of {spec.base}: desk image warm {name})"
        else:
            state = "not built"
        lines.append(f"  {name:<25}{state}")
    return lines, wanted


def timer_line() -> str:
    try:
        r = subprocess.run(["systemctl", "--user", "is-enabled", "desk-maintain.timer"], capture_output=True, text=True,
                           timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return "no systemd user session here; run desk maintain by hand when images should be renewed"
    if r.stdout.strip() == "enabled":
        return "desk-maintain.timer is on: images are rebuilt and tested weekly"
    return ("no timer: images are renewed when you run desk maintain (desk maintain --dry-run shows what is due; "
            "--install-timer does it weekly)")


def usbvfiod_line() -> str:
    """Whether desk up --usbvfiod works here: the binary, and a QEMU with vfio-user-pci. Only
    information: hosts that do not use it are not failed for lacking it."""
    from . import usbvfiod

    return usbvfiod.describe()


def guest_settings() -> str:
    """The time zone and keyboard layout images get, and where they come from."""
    zone = ("config.toml" if config.LOCAL.get("timezone") else "the host's" if config.host_timezone()
            else "the host's could not be read from /etc/localtime; timezone in config.toml sets one")
    layout = "config.toml" if config.LOCAL.get("layout") else "the default; config.toml can set pl"
    return f"time zone {config.TIMEZONE} ({zone}), keyboard layout {config.LAYOUT} ({layout})"


def run() -> bool:
    """Print the findings; False when something is missing without which no run can start."""
    try:
        family = host_family(Path("/etc/os-release").read_text())
    except OSError:
        family = None
    print(f"desk       {version()}")
    home_lines, home_problems = storage()
    print("\n".join(home_lines))
    line, connection_problem = libvirt_connection()
    print(line)
    print(f"host       {family or 'neither Arch nor Fedora: package names below are theirs'}")
    print(f"guests     {guest_settings()}")

    image_lines, wanted = images()
    by_installer = {n.what: n for needs in INSTALLERS.values() for n in needs}
    missing = {
        "to start runs": [n for n in RUNS if not n.present()],
        "to build images": [n for n in BUILDS if not n.present()] + [by_installer[what] for what in wanted],
        "for single features": [n for n in FEATURES if not n.present()],
    }
    problems = [p for p in (kvm(), connection_problem) if p] + home_problems
    print()
    if not any(missing.values()) and not problems:
        print("everything desk uses is installed")
    for title, needs in missing.items():
        if needs:
            print(f"missing {title}:")
            for n in needs:
                only = f" (only {', '.join(wanted[n.what])})" if n.what in wanted else ""
                print(f"  {n.what:<28}{n.why}{only}")
            print(f"  -> {install_command(family, needs)}")
    for p in problems:
        print(f"problem: {p}")

    print(f"\nimages in {config.IMAGES} (optional: define and build the ones you test on, desk image add, "
          "desk maintain --only NAME)")
    print("\n".join(image_lines))
    print(f"\nmaintenance (optional): {timer_line()}")
    print(f"usbvfiod (optional): {usbvfiod_line()}")
    return not missing["to start runs"] and not kvm() and not connection_problem \
        and not any("not writable" in p for p in home_problems)
