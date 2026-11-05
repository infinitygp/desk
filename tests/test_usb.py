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


class Rebind(unittest.TestCase):
    """Drivers bound again to a device that a killed process left without them."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        fake_sys(self.root, "5-1", idVendor="2516", idProduct="01fe", busnum=5, devnum=12)
        fake_sys(self.root / "5-1", "5-1:1.0", bInterfaceNumber="00")
        (self.root / "5-1" / "5-1:1.0" / "driver").mkdir()  # usbhid has it
        fake_sys(self.root / "5-1", "5-1:1.1", bInterfaceNumber="01")
        self.asked = []

    def connect(self, node, ifno):
        self.asked.append((node, ifno))

    def test_only_the_interfaces_without_a_driver_are_asked_for(self):
        self.assertEqual(usb.rebind("5-1", 5, 12, self.root, self.connect), [1])
        self.assertEqual(self.asked, [("/dev/bus/usb/005/012", 1)])

    def test_a_port_with_another_device_now_is_left_alone(self):
        self.assertEqual(usb.rebind("5-1", 5, 13, self.root, self.connect), [])
        self.assertEqual(usb.rebind("5-2", 5, 12, self.root, self.connect), [])
        self.assertEqual(self.asked, [])


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
        with self.assertRaisesRegex(DeskError, r"3\.7 \(0fd9:0084, port 3-1\), 3\.8 .*give BUS.DEVICE or port:"):
            usb.pick(twins, "0fd9:0084")


class Selectors(unittest.TestCase):
    """port: and serial: name the same device after it came back under another BUS.DEVICE."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for port, dev, serial in (("9-2.2", 19, "A00NA41632OR6N"), ("9-2.3", 20, "DUPLICATE"),
                                  ("13-1", 40, "DUPLICATE")):
            fake_sys(self.root, port, idVendor="0fd9", idProduct="008f", bDeviceClass="00", busnum=port.split("-")[0],
                     devnum=dev, serial=serial)

    def devices(self):
        return usb.host_devices(self.root)

    def test_a_port_or_serial_follows_the_device_to_its_new_number(self):
        self.assertEqual(usb.pick(self.devices(), "port:9-2.2")["dev"], 19)
        self.assertEqual(usb.pick(self.devices(), "serial:A00NA41632OR6N")["port"], "9-2.2")
        (self.root / "9-2.2" / "devnum").write_text("31\n")  # enumerated again
        self.assertEqual(usb.pick(self.devices(), "port:9-2.2")["dev"], 31)
        self.assertEqual(usb.pick(self.devices(), "serial:A00NA41632OR6N")["dev"], 31)

    def test_a_serial_two_devices_share_is_refused_with_both(self):
        with self.assertRaisesRegex(DeskError, r"several USB devices are serial:DUPLICATE: 13\.40 .*port 13-1.*"
                                               r"9\.20 .*port 9-2\.3"):
            usb.pick(self.devices(), "serial:DUPLICATE")

    def test_no_match_is_refused(self):
        for query in ("port:9-9", "serial:a00na41632or6n"):  # serials are compared as they are
            with self.subTest(query), self.assertRaisesRegex(DeskError, f"no USB device {query} on the host"):
                usb.pick(self.devices(), query)

    def test_usb_add_waits_for_a_device_that_is_coming_back(self):
        later = [[], [{**DECK, "access": False}], [DECK]]
        with mock.patch.object(usb, "host_devices", side_effect=lambda: later.pop(0) if len(later) > 1 else later[0]), \
             mock.patch("time.sleep"):
            self.assertEqual(usb.wait_present("serial:AL1", 5)["dev"], 7)
        with mock.patch.object(usb, "host_devices", return_value=[]), mock.patch("time.sleep"), \
             mock.patch("time.monotonic", side_effect=[0, 0, 6]), \
             self.assertRaisesRegex(DeskError, "no USB device serial:AL1 on the host"):
            usb.wait_present("serial:AL1", 5)

    def test_ls_shows_the_port_to_name_a_device_by(self):
        with mock.patch.object(usb, "listing", return_value=[{**DECK, "run": None}]), \
             mock.patch("builtins.print") as out:
            cli.cmd_usb(SimpleNamespace(usb_cmd="ls"))
        self.assertEqual(out.call_args.args[0], "0fd9:0084\t3.7\tport:3-1\tStream Deck +\tfree")

    def test_what_counts_as_a_device(self):
        for good in ("0fd9:008f", "9.19", "port:9-2.2", "port:13-1", "serial:A00N-41"):
            self.assertTrue(usb.is_query(good), good)
        for bad in ("deck", "port:9", "port:../x", "serial:", "9-2.2"):
            self.assertFalse(usb.is_query(bad), bad)


DECK = {"id": "0fd9:0084", "bus": 3, "dev": 7, "node": "/dev/bus/usb/003/007", "name": "Stream Deck +",
        "access": True, "port": "3-1", "serial": "AL1"}


def domain(*addresses):
    devs = "".join(usb._xml(b, d) for b, d in addresses)
    return mock.Mock(**{"XMLDesc.return_value": f"<domain><devices>{devs}</devices></domain>"})


class AddRemove(unittest.TestCase):
    def setUp(self):
        capability = mock.patch.object(doctor, "_qemu_devices", return_value='name "usb-host", bus USB\n')
        capability.start()
        self.addCleanup(capability.stop)

    def test_missing_backend_refuses_add_before_touching_the_guest(self):
        run = mock.Mock(usbvfiod={})
        with mock.patch.object(doctor, "_qemu_devices", return_value='name "qemu-xhci", bus PCI\n'), \
             mock.patch.object(usb, "host_devices") as devices, \
             self.assertRaisesRegex(DeskError, "QEMU usb-host.*qemu-hw-usb-host.*qemu-device-usb-host"):
            usb.add(run, "0fd9:0084")
        devices.assert_not_called()
        run.dom.assert_not_called()

    def test_missing_backend_refuses_up_usb_before_starting_a_run(self):
        args = SimpleNamespace(resolution=None, image="windows-11", memory=None, cpus=None, displays=1,
                               scale=None, disk=[], usb=["0fd9:0084"], usbvfiod=False)
        with mock.patch.object(doctor, "_qemu_devices", return_value=""), \
             mock.patch.object(cli, "load_image"), mock.patch("desk.monitors.check"), \
             mock.patch("desk.runs.up") as up, self.assertRaisesRegex(DeskError, "QEMU usb-host"):
            cli.cmd_up(args)
        up.assert_not_called()

    def test_up_without_usb_does_not_probe_host_passthrough(self):
        args = SimpleNamespace(resolution=None, image="windows-11", memory=None, cpus=None, displays=1,
                               scale=None, disk=[], usb=[], name=None, cold=False, force=False, label=None,
                               record=False, forward=[], shared_memory=False, usbvfiod=False)
        with mock.patch.object(doctor, "_qemu_devices", side_effect=AssertionError("USB probe on an ordinary run")), \
             mock.patch.object(cli, "load_image"), mock.patch("desk.monitors.check"), \
             mock.patch("desk.runs.up") as up, mock.patch("builtins.print"):
            cli.cmd_up(args)
        up.assert_called_once()

    def test_up_with_the_controller_is_checked_before_a_vm_starts(self):
        from desk import usbvfiod

        args = SimpleNamespace(resolution=None, image="windows-11", memory=None, cpus=None, displays=1, scale=None,
                               disk=[], usb=[], name=None, cold=False, force=False, label=None, record=False,
                               forward=[], shared_memory=False, usbvfiod=True)
        with mock.patch.object(usbvfiod, "check", side_effect=DeskError("usbvfiod is not installed")), \
                mock.patch.object(cli, "load_image"), mock.patch("desk.monitors.check"), \
                mock.patch("desk.runs.up") as up, self.assertRaisesRegex(DeskError, "usbvfiod is not installed"):
            cli.cmd_up(args)
        up.assert_not_called()

    def test_up_with_the_controller_hands_devices_over_without_qemus_usb_host(self):
        from desk import usbvfiod

        args = SimpleNamespace(resolution=None, image="windows-11", memory=None, cpus=None, displays=1, scale=None,
                               disk=[], usb=["0fd9:0084"], name=None, cold=False, force=False, label=None,
                               record=False, forward=[], shared_memory=False, usbvfiod=True)
        run = SimpleNamespace(id="u1", image="windows-11", ssh_port=2222,
                              usbvfiod={"bus": "pci.14", "hotplug": "/r/u1/usbvfiod/hotplug.sock"},
                              log_event=lambda **kw: None)
        with mock.patch.object(usbvfiod, "check", return_value="/x/usbvfiod"), \
                mock.patch.object(doctor, "_qemu_devices", side_effect=AssertionError("usb-host probe")), \
                mock.patch.object(usb, "host_devices", return_value=[DECK]), \
                mock.patch.object(cli, "load_image"), mock.patch("desk.monitors.check"), \
                mock.patch("desk.runs.up", return_value=run) as up, \
                mock.patch.object(usb, "add", return_value=DECK) as add, mock.patch("builtins.print") as out:
            cli.cmd_up(args)
        self.assertTrue(up.call_args.kwargs["usbvfiod"])
        add.assert_called_once_with(run, "0fd9:0084")
        printed = [c.args[0] for c in out.call_args_list]
        self.assertIn("run u1 up (image windows-11, ssh port 2222), USB controller of usbvfiod on pci.14", printed)
        self.assertIn("hotplug socket: /r/u1/usbvfiod/hotplug.sock (desk -r u1 usb socket)", printed)

    def test_listing_and_removal_do_not_need_the_backend(self):
        run = mock.Mock(usbvfiod={}, **{"dom.return_value": domain((3, 7))})
        with mock.patch.object(doctor, "_qemu_devices", side_effect=AssertionError("USB backend probe")), \
             mock.patch.object(usb, "host_devices", return_value=[DECK]), \
             mock.patch("desk.runs.all_runs", return_value=[]):
            self.assertEqual(usb.listing(), [{**DECK, "run": None}])
            self.assertEqual(usb.remove(run, "0fd9:0084"), DECK)
        run.dom.return_value.detachDeviceFlags.assert_called_once()

    def test_refuses_a_node_this_user_cannot_open_and_says_how(self):
        with mock.patch.object(usb, "host_devices", return_value=[{**DECK, "access": False}]), \
             self.assertRaisesRegex(DeskError, 'ATTRS{idVendor}=="0fd9".*GROUP="kvm"'):
            usb.add(mock.Mock(usbvfiod={}), "0fd9:0084")

    def test_refuses_a_device_another_run_has(self):
        other = SimpleNamespace(id="b2", usbvfiod={}, live_domain=lambda: domain((3, 7)))
        with mock.patch.object(usb, "host_devices", return_value=[DECK]), \
             mock.patch("desk.runs.all_runs", return_value=[other]), \
             self.assertRaisesRegex(DeskError, "in run b2 already"):
            usb.add(mock.Mock(usbvfiod={}), "0fd9:0084")

    def test_plugs_in_by_address(self):
        run = mock.Mock(usbvfiod={}, **{"dom.return_value": domain()})
        with mock.patch.object(usb, "host_devices", return_value=[DECK]), \
             mock.patch.object(usb, "present", return_value={(3, 7)}), \
             mock.patch("desk.runs.all_runs", return_value=[]):
            usb.add(run, "0fd9:0084")
        xml = run.dom.return_value.attachDeviceFlags.call_args[0][0]
        self.assertIn("<address bus='3' device='7'/>", xml)

    def test_takes_back_a_device_gone_from_the_host_by_its_address(self):
        run = mock.Mock(usbvfiod={}, **{"dom.return_value": domain((3, 7))})
        with mock.patch.object(usb, "host_devices", return_value=[]):
            self.assertEqual(usb.remove(run, "3.7")["dev"], 7)
        run.dom.return_value.detachDeviceFlags.assert_called_once()


class Gone(unittest.TestCase):
    """Entries of devices that left the host: shown, and taken out before the next add."""

    def setUp(self):
        capability = mock.patch.object(doctor, "_qemu_devices", return_value='name "usb-host", bus USB\n')
        capability.start()
        self.addCleanup(capability.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sys = Path(self.tmp.name)
        fake_sys(self.sys, "usb13", idVendor="1d6b", idProduct="0002", bDeviceClass="09", busnum=13, devnum=1)
        fake_sys(self.sys, "13-1", idVendor="0fd9", idProduct="0084", bDeviceClass="00", busnum=13, devnum=40)
        fake_sys(self.sys, "3-1", idVendor="1209", idProduct="0001", bDeviceClass="00", busnum=3, devnum=8)
        fake_sys(self.sys, "13-1:1.0", bInterfaceClass="03")

    def test_only_numbers_absent_from_the_host_are_gone(self):
        dom = domain((13, 13), (3, 8), (13, 1), (13, 14))
        # 13.1 is a hub now: a number in use is never taken for gone, whatever has it.
        self.assertEqual(usb.gone(dom, self.sys), [(13, 13), (13, 14)])
        self.assertEqual(usb.present(self.sys), {(13, 1), (13, 40), (3, 8)})

    def test_add_takes_out_every_gone_entry_first_and_logs_it(self):
        stale = [(13, n) for n in range(13, 27)]
        dom = domain(*stale, (3, 8))
        run = mock.Mock(usbvfiod={}, **{"dom.return_value": dom})
        new = {**DECK, "bus": 13, "dev": 40, "node": "/dev/bus/usb/013/040"}
        with mock.patch.object(usb, "host_devices", return_value=[new]), \
             mock.patch.object(usb, "SYS", self.sys), mock.patch("desk.runs.all_runs", return_value=[]), \
             mock.patch("sys.stderr"):
            usb.add(run, "13.40")
        detached = [c.args[0] for c in dom.detachDeviceFlags.call_args_list]
        self.assertEqual(detached, [usb._xml(b, d) for b, d in stale])
        self.assertNotIn(usb._xml(3, 8), detached)
        self.assertEqual([c.kwargs for c in run.log_event.call_args_list][:14],
                         [{"usb": "remove", "device": f"13.{n}", "reason": "gone from the host"}
                          for n in range(13, 27)])
        self.assertIn("<address bus='13' device='40'/>", dom.attachDeviceFlags.call_args.args[0])

    def test_a_device_on_the_host_is_never_taken_out(self):
        dom = domain((3, 8))
        run = mock.Mock(usbvfiod={}, **{"dom.return_value": dom})
        new = {**DECK, "bus": 13, "dev": 40}
        with mock.patch.object(usb, "host_devices", return_value=[new]), \
             mock.patch.object(usb, "SYS", self.sys), mock.patch("desk.runs.all_runs", return_value=[]):
            usb.add(run, "13.40")
        dom.detachDeviceFlags.assert_not_called()

    def test_a_failed_take_out_says_which_entry_and_adds_nothing(self):
        dom = domain((13, 13))
        dom.detachDeviceFlags.side_effect = usb.libvirt.libvirtError("busy")
        run = mock.Mock(usbvfiod={}, **{"dom.return_value": dom})
        with mock.patch.object(usb, "host_devices", return_value=[{**DECK, "bus": 13, "dev": 40}]), \
             mock.patch.object(usb, "SYS", self.sys), mock.patch("desk.runs.all_runs", return_value=[]), \
             self.assertRaisesRegex(DeskError, "13.13, gone from the host"):
            usb.add(run, "13.40")
        dom.attachDeviceFlags.assert_not_called()

    def test_listing_shows_gone_entries_with_their_run(self):
        other = SimpleNamespace(id="b2", usbvfiod={}, live_domain=lambda: domain((13, 13), (13, 40)))
        present = {**DECK, "bus": 13, "dev": 40}
        with mock.patch.object(usb, "host_devices", return_value=[present]), \
             mock.patch.object(usb, "SYS", self.sys), mock.patch("desk.runs.all_runs", return_value=[other]):
            rows = usb.listing()
        self.assertEqual(rows[0]["run"], "b2")
        self.assertEqual([(r["bus"], r["dev"], r["run"]) for r in rows if r.get("gone")], [(13, 13, "b2")])
        with mock.patch.object(usb, "listing", return_value=rows), \
             mock.patch("builtins.print") as out:
            cli.cmd_usb(SimpleNamespace(usb_cmd="ls"))
        self.assertIn("13.13\t-\t-\tgone, still in run b2", out.call_args_list[1].args[0])


if __name__ == "__main__":
    unittest.main()
