"""Windows guests answer through the in-session agent; what desk makes of a refusal there."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from desk import a11y, forward, monitors
from desk.config import DeskError, load_image

RUN = SimpleNamespace(id="t", agent_port=1, ssh_port=1, scale=None, displays=1, image="windows-11")


class Focus(unittest.TestCase):
    def focus(self, *replies):
        with mock.patch("desk.a11y.winagent.request", side_effect=list(replies)) as request:
            try:
                return a11y.focus(RUN, "Notepad")
            finally:
                self.calls = request.call_count

    def test_in_front(self):
        self.assertEqual(self.focus({"title": "Notepad", "focused": True}), "Notepad")

    def test_refused_once(self):
        self.assertEqual(self.focus({"title": "Notepad", "focused": False}, {"title": "Notepad", "focused": True}),
                         "Notepad")
        self.assertEqual(self.calls, 2)

    def test_kept_behind(self):
        with self.assertRaisesRegex(DeskError, "kept 'Notepad' behind"):
            self.focus({"title": "Notepad", "focused": False}, {"title": "Notepad", "focused": False})


class SetText(unittest.TestCase):
    """A field's text on Windows: a click into it and real keys, as a web view's page sees only those
    (UI Automation's SetValue changed what WebView2 reported, not the page), then read back."""

    SPEC = {"pid": 1, "path": [0, 3], "role": "entry", "name": "Search actions and widgets"}
    RUN = SimpleNamespace(agent_port=1, dom=lambda: "dom")

    def replace(self, text: str, value: str | None = None, role: str = "entry", answer: dict | None = None):
        node = {**self.SPEC, "role": role, "value": value} if value is not None else {**self.SPEC, "role": role}
        with mock.patch("desk.a11y._visible_point", return_value=(123, 202)), \
                mock.patch("desk.a11y.display") as display, \
                mock.patch("desk.a11y.snapshot", return_value={"nodes": [node]}), \
                mock.patch("desk.a11y._helper", return_value=answer or {}) as helper:
            display.key_plan.side_effect = lambda t, layout: None if "\n" in t else [[30]] * len(t)
            said = a11y.replace_text(self.RUN, {**self.SPEC, "role": role}, text)
        return said, display, helper

    def test_typed_and_read_back(self):
        said, display, helper = self.replace("zażółć", value="zażółć")
        self.assertEqual(said, "")
        display.click.assert_called_once_with("dom", 123, 202, run=self.RUN)
        self.assertEqual(display.key.call_args_list[0].args, ("dom", "ctrl+a"))
        helper.assert_not_called()  # no SetValue

    def test_the_field_holds_something_else(self):
        self.assertEqual(self.replace("1234567890", value="12345")[0], "the field shows '12345' afterwards")

    def test_an_emptied_field_that_chromium_reports_as_its_name(self):
        said, display, _ = self.replace("", value="Search actions and widgets")
        self.assertEqual(said, "")
        self.assertEqual(display.key.call_args_list[-1].args, ("dom", "delete"))

    def test_what_the_keyboard_cannot_type_goes_through_ui_automation_and_says_so(self):
        answer = {"done": True, "text": "line one\rline two"}
        said, _, helper = self.replace("line one\nline two", answer=answer)
        self.assertIn("set through UI Automation only", said)
        self.assertEqual(helper.call_args.args[1], "set-text")

    def test_a_password_box_does_not_tell(self):
        self.assertEqual(self.replace("secret", value="••••", role="password text")[0], "")


class WinSettings(unittest.TestCase):
    """desk win and a scenario's win step: the same words, each setting read back afterwards."""

    def test_the_words_of_a_win_step(self):
        from desk import winsettings

        args = winsettings.parse(["theme", "dark", "--only", "system"])
        self.assertEqual((args.win_cmd, args.mode, args.only), ("theme", "dark", "system"))
        self.assertEqual(winsettings.parse(["text-size", "150"]).percent, 150)
        self.assertEqual(winsettings.parse(["wallpaper", "C:\\Windows\\Web\\Wallpaper\\ThemeB\\img24.jpg"]).path,
                         "C:\\Windows\\Web\\Wallpaper\\ThemeB\\img24.jpg")
        for bad in (["accent", "red"], ["text-size", "300"], ["transparency", "maybe"], ["theme"]):
            with self.subTest(bad=bad), self.assertRaisesRegex(DeskError, "^win: "):
                winsettings.parse(bad)

    def test_a_setting_windows_did_not_take_is_desks_failure(self):
        from desk import config, winsettings

        spec = SimpleNamespace(family="windows", root_user="tester", cmd=lambda script: script)
        answers = iter(["", "0"])  # the write, then what Windows has: still 0
        with mock.patch("desk.winsettings._ps", side_effect=lambda run, spec, script: next(answers)), \
                mock.patch("desk.winsettings._broadcast"), \
                self.assertRaisesRegex(winsettings.NotApplied, "transparency effects did not take: Windows has 0 "
                                                               "instead of 1"):
            winsettings.transparency(RUN, spec, "on")
        self.assertTrue(issubclass(winsettings.NotApplied, config.NotDone))  # exit 2 in a scenario

    def test_text_size_100_is_no_value(self):
        from desk import winsettings

        spec = SimpleNamespace(family="windows", root_user="tester", cmd=lambda script: script)
        scripts = []

        def ps(run, spec, script):
            scripts.append(script)
            return ""

        with mock.patch("desk.winsettings._ps", side_effect=ps), mock.patch("desk.winsettings._broadcast"):
            self.assertTrue(winsettings.text_size(RUN, spec, 100).startswith("text size 100 %"))
        self.assertIn("Remove-ItemProperty", scripts[0])

    def test_a_win_step_is_refused_for_another_family(self):
        from desk import scenario

        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "s.toml"
            f.write_text('image = "a"\n[[step]]\nwin = "transparency off"\n')
            data = scenario.load(f)
            with self.assertRaisesRegex(DeskError, "step 1: win changes Windows settings, and fedora is a linux"):
                scenario.check_observations(data, SimpleNamespace(family="linux", name="fedora"))
            f.write_text('image = "a"\n[[step]]\nwin = "accent red"\n')
            with self.assertRaisesRegex(DeskError, "step 1: win: argument win_cmd: invalid choice"):
                scenario.load(f)


class Scale(unittest.TestCase):
    def test_a_registry_that_refuses_costs_no_reboot(self):
        refused = subprocess.CompletedProcess([], 1, b"", b"Set-ItemProperty : Requested registry access is not allowed.")
        with mock.patch("desk.monitors.guest.ssh_run", return_value=refused), \
                mock.patch("desk.monitors.guest.reboot") as reboot, mock.patch("desk.winagent.request"), \
                self.assertRaisesRegex(DeskError, "did not take scale 150 %.*registry access"):
            monitors._windows(RUN, load_image("windows-11"), None, 150)
        reboot.assert_not_called()

    def test_a_scale_that_did_not_stick(self):
        done = subprocess.CompletedProcess([], 0, b"", b"")
        with mock.patch("desk.monitors.guest.ssh_run", return_value=done), mock.patch("desk.monitors.guest.reboot"), \
                mock.patch("desk.winagent.request", return_value={"stdout": "96\r\n", "code": 0}), \
                self.assertRaisesRegex(DeskError, "at 100 % instead of 150 %"):
            monitors._windows(RUN, load_image("windows-11"), None, 150)

    def test_a_scale_that_stuck(self):
        done = subprocess.CompletedProcess([], 0, b"", b"")
        with mock.patch("desk.monitors.guest.ssh_run", return_value=done), mock.patch("desk.monitors.guest.reboot"), \
                mock.patch("desk.winagent.request", return_value={"stdout": "144\r\n", "code": 0}):
            monitors._windows(RUN, load_image("windows-11"), None, 150)


class Taskbar(unittest.TestCase):
    def test_a_shell_that_did_not_come_back_is_not_done(self):
        from desk import winsettings

        spec = SimpleNamespace(root_user="tester", cmd=lambda script: script)
        refused = subprocess.CompletedProcess([], 1, b"", b"Explorer (the taskbar and desktop) did not start again\n")
        with mock.patch("desk.winsettings.guest.ssh_run", side_effect=[subprocess.CompletedProcess([], 0, b"", b""),
                                                                       refused]) as ssh, \
                mock.patch("desk.winsettings.display") as display, mock.patch("desk.winsettings.time.sleep"), \
                self.assertRaisesRegex(DeskError, "did not start again"):
            winsettings.taskbar(mock.Mock(), spec, align="left")
        self.assertIn("if (-not (Get-Process explorer", ssh.call_args.args[2])
        display.wait_stable.assert_not_called()


class Forward(unittest.TestCase):
    def test_another_local_port_for_a_forwarded_one_is_refused(self):
        home = tempfile.TemporaryDirectory(prefix="desk-forward-")
        self.addCleanup(home.cleanup)
        run = SimpleNamespace(**vars(RUN), dir=Path(home.name))
        live = [{"port": 22, "local": 40001, "pid": 1, "alive": True}]
        with mock.patch("desk.forward.listing", return_value=live):
            self.assertEqual(forward.start(run, 22), 40001)
            self.assertEqual(forward.start(run, 22, 40001), 40001)
            with self.assertRaisesRegex(DeskError, "forwarded to 127.0.0.1:40001 already"):
                forward.start(run, 22, 40002)


if __name__ == "__main__":
    unittest.main()
