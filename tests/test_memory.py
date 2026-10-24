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
             mock.patch.object(runs, "gc_idle") as idle, mock.patch.object(runs, "_gc_idle_in_background"), \
             mock.patch("desk.maintain.check_usable", return_value=[]), \
             mock.patch.object(runs.time, "sleep") as sleep:
            self.assertIs(runs.up(SPEC), run)
        self.assertEqual(len(locks), 3)  # the lock taken anew for each try
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(idle.call_count, 2)  # idle runs give their memory back while it waits

    def test_other_errors_are_not_waited_for(self):
        with mock.patch.object(runs, "state_lock", return_value=nullcontext()), \
             mock.patch.object(runs, "gc"), \
             mock.patch.object(runs, "check_memory"), \
             mock.patch.object(runs, "_up", side_effect=DeskError("run x already exists")), \
             mock.patch.object(runs, "gc_idle") as idle, mock.patch.object(runs, "_gc_idle_in_background") as later, \
             mock.patch("desk.maintain.check_usable", return_value=[]), \
             mock.patch.object(runs.time, "sleep") as sleep, \
             self.assertRaisesRegex(DeskError, "already exists"):
            runs.up(SPEC)
        sleep.assert_not_called()
        # Enough memory: the start does not wait for idle runs' reports.
        idle.assert_not_called()
        later.assert_called_once()

    def test_an_idle_run_of_the_image_goes_first(self):
        # It may hold the warm state this start restores; another image's idle run does not.
        for image, waits in (("fedora-44-gnome", True), ("windows-11", False)):
            idle_run = SimpleNamespace(image=image, idle=lambda: 3 * 3600)
            with self.subTest(image=image), mock.patch.object(runs, "state_lock", return_value=nullcontext()), \
                    mock.patch.object(runs, "gc"), mock.patch.object(runs, "check_memory"), \
                    mock.patch.object(runs, "_up"), mock.patch.object(runs, "all_runs", return_value=[idle_run]), \
                    mock.patch.object(runs, "gc_idle") as idle, \
                    mock.patch.object(runs, "_gc_idle_in_background") as later, \
                    mock.patch("desk.maintain.check_usable", return_value=[]):
                runs.up(SPEC)
            self.assertEqual((idle.called, later.called), (waits, not waits))


class IdleRunsInTheBackground(unittest.TestCase):
    def test_the_background_cleanup_runs(self):
        from desk.config import LOGS

        cleanup = runs._gc_idle_in_background()
        self.assertEqual(cleanup.wait(60), 0)
        self.assertNotIn("Traceback", (LOGS / "gc-idle.log").read_text())


if __name__ == "__main__":
    unittest.main()
