import unittest

from desk.a11y import _on_screen, center, format_node, format_snapshot
from desk.guest import remote_command


def node(ref, depth, role, name, **kw):
    return {"ref": ref, "depth": depth, "role": role, "name": name, "bbox": [10, 20, 100, 40],
            "states": kw.pop("states", []), **kw}


class Format(unittest.TestCase):
    def test_node(self):
        n = node("e3", 1, "entry", "Nazwa zadania", value="Zażółć", states=["editable", "focused"])
        self.assertEqual(format_node(n), '  @e3 entry "Nazwa zadania" value="Zażółć" [editable,focused] (60,40)')
        self.assertTrue(format_node(n, indent=False).startswith("@e3"))

    def test_snapshot_groups_by_window(self):
        snap = {"tops": [{"title": "A", "app": "a", "focus": True, "start": 0},
                         {"title": "B", "app": "b", "focus": False, "start": 1}],
                "nodes": [node("e1", 0, "button", "OK"), node("e2", 0, "link", "Docs")],
                "truncated": False}
        out = format_snapshot(snap).splitlines()
        self.assertEqual(out[0], 'window "A" (a) [focused]')
        self.assertIn("@e1", out[1])
        self.assertEqual(out[2], 'window "B" (b)')
        self.assertIn("@e2", out[3])

    def test_offscreen_is_summarised(self):
        snap = {"tops": [{"title": "A", "app": "a", "focus": True, "start": 0}],
                "nodes": [node("e1", 0, "button", "OK"), node("e2", 0, "button", "Proceed", offscreen=True)],
                "truncated": False}
        short = format_snapshot(snap)
        self.assertNotIn("@e2", short)
        self.assertIn("1 more outside the visible area", short)
        full = format_snapshot(snap, offscreen=True)
        self.assertIn('@e2 button "Proceed" [offscreen]', full)
        self.assertNotIn("(60,40)", full.splitlines()[-1])

    def test_geometry(self):
        self.assertEqual(center([10, 20, 100, 40]), (60, 40))
        self.assertTrue(_on_screen([0, 0, 10, 10], (1280, 800)))
        self.assertFalse(_on_screen([100, -950, 20, 20], (1280, 800)))


class RemoteCommand(unittest.TestCase):
    def test_snippet_is_passed_whole(self):
        self.assertEqual(remote_command(["a && b"]), "a && b")
        self.assertTrue(remote_command(["a && b"], gui=True).endswith(
            "systemd-run --user --collect --quiet $lang -- bash -c 'a && b'"))

    def test_argv_is_quoted(self):
        self.assertEqual(remote_command(["echo", "a b"]), "echo 'a b'")

    def test_gui(self):
        cmd = remote_command(["firefox"], gui=True)
        self.assertIn("/proc/$shell/environ", cmd)
        self.assertTrue(cmd.endswith("-- bash -c firefox"))


class BestMatch(unittest.TestCase):
    def test_open_dialog_wins_over_the_window_behind_it(self):
        from desk.a11y import best_match

        browser_close = {"pid": 1, "path": [0, 3], "role": "button", "name": "Close"}
        dialog = {"pid": 1, "path": [0, 9, 2], "role": "dialog", "name": "Restore"}
        dialog_close = {"pid": 1, "path": [0, 9, 2, 7], "role": "button", "name": "Close"}
        snap = {"nodes": [browser_close, dialog, dialog_close]}
        self.assertIs(best_match(snap, [browser_close, dialog_close]), dialog_close)
        self.assertIs(best_match({"nodes": [browser_close, dialog_close]}, [browser_close, dialog_close]),
                      browser_close)

    def test_visible_first(self):
        from desk.a11y import best_match

        hidden = {"pid": 1, "path": [0, 1], "role": "button", "name": "Add", "offscreen": True}
        shown = {"pid": 1, "path": [0, 2], "role": "button", "name": "Add"}
        self.assertIs(best_match({"nodes": [hidden, shown]}, [hidden, shown]), shown)


class Pick(unittest.TestCase):
    def test_whole_name_wins(self):
        from desk.a11y import pick

        save = {"pid": 1, "path": [0, 1], "role": "button", "name": "Save"}
        save_as = {"pid": 1, "path": [0, 2], "role": "button", "name": "Save as..."}
        snap = {"nodes": [save_as, save]}
        self.assertIs(pick(snap, [save_as, save], "save"), save)

    def test_different_names_containing_the_query_are_not_guessed(self):
        from desk.a11y import pick
        from desk.config import DeskError

        a = {"pid": 1, "path": [0, 1], "role": "button", "name": "Save draft"}
        b = {"pid": 1, "path": [0, 2], "role": "button", "name": "Save as..."}
        with self.assertRaises(DeskError) as e:
            pick({"nodes": [a, b]}, [a, b], "Save")
        self.assertIn("'Save as...'", str(e.exception))

    def test_one_name_containing_the_query_is_enough(self):
        from desk.a11y import pick

        link = {"pid": 1, "path": [0, 1], "role": "link", "name": "Backups"}
        text = {"pid": 1, "path": [0, 2], "role": "heading", "name": "Backups", "offscreen": True}
        self.assertIs(pick({"nodes": [link, text]}, [link, text], "Backup"), link)

    def test_only_the_best_ranked_count(self):
        from desk.a11y import pick

        shown = {"pid": 1, "path": [0, 1], "role": "button", "name": "Delete task"}
        hidden = {"pid": 1, "path": [0, 2], "role": "button", "name": "Delete all", "offscreen": True}
        self.assertIs(pick({"nodes": [shown, hidden]}, [shown, hidden], "Delete"), shown)


if __name__ == "__main__":
    unittest.main()
