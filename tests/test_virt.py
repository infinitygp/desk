import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

import libvirt

from desk.virt import domain_size, drop_usb_keyboard, prepare_domain_xml

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
    USB = "<input type='keyboard' bus='usb'><alias name='input0'/><address type='usb' bus='0' port='1'/></input>"

    def dom(self, *inputs: str) -> mock.Mock:
        return mock.Mock(**{"XMLDesc.return_value": f"<domain><devices>{''.join(inputs)}</devices></domain>"})

    def test_keys_go_through_ps2_where_there_is_one(self):
        dom = self.dom(self.USB, "<input type='mouse' bus='ps2'/>", "<input type='keyboard' bus='ps2'/>")
        drop_usb_keyboard(dom)
        xml, flags = dom.detachDeviceFlags.call_args.args
        self.assertEqual(ET.fromstring(xml).find("alias").get("name"), "input0")
        self.assertEqual(flags, libvirt.VIR_DOMAIN_AFFECT_LIVE)

    def test_a_usb_keyboard_alone_stays(self):
        for inputs in ((self.USB,), ("<input type='keyboard' bus='ps2'/>",)):
            dom = self.dom(*inputs)
            drop_usb_keyboard(dom)
            dom.detachDeviceFlags.assert_not_called()

    def test_a_failed_unplug_is_said_and_the_run_goes_on(self):
        dom = self.dom(self.USB, "<input type='keyboard' bus='ps2'/>")
        dom.detachDeviceFlags.side_effect = libvirt.libvirtError("no")
        with mock.patch("builtins.print") as said:
            drop_usb_keyboard(dom)
        self.assertIn("keys go through the USB keyboard", said.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
