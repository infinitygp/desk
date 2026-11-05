import unittest

from desk import hubs
from desk.config import DeskError


def entry(path, hub_ports=None, speed="high", id="virtual 1"):
    return {"path": path, "speed": speed, "id": id, "hub_ports": hub_ports}


class Paths(unittest.TestCase):
    def test_a_path_is_the_ports_from_the_root_port_down(self):
        self.assertEqual(hubs.parse_path("5.1.1.4"), (5, 1, 1, 4))
        self.assertEqual(hubs.parse_path("2"), (2,))
        self.assertEqual(hubs.fmt((5, 1, 4)), "5.1.4")

    def test_what_is_no_path_is_refused_with_the_form(self):
        for bad in ("", "0", "5.0", "5..1", "5.", ".5", "a.b", "05", "5.1.x", 5, None):
            with self.subTest(bad), self.assertRaisesRegex(DeskError, "ports from the root port down"):
                hubs.parse_path(bad)

    def test_limits_of_the_controller(self):
        self.assertEqual(hubs.parse_path("8.15.15.15.15.15")[0], 8)
        for bad, message in (("9", "8 root ports"), ("5.1.1.1.1.1.1", "five"), ("5.16", "at most 15 ports"),
                             ("1.4", "hubs stand on the USB 2 root ports 5 to 8")):
            with self.subTest(bad), self.assertRaisesRegex(DeskError, message):
                hubs.parse_path(bad)
        self.assertEqual(hubs.parse_path("1"), (1,))  # a device alone may use a USB 3 root port

    def test_at_names_what_desk_usb_rm_takes_out(self):
        self.assertEqual(hubs.AT.fullmatch("at:5.1.4")[1], "5.1.4")
        for bad in ("5.1", "at:", "at:5.", "at:0"):
            self.assertIsNone(hubs.AT.fullmatch(bad), bad)


class HubOptions(unittest.TestCase):
    def test_a_hub_with_everything_said(self):
        hub = hubs.parse_hub("5.1=067b:2586/4/hs")
        self.assertEqual(hub, hubs.Hub((5, 1), 0x067B, 0x2586, 4, True))
        self.assertEqual(hub.spec(), "067b:2586/4/hs")
        self.assertEqual(hub.payload(), {"path": "5.1", "spec": {"vid": 0x067B, "pid": 0x2586, "ports": 4,
                                                                   "speed": "high"}})

    def test_the_defaults_are_a_high_speed_hub_of_eight_ports_with_a_generic_id(self):
        hub = hubs.parse_hub("5=/4")
        self.assertEqual((hub.vendor, hub.product, hub.ports, hub.high), (0x0409, 0x55AA, 4, True))
        self.assertEqual(hubs.parse_hub("5=067b:2586").ports, 8)
        self.assertEqual(hubs.Hub((5,)).payload()["spec"]["speed"], "high")

    def test_full_speed_is_asked_for(self):
        hub = hubs.parse_hub("5=/2/fs")
        self.assertFalse(hub.high)
        self.assertEqual(hub.payload()["spec"], {"vid": 0x0409, "pid": 0x55AA, "ports": 2, "speed": "full"})
        self.assertTrue(hubs.parse_hub("5=/hs").high)

    def test_what_is_no_hub_option_is_refused(self):
        for bad in ("5", "5=", "5=067b/4", "5=067b:2586/0", "5=/16", "5=/4/xs", "x=/4", 3):
            with self.subTest(bad), self.assertRaises(DeskError):
                hubs.parse_hub(bad)
        with self.assertRaisesRegex(DeskError, "hub 1: hubs stand on the USB 2 root ports"):
            hubs.parse_hub("1=/4")
        with self.assertRaisesRegex(DeskError, "no room for a device"):
            hubs.parse_hub("5.1.1.1.1.1=/4")
        with self.assertRaisesRegex(DeskError, "given twice"):
            hubs.parse_hubs(["5=/4", "5=/8"])

    def test_none_is_no_hubs(self):
        self.assertEqual(hubs.parse_hubs(None), {})


class Plan(unittest.TestCase):
    def test_an_empty_tree_gets_the_hubs_on_the_way_top_first(self):
        new = hubs.plan((5, 1, 4), {}, [])
        self.assertEqual([h.path for h in new], [(5,), (5, 1)])
        self.assertTrue(all(h.high and h.ports == 8 for h in new))

    def test_a_device_on_a_root_port_needs_no_hub(self):
        self.assertEqual(hubs.plan((5,), {}, []), [])

    def test_wanted_hubs_are_made_as_wanted(self):
        wanted = hubs.parse_hubs(["5=067b:2586/4/hs", "5.1=067b:2586/4/hs"])
        new = hubs.plan((5, 1, 4), wanted, [])
        self.assertEqual([h.spec() for h in new], ["067b:2586/4/hs"] * 2)

    def test_hubs_that_are_there_are_reused_and_only_the_missing_ones_made(self):
        tree = [entry("5", 4)]
        new = hubs.plan((5, 1, 4), hubs.parse_hubs(["5.1=/4"]), tree)
        self.assertEqual([h.path for h in new], [(5, 1)])
        self.assertEqual(hubs.plan((5, 1), {}, tree), [])

    def test_a_hub_that_is_there_keeps_what_it_is(self):
        tree = [entry("5", 4)]
        self.assertEqual(hubs.plan((5, 1), hubs.parse_hubs(["5=/4/hs"]), tree), [])
        with self.assertRaisesRegex(DeskError, "a high speed hub with 4 ports already.*not 8 ports at high speed"):
            hubs.plan((5, 1), hubs.parse_hubs(["5=/8/hs"]), tree)
        with self.assertRaisesRegex(DeskError, "keeps it until it is removed.*not 4 ports at full speed"):
            hubs.plan((5, 1), hubs.parse_hubs(["5=/4/fs"]), tree)

    def test_a_hub_option_off_the_way_is_refused(self):
        with self.assertRaisesRegex(DeskError, r"--hub 5\.2 is not on the way to 5\.1\.4 \(hubs: 5, 5\.1\)"):
            hubs.plan((5, 1, 4), hubs.parse_hubs(["5.2=/4"]), [])
        with self.assertRaisesRegex(DeskError, "none, it is on a root port"):
            hubs.plan((5,), hubs.parse_hubs(["5=/4"]), [])

    def test_an_occupied_path_is_refused(self):
        with self.assertRaisesRegex(DeskError, r"port path 5\.1 holds the device virtual 1 already"):
            hubs.plan((5, 1), {}, [entry("5", 4), entry("5.1")])
        with self.assertRaisesRegex(DeskError, "holds a high speed hub with 4 ports already"):
            hubs.plan((5,), {}, [entry("5", 4)])

    def test_nothing_can_be_plugged_in_below_a_device(self):
        with self.assertRaisesRegex(DeskError, r"port path 5 holds the device virtual 1, which is no hub.*5\.1"):
            hubs.plan((5, 1), {}, [entry("5")])

    def test_a_port_beyond_the_hub_is_refused(self):
        with self.assertRaisesRegex(DeskError, r"hub at 5 has 4 ports; port path 5\.5 needs port 5"):
            hubs.plan((5, 5), hubs.parse_hubs(["5=/4"]), [])
        with self.assertRaisesRegex(DeskError, r"hub at 5 has 4 ports; port path 5\.5\.1 needs port 5"):
            hubs.plan((5, 5, 1), {}, [entry("5", 4)])

    def test_a_high_speed_hub_below_a_full_speed_one_is_refused_before_the_controller_does(self):
        with self.assertRaisesRegex(DeskError, "high speed and the hub above it full speed"):
            hubs.plan((5, 1, 1), hubs.parse_hubs(["5=/4/fs"]), [])
        with self.assertRaisesRegex(DeskError, "high speed and the hub above it full speed"):
            hubs.plan((5, 1, 1), {}, [entry("5", 4, speed="full")])
        new = hubs.plan((5, 1, 1), hubs.parse_hubs(["5=/4/fs", "5.1=/4/fs"]), [])
        self.assertEqual([h.speed for h in new], ["full", "full"])


if __name__ == "__main__":
    unittest.main()
