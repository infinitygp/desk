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
    else:
        raise DeskError(f"unknown installer {spec.installer!r}")
    # The desktop installers are graphical; `desk image watch` shows their screen meanwhile.
    _log(f"installing {spec.name} ({spec.installer}); serial console in {log}")
    subprocess.run(common + extra, check=True)


def _wait_shutoff(name: str) -> None:
    while True:
        dom = lookup(name)
        if dom is None:
            raise DeskError(f"build domain {name} disappeared")
        if not dom.isActive():
            return
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
    if spec.files:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for d in spec.files:
                src = (spec.dir / d).resolve()
                for item in sorted(src.iterdir()):
                    tar.add(item, arcname=item.name)
        r = guest.ssh_run(port, guest.ROOT, "rm -rf /tmp/desk-provision && mkdir -p /tmp/desk-provision/files && "
                          "tar -C /tmp/desk-provision/files -xf -", input=buf.getvalue(), capture=True)
        if r.returncode != 0:
            raise DeskError(f"copying provision files failed: {r.stderr.decode(errors='replace')}")
    for script in spec.provision_scripts:
        r = guest.ssh_run(port, guest.ROOT, "bash -s", input=(spec.dir / script).read_bytes(), capture=True)
        if r.returncode != 0:
            raise DeskError(f"{script} failed ({r.returncode}): {r.stderr.decode(errors='replace')}")
    if spec.relogin:
        # GDM on Ubuntu 26.04 now and then does not log in automatically after a restart (one
        # warm-up in four here), so give it a second restart before failing.
        for attempt in (1, 2):
            guest.ssh_run(port, guest.ROOT, spec.relogin, capture=True)
            guest.wait_session_gone(port, spec.user, spec.ready)
            try:
                guest.wait_ready(port, spec.user, spec.ready, timeout=90 if attempt == 1 else 300)
                break
            except DeskError:
                if attempt == 2:
                    raise
                _log("session did not come back after relogin, restarting the display manager again")


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
    )
    _log(f"booting {name}")
    dom = conn().createXML(xml, 0)
    try:
        guest.wait_ssh(port, spec.user, timeout=300)
        guest.wait_ready(port, spec.user, spec.ready)
        provision(spec, port)
        # The first session start keeps drawing for a while (shell, extensions, indexers).
        time.sleep(15)
        _, stable = display.wait_stable(dom, timeout=60, frames=8, interval=0.5)
        if not stable:
            _log("warning: screen did not settle, saving anyway")
        (wd / "before-save.png").write_bytes(display.screenshot(dom))
        # Dirty pages would otherwise live only in the saved RAM and reach the disk inside each
        # run's overlay, where they look like changes made by the run.
        guest.ssh_run(port, guest.ROOT, "sync", capture=True, timeout=120)
        _log("saving memory state")
        dom.save(str(wd / "memory.save"))
    except BaseException:
        if dom.isActive():
            # What the screen showed is usually the quickest explanation of a failed warm-up.
            try:
                (wd / "failed.png").write_bytes(display.screenshot(dom))
                _log(f"screen at failure: {wd / 'failed.png'}")
            except Exception:  # noqa: BLE001 - best effort while already failing
                pass
            dom.destroy()
        raise
    disk.chmod(0o444)
    meta = {"image": spec.name, "created": time.time(), "base_mtime_ns": spec.base_disk.stat().st_mtime_ns}
    (wd / "meta.json").write_text(json.dumps(meta, indent=2))
    _log(f"warm state ready: {wd}")
