import tempfile
import unittest
from pathlib import Path

from desk import scenario
from desk.config import DeskError


def write(text: str) -> Path:
    f = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
    f.write(text)
    f.close()
    return Path(f.name)


class Load(unittest.TestCase):
    def test_valid_file(self):
        data = scenario.load(write('''
image = "fedora-44-cockpit"
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
        ):
            with self.subTest(text=text), self.assertRaises(DeskError):
                scenario.load(write(text))

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
'''))
        self.assertEqual(len(data["step"]), 11)

    def test_a_missing_file_is_a_mistake_too(self):
        with self.assertRaisesRegex(DeskError, "No such file"):
            scenario.load(Path("/nonexistent/scenario.toml"))

    def test_disks_and_console(self):
        data = scenario.load(write('''
image = "fedora-44-cockpit"
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
