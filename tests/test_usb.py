import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from desk import cli, doctor, usb
from desk.config import DeskError


def fake_sys(root: Path, name: str, **attrs) -> None:
    d = root / name
    d.mkdir()
    for k, v in attrs.items():
        (d / k).write_text(f"{v}\n")


class Devices(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        fake_sys(root, "usb3", idVendor="1d6b", idProduct="0003", bDeviceClass="09", busnum=3, devnum=1)
        fake_sys(root, "3-1", idVendor="0fd9", idProduct="0084", bDeviceClass="00", busnum=3, devnum=7,
                 manufacturer="Elgato", product="Stream Deck +")
        fake_sys(root, "3-1:1.0", bInterfaceClass="03")  # an interface, not a device
        self.devices = usb.host_devices(root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_lists_devices_without_hubs_and_interfaces(self):
        self.assertEqual([(d["id"], d["bus"], d["dev"], d["name"], d["node"]) for d in self.devices],
                         [("0fd9:0084", 3, 7, "Elgato Stream Deck +", "/dev/bus/usb/003/007")])

    def test_picks_by_id_or_address(self):
        self.assertEqual(usb.pick(self.devices, "0FD9:0084")["dev"], 7)
        self.assertEqual(usb.pick(self.devices, "3.7")["id"], "0fd9:0084")
        with self.assertRaisesRegex(DeskError, "no USB device 1234:5678"):
            usb.pick(self.devices, "1234:5678")
        with self.assertRaisesRegex(DeskError, "VENDOR:PRODUCT"):
            usb.pick(self.devices, "deck")

    def test_two_alike_need_the_address(self):
        twins = self.devices + [{**self.devices[0], "dev": 8}]
        with self.assertRaisesRegex(DeskError, "3.7, 3.8; give BUS.DEVICE"):
            usb.pick(twins, "0fd9:0084")


DECK = {"id": "0fd9:0084", "bus": 3, "dev": 7, "node": "/dev/bus/usb/003/007", "name": "Stream Deck +",
        "access": True}


def domain(*addresses):
    devs = "".join(usb._xml(b, d) for b, d in addresses)
    return mock.Mock(**{"XMLDesc.return_value": f"<domain><devices>{devs}</devices></domain>"})


class AddRemove(unittest.TestCase):
    def setUp(self):
        capability = mock.patch.object(doctor, "_qemu_devices", return_value='name "usb-host", bus USB\n')
        capability.start()
        self.addCleanup(capability.stop)

    def test_missing_backend_refuses_add_before_touching_the_guest(self):
        run = mock.Mock()
        with mock.patch.object(doctor, "_qemu_devices", return_value='name "qemu-xhci", bus PCI\n'), \
             mock.patch.object(usb, "host_devices") as devices, \
             self.assertRaisesRegex(DeskError, "QEMU usb-host.*qemu-hw-usb-host.*qemu-device-usb-host"):
            usb.add(run, "0fd9:0084")
        devices.assert_not_called()
        run.dom.assert_not_called()

    def test_missing_backend_refuses_up_usb_before_starting_a_run(self):
        args = SimpleNamespace(resolution=None, image="windows-11", memory=None, cpus=None, displays=1,
                               scale=None, disk=[], usb=["0fd9:0084"])
        with mock.patch.object(doctor, "_qemu_devices", return_value=""), \
             mock.patch.object(cli, "load_image"), mock.patch("desk.monitors.check"), \
             mock.patch("desk.runs.up") as up, self.assertRaisesRegex(DeskError, "QEMU usb-host"):
            cli.cmd_up(args)
        up.assert_not_called()

    def test_up_without_usb_does_not_probe_host_passthrough(self):
        args = SimpleNamespace(resolution=None, image="windows-11", memory=None, cpus=None, displays=1,
                               scale=None, disk=[], usb=[], name=None, cold=False, force=False, label=None,
                               record=False, forward=[])
        with mock.patch.object(doctor, "_qemu_devices", side_effect=AssertionError("USB probe on an ordinary run")), \
             mock.patch.object(cli, "load_image"), mock.patch("desk.monitors.check"), \
             mock.patch("desk.runs.up") as up, mock.patch("builtins.print"):
            cli.cmd_up(args)
        up.assert_called_once()

    def test_listing_and_removal_do_not_need_the_backend(self):
        run = mock.Mock(**{"dom.return_value": domain((3, 7))})
        with mock.patch.object(doctor, "_qemu_devices", side_effect=AssertionError("USB backend probe")), \
             mock.patch.object(usb, "host_devices", return_value=[DECK]), \
             mock.patch("desk.runs.all_runs", return_value=[]):
            self.assertEqual(usb.listing(), [{**DECK, "run": None}])
            self.assertEqual(usb.remove(run, "0fd9:0084"), DECK)
        run.dom.return_value.detachDeviceFlags.assert_called_once()

    def test_refuses_a_node_this_user_cannot_open_and_says_how(self):
        with mock.patch.object(usb, "host_devices", return_value=[{**DECK, "access": False}]), \
             self.assertRaisesRegex(DeskError, 'ATTRS{idVendor}=="0fd9".*GROUP="kvm"'):
            usb.add(mock.Mock(), "0fd9:0084")

    def test_refuses_a_device_another_run_has(self):
        other = SimpleNamespace(id="b2", live_domain=lambda: domain((3, 7)))
        with mock.patch.object(usb, "host_devices", return_value=[DECK]), \
             mock.patch("desk.runs.all_runs", return_value=[other]), \
             self.assertRaisesRegex(DeskError, "in run b2 already"):
            usb.add(mock.Mock(), "0fd9:0084")

    def test_plugs_in_by_address(self):
        run = mock.Mock()
        with mock.patch.object(usb, "host_devices", return_value=[DECK]), \
             mock.patch("desk.runs.all_runs", return_value=[]):
            usb.add(run, "0fd9:0084")
        xml = run.dom.return_value.attachDeviceFlags.call_args[0][0]
        self.assertIn("<address bus='3' device='7'/>", xml)

    def test_takes_back_a_device_gone_from_the_host_by_its_address(self):
        run = mock.Mock(**{"dom.return_value": domain((3, 7))})
        with mock.patch.object(usb, "host_devices", return_value=[]):
            self.assertEqual(usb.remove(run, "3.7")["dev"], 7)
        run.dom.return_value.detachDeviceFlags.assert_called_once()


if __name__ == "__main__":
    unittest.main()
