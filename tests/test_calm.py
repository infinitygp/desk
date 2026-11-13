import subprocess
import unittest
from unittest import mock

import libvirt

from desk import calm, selftest
from desk.config import DeskError, load_image
from desk.guest import NoConnection


class Guest:
    """A domain whose CPU time follows `cpu(t)` on a clock that only `sleep` moves."""

    def __init__(self, cpu):
        self.now = 0.0
        self.cpu = cpu

    def info(self):
        return [1, 0, 0, 4, int(self.cpu(self.now) * 1e9)]

    def patched(self):
        def sleep(seconds):
            self.now += seconds

        return mock.patch.multiple("desk.calm.time", sleep=sleep, monotonic=lambda: self.now)


def spike(rate: float, start: float, end: float, base: float = 0.5):
    """CPU seconds used by `base` vCPUs, and `rate` between `start` and `end`."""
    def cpu(t):
        busy = max(0.0, min(t, end) - start)
        return base * t + (rate - base) * busy
    return cpu


class Watch(unittest.TestCase):
    def test_average_and_busiest_window(self):
        guest = Guest(spike(4.0, 30, 45))  # 15 s at 4 vCPUs in a minute otherwise at 0.5
        with guest.patched():
            average, peak = calm.watch(guest, 60)
        self.assertAlmostEqual(average, (0.5 * 60 + 3.5 * 15) / 60)
        self.assertAlmostEqual(peak, 4.0)

    def test_less_than_a_window_has_no_busier_moment_than_its_average(self):
        guest = Guest(lambda t: 2 * t)
        with guest.patched():
            self.assertEqual(calm.watch(guest, 6), (2.0, 2.0))

    def test_cpu_time_that_cannot_be_read_is_no_measurement(self):
        dom = mock.Mock()
        dom.info.side_effect = libvirt.libvirtError("Domain not found")
        with self.assertRaisesRegex(DeskError, "cannot read the guest's CPU time: Domain not found"):
            calm.cpu_seconds(dom)
        with mock.patch("desk.calm.time.sleep"), self.assertRaises(DeskError):
            calm.watch(dom, 60)


class Wait(unittest.TestCase):
    def test_a_guest_that_calms_down_is_handed_over_when_a_window_is_calm(self):
        guest = Guest(spike(4.0, 0, 30, base=0.1))
        with guest.patched():
            self.assertIsNone(calm.wait(guest, 300))
        # 42 s: the window of 27-42 s holds 3 s at 4 vCPUs and 12 s at 0.1, 0.88 vCPUs.
        self.assertEqual(guest.now, 42)

    def test_a_guest_that_stays_busy_gives_its_last_window_at_the_limit(self):
        guest = Guest(lambda t: 3 * t)
        with guest.patched():
            self.assertAlmostEqual(calm.wait(guest, 30), 3.0)
        self.assertEqual(guest.now, 30)

    def test_at_least_one_whole_window_is_measured(self):
        for cpu, expected in ((lambda t: 0.0, None), (lambda t: 3 * t, 3.0)):
            guest = Guest(cpu)
            with guest.patched():
                got = calm.wait(guest, 0)
            self.assertEqual(guest.now, calm.WINDOW)
            self.assertEqual(got, expected)


class Processes(unittest.TestCase):
    def listing(self, spec, out: bytes, code: int = 0, err: bytes = b""):
        run = mock.Mock(ssh_port=22)
        done = subprocess.CompletedProcess([], code, out, err)
        with mock.patch("desk.calm.guest.ssh_run", return_value=done) as ssh:
            return calm.processes(run, spec), ssh

    def test_linux_sums_per_name(self):
        used, ssh = self.listing(load_image("fedora-44-gnome"), b"systemd               71\n"
                                 b"tmux: server           12\nsshd 4\nsshd  6\nwarning without a number\n")
        self.assertEqual(used, {"systemd": 71.0, "tmux: server": 12.0, "sshd": 10.0})
        self.assertEqual(ssh.call_args.args[2], "ps -eo comm=,cputimes=")

    def test_windows_sums_per_name_with_spaces_in_names(self):
        spec = load_image("windows-11")
        used, ssh = self.listing(spec, b"Memory Compression\t12.5\r\nsvchost\t1.5\r\nsvchost\t2\r\n")
        self.assertEqual(used, {"Memory Compression": 12.5, "svchost": 3.5})
        self.assertIn("-EncodedCommand", ssh.call_args.args[2])

    def test_a_listing_that_failed_or_is_empty_is_no_listing(self):
        spec = load_image("fedora-44-gnome")
        with self.assertRaisesRegex(DeskError, "could not list its processes: boom"):
            self.listing(spec, b"", 1, b"boom\n")
        with self.assertRaisesRegex(DeskError, "nothing readable"):
            self.listing(spec, b"not a listing\n")
        with self.assertRaises(NoConnection):
            self.listing(spec, b"", 255, b"Connection refused")
        with mock.patch("desk.calm.guest.ssh_run", side_effect=subprocess.TimeoutExpired("ssh", 60)), \
                self.assertRaisesRegex(DeskError, "within 60 s"):
            calm.processes(mock.Mock(), spec)

    def test_a_family_it_cannot_list(self):
        with self.assertRaisesRegex(DeskError, "openwrt guest cannot be listed"):
            calm.processes(mock.Mock(), load_image("openwrt-25.12"))


class Busiest(unittest.TestCase):
    def test_the_five_that_gained_most(self):
        before = {"TiWorker": 1.0, "MsMpEng": 2.0, "gone": 5.0, "idle": 7.0}
        after = {"TiWorker": 10.1, "MsMpEng": 8.5, "new": 0.34, "idle": 7.0, "a": 3.0, "b": 2.0, "c": 1.0, "d": 0.5}
        self.assertEqual(calm.busiest(before, after), "TiWorker 9.1 s, MsMpEng 6.5 s, a 3.0 s, b 2.0 s, c 1.0 s")

    def test_nothing_gained(self):
        self.assertEqual(calm.busiest({"a": 1.0}, {"a": 1.0}), "no process used CPU time")


class SelftestStep(unittest.TestCase):
    spec = load_image("windows-11")

    def step(self, watched=(0.52, 0.71), listings=(), processes_error=None):
        run = mock.Mock()
        with mock.patch("desk.selftest.calm.watch", return_value=watched) as watch, \
                mock.patch("desk.selftest.calm.processes", side_effect=processes_error or list(listings)):
            return selftest._calm(run, self.spec), watch, run

    def test_a_calm_guest_passes(self):
        text, watch, run = self.step(listings=[{}])
        self.assertEqual(text, "0.52 vCPUs on average, 0.71 in its busiest 15 s")
        watch.assert_called_once_with(run.dom(), 180)

    def test_a_busy_guest_fails_and_names_what_worked(self):
        with self.assertRaises(selftest.Failed) as e:
            self.step((2.31, 4.02), [{"TiWorker": 1.0}, {"TiWorker": 10.1, "MsMpEng": 6.5}])
        self.assertEqual(str(e.exception), "2.31 vCPUs on average (limit 1.0), 4.02 in its busiest 15 s (limit 2.0); "
                                           "busiest in the guest: TiWorker 9.1 s, MsMpEng 6.5 s")

    def test_a_peak_alone_fails(self):
        with self.assertRaises(selftest.Failed):
            self.step((0.5, 2.5), [{"a": 0.0}, {"a": 1.0}])

    def test_numbers_fail_without_a_listing_too(self):
        with self.assertRaisesRegex(selftest.Failed, r"2\.31 vCPUs.*; the guest did not say what worked: no list$"):
            self.step((2.31, 4.02), processes_error=DeskError("no list"))
        with self.assertRaisesRegex(selftest.Failed, "; the guest did not say what worked: gone$"):
            self.step((2.31, 4.02), [{"a": 0.0}, DeskError("gone")])

    def test_cpu_time_that_cannot_be_read_never_passes(self):
        run = mock.Mock()
        with mock.patch("desk.selftest.calm.watch", side_effect=DeskError("cannot read the guest's CPU time")), \
                mock.patch("desk.selftest.calm.processes", return_value={}), \
                self.assertRaisesRegex(DeskError, "cannot read the guest's CPU time"):
            selftest._calm(run, self.spec)


class Settings(unittest.TestCase):
    def test_only_images_that_ask_for_it_are_checked(self):
        self.assertEqual(load_image("windows-11").calm_seconds, 180)
        self.assertIsNone(load_image("fedora-44-gnome").calm_seconds)


if __name__ == "__main__":
    unittest.main()
