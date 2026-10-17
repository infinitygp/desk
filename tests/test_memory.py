import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest import mock

from desk import runs
from desk.config import DeskError

SPEC = SimpleNamespace(name="fedora-44-gnome", memory_mib=4096)


class CheckMemory(unittest.TestCase):
    def check(self, avail, stall):
        with mock.patch.object(runs, "_mem_available_mib", return_value=avail), \
             mock.patch.object(runs, "_promised_mib", return_value=0), \
             mock.patch.object(runs, "_memory_stall_percent", return_value=stall), \
             mock.patch.object(runs, "all_runs", return_value=[]):
            runs.check_memory(SPEC)

    def test_enough(self):
        self.check(20000, 0.5)

    def test_too_little_available(self):
        with self.assertRaisesRegex(DeskError, "not enough free memory.*3000 MiB available"):
            self.check(3000, 0.0)

    def test_swapping_host_counts_as_short(self):
        with self.assertRaisesRegex(DeskError, "not enough free memory.*swapping"):
            self.check(20000, 7.0)


class WaitForMemory(unittest.TestCase):
    def test_up_waits_in_line_without_the_lock(self):
        short = DeskError("not enough free memory for fedora-44-gnome: ...")
        run = mock.Mock()
        locks = []
        with mock.patch.object(runs, "state_lock", side_effect=lambda: locks.append(1) or nullcontext()), \
             mock.patch.object(runs, "gc"), \
             mock.patch.object(runs, "check_memory", side_effect=[short, short, None]), \
             mock.patch.object(runs, "_up", return_value=run), \
             mock.patch.object(runs, "gc_idle"), \
             mock.patch("desk.maintain.check_usable", return_value=[]), \
             mock.patch.object(runs.time, "sleep") as sleep:
            self.assertIs(runs.up(SPEC), run)
        self.assertEqual(len(locks), 3)  # the lock taken anew for each try
        self.assertEqual(sleep.call_count, 2)

    def test_other_errors_are_not_waited_for(self):
        with mock.patch.object(runs, "state_lock", return_value=nullcontext()), \
             mock.patch.object(runs, "gc"), \
             mock.patch.object(runs, "check_memory"), \
             mock.patch.object(runs, "_up", side_effect=DeskError("run x already exists")), \
             mock.patch.object(runs, "gc_idle"), \
             mock.patch("desk.maintain.check_usable", return_value=[]), \
             mock.patch.object(runs.time, "sleep") as sleep, \
             self.assertRaisesRegex(DeskError, "already exists"):
            runs.up(SPEC)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
