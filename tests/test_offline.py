import re
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from desk import offline
from desk.config import DeskError, NotDone

RUN = SimpleNamespace(ssh_port=1)
LINUX = SimpleNamespace(family="linux", root_user="root", shell="bash", cmd=lambda s: s)
OPENWRT = SimpleNamespace(family="openwrt", root_user="root", shell="sh", cmd=lambda s: s)
WINDOWS = SimpleNamespace(family="windows", root_user="tester", shell="powershell", cmd=lambda s: s)


class Rules(unittest.TestCase):
    """What goes into the guest's firewall: outgoing refused, but DNS, answers and loopback."""

    def test_linux_has_a_table_of_its_own_loaded_again_at_boot(self):
        script = offline._linux(None)
        self.assertIn("table inet desk_offline", script)
        self.assertIn("ct state established,related accept", script)  # desk's SSH answers
        self.assertIn("th dport 53 accept", script)
        self.assertIn("systemctl enable --quiet desk-offline.service", script)
        self.assertIn("exit 4", script)  # no nft: said, not failed silently

    def test_only_some_ports(self):
        self.assertIn("th dport { 443, 80 } reject", offline._linux([443, 80]))
        self.assertIn("-RemotePort 443", offline._windows([443]))

    def test_openwrt_is_a_chain_fw4_includes(self):
        script = offline._openwrt([443])
        self.assertIn("/etc/nftables.d/99-desk-offline.nft", script)
        self.assertIn("chain desk_offline", script)
        self.assertIn("fw4 reload", script)

    def test_windows_blocks_every_remote_port_but_dns(self):
        script = offline._windows(None)
        self.assertIn("-RemotePort 1-52,54-65535 -Action Block", script)
        self.assertEqual(script.count("New-NetFirewallRule"), 2)  # TCP and UDP


class Apply(unittest.TestCase):
    def ssh(self, *answers):
        return mock.patch("desk.offline.guest.ssh_run", side_effect=[
            subprocess.CompletedProcess([], rc, out.encode(), b"") for rc, out in answers])

    def test_on_and_read_back(self):
        with self.ssh((0, ""), (0, "1")), mock.patch("desk.offline.guest.lost"):
            self.assertEqual(offline.on(RUN, LINUX, [443]), "offline: connections to port 443 going out of the "
                                                            "guest refused")

    def test_a_guest_without_the_tool_says_so(self):
        for family, rc, said in ((LINUX, 4, "this guest has no nft (the nftables package)"),
                                 (WINDOWS, 5, "Windows Firewall is off")):
            with self.subTest(family=family.family), self.ssh((rc, said)), mock.patch("desk.offline.guest.lost"), \
                    self.assertRaisesRegex(DeskError, "cannot cut the guest off: " + re.escape(said)):
                offline.on(RUN, family)

    def test_rules_that_did_not_take_are_desks_failure(self):
        with self.ssh((0, ""), (0, "0")), mock.patch("desk.offline.guest.lost"), \
                self.assertRaisesRegex(NotDone, "did not take desk's rules"):
            offline.on(RUN, OPENWRT)
        with self.ssh((0, ""), (0, "2")), mock.patch("desk.offline.guest.lost"), \
                self.assertRaisesRegex(NotDone, "still in the guest"):
            offline.off(RUN, WINDOWS)

    def test_a_firewall_that_cannot_be_read(self):
        with self.ssh((1, "nft: command not found")), mock.patch("desk.offline.guest.lost"), \
                self.assertRaisesRegex(NotDone, "cannot read the guest's firewall: nft: command not found"):
            offline.state(RUN, LINUX)


class Step(unittest.TestCase):
    def test_the_offline_step(self):
        import tempfile
        from pathlib import Path

        from desk import scenario

        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "s.toml"
            f.write_text('image = "a"\n[[step]]\noffline = [443]\n[[step]]\noffline = true\n[[step]]\noffline = false')
            self.assertEqual([st["offline"] for st in scenario.load(f)["step"]], [[443], True, False])
            for bad in ("[]", "[0]", "[70000]", '"yes"', "[true]"):
                f.write_text(f'image = "a"\n[[step]]\noffline = {bad}')
                with self.subTest(bad=bad), self.assertRaises(DeskError):
                    scenario.load(f)
        with mock.patch("desk.offline.on", return_value="offline: …") as on, \
                mock.patch("desk.offline.off", return_value="online: …") as off:
            scenario._step(RUN, LINUX, mock.Mock(), {"offline": [443]})
            scenario._step(RUN, LINUX, mock.Mock(), {"offline": False})
        on.assert_called_once_with(RUN, LINUX, [443])
        off.assert_called_once_with(RUN, LINUX)
