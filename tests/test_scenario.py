import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from desk import browser, guest, scenario
from desk.config import DeskError


def write(text: str) -> Path:
    f = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
    f.write(text)
    f.close()
    return Path(f.name)


class Load(unittest.TestCase):
    def test_valid_file(self):
        data = scenario.load(write('''
image = "fedora-44-gnome-cockpit"
record = true
[build]
run = "make rpm"
artifacts = ["*.rpm"]
[[step]]
root = "dnf install -y /tmp/desk/*.rpm"
[[step]]
click = "Containers"
role = "link"
[[step]]
key = ["ctrl+l", "f5"]
[[step]]
reboot = true
'''))
        self.assertEqual(len(data["step"]), 4)
        self.assertEqual(scenario.describe(data["step"][1]), ("click", "Containers"))
        self.assertEqual(scenario.describe(data["step"][2]), ("key", "ctrl+l f5"))
        self.assertEqual(scenario.describe(data["step"][3]), ("reboot", ""))

    def test_rejects_mistakes_before_starting_anything(self):
        for text in (
            '[[step]]\nclick = "x"',                               # no image
            'image = "a"\n[[step]]\nclick = "x"\nkey = "f5"',      # two actions
            'image = "a"\n[[step]]\nrole = "link"',                # no action
            'image = "a"\n[[step]]\nclik = "x"',                   # typo
            'image = "a"\nrecrod = true',                          # unknown top-level key
            'image = "a"\n[build]\ncommand = "make"',              # unknown build key
            'image = "a"\n[build]\ndir = "no-such-directory"',
            'image = "a"\n[build]\ndir = ""',
            'image = "a"\ndisks = "8G"',                           # disks is a list
            'image = "a"\ndisks = ["8G:Bad_Name"]',                # not a disk name
            'image = "a"\nrecord = "yes"',
            'image = "a"\n[[step]]\nclick = 5',                    # a name is a text
            'image = "a"\n[[step]]\nclick = ""',
            'image = "a"\n[[step]]\nconsole_errors = "0"',
            'image = "a"\n[[step]]\nconsole_errors = true',
            'image = "a"\n[[step]]\nwait = "abc"',
            'image = "a"\n[[step]]\nwait = false',
            'image = "a"\n[[step]]\nkey = 12',
            'image = "a"\n[[step]]\nkey = []',
            'image = "a"\n[[step]]\nreboot = false',
            'image = "a"\n[[step]]\nexec = "true"\ntimeout = "long"',
            'image = "a"\n[[step]]\nexec = "true"\nrc = "0"',
            'image = "a"\n[[step]]\nexpect = "x"\nexact = "no"',
            'image = "a"\n[[step]]\ngui = "app"\nenv = "A=1"',
            'image = "a"\n[[step]]\ndisk_add = "lots"',
            'image = "a"\n[[step]]\nclock = "tomorrow"',
            'image = "a"\n[[step]]\nclock = "+3d"\nstep = "daily"',
            # Options the action never reads: the step would run as if they were not there.
            'image = "a"\n[[step]]\nclick = "x"\nrc = 7',
            'image = "a"\n[[step]]\nclick = "x"\noutput = "y"',
            'image = "a"\n[[step]]\nkey = "f5"\ntimeout = 3',
            'image = "a"\n[[step]]\ntype = "x"\nrole = "text"',
            'image = "a"\n[[step]]\nexec = "true"\nenv = {A = "1"}',
            'image = "a"\n[[step]]\nclick = "x',                   # not TOML
            'image = "a"\n[[step]]\nopen = "localhost:9090/x"',     # Firefox would guess http://
            'image = "a"\n[[step]]\nviewport = "390 x 700"',
            'image = "a"\n[[step]]\nviewport = "phone"',
            'image = "a"\n[[step]]\nviewport = "390x700"\ntimeout = 5',
            'image = "a"\n[[step]]\nopen = "/x"\nbrowser = "edge"',
            'image = "a"\n[[step]]\nopen = "/x"\nbrowser = ["chromium"]',
            'image = "a"\n[[step]]\nclick = "x"\nbrowser = "chromium"',
            'image = "a"\n[[step]]\nclick = "x"\nbutton = "double"',
            'image = "a"\n[[step]]\nclick = "x"\nwindow = ""',
            'image = "a"\n[[step]]\nexpect = "x"\nbutton = "right"',
            'image = "a"\n[[step]]\ngui = "app"\ntimeout = 30',              # waits only with rc or output
            'image = "a"\n[[step]]\nset_text = "Repository URL"',           # what it gets is missing
            'image = "a"\n[[step]]\nmove = [10]',
            'image = "a"\n[[step]]\nmove = ["10", "20"]',
            'image = "a"\n[[step]]\nmove = [10, -1]',
            'image = "a"\n[[step]]\ndrag = [1, 2, 3]',
            'image = "a"\n[[step]]\nscroll = [1, 2, 0]',
            'image = "a"\n[[step]]\nscroll = [1, 2, 1.5]',
            'image = "a"\n[[step]]\nclick = [1, 2, 3]',
            'image = "a"\n[[step]]\nclick = [1, 2]\nrole = "button"',       # role finds by name
            'image = "a"\n[[step]]\nmove = [1, 2]\nbutton = "right"',
            'image = "a"\n[[step]]\nclick = [1, true]',
            'image = "a"\n[[step]]\nrelease = true',                         # nothing pressed
            'image = "a"\n[[step]]\npull = "/home/tester/a.tar"',            # to where
            'image = "a"\nbrowser = 3',
            'image = "a"\n[[step]]\nopen = "http://{guest:70000}/"',
            'image = "a"\n[[step]]\nexec = "x"\nto = "out/"',
            'image = "a"\n[[step]]\npress = "A"\n[[step]]\npress = "B"',   # still held
            'image = "a"\n[[step]]\npress = [1, 2]\nrole = "button"',
            'image = "a"\n[[step]]\ndrag = ["A"]',
            'image = "a"\n[[step]]\nrelease = false',
            'image = "a"\n[[step]]\nset_text = "URL"\ntext = 5',
            'image = "a"\n[[step]]\nclick = "URL"\ntext = "x"',
            'image = "a"\n[[step]]\ngui = "app"\nuntil = true',
            'image = "a"\n[[step]]\nclick = "x"\nuntil = true',
            'image = "a"\n[[step]]\nexpect = "x"\nstate = "visible"',        # no listing gives it
            'image = "a"\n[[step]]\nexpect = "x"\nstate = []',
            'image = "a"\n[[step]]\nexpect_absent = "x"\nstate = "focused"',  # absent has no state
            'image = "a"\n[[step]]\nclick = "x"\nstate = "focused"',
            'image = "a"\n[[step]]\ntype = "x"\nbutton = "right"',
        ):
            with self.subTest(text=text), self.assertRaises(DeskError):
                scenario.load(write(text))

    def test_build_dir_is_relative_to_the_scenario(self):
        # A repository of several projects: cockpit-x/test/desk/basic.toml builds in cockpit-x.
        with tempfile.TemporaryDirectory() as top:
            subprocess.run(["git", "init", "-q", top], check=True)
            here = Path(top, "cockpit-x", "test", "desk")
            here.mkdir(parents=True)
            Path(top, "cockpit-x", "cockpit-x-1.rpm").write_text("")
            plain, other = here / "plain.toml", here / "dir.toml"
            plain.write_text('image = "a"\n[build]\nartifacts = ["cockpit-x/*.rpm"]')
            other.write_text('image = "a"\n[build]\ndir = "../.."\nartifacts = ["*.rpm"]')
            self.assertEqual(scenario._base(plain, scenario.load(plain)), Path(top).resolve())
            base = scenario._base(other, scenario.load(other))
            self.assertEqual(base, Path(top, "cockpit-x").resolve())
            self.assertEqual([f.name for f in scenario._build(scenario.load(other), base)[0]], ["cockpit-x-1.rpm"])

    def test_no_build_takes_the_artifacts_as_they_are_and_says_so(self):
        with tempfile.TemporaryDirectory() as top:
            repo = Path(top)
            (repo / "app-1.rpm").write_text("")
            data = {"build": {"run": "touch built-anyway", "artifacts": ["*.rpm"]}}
            files, said = scenario._build(data, repo, skip=True)
            self.assertEqual([f.name for f in files], ["app-1.rpm"])
            self.assertFalse((repo / "built-anyway").exists())
            self.assertRegex(said, r"^skipped \(--no-build\); artifacts: app-1\.rpm \(\d{4}-\d\d-\d\d \d\d:\d\d\)$")
            (repo / "app-1.rpm").unlink()
            # Nothing to take: desk's failure before any run starts, never a run without the files.
            with self.assertRaisesRegex(DeskError, r"no build artifact matches \*\.rpm \(--no-build: build it"):
                scenario._build(data, repo, skip=True)
            self.assertEqual(scenario._build({"build": {"run": "touch x", "artifacts": []}}, repo)[1], "")

    def test_a_build_for_each_image(self):
        text = ('image = "fedora-44-gnome"\n[build]\nrun = "make"\nartifacts = ["plugin/a.so", "test/p0.sh"]\n'
                '[build.image.arch-gnome]\nrun = "make DISTRO=arch"\n'
                'artifacts = ["plugin/build/arch/a.so", "test/p0.sh"]\n'
                '[build.image.debian-13-gnome]\nrun = "make DISTRO=debian13"\n')
        data = scenario.load(write(text))
        self.assertEqual(scenario.for_image(data, "fedora-44-gnome")["build"],
                         {"run": "make", "artifacts": ["plugin/a.so", "test/p0.sh"]})
        self.assertEqual(scenario.for_image(data, "arch-gnome")["build"]["artifacts"][0], "plugin/build/arch/a.so")
        # What a variant leaves out comes from [build].
        self.assertEqual(scenario.for_image(data, "debian-13-gnome")["build"],
                         {"run": "make DISTRO=debian13", "artifacts": ["plugin/a.so", "test/p0.sh"]})
        with self.assertRaisesRegex(DeskError, r"\[build\] is for fedora-44-gnome and arch-gnome, debian-13-gnome, "
                                               r"not windows-11: add a \[build.image.windows-11\] table"):
            scenario.for_image(data, "windows-11")
        plain = scenario.load(write('image = "a"\n[build]\nrun = "make"\nartifacts = ["x"]'))
        self.assertEqual(scenario.for_image(plain, "b")["build"], {"run": "make", "artifacts": ["x"]})  # as before
        for bad in ('image = "a"\n[build.image.b]\nrun = 3', 'image = "a"\n[build.image.b]\nimage = "c"',
                    'image = "a"\n[build]\nimage = "b"'):
            with self.subTest(bad=bad), self.assertRaises(DeskError):
                scenario.load(write(bad))

    def test_what_each_action_takes(self):
        data = scenario.load(write('''
image = "a"
[[step]]
click = "Save"
role = "button"
exact = false
timeout = 5
name = "save it"
soft = true
[[step]]
click = "Example"
button = "right"
window = "System tray overflow window."
[[step]]
gui = ["app", "--flag"]
env = {LANG = "pl_PL.UTF-8", DEPTH = 2}
[[step]]
exec = "systemctl is-active foo"
until = true
timeout = 30.5
rc = 3
output = "active"
[[step]]
wait = 1.5
[[step]]
wait = true
timeout = 20
[[step]]
reboot = "dnf offline reboot -y"
timeout = 3600
[[step]]
clock = "+3d"
step = "1d"
[[step]]
clock = "reset"
[[step]]
type = "text"
anywhere = true
[[step]]
prepare = "admin"
[[step]]
disk_add = "8G:data"
[[step]]
open = "/example#/tasks"
timeout = 60
[[step]]
open = "https://localhost:9090/"
[[step]]
viewport = "390x700"
[[step]]
viewport = "reset"
[[step]]
open = "https://localhost:9090/"
browser = "chromium"
[[step]]
viewport = "390x700"
browser = "firefox"
'''))
        self.assertEqual(len(data["step"]), 18)

    def test_usb_steps_name_a_device_that_may_change_its_number(self):
        data = scenario.load(write('''image = "a"
usb = ["port:9-2.2"]
[[step]]
usb_rm = "serial:A00NA41632OR6N"
[[step]]
usb_add = "port:9-2.2"
timeout = 30
[[step]]
usb_add = "0fd9:008f"
'''))
        self.assertEqual([scenario.describe(s) for s in data["step"]],
                         [("usb_rm", "serial:A00NA41632OR6N"), ("usb_add", "port:9-2.2"), ("usb_add", "0fd9:008f")])
        for bad in ('usb_add = "deck"', 'usb_add = "9-2.2"', 'usb_rm = "port:9-2.2"\ntimeout = 5', 'usb_add = 3'):
            with self.subTest(bad), self.assertRaises(DeskError):
                scenario.load(write(f'image = "a"\n[[step]]\n{bad}'))
        with self.assertRaisesRegex(DeskError, "port:BUS-PORT"):
            scenario.load(write('image = "a"\nusb = ["deck"]'))

    def test_usb_steps_hand_over_and_take_back_by_what_they_name(self):
        deck = {"id": "0fd9:008f", "name": "Stream Deck XL", "bus": 9, "dev": 31}
        run = SimpleNamespace()
        with mock.patch("desk.usb.wait_present") as present, mock.patch("desk.usb.add", return_value=deck) as add, \
             mock.patch("desk.usb.remove", return_value=deck) as remove:
            got = scenario._step(run, SimpleNamespace(), None, {"usb_add": "port:9-2.2", "timeout": 30})
            self.assertEqual(got, "USB device 0fd9:008f Stream Deck XL (9.31) handed to the guest")
            present.assert_called_once_with("port:9-2.2", 30)
            add.assert_called_once_with(run, "port:9-2.2", at=None, hub_options=None)
            got = scenario._step(run, SimpleNamespace(), None, {"usb_rm": "port:9-2.2"})
            self.assertEqual(got, "USB device 0fd9:008f (9.31) back with the host")
            remove.assert_called_once_with(run, "port:9-2.2")

    def test_usb_steps_place_devices_at_port_paths_and_take_them_out_there(self):
        deck = {"id": "0fd9:008f", "name": "Stream Deck XL", "bus": 9, "dev": 31, "at": "5.1.4"}
        run = SimpleNamespace()
        hubs = ["5=067b:2586/4/hs", "5.1=067b:2586/4/hs"]
        with mock.patch("desk.usb.wait_present"), mock.patch("desk.usb.add", return_value=deck) as add, \
             mock.patch("desk.usb.remove", return_value={"id": "at:5.1", "tree": True, "at": "5.1",
                                                          "below": ["5.1.4"]}) as remove:
            got = scenario._step(run, SimpleNamespace(), None, {"usb_add": "port:9-2.2", "at": "5.1.4", "hubs": hubs})
            self.assertEqual(got, "USB device 0fd9:008f Stream Deck XL (9.31) handed to the guest at port path 5.1.4")
            add.assert_called_once_with(run, "port:9-2.2", at="5.1.4", hub_options=hubs)
            got = scenario._step(run, SimpleNamespace(), None, {"usb_rm": "at:5.1"})
            self.assertEqual(got, "at:5.1 removed, with 5.1.4 below it")
            remove.assert_called_once_with(run, "at:5.1")

    def test_port_paths_need_the_usbvfiod_controller_and_are_checked_without_a_guest(self):
        steps = '[[step]]\nusb_add = "port:9-2.2"\nat = "5.1.4"\nhubs = ["5=067b:2586/4", "5.1=/4"]\n'
        data = scenario.load(write('image = "a"\nusbvfiod = true\n' + steps))
        self.assertTrue(data["usbvfiod"])
        for text, message in (
                ('image = "a"\n' + steps, "usbvfiod = true"),
                ('image = "a"\n[[step]]\nusb_rm = "at:5.1"', "usbvfiod = true"),
                ('image = "a"\nusbvfiod = 1\n', "usbvfiod is true or false"),
                ('image = "a"\nusbvfiod = true\n[[step]]\nusb_add = "port:9-2.2"\nhubs = ["5=/4"]', "hubs go with at"),
                ('image = "a"\nusbvfiod = true\n[[step]]\nusb_add = "port:9-2.2"\nat = "1.4"', "takes a port path"),
                ('image = "a"\nusbvfiod = true\n[[step]]\nusb_add = "port:9-2.2"\nat = "5.1.9"\n'
                 'hubs = ["5.1=/4"]', "has 4 ports"),
                ('image = "a"\nusbvfiod = true\n[[step]]\nusb_add = "port:9-2.2"\nat = "5.1"\n'
                 'hubs = ["6=/4"]', "not on the way"),
                ('image = "a"\nusbvfiod = true\n[[step]]\nusb_rm = "at:5.1"\nat = "5.1"', "does not use at")):
            with self.subTest(text), self.assertRaisesRegex(DeskError, message):
                scenario.load(write(text))

    def test_host_steps_take_a_program_and_save_for_later_steps(self):
        data = scenario.load(write('''image = "a"
[[step]]
host = ["del", "connect"]
save = "ADDR"
timeout = 40
[[step]]
usb_add = "{ADDR}"
[[step]]
exec = "echo {ADDR}"
output = "{ADDR}"
'''))
        self.assertEqual(scenario.describe(data["step"][0]), ("host", "del connect"))
        for bad, why in (('host = "del connect"', "no shell"), ('host = []', "no shell"),
                         ('host = ["x"]\nsave = "addr"', "capitals"), ('exec = "x"\nsave = "A"', "does not use save"),
                         ('usb_add = "{ADDR}"', "no step before it saves ADDR"),
                         ('exec = "echo {LATER}"\n[[step]]\nhost = ["x"]\nsave = "LATER"', "saves LATER")):
            with self.subTest(bad), self.assertRaisesRegex(DeskError, why):
                scenario.load(write(f'image = "a"\n[[step]]\n{bad}'))

    def test_a_host_step_runs_without_a_shell_names_the_run_and_saves_its_stdout(self):
        import sys
        run = SimpleNamespace(id="r1", dir=Path("/runs/r1"), ssh_port=2222)
        steps = SimpleNamespace(saved={}, base=Path.cwd())
        code = "import os; print('ignored'); print(os.environ['DESK_RUN'], os.environ['DESK_RUN_DIR'], " \
               "os.environ['DESK_SSH_PORT'], os.environ['X'], '$HOME')"
        got = scenario._step(run, SimpleNamespace(), steps,
                             {"host": [sys.executable, "-c", code], "save": "OUT", "env": {"X": 1}})
        self.assertEqual(got, "exit 0: r1 /runs/r1 2222 1 $HOME")
        self.assertEqual(steps.saved, {"OUT": "ignored\nr1 /runs/r1 2222 1 $HOME"})
        later = scenario._substituted({"usb_add": "{OUT}", "name": "x"}, {"OUT": "13.40"})
        self.assertEqual(later, {"usb_add": "13.40", "name": "x"})
        with self.assertRaisesRegex(DeskError, r"\{MISSING\} has no value"):
            scenario._substituted({"exec": "echo {MISSING}"}, {})

    def test_a_named_host_step_still_shows_its_command(self):
        self.assertEqual(scenario.step_label({"host": ["del", "connect"], "name": "reconnect"}),
                         "reconnect (host del connect)")
        self.assertEqual(scenario.step_label({"host": ["del", "stop"]}), "host del stop")
        self.assertEqual(scenario.step_label({"exec": "true", "name": "check"}), "check")

    def test_a_host_step_fails_like_exec(self):
        import sys
        run = SimpleNamespace(id="r1", dir=Path("/runs/r1"), ssh_port=2222)
        steps = SimpleNamespace(saved={}, base=Path.cwd())
        fail = [sys.executable, "-c", "import sys; print('said'); print('wrong', file=sys.stderr); sys.exit(3)"]
        with self.assertRaisesRegex(scenario.StepFailed, "(?s)exit 3.*stdout:\nsaid.*stderr:\nwrong"):
            scenario._step(run, SimpleNamespace(), steps, {"host": fail})
        self.assertEqual(scenario._step(run, SimpleNamespace(), steps, {"host": fail, "rc": 3}), "exit 3: said")
        with self.assertRaisesRegex(scenario.StepFailed, "still running after 1s"):
            scenario._step(run, SimpleNamespace(), steps,
                           {"host": [sys.executable, "-c", "import time; time.sleep(5)"], "timeout": 1})
        with self.assertRaisesRegex(DeskError, "cannot run /nonexistent/tool"):
            scenario._step(run, SimpleNamespace(), steps, {"host": ["/nonexistent/tool"]})

    def test_a_missing_file_is_a_mistake_too(self):
        with self.assertRaisesRegex(DeskError, "No such file"):
            scenario.load(Path("/nonexistent/scenario.toml"))

    def test_disks_and_console(self):
        data = scenario.load(write('''
image = "fedora-44-gnome-cockpit"
disks = ["8G:data"]
[[step]]
disk_rm = "data"
[[step]]
console_errors = 0
'''))
        self.assertEqual(data["disks"], ["8G:data"])
        self.assertEqual(scenario.describe(data["step"][0]), ("disk_rm", "data"))


    def test_until_only_for_commands(self):
        data = scenario.load(write('image = "a"\n[[step]]\nexec = "systemctl is-active foo"\nuntil = true\n'))
        self.assertTrue(data["step"][0]["until"])
        with self.assertRaises(DeskError):
            scenario.load(write('image = "a"\n[[step]]\nclick = "x"\nuntil = true\n'))

    def test_failure_shows_the_end_of_the_output(self):
        out = "\n".join(f"line {i}" for i in range(40))
        self.assertEqual(scenario.tail(out).splitlines(), [f"line {i}" for i in range(25, 40)])


class FixedWaits(unittest.TestCase):
    def test_flags_fixed_waits_but_not_polling(self):
        data = {"step": [{"wait": 2}, {"wait": True}, {"exec": "sleep 1; pgrep -x app"},
                         {"exec": "sleep 1; test -f x", "until": True}, {"root": "systemctl start x"}]}
        found = scenario.fixed_waits(data)
        self.assertEqual([w.split(":")[0] for w in found], ["step 1", "step 3"])


class ConsoleErrors(unittest.TestCase):
    """console_errors counts what it can tell: Chromium's messages have no level."""

    def check(self, *consoles: list[str], value: int = 0) -> list:
        steps = SimpleNamespace(console_seen=0, console_unlevelled=0)
        out = []
        with mock.patch("desk.scenario.browser.console", side_effect=consoles):
            for _ in consoles:
                try:
                    out.append(scenario._step(SimpleNamespace(), SimpleNamespace(), steps,
                                              {"console_errors": value}))
                except Exception as e:  # noqa: BLE001 - what each check ended with
                    out.append(e)
        return out

    def test_firefox_errors_are_counted(self):
        ok, bad = self.check(['10:00:00 console.log: "x"'], ['10:00:00 console.log: "x"',
                                                            '10:00:01 console.error: "y"'])
        self.assertEqual(ok, "0 console errors")
        self.assertIsInstance(bad, scenario.StepFailed)

    def test_reset_leaves_out_what_came_before_and_says_so_and_the_count_starts_there(self):
        before = ['10:00:00 console.error: "Cockpit\'s own Overview"']
        after = before + ['10:00:05 console.error: "the product\'s"']
        steps = SimpleNamespace(console_seen=0, console_unlevelled=0)
        with mock.patch("desk.scenario.browser.console", side_effect=[before, after]):
            reset = scenario._step(SimpleNamespace(), SimpleNamespace(), steps, {"console_errors": "reset"})
            self.assertEqual(reset, "1 console messages before this point left out: "
                                    "10:00:00 console.error: \"Cockpit's own Overview\"")
            with self.assertRaisesRegex(scenario.StepFailed, "1 console errors \\(at most 0\\).*the product's"):
                scenario._step(SimpleNamespace(), SimpleNamespace(), steps, {"console_errors": 0})
        self.assertEqual(scenario.load(write('image = "a"\n[[step]]\nconsole_errors = "reset"'))["step"][0]
                         ["console_errors"], "reset")
        with self.assertRaises(DeskError):
            scenario.load(write('image = "a"\n[[step]]\nconsole_errors = "start"'))

    def test_chromium_messages_cannot_be_counted_as_clean(self):
        maybe = '10:00:00 Chromium console: "an error" (https://app.test/, line 8)'
        uncaught = "10:00:01 JavaScript error: https://app.test/, line 11: Uncaught TypeError: x"
        first, second = self.check([maybe], [maybe])
        self.assertIsInstance(first, browser.Unchecked)
        self.assertIn("1 Chromium console messages that could be errors or not", str(first))
        # Only new ones since the previous check.
        self.assertEqual(second, "0 console errors")
        # An uncaught exception is an error whatever else there is: the product's failure.
        (failed,) = self.check([maybe, uncaught])
        self.assertIsInstance(failed, scenario.StepFailed)

    def test_the_steps_stop_with_it_as_desks_failure(self):
        run = mock.Mock()
        with mock.patch("desk.scenario._step", side_effect=browser.Unchecked("1 Chromium console messages")), \
                mock.patch("desk.scenario.Steps"), mock.patch("desk.scenario.display"), \
                self.assertRaises(browser.Unchecked):
            scenario._run_steps(run, LostConnection.SPEC, {"step": [{"console_errors": 0, "soft": True},
                                                                    {"exec": "true"}]})
        self.assertEqual(len([c for c in run.log_event.call_args_list if c.kwargs.get("ok") is False]), 1)


class LostConnection(unittest.TestCase):
    """ssh's 255 is no answer from the guest: desk's failure (exit 2), not a failed step (1)."""

    RUN = SimpleNamespace(ssh_port=1, user="tester")
    SPEC = SimpleNamespace(root_user="root", family="linux", shell="bash")

    def shell(self, step: dict, *codes: int) -> str:
        answers = [subprocess.CompletedProcess([], c, b"", b"Connection refused" if c == 255 else b"") for c in codes]
        with mock.patch("desk.scenario.guest.ssh_run", side_effect=answers), \
                mock.patch("desk.scenario.time.sleep"):
            return scenario._shell(self.RUN, self.SPEC, step, as_root=False)

    def test_no_connection_is_not_a_failed_step(self):
        # The command's 255, then the same from a plain `exit 0`: ssh's own.
        with self.assertRaisesRegex(guest.NoConnection, "Connection refused"):
            self.shell({"exec": "true"}, 255, 255)

    def test_a_program_that_exits_255_itself_is_a_failed_step(self):
        with self.assertRaisesRegex(scenario.StepFailed, "exit 255"):
            self.shell({"exec": "myprog --selfcheck"}, 255, 0)

    def test_a_reset_handshake_is_not_a_failed_step(self):
        # 331002: the guest answered `exit 0` right after; only ssh's line says whose 255 it was.
        reset = b"kex_exchange_identification: read: Connection reset by peer\r\n"
        answers = [subprocess.CompletedProcess([], 255, b"", reset), subprocess.CompletedProcess([], 0, b"", b"")]
        with mock.patch("desk.scenario.guest.ssh_run", side_effect=answers), mock.patch("desk.scenario.time.sleep"), \
                self.assertRaisesRegex(guest.NoConnection, "no SSH connection.*kex_exchange_identification"):
            scenario._shell(self.RUN, self.SPEC, {"exec": "true"}, as_root=False)

    def test_an_agent_that_does_not_answer_stops_the_steps_as_desks_failure(self):
        from desk import winagent

        run = mock.Mock()
        gone = winagent.AgentGone("in-session agent not reachable ([Errno 104] Connection reset by peer)")
        with mock.patch("desk.scenario._step", side_effect=gone), mock.patch("desk.scenario.Steps"), \
                mock.patch("desk.scenario.display"), self.assertRaises(winagent.AgentGone):
            scenario._run_steps(run, self.SPEC, {"step": [{"click": "Configure", "soft": True}, {"exec": "true"}]})
        self.assertEqual(len([c for c in run.log_event.call_args_list if c.kwargs.get("ok") is False]), 1)

    def test_until_tries_again_while_the_guest_comes_back(self):
        self.assertEqual(self.shell({"exec": "true", "until": True}, 255, 255, 0), "")

    def test_a_step_that_expects_255_gets_it(self):
        self.assertEqual(self.shell({"exec": "reboot", "rc": 255}, 255), "")

    def test_an_output_of_several_lines_is_found_in_windows_lines(self):
        answer = subprocess.CompletedProcess([], 0, b"3\r\nTrue\r\n", b"")
        with mock.patch("desk.scenario.guest.ssh_run", return_value=answer):
            self.assertEqual(scenario._shell(self.RUN, self.SPEC, {"exec": "x", "output": "3\nTrue"}, as_root=False),
                             "True")

    def test_steps_on_the_accessibility_tree_do_not_wait_it_out(self):
        from desk import steps

        lost = subprocess.CompletedProcess([], 255, b"", b"Connection refused")
        target = SimpleNamespace(ssh_port=1, user="tester", agent_port=0, dom=lambda: None)
        for step in (lambda s: s.wait_for("Saved", timeout=30), lambda s: s.first(("Saved", None), timeout=30),
                     lambda s: s.gui("gnome-text-editor")):
            with self.subTest(step=step), mock.patch("desk.guest.ssh_run", return_value=lost), \
                    mock.patch("desk.steps.display"), mock.patch("desk.steps.time.sleep") as slept, \
                    self.assertRaisesRegex(guest.NoConnection, "no SSH connection to the guest.*Connection refused"):
                step(steps.Steps(SimpleNamespace(family="linux", need_desktop=lambda what: None), target))
            slept.assert_not_called()

    def test_the_steps_stop_with_it(self):
        run = mock.Mock()
        with mock.patch("desk.scenario._step", side_effect=guest.NoConnection("no SSH connection")), \
                mock.patch("desk.scenario.Steps"), mock.patch("desk.scenario.display"), \
                mock.patch("desk.scenario.runs.host_state", return_value="the VM is no longer running"), \
                self.assertRaisesRegex(guest.NoConnection, "no SSH connection; the VM is no longer running"):
            scenario._run_steps(run, self.SPEC, {"step": [{"exec": "true", "soft": True}, {"exec": "true"}]})
        failed = [c.kwargs for c in run.log_event.call_args_list if c.kwargs.get("ok") is False]
        self.assertIn("the VM is no longer running", failed[0]["detail"])  # for the report

    def test_host_state_says_whether_the_vm_still_runs_and_what_the_host_has(self):
        from desk import runs

        with tempfile.TemporaryDirectory() as tmp:
            run = SimpleNamespace(id="r1", dir=Path(tmp), domain="desk-r1", live_domain=lambda: None, usbvfiod={})
            with mock.patch("desk.runs.all_runs", return_value=[run]):
                said = runs.host_state(run)
            self.assertTrue(said.startswith("the VM is no longer running (QEMU ended; coredumpctl and libvirt's "
                                            "log of desk-r1 may say why); host: "))
            self.assertIn(f"GiB free in {tmp}", said)
            self.assertIn("MiB memory available", said)
            self.assertIn("other runs: none", said)
            run.live_domain = lambda: object()
            with mock.patch("desk.runs.all_runs", return_value=[run]):
                self.assertTrue(runs.host_state(run).startswith("the VM is running but does not answer; host: "))


class OtherWindows(unittest.TestCase):
    """`window` reaches elements outside the focused window: the taskbar, a tray's flyout."""

    SNAP = {"tops": [{"app": "example-app", "pid": 1, "title": "Example", "focus": True, "start": 0},
                     {"app": "explorer", "pid": 2, "title": "System tray overflow window.", "focus": False,
                      "start": 1}],
            "nodes": [{"pid": 1, "path": [0], "role": "window", "name": "Example", "depth": 0, "states": [],
                       "bbox": [0, 0, 800, 600]},
                      {"pid": 2, "path": [0, 0], "role": "button", "name": "Example", "depth": 1, "states": [],
                       "bbox": [1030, 700, 32, 32]}],
            "truncated": False}

    def steps(self):
        from desk import steps

        target = SimpleNamespace(agent_port=1, dom=lambda: "dom")
        return steps.Steps(SimpleNamespace(family="windows", need_desktop=lambda what: None), target), target

    def test_a_click_in_another_window_with_the_button_asked_for(self):
        s, target = self.steps()
        with mock.patch("desk.a11y.snapshot", return_value=self.SNAP) as snapshot, \
                mock.patch("desk.a11y._visible_point", return_value=(1046, 715)) as point, \
                mock.patch("desk.steps.display") as display:
            s.click("Example", button="right", window="overflow")
        self.assertTrue(snapshot.call_args.kwargs["all_windows"])
        self.assertEqual(point.call_args.args[1]["pid"], 2)  # the icon, not the window of the same name
        display.click.assert_called_once_with("dom", 1046, 715, button="right", count=1, run=target)

    def test_no_such_window_says_which_there_are(self):
        s, _ = self.steps()
        with mock.patch("desk.a11y.snapshot", return_value=self.SNAP), mock.patch("desk.steps.time.sleep"), \
                self.assertRaisesRegex(DeskError, "no window with 'Taskbar' in its title or application name.*"
                                                  r"'System tray overflow window\.' \(explorer\)"):
            s.wait_for("Show Hidden Icons", window="Taskbar", timeout=0.01)


class ExpectState(unittest.TestCase):
    """`expect` with a state the element must have, and `window` on expect and expect_absent."""

    SNAP = {"tops": [{"app": "example-app", "pid": 1, "title": "Example", "focus": True, "start": 0},
                     {"app": "gnome-shell", "pid": 2, "title": "Main stage", "focus": False, "start": 2}],
            "nodes": [{"pid": 1, "path": [0, 1], "role": "push button", "name": "Devices", "states": [],
                       "bbox": [0, 0, 80, 30]},
                      {"pid": 1, "path": [0, 2], "role": "combo box", "name": "Device", "states": ["focused"],
                       "bbox": [0, 40, 80, 30]},
                      {"pid": 2, "path": [0, 0], "role": "menu", "name": "Example", "states": [],
                       "bbox": [1150, 0, 30, 30]}],
            "truncated": False}

    def steps(self):
        from desk import steps

        target = SimpleNamespace(agent_port=0, dom=lambda: "dom")
        return steps.Steps(SimpleNamespace(family="linux", need_desktop=lambda what: None), target)

    def test_the_element_that_has_the_state(self):
        with mock.patch("desk.a11y.snapshot", return_value=self.SNAP):
            # "Devices" comes first and contains "Device" too, but has no focus.
            self.assertEqual(self.steps().wait_for("Device", exact=False, states=["focused"])["role"], "combo box")

    def test_there_but_without_the_state_names_it(self):
        with mock.patch("desk.a11y.snapshot", return_value=self.SNAP), mock.patch("desk.steps.time.sleep"), \
                self.assertRaisesRegex(DeskError, "'Devices' is there but not checked within 0s; "
                                                  "it is in no such state"):
            self.steps().wait_for("Devices", states=["checked"], timeout=0.01)

    def test_another_window_for_expect_absent(self):
        s = self.steps()
        focused = {**self.SNAP, "tops": self.SNAP["tops"][:1], "nodes": self.SNAP["nodes"][:2]}

        def snapshot(run, all_windows=False, **kw):
            return self.SNAP if all_windows else focused

        with mock.patch("desk.a11y.snapshot", side_effect=snapshot), mock.patch("desk.steps.time.sleep"):
            s.wait_gone("Example", role="menu", timeout=0.01)  # not in the focused window
            with self.assertRaisesRegex(DeskError, "'Example' is still on screen"):
                s.wait_gone("Example", role="menu", window="gnome-shell", timeout=0.01)

    def test_a_tree_that_could_not_be_read_is_desks_failure_not_the_products(self):
        from desk import a11y

        s = self.steps()
        broken = DeskError("a11y helper failed: Couldn't connect to accessibility bus")
        with mock.patch("desk.a11y.snapshot", side_effect=broken), mock.patch("desk.steps.time.sleep"):
            for wait in (lambda: s.wait_for("Configure", timeout=0.01), lambda: s.wait_gone("Quit", timeout=0.01)):
                with self.subTest(wait=wait), self.assertRaisesRegex(a11y.Unread, "accessibility bus"):
                    wait()
        run = mock.Mock()
        with mock.patch("desk.scenario._step", side_effect=a11y.Unread("could not be read")), \
                mock.patch("desk.scenario.Steps"), mock.patch("desk.scenario.display"), \
                self.assertRaises(a11y.Unread):
            scenario._run_steps(run, LostConnection.SPEC, {"step": [{"expect": "x", "soft": True}, {"exec": "true"}]})

    def test_state_is_read_from_the_file(self):
        data = scenario.load(write('image = "a"\n[[step]]\nexpect = "x"\nstate = ["focused", "checked"]\n'
                                   'window = "Example"\n[[step]]\nexpect_absent = "y"\nwindow = "Example"'))
        self.assertEqual(data["step"][0]["state"], ["focused", "checked"])


class GuiWaited(unittest.TestCase):
    """A gui step with rc or output: the program in the session, waited for and checked like exec."""

    def run_(self, agent_port=0):
        return SimpleNamespace(ssh_port=1, user="tester", agent_port=agent_port)

    SPEC = SimpleNamespace(root_user="root", family="linux", shell="bash", need_desktop=lambda what: None)

    def test_loads_with_rc_output_timeout_until(self):
        data = scenario.load(write('image = "a"\n[[step]]\ngui = "wl-paste"\noutput = "x"\ntimeout = 20\n'
                                   'until = true\n[[step]]\ngui = ["msiexec.exe", "/i", "a.msi", "/qn"]\nrc = 0'))
        self.assertEqual(len(data["step"]), 2)

    def test_linux_in_the_session_with_its_limit_in_the_guest(self):
        answer = subprocess.CompletedProcess([], 0, b"copied text\n", b"")
        with mock.patch("desk.scenario.guest.ssh_run", return_value=answer) as ssh:
            said = scenario._step(self.run_(), self.SPEC, mock.Mock(),
                                  {"gui": "wl-paste", "output": "copied", "timeout": 20})
        self.assertEqual(said, "copied text")
        command = ssh.call_args.args[2]
        self.assertIn("systemd-run --user", command)
        self.assertIn("--wait --pipe", command)
        self.assertIn("RuntimeMaxSec=20", command)

    WINDOWS = SimpleNamespace(family="windows", need_desktop=lambda what: None)

    def test_windows_through_the_agent_without_a_file_between(self):
        with mock.patch("desk.scenario.winagent.request", return_value={"stdout": "3\r\n", "code": 0}) as agent:
            said = scenario._step(self.run_(agent_port=2), self.WINDOWS, mock.Mock(),
                                  {"gui": ["msiexec.exe", "/i", "a.msi", "/qn"], "rc": 0})
        self.assertEqual(said, "3")
        asked = agent.call_args.kwargs
        self.assertEqual((asked["cmd"], asked["file"], asked["args"], asked["wait"], asked["timeout_s"]),
                         ("run", "msiexec.exe", ["/i", "a.msi", "/qn"], True, 600))

    def test_a_program_over_its_time_is_stopped_and_said_so(self):
        with mock.patch("desk.scenario.winagent.request", return_value={"timeout": True, "stdout": "half"}), \
                self.assertRaisesRegex(scenario.StepFailed, "still running after 5s, stopped"):
            scenario._step(self.run_(agent_port=2), self.WINDOWS, mock.Mock(),
                           {"gui": "powershell.exe -Command Start-Sleep 99", "rc": 0, "timeout": 5})

    def test_without_rc_or_output_it_starts_the_program_as_before(self):
        steps = mock.Mock()
        with mock.patch("desk.scenario.guest.ssh_run") as ssh:
            scenario._step(self.run_(), self.SPEC, steps, {"gui": "gnome-calculator"})
        steps.gui.assert_called_once_with("gnome-calculator", env={})
        ssh.assert_not_called()


class SetText(unittest.TestCase):
    """set_text: a field found as click finds it, its text replaced as desk set-text does, then read back."""

    NODE = {"pid": 1, "path": [0, 4], "role": "entry", "name": "Repository URL", "states": ["editable"],
            "value": "https://example.org/x.git", "bbox": [0, 0, 200, 30]}

    def steps(self):
        from desk import steps

        target = SimpleNamespace(agent_port=0, dom=lambda: "dom")
        return steps.Steps(SimpleNamespace(family="linux", layout="us", need_desktop=lambda what: None), target)

    def test_loads(self):
        data = scenario.load(write('image = "a"\n[[step]]\nset_text = "Repository URL"\nrole = "entry"\n'
                                   'text = "/srv/repo.git"\n[[step]]\nset_text = "Filter"\ntext = ""'))
        self.assertEqual(data["step"][1]["text"], "")

    def test_the_field_holds_the_text(self):
        s = self.steps()
        with mock.patch.object(s, "wait_for", return_value=self.NODE) as found, \
                mock.patch("desk.a11y.replace_text", return_value="") as replaced:
            self.assertEqual(s.set_text("Repository URL", "/srv/repo.git", role="entry"), "")
        found.assert_called_once_with("Repository URL", "entry", True, 60, None)
        spec = replaced.call_args.args[1]
        self.assertNotIn("value", spec)  # an editable field is found again by its name, not its old text
        self.assertEqual(replaced.call_args.args[2], "/srv/repo.git")

    def test_the_field_rather_than_its_label_and_a_text_set_behind_the_page_is_desks_failure(self):
        from desk.config import NotDone

        s = self.steps()
        label = {**self.NODE, "role": "label", "states": [], "path": [0, 3]}
        s.snap = {"nodes": [label, self.NODE]}
        with mock.patch.object(s, "wait_for", return_value=label), \
                mock.patch("desk.a11y._matches", return_value=[label, self.NODE]), \
                mock.patch("desk.a11y.replace_text", return_value="") as replaced:
            s.set_text("Repository URL", "/srv/x")
        self.assertEqual(replaced.call_args.args[1]["path"], [0, 4])
        with mock.patch.object(s, "wait_for", return_value=self.NODE), \
                mock.patch("desk.a11y.replace_text", return_value="the field did not take the keyboard focus, so "
                                                                  "its text was replaced through AT-SPI only"), \
                self.assertRaisesRegex(NotDone, "replaced through AT-SPI only"):
            s.set_text("Repository URL", "/srv/x")

    def test_a_field_that_did_not_take_it_fails_with_what_it_shows(self):
        s = self.steps()
        with mock.patch.object(s, "wait_for", return_value=self.NODE), \
                mock.patch("desk.a11y.replace_text", return_value="the field shows 'read only' afterwards"), \
                self.assertRaisesRegex(DeskError, "entry 'Repository URL' did not take '/srv/x': "
                                                  "the field shows 'read only' afterwards"):
            s.set_text("Repository URL", "/srv/x")


class PointerSteps(unittest.TestCase):
    """The machine's mouse at screen pixels: click [x, y], move, drag, scroll."""

    def steps(self):
        from desk import steps

        target = SimpleNamespace(agent_port=0, dom=lambda: "dom")
        return steps.Steps(SimpleNamespace(family="linux", need_desktop=lambda what: None), target), target

    def test_loads(self):
        data = scenario.load(write('image = "a"\n[[step]]\nclick = [640, 400]\nbutton = "right"\n'
                                   '[[step]]\nclick = "Open"\ndouble = true\n[[step]]\nmove = [10, 20]\n'
                                   '[[step]]\ndrag = [1, 2, 300, 400]\n[[step]]\nscroll = [5, 5, -3]'))
        self.assertEqual([scenario.describe(st)[0] for st in data["step"]],
                         ["click", "click", "move", "drag", "scroll"])

    def test_each_goes_to_its_display_function_after_the_points_are_checked(self):
        s, target = self.steps()
        with mock.patch("desk.steps.display") as display:
            s.pointer("click", [640, 400], "right", 2)
            s.pointer("drag", [1, 2, 300, 400])
            s.pointer("scroll", [5, 5, -3])
            s.pointer("move", [10, 20])
        self.assertEqual(display.check_points.call_args_list[1].args, ("dom", [(1, 2), (300, 400)]))
        display.click.assert_called_once_with("dom", 640, 400, button="right", count=2, run=target)
        display.drag.assert_called_once_with("dom", 1, 2, 300, 400, run=target)
        display.scroll.assert_called_once_with("dom", 5, 5, -3, run=target)
        display.move.assert_called_once_with("dom", 10, 20, run=target)

    def test_a_point_off_the_screen_sends_nothing_and_is_desks_failure(self):
        from desk import display

        s, _ = self.steps()
        with mock.patch("desk.display.screen_sizes", return_value=[(1280, 800)]), \
                mock.patch("desk.display.qmp") as qmp, \
                self.assertRaisesRegex(display.OffScreen, r"\(1300,10\) is outside the screen, which is 1280"):
            s.pointer("drag", [10, 10, 1300, 10])
        qmp.assert_not_called()  # not even the press, which a drag that ends off the screen once left down
        with mock.patch("desk.display.screen_sizes", return_value=[(1280, 800)]), \
                self.assertRaisesRegex(display.OffScreen, "below monitor 1, which is 800 pixels high"):
            display.check_points("dom", [(10, 900)])
        run = mock.Mock()
        with mock.patch("desk.scenario._step", side_effect=display.OffScreen("off")), \
                mock.patch("desk.scenario.Steps"), mock.patch("desk.scenario.display.screenshot"), \
                self.assertRaises(display.OffScreen):
            scenario._run_steps(run, LostConnection.SPEC, {"step": [{"move": [9999, 1], "soft": True}, {"exec": "x"}]})


class HoldAndRelease(unittest.TestCase):
    """press keeps a button down for the steps that follow; move glides with it; release lets go."""

    def steps(self):
        from desk import steps

        target = SimpleNamespace(agent_port=0, dom=lambda: "dom")
        s = steps.Steps(SimpleNamespace(family="linux", need_desktop=lambda what: None), target)
        places = {"Key 1 of 15 keys": (400, 200), "Media control": (120, 240), "Key 6 of 15 keys": (600, 300)}
        s.point = lambda where, **find: tuple(where) if isinstance(where, list) else places[where]
        return s, target

    def test_loads(self):
        data = scenario.load(write('image = "a"\n[[step]]\npress = "Media control"\nrole = "button"\n'
                                   '[[step]]\nmove = "Key 1 of 15 keys"\n[[step]]\nkey = "escape"\n'
                                   '[[step]]\nrelease = true\n[[step]]\ndrag = ["Media control", "Key 1"]\n'
                                   '[[step]]\npress = [10, 20]\nbutton = "right"\n[[step]]\nrelease = [30, 40]'))
        self.assertEqual(len(data["step"]), 7)

    def test_press_glide_and_release_by_name(self):
        s, target = self.steps()
        with mock.patch("desk.steps.display") as display:
            s.press("Media control")
            s.pointer("move", "Key 1 of 15 keys")
            self.assertEqual(s.held, "left")
            s.release("Key 6 of 15 keys")
        display.press.assert_called_once_with("dom", 120, 240, button="left", run=target)
        self.assertEqual([c.args for c in display.glide.call_args_list],
                         [("dom", 120, 240, 400, 200), ("dom", 400, 200, 600, 300)])
        display.move.assert_not_called()  # with the button down a move glides, for drag-and-drop
        display.release.assert_called_once_with("dom", 600, 300, button="left", run=target)
        self.assertIsNone(s.held)

    def test_a_drag_from_name_to_name(self):
        s, target = self.steps()
        with mock.patch("desk.steps.display") as display:
            s.pointer("drag", ["Media control", "Key 1 of 15 keys"])
        display.drag.assert_called_once_with("dom", 120, 240, 400, 200, run=target)

    def test_a_button_held_when_a_step_fails_or_the_steps_end_is_let_go(self):
        from desk import steps as steps_module

        for later in ({"expect": "x"}, None):
            run = mock.Mock(agent_port=0)
            data = {"step": [{"press": [10, 20]}] + ([later] if later else [])}
            with self.subTest(later=later), mock.patch("desk.steps.display") as display, \
                    mock.patch("desk.scenario.display.screenshot"), \
                    mock.patch.object(steps_module.Steps, "wait_for", side_effect=DeskError("did not appear")):
                scenario._run_steps(run, SimpleNamespace(family="linux", need_desktop=lambda w: None), data)
            display.release.assert_called_once_with(run.dom(), 10, 20, button="left", run=run)

    def test_a_drag_lets_go_also_when_its_way_fails(self):
        from desk import display

        with mock.patch("desk.display.press"), mock.patch("desk.display.glide", side_effect=DeskError("off")), \
                mock.patch("desk.display.release") as release, self.assertRaises(DeskError):
            display.drag("dom", 1, 2, 3, 4)
        release.assert_called_once_with("dom", 3, 4, run=None)


class Pull(unittest.TestCase):
    """pull: a guest's file onto the host for a later scenario, its SHA-256 checked both sides."""

    RUN = SimpleNamespace(ssh_port=1, user="tester", log_event=lambda **kw: None)
    LINUX = SimpleNamespace(family="linux", shell="bash", root_user="root")

    def pull(self, base, step, spec=None, guest_says=None, content=b"backup", scp_rc=0):
        import hashlib

        said = guest_says or subprocess.CompletedProcess([], 0, (hashlib.sha256(b"backup").hexdigest() + "  -\n")
                                                         .encode(), b"")

        def scp(port, src, dst, timeout=None):
            if scp_rc == 0:
                Path(dst).write_bytes(content)
            return scp_rc

        with mock.patch("desk.scenario.guest.ssh_run", return_value=said), \
                mock.patch("desk.scenario.guest.scp", side_effect=scp) as copied:
            return scenario._pull(self.RUN, spec or self.LINUX, base, step), copied

    def test_into_a_folder_with_its_size_and_sum(self):
        import hashlib

        with tempfile.TemporaryDirectory() as top:
            said, copied = self.pull(Path(top), {"pull": "/home/tester/Documents/fedora-full.backup",
                                                 "to": "target/desk-handoff/"})
            dest = Path(top, "target/desk-handoff/fedora-full.backup")
            self.assertEqual(dest.read_bytes(), b"backup")
            self.assertEqual(said, f"{dest} (6 bytes, sha256 {hashlib.sha256(b'backup').hexdigest()})")
            self.assertEqual(copied.call_args.args[1],
                             "tester@127.0.0.1:/home/tester/Documents/fedora-full.backup")

    def test_a_windows_path_keeps_its_name(self):
        windows = SimpleNamespace(family="windows", cmd=lambda script: script)
        with tempfile.TemporaryDirectory() as top:
            said, _ = self.pull(Path(top), {"pull": "C:/Users/tester/windows-expected.txt", "to": "out/"}, windows)
            self.assertTrue(Path(top, "out/windows-expected.txt").exists())

    def test_no_such_file_in_the_guest_is_a_failed_step(self):
        gone = subprocess.CompletedProcess([], 3, b"", b"")
        with tempfile.TemporaryDirectory() as top, \
                self.assertRaisesRegex(scenario.StepFailed, "no file /home/tester/x in the guest"):
            self.pull(Path(top), {"pull": "/home/tester/x", "to": "out/"}, guest_says=gone)

    def test_a_copy_that_differs_or_fails_is_desks_failure(self):
        from desk.config import NotDone

        with tempfile.TemporaryDirectory() as top:
            with self.assertRaisesRegex(NotDone, "has SHA-256 .*, the guest's /home/tester/x"):
                self.pull(Path(top), {"pull": "/home/tester/x", "to": "out/"}, content=b"truncat")
            with self.assertRaisesRegex(NotDone, r"copying /home/tester/x to .* failed \(scp exit 1\)"):
                self.pull(Path(top), {"pull": "/home/tester/x", "to": "out/"}, scp_rc=1)


class BrowserMachine(unittest.TestCase):
    """browser = IMAGE: a desktop machine whose browser opens the pages of a guest without one."""

    def test_guest_ports_in_an_address(self):
        self.assertEqual(scenario._guest_url("http://{guest:8080}/cgi-bin/luci", None, None),
                         "http://localhost:8080/cgi-bin/luci")
        machine = scenario._Browser(SimpleNamespace(id="b1", ssh_port=2, user="tester"), mock.Mock())
        route = subprocess.CompletedProcess([], 0, b"default via 192.168.69.1 dev enp1s0 proto dhcp\n", b"")
        with mock.patch("desk.scenario.guest.ssh_run", return_value=route), mock.patch("desk.scenario.guest.lost"), \
                mock.patch("desk.forward.start", return_value=53311) as forwarded:
            self.assertEqual(scenario._guest_url("http://{guest:80}/", "run", machine), "http://192.168.69.1:53311/")
        forwarded.assert_called_once_with("run", 80)

    def test_its_steps_log_into_the_scenarios_run(self):
        owner = mock.Mock()
        machine = scenario._Browser(SimpleNamespace(id="b1", dom=lambda: "browser-dom"), owner)
        machine.log_event(step=1)
        machine.next_shot_path("x")
        owner.log_event.assert_called_once_with(step=1)
        owner.next_shot_path.assert_called_once_with("x")
        self.assertEqual(machine.dom(), "browser-dom")

    def test_commands_go_to_the_guest_and_the_rest_to_the_browser_machine(self):
        run = mock.Mock()
        machine = scenario._Browser(mock.Mock(), run)
        went = []
        with mock.patch("desk.scenario._step", side_effect=lambda target, spec, steps, step: went.append(
                (scenario.describe(step)[0], target is machine)) or ""), \
                mock.patch("desk.scenario.runs.spec_for"), mock.patch("desk.scenario.Steps"), \
                mock.patch("desk.scenario._guest_url", side_effect=lambda value, run, machine: value):
            scenario._run_steps(run, LostConnection.SPEC, {"step": [{"root": "x"}, {"open": "http://{guest:80}/"},
                                                                    {"click": "Save"}, {"reboot": True}]},
                                machine=machine)
        self.assertEqual(went, [("root", False), ("open", True), ("click", True), ("reboot", False)])

    def test_it_must_be_a_linux_desktop(self):
        with mock.patch("desk.scenario.load_image", return_value=SimpleNamespace(
                name="windows-11", family="windows", need_desktop=lambda what: None)), \
                self.assertRaisesRegex(DeskError, "Linux desktops only"):
            scenario.browser_machine({"browser": "windows-11"})
        self.assertIsNone(scenario.browser_machine({"image": "a"}))


class SplitWindows(unittest.TestCase):
    def test_paths_keep_their_backslashes(self):
        self.assertEqual(scenario.split_windows(r'C:\desk\app.exe --dir C:\Users\tester'),
                         [r"C:\desk\app.exe", "--dir", r"C:\Users\tester"])

    def test_quotes_group_and_disappear(self):
        self.assertEqual(scenario.split_windows(r'"C:\Program Files\app.exe" --name "a b"'),
                         [r"C:\Program Files\app.exe", "--name", "a b"])
        self.assertEqual(scenario.split_windows('cmd.exe /c "set X=1&& start app.exe"'),
                         ["cmd.exe", "/c", "set X=1&& start app.exe"])

    def test_backslashes_before_a_quote(self):
        self.assertEqual(scenario.split_windows(r'a\"b c'), ['a"b', "c"])
        self.assertEqual(scenario.split_windows(r'"a\\" b'), ["a\\", "b"])


if __name__ == "__main__":
    unittest.main()


class SeveralImages(unittest.TestCase):
    FILE = ('image = "fedora-44-gnome"\nimages = ["arch-gnome", "fedora-44-kde"]\n'
            '[[step]]\nexec = "true"\n[[step]]\nclick = "Close"\nonly = ["gnome"]\n'
            '[[step]]\nkey = "alt+f4"\nonly = ["kde", "arch-gnome"]\n')

    def test_only_matches_an_image_by_its_name_and_blocks(self):
        self.assertEqual(scenario.image_blocks("fedora-44-gnome-cockpit"),
                         {"fedora-44-gnome-cockpit", "fedora-44", "gnome", "cockpit"})
        self.assertEqual(scenario.image_blocks("windows-11"), {"windows-11"})
        data = scenario.load(write(self.FILE))
        close, quit_ = data["step"][1], data["step"][2]
        self.assertTrue(scenario.is_for(close, "arch-gnome"))
        self.assertFalse(scenario.is_for(close, "fedora-44-kde"))
        self.assertTrue(scenario.is_for(quit_, "fedora-44-kde") and scenario.is_for(quit_, "arch-gnome"))
        self.assertFalse(scenario.is_for(quit_, "fedora-44-gnome"))
        # Exact block names: "fedora" is no block of fedora-44.
        self.assertFalse(scenario.is_for({"only": ["fedora"]}, "fedora-44-gnome"))

    def test_mistakes_are_refused_before_anything_runs(self):
        for bad, said in (
            ('image = "a"\nimages = "b"', "images is a list"),
            ('image = "a"\nimages = ["b", "b"]', "each once"),
            ('image = "a"\nimages = ["a"]', "without image itself"),
            ('image = "a"\n[[step]]\nexec = "true"\nonly = "a"', "only takes a list"),
            ('image = "a"\n[[step]]\nexec = "true"\nonly = []', "only takes a list"),
            # A typo would skip the step on every image.
            ('image = "fedora-44-gnome"\nimages = ["arch-gnome"]\n[[step]]\nexec = "true"\nonly = ["kde"]',
             "step 1: only names kde, which none of the scenario's images"),
            ('image = "fedora-44-gnome"\nimages = ["arch-gnome"]\n[[step]]\nexec = "true"\nonly = ["arch"]',
             "every step has an only that leaves out fedora-44-gnome: nothing would be checked"),
            ('image = "fedora-44-gnome"\nimages = ["arch-gnome", "debian-13-gnome"]\n[build]\nrun = "make"\n'
             '[build.image.arch-gnome]\nrun = "make DISTRO=arch"', r"add a \[build.image.debian-13-gnome\] table"),
        ):
            with self.subTest(bad=bad), self.assertRaisesRegex(DeskError, said):
                scenario.load(write(bad))

    def test_an_image_of_no_step_is_refused_before_a_run_starts(self):
        data = scenario.load(write(self.FILE))
        # --image of one the file does not list: no step but the first is for it, and --from 2 leaves none.
        with mock.patch("desk.scenario.runs.up") as up, \
                self.assertRaisesRegex(DeskError, "every step from 2 on has an only that leaves out windows-11"):
            scenario._some_step_for(data, "windows-11", 2)
        scenario._some_step_for(data, "windows-11")  # step 1 has no only
        only_kde = write('image = "fedora-44-gnome"\nimages = ["fedora-44-kde"]\n[[step]]\nexec = "true"\n'
                         'only = ["gnome"]\n[[step]]\nexec = "true"\nonly = ["kde"]\n')
        with mock.patch("desk.scenario.runs.up") as up, \
                self.assertRaisesRegex(DeskError, "leaves out windows-11: nothing would be checked"):
            scenario.run_scenario(only_kde, image="windows-11")
        up.assert_not_called()

    def test_a_step_for_other_images_is_logged_as_skipped_not_run(self):
        run = mock.Mock()
        spec = SimpleNamespace(name="fedora-44-gnome", family="linux")
        data = {"step": [{"exec": "true"}, {"key": "alt+f4", "only": ["kde"]}, {"exec": "true"}]}
        with mock.patch("desk.scenario._step", return_value="") as step, mock.patch("desk.scenario.Steps"), \
                mock.patch("sys.stdout") as out:
            self.assertTrue(scenario._run_steps(run, spec, data))
        self.assertEqual(step.call_count, 2)
        self.assertIn(mock.call(step=2, action="key alt+f4", kind="key", skipped="only on kde"),
                      run.log_event.call_args_list)
        self.assertRegex("".join(c.args[0] for c in out.write.call_args_list), r"SKIP   2\. key alt\+f4 .*only on kde")

    def run_all(self, outcomes) -> tuple[int, str]:
        path = write(self.FILE)
        with mock.patch("desk.scenario.run_scenario", side_effect=outcomes) as each, \
                mock.patch("sys.stdout") as out, mock.patch("sys.stderr"):
            code = scenario.run_all(path)
        self.assertEqual([c.kwargs["image"] for c in each.call_args_list],
                         ["fedora-44-gnome", "arch-gnome", "fedora-44-kde"])
        return code, "".join(c.args[0] for c in out.write.call_args_list)

    def test_all_runs_every_image_and_a_finding_comes_before_what_was_not_checked(self):
        self.assertEqual(self.run_all([True, True, True])[0], 0)
        code, said = self.run_all([True, DeskError("base image arch-gnome not built yet (desk image build "
                                                   "arch-gnome)"), False])
        self.assertEqual(code, 1)
        self.assertRegex(said, r"arch-gnome +NOT CHECKED: base image arch-gnome not built yet")
        self.assertRegex(said, r"fedora-44-kde +FAILED")
        # Never green when one could not be checked, and never 1 for desk's own failure.
        self.assertEqual(self.run_all([True, DeskError("no SSH connection"), True])[0], 2)


def passed(i: int, label: str, seconds: float = 0.1) -> tuple:
    return (i, label, True, "", seconds)


def failed(i: int, label: str, detail: str, seconds: float = 0.1) -> tuple:
    return (i, label, False, detail, seconds)


def a_run(run_id: str, *steps: tuple, cold: bool = False, built: bool = True, error: str | None = None) -> dict:
    return {"run": run_id, "cold": cold, "steps": list(steps), "built": built, "error": error}


class RepeatedRuns(unittest.TestCase):
    FILE = ('image = "fedora-44-gnome"\n[[step]]\nexec = "true"\n[[step]]\nclick = "Close"\n'
            '[[step]]\nexec = "true"\nname = "last"\n')
    STEPS = ("exec true", "click Close", "last")

    def repeat(self, runs: list[dict], no_build: bool = False) -> tuple[int, str, str, list[bool]]:
        """run_repeat over runs a fake run_scenario plays one by one: what it returned, printed, and
        the no_build each run was started with."""
        seen: list[bool] = []
        script = iter(runs)

        def fake(path, keep=False, image=None, no_build=False, result=None):
            seen.append(no_build)
            one = next(script)
            result.update({k: v for k, v in one.items() if k != "error" and (k != "built" or v)})
            if one["error"]:
                raise DeskError(one["error"])
            return all(passed is not False for _, _, passed, _, _ in one["steps"])

        out, err = io.StringIO(), io.StringIO()
        with mock.patch("desk.scenario.run_scenario", side_effect=fake), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = scenario.run_repeat(write(self.FILE), len(runs), no_build=no_build)
        return code, out.getvalue(), err.getvalue(), seen

    def clean(self, run_id: str, **kw) -> dict:
        return a_run(run_id, *(passed(i, label) for i, label in enumerate(self.STEPS, 1)), **kw)

    def test_all_runs_passing_is_0_and_only_the_first_builds(self):
        code, said, err, seen = self.repeat([self.clean("a"), self.clean("b", cold=True), self.clean("c")])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(seen, [False, True, True])
        for k in (1, 2, 3):
            self.assertIn(f"== run {k} of 3", said)
        self.assertRegex(said, r"run 2  b  cold  passed")
        self.assertRegex(said, r"run 1  a  warm  passed")
        self.assertRegex(said, r"2\. click Close +passed 3 of 3, 0\.1s to 0\.1s")
        self.assertIn("no step both passed and failed", said)
        self.assertIn("3 of 3 runs counted, 0 not checked", said)
        # --no-build is the user's for every run.
        self.assertEqual(self.repeat([self.clean("a"), self.clean("b")], no_build=True)[3], [True, True])

    def test_a_step_that_passes_in_one_run_and_fails_in_another_is_named_with_its_failure(self):
        code, said, err, _ = self.repeat([
            self.clean("a"),
            a_run("b", passed(1, "exec true", 0.3), failed(2, "click Close", "no element Close\nfocused: Files", 2.5)),
            self.clean("c"),
            a_run("d", passed(1, "exec true"), failed(2, "click Close", "no element Close\nfocused: Files")),
        ])
        self.assertEqual((code, err), (1, ""))
        self.assertRegex(said, r"run 2  b  warm  FAILED at step 2")
        # The step after the failed one ran in two of the four runs only.
        self.assertRegex(said, r"3\. last +passed 2 of 2")
        self.assertRegex(said, r"2\. click Close +passed 2 of 4, 0\.1s to 2\.5s")
        block = said.split("steps that both passed and failed:\n")[1]
        self.assertRegex(block, r"2\. click Close +passed 2 of 4\n +no element Close  \(run b, d\)\n")
        self.assertNotIn("focused: Files", block)
        self.assertNotIn("1. exec", block)

    def test_a_run_desk_could_not_finish_is_not_counted(self):
        # Its half-run steps, a failed one among them, say nothing about the product.
        lost = a_run("b", passed(1, "exec true"), failed(2, "click Close", "no SSH connection"), cold=True,
                     error="no SSH connection to the guest")
        code, said, err, seen = self.repeat([self.clean("a"), lost, self.clean("c")])
        self.assertEqual(code, 2)
        self.assertEqual(seen, [False, True, True])
        self.assertIn("desk: no SSH connection to the guest", said)
        self.assertRegex(said, r"run 2  b  cold  NOT CHECKED: no SSH connection to the guest")
        self.assertRegex(said, r"2\. click Close +passed 2 of 2")
        self.assertIn("no step both passed and failed", said)
        self.assertIn("2 of 3 runs counted, 1 not checked", said)
        self.assertIn("only 2 of 3 runs were counted", err)
        # A finding comes before what was not checked.
        broken = a_run("c", passed(1, "exec true"), failed(2, "click Close", "no element Close"))
        code, said, err, _ = self.repeat([self.clean("a"), lost, broken])
        self.assertEqual((code, err), (1, ""))
        self.assertRegex(said, r"2\. click Close +passed 1 of 2")

    def test_a_run_that_stops_before_its_build_is_done_stops_the_repeat(self):
        # Nothing was built (the collector has no "built"): the error is the command's, once, exit 2.
        with mock.patch("desk.scenario.run_scenario", side_effect=DeskError("build failed: make")) as each, \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(DeskError, "build failed: make"):
            scenario.run_repeat(write(self.FILE), 3)
        self.assertEqual(each.call_count, 1)

    def test_a_first_run_lost_after_its_build_goes_on_with_that_build(self):
        code, said, _, seen = self.repeat([a_run("a", error="no memory for the guest"), self.clean("b"),
                                             self.clean("c")])
        self.assertEqual((code, seen), (2, [False, True, True]))
        self.assertIn("2 of 3 runs counted, 1 not checked", said)
        # Nothing counted says nothing of the steps, rather than that none was flaky.
        code, said, err, _ = self.repeat([a_run("a", error="no memory"), a_run("b", error="no memory")])
        self.assertEqual(code, 2)
        self.assertIn("0 of 2 runs counted, 2 not checked", said)
        self.assertNotIn("steps", said)
        self.assertIn("only 0 of 2 runs were counted", err)

    def test_a_skipped_step_is_shown_as_skipped_and_never_as_passed(self):
        skipped = (2, "click Close", None, "only on kde", 0.0)
        code, said, _, _ = self.repeat([a_run("a", passed(1, "exec true"), skipped, passed(3, "last")),
                                        a_run("b", passed(1, "exec true"), skipped, passed(3, "last"))])
        self.assertEqual(code, 0)
        self.assertRegex(said, r"2\. click Close +skipped\n")
        self.assertRegex(said, r"3\. last +passed 2 of 2")


class StepLog(unittest.TestCase):
    def test_each_step_is_logged_with_its_kind_and_handed_to_the_collector(self):
        run = mock.Mock()
        spec = SimpleNamespace(name="fedora-44-gnome", family="linux")
        data = {"step": [{"exec": "true"}, {"key": "alt+f4", "only": ["kde"]},
                         {"click": "Close", "name": "close it"}, {"exec": "never"}]}
        got: list = []
        with mock.patch("desk.scenario._step", side_effect=["fine", scenario.StepFailed("no element\nmore")]), \
                mock.patch("desk.scenario.Steps"), mock.patch("desk.scenario.display"), mock.patch("sys.stdout"):
            self.assertFalse(scenario._run_steps(run, spec, data, outcomes=got))
        calls = run.log_event.call_args_list
        self.assertIn(mock.call(step=1, action="exec true", kind="exec", ok=True, detail="fine", seconds=mock.ANY),
                      calls)
        self.assertIn(mock.call(step=2, action="key alt+f4", kind="key", skipped="only on kde"), calls)
        # The name is the label; the kind stays the action.
        self.assertIn(mock.call(step=3, action="close it", kind="click", ok=False, detail="no element\nmore",
                                seconds=mock.ANY), calls)
        self.assertEqual(got, [(1, "exec true", True, "fine", mock.ANY), (2, "key alt+f4", None, "only on kde", 0.0),
                               (3, "close it", False, "no element\nmore", mock.ANY)])

    def started(self, domain: str) -> tuple[mock.Mock, dict, mock.Mock]:
        run = mock.Mock(id="r1", domain=domain)
        spec = SimpleNamespace(name="fedora-44-gnome", family="linux")
        result: dict = {}
        with mock.patch("desk.scenario.load_image", return_value=spec), mock.patch("desk.scenario.runs") as runs, \
                mock.patch("desk.scenario.evidence") as evidence, mock.patch("sys.stdout"), \
                mock.patch("desk.scenario._run_steps", return_value=True) as steps:
            runs.up.return_value = run
            runs.agent_process.return_value = (None, None)
            evidence.finish.return_value = (None, [])
            path = write('image = "fedora-44-gnome"\n[[step]]\nexec = "true"\n')
            self.assertTrue(scenario.run_scenario(path, result=result))
        self.assertIs(steps.call_args.kwargs["outcomes"], result["steps"])
        return run, result, path

    def test_the_scenario_header_says_whether_the_run_started_warm_or_cold(self):
        for domain, start in (("desk-r1", "cold"), ("desk-fedora-44-gnome-warm", "warm")):
            with self.subTest(start=start):
                run, result, path = self.started(domain)
                run.log_event.assert_any_call(scenario=str(path.resolve()), steps=1, start=start)
                self.assertEqual((result["run"], result["cold"], result["built"]), ("r1", start == "cold", True))

    def test_a_build_that_fails_leaves_the_collector_without_built(self):
        result: dict = {}
        spec = SimpleNamespace(name="fedora-44-gnome", family="linux")
        with mock.patch("desk.scenario.load_image", return_value=spec), \
                mock.patch("desk.scenario._build", side_effect=DeskError("build failed: make")), \
                mock.patch("desk.scenario.runs") as runs, self.assertRaisesRegex(DeskError, "build failed"):
            scenario.run_scenario(write('image = "fedora-44-gnome"\n[[step]]\nexec = "true"\n'), result=result)
        self.assertEqual(result, {})
        runs.up.assert_not_called()
