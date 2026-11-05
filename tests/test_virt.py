import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from desk.virt import domain_size, prepare_domain_xml

TEMPLATE = """
<domain type='kvm'>
  <name>build-x</name>
  <uuid>11111111-2222-3333-4444-555555555555</uuid>
  <memory unit='KiB'>4194304</memory>
  <currentMemory unit='KiB'>4194304</currentMemory>
  <vcpu placement='static'>4</vcpu>
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
    <video><model type='virtio' heads='1' primary='yes'/></video>
    <input type='mouse' bus='ps2'/>
  </devices>
</domain>
"""


ARGS = dict(name="desk-r1", disk=Path("/runs/r1/disk.qcow2"), nvram=Path("/runs/r1/VARS.qcow2"),
            ssh_port=40000, console_log=Path("/runs/r1/console.log"))


def prepare(**kw):
    return ET.fromstring(prepare_domain_xml(TEMPLATE, **{**ARGS, **kw}))


class PrepareDomainXml(unittest.TestCase):
    def test_monitors_are_one_gpu_each(self):
        root = prepare(displays=3, resolution=(1920, 1080))
        models = [v.find("model") for v in root.findall("devices/video")]
        self.assertEqual([m.get("heads") for m in models], ["1", "1", "1"])
        self.assertEqual(models[0].get("primary"), "yes")
        self.assertEqual([(m.find("resolution").get("x"), m.find("resolution").get("y")) for m in models],
                         [("1920", "1080")] * 3)

    def test_default_keeps_the_images_monitor(self):
        root = prepare()
        self.assertEqual(len(root.findall("devices/video")), 1)
        self.assertIsNone(root.find("devices/video/model/resolution"))

    def test_identity(self):
        root = prepare()
        self.assertEqual(root.findtext("name"), "desk-r1")
        self.assertNotEqual(root.findtext("uuid"), "11111111-2222-3333-4444-555555555555")
        self.assertEqual(prepare(keep_uuid=True).findtext("uuid"), "11111111-2222-3333-4444-555555555555")
        self.assertIsNone(root.find("seclabel"))

    def test_size(self):
        self.assertEqual(domain_size(prepare_domain_xml(TEMPLATE, **ARGS)), (4096, 4))
        root = prepare(memory_mib=8192, vcpus=6)
        self.assertEqual([root.findtext(t) for t in ("memory", "currentMemory", "vcpu")], ["8388608", "8388608", "6"])
        self.assertEqual(root.find("vcpu").get("placement"), "static")
        self.assertEqual(root.find("cpu/topology").attrib, {"sockets": "1", "cores": "6", "threads": "1"})
        # The count a warm state was saved with comes back as it was saved.
        self.assertIsNone(prepare(memory_mib=4096, vcpus=4).find("cpu"))

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


class UsbKeyboard(unittest.TestCase):
    def keyboards(self, *buses: str, **kw) -> list[str]:
        inputs = "".join(f"<input type='keyboard' bus='{b}'/>" for b in buses)
        root = ET.fromstring(prepare_domain_xml(TEMPLATE.replace("<input type='mouse' bus='ps2'/>", inputs),
                                                **{**ARGS, **kw}))
        return [i.get("bus") for i in root.findall("devices/input") if i.get("type") == "keyboard"]

    def test_keys_go_through_ps2_where_there_is_one(self):
        self.assertEqual(self.keyboards("usb", "ps2"), ["ps2"])

    def test_a_usb_keyboard_alone_stays(self):
        self.assertEqual(self.keyboards("usb"), ["usb"])

    def test_a_save_image_keeps_its_devices(self):
        self.assertEqual(self.keyboards("usb", "ps2", keep_uuid=True), ["usb", "ps2"])


class SharedMemory(unittest.TestCase):
    TEMPLATE = ("<domain><name>x</name><memory unit='KiB'>1024</memory><os><nvram>n</nvram></os>"
                "<devices><disk device='disk'><source file='a'/></disk></devices></domain>")

    def xml(self, **kw):
        from desk.virt import prepare_domain_xml
        return ET.fromstring(prepare_domain_xml(self.TEMPLATE, name="d", disk=Path("/d"), nvram=Path("/n"),
                                                ssh_port=1, console_log=Path("/c"), **kw))

    def test_guest_ram_is_a_shared_memfd_only_when_asked(self):
        self.assertIsNone(self.xml().find("memoryBacking"))
        backing = self.xml(shared_memory=True).find("memoryBacking")
        self.assertEqual(backing.find("source").get("type"), "memfd")
        self.assertEqual(backing.find("access").get("mode"), "shared")


if __name__ == "__main__":
    unittest.main()
