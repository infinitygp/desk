import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from desk.virt import prepare_domain_xml

TEMPLATE = """
<domain type='kvm'>
  <name>build-x</name>
  <uuid>11111111-2222-3333-4444-555555555555</uuid>
  <seclabel type='dynamic' model='selinux'/>
  <os firmware='efi'>
    <type arch='x86_64' machine='q35'>hvm</type>
    <nvram format='qcow2'>/old/VARS.qcow2</nvram>
    <kernel>/k</kernel><initrd>/i</initrd><cmdline>inst.ks=file:/ks.cfg</cmdline>
  </os>
  <devices>
    <disk type='file' device='disk'>
      <source file='/old/disk.qcow2'/>
      <backingStore type='file'><source file='/old/base.qcow2'/></backingStore>
      <target dev='vda' bus='virtio'/>
    </disk>
    <disk type='file' device='cdrom'><target dev='sda' bus='sata'/></disk>
    <interface type='user'>
      <backend type='passt'/>
      <portForward proto='tcp'><range start='1' to='22'/></portForward>
    </interface>
    <serial type='file'><source path='/old/console.log'/></serial>
    <channel type='unix'>
      <source mode='bind' path='/run/old'/>
      <target type='virtio' name='org.qemu.guest_agent.0' state='connected'/>
    </channel>
    <tpm model='tpm-crb'/>
    <graphics type='vnc'/>
    <input type='mouse' bus='ps2'/>
  </devices>
</domain>
"""


def prepare(**kw):
    args = dict(name="desk-r1", disk=Path("/runs/r1/disk.qcow2"), nvram=Path("/runs/r1/VARS.qcow2"),
                ssh_port=40000, console_log=Path("/runs/r1/console.log"))
    args.update(kw)
    return ET.fromstring(prepare_domain_xml(TEMPLATE, **args))


class PrepareDomainXml(unittest.TestCase):
    def test_identity(self):
        root = prepare()
        self.assertEqual(root.findtext("name"), "desk-r1")
        self.assertNotEqual(root.findtext("uuid"), "11111111-2222-3333-4444-555555555555")
        self.assertEqual(prepare(keep_uuid=True).findtext("uuid"), "11111111-2222-3333-4444-555555555555")
        self.assertIsNone(root.find("seclabel"))

    def test_boot_and_storage(self):
        root = prepare()
        for tag in ("kernel", "initrd", "cmdline"):
            self.assertIsNone(root.find(f"os/{tag}"))
        self.assertEqual(root.findtext("os/nvram"), "/runs/r1/VARS.qcow2")
        disks = root.findall("devices/disk")
        self.assertEqual(len(disks), 1, "cdrom must be dropped")
        self.assertEqual(disks[0].find("source").get("file"), "/runs/r1/disk.qcow2")
        self.assertIsNone(disks[0].find("backingStore"))

    def test_devices(self):
        root = prepare()
        pfs = root.findall("devices/interface/portForward")
        self.assertEqual(len(pfs), 1)
        self.assertEqual(pfs[0].get("address"), "127.0.0.1")
        self.assertEqual(pfs[0].find("range").attrib, {"start": "40000", "to": "22"})
        self.assertEqual(root.find("devices/serial/source").get("path"), "/runs/r1/console.log")
        self.assertIsNone(root.find("devices/tpm"))
        self.assertIsNone(root.find("devices/graphics"))
        self.assertTrue(any(i.get("type") == "tablet" for i in root.findall("devices/input")))
        ch = root.find("devices/channel")
        self.assertNotIn("path", ch.find("source").attrib)
        self.assertNotIn("state", ch.find("target").attrib)


if __name__ == "__main__":
    unittest.main()
