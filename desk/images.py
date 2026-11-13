"""Golden images: unattended install (base) and a saved, logged-in desktop (warm)."""

import hashlib
import io
import json
import os
import re
import shlex
import shutil
import tarfile
import tempfile
import subprocess
import time
import xml.etree.ElementTree as ET
import zoneinfo
from dataclasses import MISSING, fields, replace
from pathlib import Path

import libvirt

from . import display, guest, steps
from .config import (BASE, BLOCK_ROOTS, CACHE, INSTALL_KEYS, LIST_KEYS, LOGS, PATH_KEYS, SSH_KEY, WARM, DeskError,
                     ImageSpec)
from .virt import conn, free_port, lookup, prepare_domain_xml, qemu_img_overlay, reflink_copy


def _log(msg: str) -> None:
    print(f"[desk] {msg}", flush=True)


def build_domain_name(spec: ImageSpec) -> str:
    return f"build-{spec.name}"


# Longest an install may take, Windows included, before the build counts as stuck.
BUILD_TIMEOUT = 3 * 3600


def new_version() -> str:
    # UTC: the hour that repeats when summer time ends would reorder local names, or repeat one.
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime())


def versions(root: Path, name: str) -> list[Path]:
    """Builds of `name` under BASE or WARM, oldest first."""
    pattern = re.compile(re.escape(name) + r"\.\d{8}-\d{6}")
    # Resolved like current() and the backing chains they are compared with.
    return sorted(p.resolve() for p in root.glob(f"{name}.*") if pattern.fullmatch(p.name) and not p.is_symlink())


def promote(link: Path, target: Path) -> None:
    """Make `target` the current build: one rename, so `desk up` never sees half of a switch."""
    if link.exists() and not link.is_symlink():
        raise DeskError(f"{link} is left from before builds were versioned; move it to "
                        f"{link.name}.<YYYYmmdd-HHMMSS> and rebase its overlays, or delete it")
    tmp = link.with_name(f".{link.name}.new")
    tmp.unlink(missing_ok=True)
    os.symlink(target.name, tmp)
    os.replace(tmp, link)


def current(link: Path) -> Path | None:
    return link.resolve() if link.is_symlink() and link.exists() else None


# Image settings that do not change what ends up in the guest (the files the URLs lead to are
# named, version included, by virtio_win_iso and openssh_msi).
NOT_IN_GUEST = ("name", "dir", "base_version", "warm_version", "rebuild_days", "expiry", "calm_seconds",
                "cloud_image_sums", "virtio_win_url", "virtio_win_sha256", "openssh_msi_url", "openssh_msi_sha256")


def _named(path: Path) -> str:
    """A file as fingerprints know it: within its root, so that neither where desk runs from nor a
    block moved to the host's own makes a new build."""
    path = path.resolve()
    return next((str(path.relative_to(r.resolve())) for r in BLOCK_ROOTS if path.is_relative_to(r.resolve())),
                str(path))


def _settings(spec: ImageSpec, keys) -> str:
    """The settings among `keys` that differ from their defaults, so that a new setting added to
    ImageSpec with a default does not make every image look changed."""
    data = {}
    for f in fields(spec):
        value = getattr(spec, f.name)
        if f.name in keys and (f.default is MISSING or value != f.default):
            if f.name in PATH_KEYS + LIST_KEYS and value:
                value = [_named(spec.path(v)) for v in value] if isinstance(value, list) else _named(spec.path(value))
            data[f.name] = value
    return json.dumps(data, sort_keys=True, default=str)


def _hash_files(h, spec: ImageSpec, rels: list[str]) -> None:
    for rel in rels:
        src = spec.path(rel).resolve()
        for f in [src] if src.is_file() else sorted(q for q in src.rglob("*") if q.is_file()):
            h.update(_named(f).encode() + b"\0" + f.read_bytes() + b"\0")


def install_fingerprint(spec: ImageSpec) -> str:
    h = hashlib.sha256(_settings(spec, INSTALL_KEYS).encode())
    # Install files that provisioning copies again at every warm-up (Windows' agent) do not
    # need a new install when they change.
    refreshed = {spec.path(f).resolve() for f in spec.files or []}
    _hash_files(h, spec, [f for f in (spec.kickstart, spec.user_data, spec.unattend, spec.disk_files) if f]
                + [f for f in spec.unattend_files or [] if spec.path(f).resolve() not in refreshed]
                + list(spec.install or []))
    return h.hexdigest()


def warm_fingerprint(spec: ImageSpec) -> str:
    """Everything a warm state depends on besides the base disk: the whole image definition and
    the provisioning scripts and files."""
    h = hashlib.sha256(_settings(spec, {f.name for f in fields(spec)} - set(NOT_IN_GUEST)).encode())
    _hash_files(h, spec, spec.provision_scripts + list(spec.files or []) + ([spec.prepare] if spec.prepare else []))
    return h.hexdigest()


def build(spec: ImageSpec, force: bool = False, promote_it: bool = True) -> str:
    """Install a new build of the base image and return its version; re-running attaches to an
    install that is still in progress. Builds sit next to each other, so runs keep using the
    build they started from."""
    refuse_derived(spec, "build")
    if spec.base:
        raise DeskError(f"{spec.name} is a variant of {spec.base}: "
                        f"build {spec.base}, then `desk image warm {spec.name}`")
    name = build_domain_name(spec)
    dom = lookup(name)
    link = BASE / spec.base_name
    deadline = time.monotonic() + BUILD_TIMEOUT
    vspec = None
    if dom is not None:
        root = ET.fromstring(dom.XMLDesc(0))
        disk = Path(next(d for d in root.iter("disk") if d.get("device") == "disk").find("source").get("file"))
        if dom.isActive() and disk.exists():
            vspec = replace(spec, base_version=disk.parent.name.removeprefix(f"{spec.base_name}."))
            _log(f"attaching to running build domain {name}")
        else:
            # Left by an interrupted build; whatever it installed is incomplete.
            _log(f"discarding the unfinished build domain {name}")
            _discard_build(name)
    try:
        if vspec is None:
            if promote_it and current(link) and not force:
                raise DeskError(f"{spec.name} already built (use --force to install a new build)")
            vspec = replace(spec, base_version=new_version())
            vspec.base_dir.mkdir(parents=True)
            _virt_install(vspec, name, deadline)
        _wait_shutoff(name, timeout=max(60.0, deadline - time.monotonic()))
        log = LOGS / f"{name}.log"
        problem = install_problem(spec, log.read_text(errors="replace") if log.exists() else "")
        if problem:
            raise DeskError(f"the install of {spec.name} is not usable: {problem} (serial console: {log})")
    except BaseException:
        # A stuck or failed install (boot failure, prompt, hung download) must not block the next build.
        _discard_build(name)
        raise
    _finalize(vspec, name)
    if spec.install:
        _install(vspec, deadline)
    _seal(vspec)
    if promote_it:
        promote(link, vspec.base_dir)
    return vspec.base_version


def install_problem(spec: ImageSpec, console: str) -> str | None:
    """Why an install that powered off cannot be used, going by what it wrote to the serial
    console; None: it finished. Powering off alone proves little: a guest that crashed, or whose
    setup script failed on the way, powers off too."""
    marker = spec.install_marker
    if not marker or marker in console:
        return None
    said = [line.strip() for line in console.splitlines() if marker.split(":")[0] + ":" in line]
    return said[-1][-500:] if said else f"it powered off without reporting {marker!r}, so its setup did not finish"


def refuse_derived(spec: ImageSpec, command: str) -> None:
    """A derived image is a disk that was saved from a run: building or warming it would put the
    source's content in its place."""
    if spec.derived_from:
        raise DeskError(f"{spec.name} is derived from {spec.derived_from}: its disk was saved from a run, so "
                        f"desk image {command} does not apply to it (desk image save makes a new one from a run, "
                        "desk image rm removes it)")


def _discard_build(name: str) -> None:
    dom = lookup(name)
    if dom is None:
        return
    try:
        if dom.isActive():
            shot = LOGS / f"{name}-stuck.png"
            try:
                shot.write_bytes(display.screenshot(dom))
                _log(f"screen of the unfinished install: {shot}")
            except Exception:  # noqa: BLE001 - the screenshot is only a hint
                pass
            dom.destroy()
        dom.undefineFlags(libvirt.VIR_DOMAIN_UNDEFINE_NVRAM | libvirt.VIR_DOMAIN_UNDEFINE_TPM)
    except libvirt.libvirtError as e:
        _log(f"warning: could not remove {name}: {e}")


def _checksum(path: Path, algorithm: str = "sha256") -> str:
    h = hashlib.new(algorithm)
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# Images and the checksums they are checked against come over HTTPS only, redirects included.
HTTPS_ONLY = ["--proto", "=https", "--proto-redir", "=https"]


def _download(url: str, target: Path) -> None:
    _log(f"downloading {url}")
    # Give up on a stalled transfer instead of hanging an unattended build.
    subprocess.run(["curl", *HTTPS_ONLY, "-fL", "--progress-bar", "--retry", "3", "--speed-limit", "10000",
                    "--speed-time", "300", "-o", f"{target}.part", url], check=True)
    Path(f"{target}.part").rename(target)


def _fetch(spec: ImageSpec, file: str, source: str) -> Path:
    """The cached file the setting `file` names, downloaded from `<source>_url` when it is missing
    and checked against `<source>_sha256`."""
    target = CACHE / getattr(spec, file)
    url, sha256 = getattr(spec, f"{source}_url"), getattr(spec, f"{source}_sha256")
    if not target.exists():
        if not url:
            raise DeskError(f"{target} missing and no {source}_url to fetch it from")
        CACHE.mkdir(parents=True, exist_ok=True)
        _download(url, target)
    if sha256 and _checksum(target) != sha256:
        raise DeskError(f"{target} does not match {source}_sha256; delete it to download again")
    return target


def _fetch_published(url: str, sums_url: str, pick) -> Path:
    """`url` into the cache, checked against the checksum published with it (`pick` finds it in
    the sums file); downloaded again only when that checksum changes."""
    sums = subprocess.run(["curl", *HTTPS_ONLY, "-fsSL", "--retry", "3", "--max-time", "60", sums_url],
                          capture_output=True, text=True, check=True).stdout
    want = pick(sums)
    # Debian publishes SHA-512 sums only.
    algorithm = "sha512" if len(want) == 128 else "sha256"
    target = CACHE / url.rsplit("/", 1)[-1]
    if not target.exists() or _checksum(target, algorithm) != want:
        _download(url, target)
        if _checksum(target, algorithm) != want:
            raise DeskError(f"{target} does not match {sums_url}")
    return target


def _fetch_cloud_image(spec: ImageSpec) -> Path:
    """The latest cloud image."""
    url = spec.cloud_image_url
    if spec.cloud_image_sums:
        folder, name = url.rsplit("/", 1)
        return _fetch_published(url, f"{folder}/{spec.cloud_image_sums}", lambda sums: checksum_for(sums, name))
    return _fetch_published(url, f"{url}.SHA256", lambda sums: sums.split()[0])


def checksum_for(sums: str, name: str) -> str:
    """The checksum of `name` in a sums file (`<hex> *<name>` or `<hex>  <name>` lines)."""
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name:
            return parts[0].lower()
    raise DeskError(f"{name} is not listed in the published checksums")


def _fetch_disk_image(spec: ImageSpec) -> Path:
    folder, name = spec.disk_image_url.rsplit("/", 1)
    return _fetch_published(spec.disk_image_url, f"{folder}/sha256sums", lambda sums: checksum_for(sums, name))


def _render(spec: ImageSpec, template: str) -> str:
    return _fill(spec, spec.path(template).read_text(), template)


def _pubkey() -> str:
    """The public half of the key desk logs in to guests with; a new DESK_HOME gets its pair here."""
    pub = SSH_KEY.with_suffix(".pub")
    if not pub.exists():
        keys = SSH_KEY.parent
        keys.parent.mkdir(parents=True, exist_ok=True)
        # Made aside and renamed into place, so two first builds at once end up with one key.
        tmp = Path(tempfile.mkdtemp(prefix=f"{keys.name}.", dir=keys.parent))
        try:
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "desk",
                            "-f", str(tmp / SSH_KEY.name)], check=True, stdin=subprocess.DEVNULL)
            try:
                tmp.rename(keys)
            except OSError:
                pass  # the other build's key is in place
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return pub.read_text()


def _fill(spec: ImageSpec, text: str, template: str) -> str:
    pubkey = _pubkey().strip()
    # The password of every image's user is its name.
    text = text.replace("@SSH_PUBKEY@", pubkey).replace("@PASSWORD@", spec.user)
    if spec.release:
        text = text.replace("@RELEASE@", spec.release)
    if "@RELEASE@" in text:
        raise DeskError(f"{template} uses @RELEASE@ but {spec.name} sets no release")
    if "@PASSWORD_HASH@" in text:
        hashed = subprocess.run(["openssl", "passwd", "-6", spec.user], capture_output=True, text=True,
                                check=True).stdout.strip()
        text = text.replace("@PASSWORD_HASH@", hashed)
    return text


def _virt_install(spec: ImageSpec, name: str, deadline: float) -> None:
    work = CACHE / spec.name
    work.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    log = LOGS / f"{name}.log"
    common = [
        "virt-install", "--connect", conn().getURI(), "--name", name,
        "--memory", str(spec.memory_mib), "--vcpus", str(spec.vcpus),
        "--cpu", "host-passthrough", "--machine", "q35",
        "--boot", "uefi" if spec.secure_boot else "uefi,firmware.feature0.name=secure-boot,firmware.feature0.enabled=no",
        "--disk", f"path={spec.base_disk},size={spec.disk_gb},format=qcow2,bus=virtio,discard=unmap",
        "--os-variant", spec.os_variant,
        "--network", "type=user,backend.type=passt,model=virtio",
        "--graphics", "none", "--video", "virtio",
        "--serial", f"file,path={log}",
        "--noautoconsole", "--noreboot",
        # Minutes; virt-install leaves the domain running when it runs out, build() cleans up.
        "--wait", str(max(1, int((deadline - time.monotonic()) / 60))),
    ]
    if spec.installer == "kickstart":
        (work / "ks.cfg").write_text(_render(spec, spec.kickstart))
        extra = [
            "--location", spec.location,
            "--initrd-inject", str(work / "ks.cfg"),
            "--extra-args", "inst.ks=file:/ks.cfg console=ttyS0 inst.text",
        ]
    elif spec.installer == "cloudimg":
        image = _fetch_cloud_image(spec)
        subprocess.run(["qemu-img", "convert", "-q", "-O", "qcow2", str(image), str(spec.base_disk)], check=True)
        # Its root partition comes last, so cloud-init grows it into the space added here.
        subprocess.run(["qemu-img", "resize", "-q", str(spec.base_disk), f"{spec.disk_gb}G"], check=True)
        (work / "user-data").write_text(_render(spec, spec.user_data))
        (work / "meta-data").write_text(f"instance-id: iid-{spec.name}\n")
        disk = f"path={spec.base_disk},format=qcow2,bus=virtio,discard=unmap"
        common = [disk if a.startswith(f"path={spec.base_disk},") else a for a in common]
        extra = [
            "--import",
            "--cloud-init", f"user-data={work / 'user-data'},meta-data={work / 'meta-data'}",
        ]
    elif spec.installer == "disk":
        _prepare_disk(spec, work)
        disk = f"path={spec.base_disk},format=qcow2,bus=virtio,discard=unmap"
        common = [disk if a.startswith(f"path={spec.base_disk},") else a for a in common]
        # --noreboot would only define the domain: an import has no install phase to end. The
        # first boot powers off by itself, which ends the wait.
        common.remove("--noreboot")
        extra = ["--import"]
    elif spec.installer == "windows":
        _windows_install(spec, name, common, work, deadline)
        return
    else:
        raise DeskError(f"unknown installer {spec.installer!r}")
    # The desktop installers are graphical; `desk image shot` shows their screen meanwhile.
    _log(f"installing {spec.name} ({spec.installer}); serial console in {log}")
    subprocess.run(common + extra, check=True)


def last_partition(parted_machine: str) -> int:
    """The number of the partition that ends last in `parted -sm ... print` output: the one that
    can grow (OpenWrt numbers its BIOS boot partition 128, at the start of the disk)."""
    rows = [line.rstrip(";").split(":") for line in parted_machine.splitlines()]
    parts = [(float(r[2].rstrip("s")), int(r[0])) for r in rows if len(r) >= 3 and r[0].isdigit()]
    if not parts:
        raise DeskError("the disk image has no partitions")
    return max(parts)[1]


def _disk_files_tar(spec: ImageSpec, out: Path) -> None:
    """`disk_files` as a tar of files owned by root, 0644 or 0755 whatever the checkout's umask
    (dropbear refuses keys in a group-writable /etc/dropbear), text files filled in like the other
    templates. No directory entries: directories already in the image keep their modes."""
    src = spec.path(spec.disk_files).resolve()
    with tarfile.open(out, "w") as tar:
        for f in sorted(src.rglob("*")):
            if f.is_dir() and not f.is_symlink():
                continue
            if f.is_symlink() or not f.is_file():
                raise DeskError(f"{f}: disk_files holds regular files and directories only")
            data = f.read_bytes()
            try:
                data = _fill(spec, data.decode(), str(f.relative_to(spec.dir))).encode()
            except UnicodeDecodeError:
                pass
            info = tarfile.TarInfo(str(f.relative_to(src)))
            info.size, info.mtime = len(data), int(f.stat().st_mtime)
            info.mode = 0o755 if f.stat().st_mode & 0o111 else 0o644
            info.uname = info.gname = "root"
            tar.addfile(info, io.BytesIO(data))


def _prepare_disk(spec: ImageSpec, work: Path) -> None:
    """The prebuilt image as the base disk: decompressed, grown to `disk_gb`, `disk_files` added."""
    image = _fetch_disk_image(spec)
    raw = work / "disk.img"
    with raw.open("wb") as out:
        # 2: gzip's warning about trailing data (some images carry a signature after the stream).
        r = subprocess.run(["gzip", "-dc", str(image)], stdout=out, stderr=subprocess.PIPE, check=False)
    if r.returncode not in (0, 2):
        raise DeskError(f"decompressing {image} failed: {r.stderr.decode(errors='replace').strip()}")
    subprocess.run(["qemu-img", "resize", "-q", "-f", "raw", str(raw), f"{spec.disk_gb}G"], check=True)
    # The GPT's backup header sits where the image ended; move it to the new end first.
    subprocess.run(["sgdisk", "-e", str(raw)], check=True, capture_output=True)
    table = subprocess.run(["parted", "-sm", str(raw), "unit", "s", "print"], capture_output=True, text=True,
                           check=True).stdout
    part = last_partition(table)
    subprocess.run(["parted", "-s", str(raw), "resizepart", str(part), "100%"], check=True)
    commands = [f"e2fsck-f /dev/sda{part}", f"resize2fs /dev/sda{part}"]
    if spec.disk_files:
        _disk_files_tar(spec, work / "disk-files.tar")
        commands += [f"mount /dev/sda{part} /", f"tar-in {work / 'disk-files.tar'} /"]
    _log(f"preparing {raw.name}: partition {part} grown to {spec.disk_gb} GB"
         + (f", {spec.disk_files} added" if spec.disk_files else ""))
    subprocess.run(["guestfish", "-a", str(raw)], input="run\n" + "\n".join(commands) + "\n", text=True, check=True)
    subprocess.run(["qemu-img", "convert", "-q", "-f", "raw", "-O", "qcow2", str(raw), str(spec.base_disk)], check=True)
    raw.unlink()


def _unattend_iso(spec: ImageSpec, work: Path) -> Path:
    """The answer-file CD: Windows Setup looks for Autounattend.xml at the root of every drive."""
    root = work / "unattend"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    xml = spec.path(spec.unattend).read_text().replace("@IMAGE_INDEX@", str(spec.image_index))
    (root / "Autounattend.xml").write_text(xml)
    for item in spec.unattend_files or []:
        src = spec.path(item).resolve()
        for f in ([src] if src.is_file() else sorted(src.iterdir())):
            shutil.copy(f, root / f.name)
    (root / "authorized_keys").write_text(_pubkey())
    shutil.copy(_fetch(spec, "openssh_msi", "openssh_msi"), root / "openssh.msi")
    virtio_win = _fetch(spec, "virtio_win_iso", "virtio_win")
    subprocess.run(["7z", "e", "-y", f"-o{root}", str(virtio_win), "virtio-win-guest-tools.exe"],
                   check=True, capture_output=True)
    # The virtio disk driver for Setup (DriverPaths in autounattend.xml).
    subprocess.run(["7z", "e", "-y", f"-o{root / 'drivers' / 'viostor'}", str(virtio_win),
                    "viostor/w11/amd64/*"], check=True, capture_output=True)
    iso = work / "unattend.iso"
    subprocess.run(["xorriso", "-as", "mkisofs", "-quiet", "-J", "-r", "-V", "DESKUNATTEND", "-o", str(iso),
                    str(root)], check=True)
    return iso


# Enter presses while the CD's loader waits for a key; the first one that arrives is enough.
CD_PROMPT_PRESSES = 3


def _windows_install(spec: ImageSpec, name: str, common: list[str], work: Path, deadline: float) -> None:
    iso = _fetch(spec, "iso", "iso")
    unattend = _unattend_iso(spec, work)
    # virtio disk with the driver from the answer-file CD: QEMU 10.2's AHCI (SATA) emulation
    # crashes when Windows restarts a port's command engine during a read (ahci_commit_buf,
    # fixed upstream in 11.1). e1000e has an inbox driver. No TPM: Windows would otherwise turn
    # on device encryption, and runs (which drop the TPM, see virt.py) would then stop at the
    # BitLocker recovery screen.
    args = [a.replace("model=virtio", "model=e1000e") for a in common]
    # Setup reboots several times, which virt-install's wait would take for the end.
    wait = args.index("--wait")
    args = [a for a in args[:wait] + args[wait + 2:] if a != "--noreboot"]
    # A USB keyboard: this OVMF build ignores the PS/2 one, so the CD boot prompt never saw a key.
    args += ["--cdrom", str(iso), "--disk", f"path={unattend},device=cdrom,bus=sata", "--tpm", "none",
             "--input", "keyboard,bus=usb"]
    _log(f"installing {spec.name} (windows); `desk image shot {spec.name}` shows the screen")
    subprocess.run(args, check=True)
    # The CD's loader asks "Press any key to boot from CD or DVD" for five seconds once the
    # firmware has started it, which the firmware says on the serial console. Keys go in only
    # then, and only a few: pressed for longer they wait in the keyboard's queue until Setup
    # reads them, where Enter hits the focused "Cancel" (seen once the ISO was in the page cache
    # and booted in seconds). Without that line, as before: during the first 15 seconds.
    log = LOGS / f"{name}.log"
    began = time.monotonic()
    started, presses = None, 0
    while time.monotonic() < began + 15 and presses < CD_PROMPT_PRESSES:
        if started is None and log.exists() and b"BdsDxe: starting" in log.read_bytes():
            started = time.monotonic()
        if started is not None or time.monotonic() > began + 8:
            dom = lookup(name)
            if dom is not None and dom.isActive():
                try:
                    display.key(dom, "ret")
                    presses += 1 if started is not None else 0
                except DeskError:
                    pass
            time.sleep(1 if started is not None else 0.5)
        else:
            time.sleep(0.1)
    # Setup's first reboot ends the install domain (virt-install's on_reboot=destroy); start it
    # again from the disk with the final configuration for the remaining passes and first logon.
    _wait_shutoff(name, timeout=max(60.0, deadline - time.monotonic()))
    _log("Windows Setup copied the image, booting the installed system")
    lookup(name).create()


def _wait_shutoff(name: str, timeout: float | None = None) -> None:
    deadline = time.monotonic() + timeout if timeout else None
    while True:
        dom = lookup(name)
        if dom is None:
            raise DeskError(f"build domain {name} disappeared")
        if not dom.isActive():
            return
        if deadline and time.monotonic() > deadline:
            # build() ends the install on this error, keeping what its screen showed.
            raise DeskError(f"{name} still running after {timeout:.0f}s; its last screen: {LOGS / f'{name}-stuck.png'}, "
                            f"its serial console: {LOGS / f'{name}.log'}")
        time.sleep(10)


def _finalize(spec: ImageSpec, name: str) -> None:
    """Keep the install domain's definition and firmware variables with the build, then drop it."""
    dom = lookup(name)
    xml = dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
    spec.base_xml.write_text(xml)
    nvram = ET.fromstring(xml).find("os/nvram").text
    shutil.copyfile(nvram, spec.base_nvram)
    dom.undefineFlags(libvirt.VIR_DOMAIN_UNDEFINE_NVRAM | libvirt.VIR_DOMAIN_UNDEFINE_TPM)


def _install(spec: ImageSpec, deadline: float) -> None:
    """Boot the installed system and run the install scripts on it as root, then power it off:
    over SSH, so a script that fails says why at once instead of leaving an install to time out."""
    port = free_port()
    name = f"desk-install-{spec.name}-{spec.base_version}"
    # Screens and console in logs/, which outlast a build that failed and is deleted.
    logs = LOGS / name
    logs.mkdir(parents=True, exist_ok=True)
    xml = prepare_domain_xml(spec.base_xml.read_text(), name=name, disk=spec.base_disk, nvram=spec.base_nvram,
                             ssh_port=port, console_log=logs / "console.log", memory_mib=spec.memory_mib,
                             vcpus=spec.vcpus)
    _log(f"booting {name} for the install scripts")
    dom = conn().createXML(xml, 0)
    try:
        if not _boot_and_wait_ssh(dom, port, spec, logs):
            raise DeskError(f"the installed system went away while booting; last screen: {logs / 'last-boot.png'}")
        run_scripts(spec, port, spec.install, {}, timeout=max(60.0, deadline - time.monotonic()))
        _power_off(dom, spec, port)
    except BaseException:
        try:
            if dom.isActive():
                (logs / "failed.png").write_bytes(display.screenshot(dom))
                _log(f"screen at failure: {logs / 'failed.png'}")
                dom.destroy()
        except Exception:  # noqa: BLE001 - the domain may be gone already
            pass
        raise


def _seal(spec: ImageSpec, extra: dict | None = None) -> None:
    spec.base_disk.chmod(0o444)
    meta = {"image": spec.name, "created": time.time(), "fingerprint": install_fingerprint(spec), **(extra or {})}
    (spec.base_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    _log(f"base image ready: {spec.base_dir}")


# A provisioning script that runs longer than this is taken to hang.
PROVISION_TIMEOUT = 3600
# In a warm build: the disk as provisioning left it after a clean shutdown. The warm state boots
# it once more; cold runs boot it too, so both start from the same files (runs.up).
PROVISIONED = "provisioned.qcow2"


def posix_tz(name: str) -> str:
    """The POSIX TZ string of a time zone, for OpenWrt, which has no zone files: the footer of the
    host's TZif file (version 2 on)."""
    for root in zoneinfo.TZPATH:
        f = Path(root) / name
        if f.is_file():
            data = f.read_bytes()
            footer = data[:-1].rpartition(b"\n")[2]
            if data[:4] == b"TZif" and data[4:5] >= b"2" and data.endswith(b"\n") and footer.isascii() \
                    and footer.decode().isprintable() and footer:
                return footer.decode()
    raise DeskError(f"no TZif file with a POSIX rule for {name} in {', '.join(zoneinfo.TZPATH)} (tzdata)")


def provision_env(spec: ImageSpec) -> dict[str, str]:
    """What the provisioning scripts are told about the host's choices."""
    env = {"DESK_TIMEZONE": spec.timezone, "DESK_LAYOUT": spec.layout}
    if spec.family == "openwrt":
        env["DESK_POSIX_TZ"] = posix_tz(spec.timezone)
    return env


def run_scripts(spec: ImageSpec, port: int, scripts: list[str], env: dict[str, str],
                timeout: float = PROVISION_TIMEOUT) -> None:
    """Run each script in the guest as root, with `env`, in order; the first that fails ends it."""
    for script in scripts:
        for attempt in (1, 2, 3):
            if spec.family == "windows":
                # Not piped to `-Command -`: PowerShell reads that like an interactive prompt and
                # silently skips a multi-line block at the end of the script.
                if guest.scp(port, str(spec.path(script)), f"{spec.root_user}@127.0.0.1:C:/desk/provision.ps1") != 0:
                    r = subprocess.CompletedProcess([], 255, b"", b"copying the script failed")
                else:
                    # The SSH session is PowerShell's, whose environment the script inherits.
                    r = guest.ssh_run(port, spec.root_user, "".join(f"$env:{k} = '{v}'; " for k, v in env.items())
                                      + "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\\desk\\provision.ps1",
                                      capture=True, timeout=timeout)
            else:
                r = guest.ssh_run(port, spec.root_user, "".join(f"{k}={shlex.quote(v)} " for k, v in env.items())
                                  + f"{spec.shell} -s", input=spec.path(script).read_bytes(), capture=True,
                                  timeout=timeout)
            # 255: the connection itself broke. Windows does that now and then during its first
            # boot after setup; the scripts are idempotent, so run it again.
            if r.returncode != 255 or attempt == 3:
                break
            _log(f"{_named(spec.path(script))}: connection lost, retrying")
            time.sleep(10)
            guest.wait_ssh(port, spec.user, timeout=300)
        if r.returncode != 0:
            # A package manager says why on its output, and its last line of errors only sums it up.
            tail = "\n".join(r.stdout.decode(errors="replace").strip().splitlines()[-15:])
            err = r.stderr.decode(errors="replace").strip()
            raise DeskError(f"{_named(spec.path(script))} failed ({r.returncode}): {err}"
                            + (f"\nits output ended with:\n{tail}" if tail else ""))


def provision(spec: ImageSpec, port: int, relogin: bool = True) -> None:
    if not spec.provision_scripts:
        return
    env = provision_env(spec)
    if spec.files and spec.family == "windows":
        # Windows guests keep the harness's helpers in C:\desk; scp speaks to its OpenSSH. Files
        # copied there from the answer-file CD are read-only, and the running agent should not
        # keep using the old copy: stop it and the loop that would start it again (agent.ps1,
        # agent-loop.ps1); the relogin starts them again.
        guest.ssh_run(port, spec.root_user, spec.cmd(
            "Get-CimInstance Win32_Process -Filter \"CommandLine LIKE '%desk\\\\agent%'\" | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; "
            "Get-ChildItem C:\\desk -File | ForEach-Object { $_.IsReadOnly = $false }"), capture=True, timeout=60)
        for d in spec.files:
            for f in sorted(spec.path(d).resolve().iterdir()):
                if guest.scp(port, str(f), f"{spec.root_user}@127.0.0.1:C:/desk/{f.name}") != 0:
                    raise DeskError(f"copying {f.name} to the guest failed")
    elif spec.files:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for d in spec.files:
                src = spec.path(d).resolve()
                for item in sorted(src.iterdir()):
                    tar.add(item, arcname=item.name)
        r = guest.ssh_run(port, spec.root_user, "rm -rf /tmp/desk-provision && mkdir -p /tmp/desk-provision/files && "
                          "tar -C /tmp/desk-provision/files -xf -", input=buf.getvalue(), capture=True)
        if r.returncode != 0:
            raise DeskError(f"copying provision files failed: {r.stderr.decode(errors='replace')}")
    run_scripts(spec, port, spec.provision_scripts, env)
    if relogin and spec.relogin:
        # GDM now and then does not log in automatically after a restart (seen on Ubuntu 26.04,
        # one warm-up in four), so give it a second restart before failing.
        # Windows relogs by rebooting, which takes minutes on a fresh install; a second reboot
        # sent while it boots does more harm than good.
        attempts = (1, 2) if spec.family == "linux" else (2,)
        for attempt in attempts:
            try:
                guest.ssh_run(port, spec.root_user, spec.cmd(spec.relogin), capture=True, timeout=120)
            except subprocess.TimeoutExpired:
                pass  # the restart took the connection down with it
            guest.wait_session_gone(port, spec.user, spec.cmd(spec.ready))
            try:
                guest.wait_ready(port, spec.user, spec.cmd(spec.ready), timeout=90 if attempt == 1 else 420)
                break
            except DeskError:
                if attempt == 2:
                    raise
                _log("session did not come back after relogin, restarting the display manager again")


def _power_off(dom, spec: ImageSpec, port: int, timeout: float = 600, after: str = "after provisioning") -> None:
    try:
        guest.ssh_run(port, spec.root_user, spec.cmd(spec.poweroff), capture=True, timeout=60)
    except subprocess.TimeoutExpired:
        pass  # the shutdown took the connection down with it
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if not dom.isActive():
                return
        except libvirt.libvirtError:
            return  # a transient domain is gone once it has shut down
        time.sleep(2)
    raise DeskError(f"the guest did not power off within {timeout:.0f}s {after}")


def _boot_and_wait_ssh(dom, port: int, spec: ImageSpec, wd: Path, timeout: float = 300) -> bool:
    """Wait for SSH while keeping the latest screen in last-boot.png; False if the domain died."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if not dom.isActive():
                return False
            (wd / "last-boot.png").write_bytes(display.screenshot(dom))
        except Exception:  # noqa: BLE001 - a vanished transient domain raises here
            return False
        try:
            guest.wait_ssh(port, spec.user, timeout=10)
            return True
        except DeskError:
            continue
    raise DeskError(f"SSH on port {port} not reachable after {timeout:.0f}s; last screen: {wd / 'last-boot.png'}")


def warm(spec: ImageSpec, force: bool = False, promote_it: bool = True) -> str:
    """Boot the base image, provision it and shut it down (the disk cold runs boot), boot that
    disk and save the running, logged-in desktop as a new warm build; returns its version."""
    refuse_derived(spec, "warm")
    if not spec.base_xml.exists():
        raise DeskError(f"base image {spec.name} not built yet (desk image build {spec.name})")
    link = WARM / spec.name
    if promote_it and current(link) and (link / "memory.save").exists() and not force:
        raise DeskError(f"warm state for {spec.name} exists (use --force to recreate)")
    base_dir = spec.base_dir.resolve()
    vspec = replace(spec, warm_version=new_version())
    wd = vspec.warm_dir
    wd.mkdir(parents=True)

    disk, nvram, provisioned = wd / "disk.qcow2", wd / "VARS.qcow2", wd / PROVISIONED
    qemu_img_overlay(base_dir / "disk.qcow2", provisioned)
    reflink_copy(base_dir / "VARS.qcow2", nvram)
    nvram.chmod(0o644)
    port = free_port()
    agent_ports = (free_port(), spec.agent_port) if spec.agent_port else None
    # Runs restored from this state inherit the name; the version keeps it apart from the
    # previous build's runs while a new build is tested.
    name = f"desk-warm-{spec.name}-{vspec.warm_version}"

    def boot(on: Path) -> None:
        nonlocal dom
        xml = prepare_domain_xml(
            (base_dir / "domain.xml").read_text(), name=name, disk=on, nvram=nvram, ssh_port=port,
            console_log=wd / "console.log",
            agent_ports=agent_ports, sound=spec.sound, memory_mib=spec.memory_mib, vcpus=spec.vcpus,
        )
        _log(f"booting {name} from {on.name}")
        dom = conn().createXML(xml, 0)
        if not _boot_and_wait_ssh(dom, port, spec, wd):
            # QEMU itself may die under the guest (it segfaulted in its AHCI emulation while
            # Windows was on a SATA disk); one more attempt is cheap.
            _log("the guest went away while booting, starting it once more")
            dom = conn().createXML(xml, 0)
            if not _boot_and_wait_ssh(dom, port, spec, wd):
                raise DeskError(f"the guest went away while booting twice; last screen: {wd / 'last-boot.png'}")
        guest.wait_ready(port, spec.user, spec.cmd(spec.ready))

    dom = None
    try:
        boot(provisioned)
        # A clean shutdown instead of a relogin: the next boot starts its session with
        # everything provisioning installed, the way a cold run does.
        provision(spec, port, relogin=False)
        _power_off(dom, spec, port)
        provisioned.chmod(0o444)
        qemu_img_overlay(provisioned, disk)
        boot(disk)
        if spec.prepare:
            _log(f"preparing the session ({_named(spec.path(spec.prepare))})")
            target = steps.Target(id=name, image=spec.name, dir=wd, ssh_port=port, user=spec.user,
                                  agent_port=agent_ports[0] if agent_ports else 0, domain=dom)
            steps.run_prepare(spec, target)
        if spec.idle:
            _log("waiting for the guest to settle")
            _settle(spec, port)
        # The first session start keeps drawing for a while (shell, extensions, indexers).
        time.sleep(15)
        _, stable = display.wait_stable(dom, timeout=60, frames=8, interval=0.5)
        if not stable:
            _log("warning: screen did not settle, saving anyway")
        (wd / "before-save.png").write_bytes(display.screenshot(dom))
        expires = _read_expiry(spec, port)
        # Dirty pages would otherwise live only in the saved RAM and reach the disk inside each
        # run's overlay, where they look like changes made by the run.
        guest.ssh_run(port, spec.root_user, spec.cmd(spec.sync), capture=True, timeout=120)
        _log("saving memory state")
        dom.save(str(wd / "memory.save"))
    except BaseException:
        # Best effort only: the original error is what matters, and the domain may be gone
        # already (a guest that powered itself off takes its transient domain with it).
        try:
            if dom is not None and dom.isActive():
                # What the screen showed is usually the quickest explanation of a failed warm-up.
                (wd / "failed.png").write_bytes(display.screenshot(dom))
                _log(f"screen at failure: {wd / 'failed.png'}")
                dom.destroy()
            else:
                _log("the guest powered off during the warm-up")
        except Exception:  # noqa: BLE001
            _log("the warm-up domain is gone (powered off or crashed)")
        raise
    disk.chmod(0o444)
    meta = {"image": spec.name, "created": time.time(), "base": str(base_dir),
            "fingerprint": warm_fingerprint(spec), "expires": expires, "timezone": spec.timezone,
            "layout": spec.layout}
    (wd / "meta.json").write_text(json.dumps(meta, indent=2))
    if promote_it:
        promote(link, wd)
    _log(f"warm state ready: {wd}")
    return vspec.warm_version


IDLE_TIMEOUT = 960


def _settle(spec: ImageSpec, port: int) -> None:
    """Run the image's `idle` script; a warm state saved from a guest that did not settle would
    hand every run its background work."""
    try:
        r = guest.ssh_run(port, spec.root_user, spec.cmd(spec.idle), capture=True, timeout=IDLE_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise DeskError(f"the guest did not settle before the warm state was saved: the idle script did not "
                        f"finish within {IDLE_TIMEOUT} s") from None
    if r.returncode != 0:
        err = r.stderr.decode(errors="replace").strip().splitlines()
        raise DeskError(f"the guest did not settle before the warm state was saved: the idle script "
                        f"exited {r.returncode}" + (f": {err[-1]}" if err else ""))


def _read_expiry(spec: ImageSpec, port: int) -> float | None:
    if not spec.expiry:
        return None
    r = guest.ssh_run(port, spec.root_user, spec.cmd(spec.expiry), capture=True, timeout=120)
    try:
        minutes = int(r.stdout.decode().strip())
    except ValueError:
        # Without it nothing would notice the licence running out (no warnings, no reinstall).
        raise DeskError(f"could not read the licence expiry: {r.stdout.decode(errors='replace').strip()!r} "
                        f"{r.stderr.decode(errors='replace').strip()}") from None
    _log(f"licence runs out in {minutes / 1440:.0f} days")
    return time.time() + minutes * 60
