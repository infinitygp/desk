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
        display.click.assert_called_once_with("dom", 1046, 715, button="right", run=target)

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
