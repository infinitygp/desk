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
            self.assertEqual([f.name for f in scenario._build(scenario.load(other), base)], ["cockpit-x-1.rpm"])

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
        self.assertEqual(len(data["step"]), 17)

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

    def test_until_tries_again_while_the_guest_comes_back(self):
        self.assertEqual(self.shell({"exec": "true", "until": True}, 255, 255, 0), "")

    def test_a_step_that_expects_255_gets_it(self):
        self.assertEqual(self.shell({"exec": "reboot", "rc": 255}, 255), "")

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
            run = SimpleNamespace(id="r1", dir=Path(tmp), domain="desk-r1", live_domain=lambda: None)
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
