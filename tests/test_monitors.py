import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from desk import monitors


class Gnome(unittest.TestCase):
    def command(self, run, resolution=None, scale=None) -> str:
        done = subprocess.CompletedProcess([], 0, b"", b"")
        with mock.patch("desk.monitors._gnome_connectors", return_value=["Virtual-1"]), \
                mock.patch("desk.monitors._gnome_mode", side_effect=lambda run, c, r: f"{r[0]}x{r[1]}@60.000"), \
                mock.patch("desk.monitors.guest.ssh_run", return_value=done) as ssh, \
                mock.patch("desk.monitors.time.sleep"):
            monitors._gnome(run, resolution, scale)
        return ssh.call_args.args[2]

    def test_a_new_scale_keeps_the_mode_set_before(self):
        # gdctl would put a monitor without --mode back to its preferred mode.
        run = SimpleNamespace(ssh_port=1, user="tester", scale=None, resolution=[1280, 1024])
        self.assertIn("--mode 1280x1024@60.000 --scale 1.25", self.command(run, scale=125))
        self.assertIn("--mode 1920x1080@60.000", self.command(run, resolution=(1920, 1080)))
        self.assertNotIn("--mode", self.command(SimpleNamespace(ssh_port=1, user="tester", scale=None, resolution=None),
                                                 scale=150))


if __name__ == "__main__":
    unittest.main()
