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
        ):
            with self.subTest(text=text), self.assertRaises(DeskError):
                scenario.load(write(text))

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
