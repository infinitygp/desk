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


class TakenOrRefused(unittest.TestCase):
    """A mode or scale the desktop did not take is an error: the run would count with it."""

    target = SimpleNamespace(ssh_port=1, user="tester", scale=None, resolution=None)

    def hyprland(self, after: dict, said: str = "ok\nok", scale=150) -> list[str]:
        """The commands sent, also in `self.sent` when _hyprland raises."""
        import json

        before = [{"name": "Virtual-1", "width": 1280, "height": 800, "scale": 1.0}]
        answers = iter([json.dumps(before), said, json.dumps([{**before[0], **after}]), "ok\nok"])
        self.sent = []

        def user(_run, command, _what):
            self.sent.append(command)
            return next(answers)

        with mock.patch("desk.monitors._user", side_effect=user), mock.patch("desk.monitors.time.sleep"), \
                mock.patch("desk.monitors._settled", side_effect=lambda state, *_: state()):
            monitors._hyprland(self.target, None, scale)
        return self.sent

    def test_hyprland(self):
        self.assertIn("hyprctl keyword monitor Virtual-1,1280x800,0x0,1.5 && ", self.hyprland({"scale": 1.5})[1])
        self.assertIn("hyprctl dispatch forcerendererreload", self.sent[1])
        with self.assertRaisesRegex(monitors.DeskError, "runs at scale 1.6 instead of 1.5"):
            self.hyprland({"scale": 1.6})
        # The scale the run counts with is put back.
        self.assertIn("hyprctl keyword monitor Virtual-1,1280x800,0x0,1", self.sent[-1])
        with self.assertRaisesRegex(monitors.DeskError, "refused the monitor rule: invalid scale"):
            self.hyprland({"scale": 1.5}, said="invalid scale\nok")

    def test_kde(self):
        import json

        def outputs(mode, scale):
            return json.dumps({"outputs": [{"name": "Virtual-1", "connected": True, "currentModeId": mode,
                                            "scale": scale, "modes": [
                                                {"id": "1", "size": {"width": 1280, "height": 800}},
                                                {"id": "2", "size": {"width": 1920, "height": 1080}}]}]})

        for after, error in ((outputs("2", 1.25), None), (outputs("1", 1.25), "runs at 1280x800 instead of 1920x1080"),
                             (outputs("2", 1.0), "runs at scale 1.0 instead of 1.25")):
            answers = iter([outputs("1", 1.0), "", after, ""])
            with self.subTest(error=error), mock.patch("desk.monitors._user", side_effect=lambda *a: next(answers)), \
                    mock.patch("desk.monitors.time.sleep"), \
                    mock.patch("desk.monitors._settled", side_effect=lambda state, *_: state()):
                if error:
                    with self.assertRaisesRegex(monitors.DeskError, error):
                        monitors._kde(self.target, (1920, 1080), 125)
                else:
                    monitors._kde(self.target, (1920, 1080), 125)

    def test_a_moment_is_waited_for(self):
        reads = iter([((1280, 800), 1.0), ((1280, 800), 1.5)])
        with mock.patch("desk.monitors.time.sleep") as sleep:
            self.assertEqual(monitors._settled(lambda: next(reads), None, 1.5), ((1280, 800), 1.5))
        sleep.assert_called_once()


if __name__ == "__main__":
    unittest.main()
