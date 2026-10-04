import unittest

from desk.evidence import format_changes, parse_virt_diff

# Trimmed real virt-diff output (Fedora guest, warm image vs run overlay).
SAMPLE = """\
+ d 0755         12 /etc/desktest
+ - 0644          3 /etc/desktest/a.conf
- d 0755       4490 /etc
+ d 0755       4506 /etc
# changed: st_size
= - 0644          0 /etc/.updated
@@ -0,0 +1,4 @@
+# This file was created by systemd-update-done.
@@ End of diff @@
- - 0644        120 /etc/hosts
+ - 0644        140 /etc/hosts
- - 0644         50 /etc/removed.conf
+ d 0755          0 /var/lib/dnf
+ - 0644       1000 /var/log/dnf5.log
+ - 0644         10 /home/tester/.cache/x
+ - 0755      99000 /usr/bin/htop
"""


class ParseVirtDiff(unittest.TestCase):
    def test_buckets_and_noise(self):
        ch = parse_virt_diff(SAMPLE)
        self.assertEqual(ch.added, ["/etc/desktest/", "/etc/desktest/a.conf", "/usr/bin/htop"])
        self.assertEqual(ch.changed, ["/etc/hosts"])
        self.assertEqual(ch.removed, ["/etc/removed.conf"])
        # /etc/.updated, /var/lib/dnf, /var/log/..., ~/.cache are routine
        self.assertEqual(ch.hidden, 4)

    def test_all_paths(self):
        ch = parse_virt_diff(SAMPLE, all_paths=True)
        self.assertIn("/var/log/dnf5.log", ch.added)
        self.assertIn("/etc/.updated", ch.changed)
        self.assertNotIn("/etc", ch.changed, "a directory whose listing changed is not a change by itself")

    def test_format(self):
        out = format_changes(parse_virt_diff(SAMPLE)).splitlines()
        self.assertIn("+ /usr/bin/htop", out)
        self.assertIn("~ /etc/hosts", out)
        self.assertIn("- /etc/removed.conf", out)
        self.assertTrue(out[-1].startswith("3 added, 1 changed, 1 removed; 4 routine paths hidden"))


if __name__ == "__main__":
    unittest.main()
