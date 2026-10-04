"""Golden images: unattended install (base) and a saved, logged-in desktop (warm)."""

import json
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET

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


def _virt_install(spec: ImageSpec, name: str) -> None:
    ks_dir = CACHE / spec.name
    ks_dir.mkdir(parents=True, exist_ok=True)
    pubkey = SSH_KEY.with_suffix(".pub").read_text().strip()
    ks = (spec.dir / spec.kickstart).read_text().replace("@SSH_PUBKEY@", pubkey)
    (ks_dir / "ks.cfg").write_text(ks)
    log = LOGS / f"{name}.log"
    _log(f"installing {spec.name}; serial console in {log}")
    subprocess.run(
        [
            "virt-install", "--connect", conn().getURI(), "--name", name,
            "--memory", str(spec.memory_mib), "--vcpus", str(spec.vcpus),
            "--cpu", "host-passthrough", "--machine", "q35", "--boot", "uefi",
            "--disk", f"path={spec.base_disk},size={spec.disk_gb},format=qcow2,bus=virtio,discard=unmap",
            "--location", spec.location,
            "--initrd-inject", str(ks_dir / "ks.cfg"),
            "--extra-args", "inst.ks=file:/ks.cfg console=ttyS0 inst.text",
            "--os-variant", spec.os_variant,
            "--network", "type=user,backend.type=passt,model=virtio",
            "--graphics", "none", "--video", "virtio",
            "--serial", f"file,path={log}",
            "--noautoconsole", "--noreboot", "--wait", "-1",
        ],
        check=True,
    )


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
    if not spec.provision:
        return
    files = spec.dir / "files"
    if files.is_dir():
        guest.ssh_run(port, spec.user, "rm -rf /tmp/desk-provision && mkdir -p /tmp/desk-provision", capture=True)
        if guest.scp(port, str(files), f"{spec.user}@127.0.0.1:/tmp/desk-provision/") != 0:
            raise DeskError("copying provision files failed")
    script = (spec.dir / spec.provision).read_bytes()
    r = guest.ssh_run(port, guest.ROOT, "bash -s", input=script, capture=True)
    if r.returncode != 0:
        raise DeskError(f"provision script failed ({r.returncode}): {r.stderr.decode(errors='replace')}")
    if spec.relogin:
        guest.ssh_run(port, guest.ROOT, spec.relogin, capture=True)
        guest.wait_session_gone(port, spec.user, spec.ready)
        guest.wait_ready(port, spec.user, spec.ready)


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
        _log("saving memory state")
        dom.save(str(wd / "memory.save"))
    except BaseException:
        if dom.isActive():
            dom.destroy()
        raise
    disk.chmod(0o444)
    meta = {"image": spec.name, "created": time.time(), "base_mtime_ns": spec.base_disk.stat().st_mtime_ns}
    (wd / "meta.json").write_text(json.dumps(meta, indent=2))
    _log(f"warm state ready: {wd}")
