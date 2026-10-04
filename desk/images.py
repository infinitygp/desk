"""Golden images: unattended install (base) and a saved, logged-in desktop (warm)."""

import hashlib
import io
import json
import shutil
import tarfile
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import libvirt

from . import display, guest
from .config import CACHE, LOGS, SSH_KEY, DeskError, ImageSpec
from .virt import conn, free_port, lookup, prepare_domain_xml, qemu_img_overlay, reflink_copy


def _log(msg: str) -> None:
    print(f"[desk] {msg}", flush=True)


def build_domain_name(spec: ImageSpec) -> str:
    return f"build-{spec.name}"


def build(spec: ImageSpec, force: bool = False) -> None:
    """Install the base image; re-running attaches to an install that is still in progress."""
    if spec.base:
        raise DeskError(f"{spec.name} is a variant of {spec.base}: "
                        f"build {spec.base}, then `desk image warm {spec.name}`")
    name = build_domain_name(spec)
    dom = lookup(name)
    if spec.base_xml.exists() and not force:
        raise DeskError(f"{spec.base_disk} already built (use --force to rebuild)")
    if dom is None:
        if spec.base_disk.exists():
            if not force:
                raise DeskError(f"{spec.base_disk} exists without a finished build (use --force)")
            spec.base_disk.chmod(0o644)
            spec.base_disk.unlink()
        for p in (spec.base_nvram, spec.base_xml):
            p.unlink(missing_ok=True)
        _virt_install(spec, name)
    else:
        _log(f"attaching to running build domain {name}")
    _wait_shutoff(name)
    _finalize(spec, name)


def _fetch_iso(spec: ImageSpec) -> Path:
    iso = CACHE / spec.iso
    if not iso.exists():
        if not spec.iso_url:
            raise DeskError(f"{iso} missing and no iso_url to fetch it from")
        _log(f"downloading {spec.iso_url}")
        subprocess.run(["curl", "-fL", "--progress-bar", "-o", f"{iso}.part", spec.iso_url], check=True)
        Path(f"{iso}.part").rename(iso)
    if spec.iso_sha256:
        h = hashlib.sha256()
        with iso.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != spec.iso_sha256:
            raise DeskError(f"{iso} does not match iso_sha256; delete it to download again")
    return iso


def _render(spec: ImageSpec, template: str) -> str:
    pubkey = SSH_KEY.with_suffix(".pub").read_text().strip()
    text = (spec.dir / template).read_text().replace("@SSH_PUBKEY@", pubkey)
    if "@PASSWORD_HASH@" in text:
        hashed = subprocess.run(["openssl", "passwd", "-6", spec.user], capture_output=True, text=True,
                                check=True).stdout.strip()
        text = text.replace("@PASSWORD_HASH@", hashed)
    return text


def _virt_install(spec: ImageSpec, name: str) -> None:
    work = CACHE / spec.name
    work.mkdir(parents=True, exist_ok=True)
    log = LOGS / f"{name}.log"
    common = [
        "virt-install", "--connect", conn().getURI(), "--name", name,
        "--memory", str(spec.memory_mib), "--vcpus", str(spec.vcpus),
        "--cpu", "host-passthrough", "--machine", "q35", "--boot", "uefi",
        "--disk", f"path={spec.base_disk},size={spec.disk_gb},format=qcow2,bus=virtio,discard=unmap",
        "--os-variant", spec.os_variant,
        "--network", "type=user,backend.type=passt,model=virtio",
        "--graphics", "none", "--video", "virtio",
        "--serial", f"file,path={log}",
        "--noautoconsole", "--noreboot", "--wait", "-1",
    ]
    if spec.installer == "kickstart":
        (work / "ks.cfg").write_text(_render(spec, spec.kickstart))
        extra = [
            "--location", spec.location,
            "--initrd-inject", str(work / "ks.cfg"),
            "--extra-args", "inst.ks=file:/ks.cfg console=ttyS0 inst.text",
        ]
    elif spec.installer == "autoinstall":
        iso = _fetch_iso(spec)
        (work / "user-data").write_text(_render(spec, spec.user_data))
        (work / "meta-data").write_text(f"instance-id: iid-{spec.name}\n")
        extra = [
            "--location", f"{iso},kernel={spec.kernel},initrd={spec.initrd}",
            "--extra-args", spec.extra_args,
            "--cloud-init", f"user-data={work / 'user-data'},meta-data={work / 'meta-data'}",
        ]
    elif spec.installer == "windows":
        _windows_install(spec, name, common, work)
        return
    else:
        raise DeskError(f"unknown installer {spec.installer!r}")
    # The desktop installers are graphical; `desk image shot` shows their screen meanwhile.
    _log(f"installing {spec.name} ({spec.installer}); serial console in {log}")
    subprocess.run(common + extra, check=True)


def _unattend_iso(spec: ImageSpec, work: Path) -> Path:
    """The answer-file CD: Windows Setup looks for Autounattend.xml at the root of every drive."""
    root = work / "unattend"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    xml = (spec.dir / spec.unattend).read_text().replace("@IMAGE_INDEX@", str(spec.image_index))
    (root / "Autounattend.xml").write_text(xml)
    for item in spec.unattend_files or []:
        src = (spec.dir / item).resolve()
        for f in ([src] if src.is_file() else sorted(src.iterdir())):
            shutil.copy(f, root / f.name)
    (root / "authorized_keys").write_text(SSH_KEY.with_suffix(".pub").read_text())
    shutil.copy(CACHE / spec.openssh_msi, root / "openssh.msi")
    subprocess.run(["7z", "e", "-y", f"-o{root}", str(CACHE / spec.virtio_win_iso), "virtio-win-guest-tools.exe"],
                   check=True, capture_output=True)
    iso = work / "unattend.iso"
    subprocess.run(["xorriso", "-as", "mkisofs", "-quiet", "-J", "-r", "-V", "DESKUNATTEND", "-o", str(iso),
                    str(root)], check=True)
    return iso


def _windows_install(spec: ImageSpec, name: str, common: list[str], work: Path) -> None:
    iso = _fetch_iso(spec)
    unattend = _unattend_iso(spec, work)
    # SATA disk and e1000e use inbox drivers, so Setup needs nothing loaded. No TPM: Windows
    # would otherwise turn on device encryption, and runs (which drop the TPM, see virt.py)
    # would then stop at the BitLocker recovery screen.
    args = [a.replace("bus=virtio,", "bus=sata,").replace("model=virtio", "model=e1000e") for a in common]
    args = [a for a in args if a not in ("--noreboot", "--wait", "-1")]
    # A USB keyboard: this OVMF build ignores the PS/2 one, so the CD boot prompt never saw a key.
    args += ["--cdrom", str(iso), "--disk", f"path={unattend},device=cdrom,bus=sata", "--tpm", "none",
             "--input", "keyboard,bus=usb"]
    _log(f"installing {spec.name} (windows); `desk image shot {spec.name}` shows the screen")
    subprocess.run(args, check=True)
    # OVMF asks "Press any key to boot from CD or DVD" during the first seconds. Stop well before
    # Setup's own screens, where Enter would hit their focused "Cancel" button.
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        dom = lookup(name)
        if dom is not None and dom.isActive():
            try:
                display.key(dom, "ret")
            except DeskError:
                pass
        time.sleep(0.5)
    # Setup's first reboot ends the install domain (virt-install's on_reboot=destroy); start it
    # again from the disk with the final configuration for the remaining passes and first logon.
    _wait_shutoff(name, timeout=3600)
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
            raise DeskError(f"{name} still running after {timeout:.0f}s (desk image shot to see why)")
        time.sleep(10)


def _finalize(spec: ImageSpec, name: str) -> None:
    dom = lookup(name)
    xml = dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
    spec.base_xml.write_text(xml)
    nvram = ET.fromstring(xml).find("os/nvram").text
    shutil.copyfile(nvram, spec.base_nvram)
    dom.undefineFlags(libvirt.VIR_DOMAIN_UNDEFINE_NVRAM | libvirt.VIR_DOMAIN_UNDEFINE_TPM)
    spec.base_disk.chmod(0o444)
    _log(f"base image ready: {spec.base_disk}")


def provision(spec: ImageSpec, port: int) -> None:
    if not spec.provision_scripts:
        return
    if spec.files and spec.family == "windows":
        # Windows guests keep the harness's helpers in C:\desk; scp speaks to its OpenSSH. Files
        # copied there from the answer-file CD are read-only, and the running agent should not
        # keep using the old copy: stop it, the relogin starts it again.
        guest.ssh_run(port, spec.root_user, spec.cmd(
            "Get-CimInstance Win32_Process -Filter \"CommandLine LIKE '%desk\\\\agent.ps1%'\" | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; "
            "Get-ChildItem C:\\desk -File | ForEach-Object { $_.IsReadOnly = $false }"), capture=True, timeout=60)
        for d in spec.files:
            for f in sorted((spec.dir / d).resolve().iterdir()):
                if guest.scp(port, str(f), f"{spec.root_user}@127.0.0.1:C:/desk/{f.name}") != 0:
                    raise DeskError(f"copying {f.name} to the guest failed")
    elif spec.files:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for d in spec.files:
                src = (spec.dir / d).resolve()
                for item in sorted(src.iterdir()):
                    tar.add(item, arcname=item.name)
        r = guest.ssh_run(port, spec.root_user, "rm -rf /tmp/desk-provision && mkdir -p /tmp/desk-provision/files && "
                          "tar -C /tmp/desk-provision/files -xf -", input=buf.getvalue(), capture=True)
        if r.returncode != 0:
            raise DeskError(f"copying provision files failed: {r.stderr.decode(errors='replace')}")
    # PowerShell runs a script piped to `-Command -` line by line, like `bash -s`.
    runner = ("powershell.exe -NoProfile -ExecutionPolicy Bypass -Command -" if spec.family == "windows"
              else "bash -s")
    for script in spec.provision_scripts:
        for attempt in (1, 2, 3):
            r = guest.ssh_run(port, spec.root_user, runner, input=(spec.dir / script).read_bytes(), capture=True)
            # 255: the connection itself broke. Windows does that now and then during its first
            # boot after setup; provisioning scripts are idempotent, so run it again.
            if r.returncode != 255 or attempt == 3:
                break
            _log(f"{script}: connection lost, retrying")
            time.sleep(10)
            guest.wait_ssh(port, spec.user, timeout=300)
        if r.returncode != 0:
            raise DeskError(f"{script} failed ({r.returncode}): {r.stderr.decode(errors='replace')}")
    if spec.relogin:
        # GDM on Ubuntu 26.04 now and then does not log in automatically after a restart (one
        # warm-up in four here), so give it a second restart before failing.
        # Windows relogs by rebooting, which takes minutes on a fresh install; a second reboot
        # sent while it boots does more harm than good.
        attempts = (1, 2) if spec.family == "linux" else (2,)
        for attempt in attempts:
            guest.ssh_run(port, spec.root_user, spec.cmd(spec.relogin), capture=True)
            guest.wait_session_gone(port, spec.user, spec.cmd(spec.ready))
            try:
                guest.wait_ready(port, spec.user, spec.cmd(spec.ready), timeout=90 if attempt == 1 else 420)
                break
            except DeskError:
                if attempt == 2:
                    raise
                _log("session did not come back after relogin, restarting the display manager again")


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


def warm(spec: ImageSpec, force: bool = False) -> None:
    """Boot the base image, provision it and save the running, logged-in desktop."""
    if not spec.base_xml.exists():
        raise DeskError(f"base image {spec.name} not built yet (desk image build {spec.name})")
    wd = spec.warm_dir
    if (wd / "memory.save").exists() and not force:
        raise DeskError(f"warm state for {spec.name} exists (use --force to recreate)")
    if wd.exists():
        for p in wd.iterdir():
            p.chmod(0o644)
        shutil.rmtree(wd)
    wd.mkdir(parents=True)

    disk, nvram = wd / "disk.qcow2", wd / "VARS.qcow2"
    qemu_img_overlay(spec.base_disk, disk)
    reflink_copy(spec.base_nvram, nvram)
    nvram.chmod(0o644)
    port = free_port()
    name = f"desk-warm-{spec.name}"
    xml = prepare_domain_xml(
        spec.base_xml.read_text(), name=name, disk=disk, nvram=nvram, ssh_port=port,
        console_log=wd / "console.log",
        agent_ports=(free_port(), spec.agent_port) if spec.agent_port else None, sound=spec.sound,
    )
    _log(f"booting {name}")
    dom = conn().createXML(xml, 0)
    try:
        if not _boot_and_wait_ssh(dom, port, spec, wd):
            # Windows' first boot after setup has twice taken its domain down before SSH came
            # up (cause not caught yet; see last-boot.png). The next attempt worked each time.
            _log("the guest went away while booting, starting it once more")
            dom = conn().createXML(xml, 0)
            if not _boot_and_wait_ssh(dom, port, spec, wd):
                raise DeskError(f"the guest went away while booting twice; last screen: {wd / 'last-boot.png'}")
        guest.wait_ready(port, spec.user, spec.cmd(spec.ready))
        provision(spec, port)
        if spec.idle:
            _log("waiting for the guest to settle")
            guest.ssh_run(port, spec.root_user, spec.cmd(spec.idle), capture=True, timeout=420)
        # The first session start keeps drawing for a while (shell, extensions, indexers).
        time.sleep(15)
        _, stable = display.wait_stable(dom, timeout=60, frames=8, interval=0.5)
        if not stable:
            _log("warning: screen did not settle, saving anyway")
        (wd / "before-save.png").write_bytes(display.screenshot(dom))
        # Dirty pages would otherwise live only in the saved RAM and reach the disk inside each
        # run's overlay, where they look like changes made by the run.
        guest.ssh_run(port, spec.root_user, spec.cmd(spec.sync), capture=True, timeout=120)
        _log("saving memory state")
        dom.save(str(wd / "memory.save"))
    except BaseException:
        # Best effort only: the original error is what matters, and the domain may be gone
        # already (a guest that powered itself off takes its transient domain with it).
        try:
            if dom.isActive():
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
    meta = {"image": spec.name, "created": time.time(), "base_mtime_ns": spec.base_disk.stat().st_mtime_ns}
    (wd / "meta.json").write_text(json.dumps(meta, indent=2))
    _log(f"warm state ready: {wd}")
