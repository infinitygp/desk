import unittest
from types import SimpleNamespace
from unittest import mock

from desk import a11y
from desk.a11y import (
    _matches,
    _on_screen,
    _visible_point,
    center,
    explain_missing,
    format_node,
    format_snapshot,
    pick,
    role_hint,
)
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

    def test_covered_says_by_what(self):
        n = node("e4", 0, "button", "Ports", covered='section "Create and start Create Cancel"')
        self.assertEqual(format_node(n), '@e4 button "Ports" [covered] (60,40) under section "Create and start Create Cancel"')

    def test_time_cut_is_explained(self):
        snap = {"tops": [{"title": "A", "app": "a", "focus": True, "start": 0}], "nodes": [], "truncated": "time"}
        self.assertIn("took too long", format_snapshot(snap))

    def test_long_text_is_searched_whole_and_listed_short(self):
        # A log in a <pre>: what is looked for may be far into it.
        log = "podman[1234]: image pull registry:3.1.2 " * 20 + "container create 1234abcd"
        n = node("e5", 0, "static", "", text=log)
        self.assertEqual(_matches({"nodes": [n]}, "container create", None, False), [n])
        listed = format_node(n, indent=False)
        self.assertLess(len(listed), 200)
        self.assertIn('…"', listed)

    def test_point_where_the_middle_is_cut_off(self):
        self.assertIn("(329,768)", format_node(node("e9", 0, "link", "abrtd", point=[329, 768])))

    def test_geometry(self):
        self.assertEqual(center([10, 20, 100, 40]), (60, 40))
        self.assertTrue(_on_screen([0, 0, 10, 10], (1280, 800)))
        self.assertFalse(_on_screen([100, -950, 20, 20], (1280, 800)))


class VisiblePoint(unittest.TestCase):
    spec = {"pid": 1, "path": [0, 1], "role": "link", "name": "abrtd"}

    def point(self, *answers):
        replies = iter(answers)
        calls = []

        def helper(run, cmd, *args):
            calls.append(cmd)
            return {} if cmd == "scroll" else next(replies)

        run = SimpleNamespace(dom=lambda: None)
        with mock.patch("desk.a11y._helper", side_effect=helper), \
                mock.patch("desk.a11y.display.wait_stable", return_value=(b"", True)), \
                mock.patch("desk.a11y.display.png_size", return_value=(1280, 800)):
            return _visible_point(run, self.spec), calls

    def test_cut_off_by_a_scrolling_container_is_scrolled_into_view_first(self):
        self.assertEqual(self.point({"point": [329, 788], "clipped": True}, {"point": [329, 500], "clipped": False}),
                         ((329, 500), ["resolve", "scroll", "resolve"]))

    def test_what_scrolling_leaves_outside_its_container_is_clicked_where_it_is_hit(self):
        # A menu hanging out of the box of what holds it.
        self.assertEqual(self.point({"point": [329, 788], "clipped": True}, {"point": [329, 788], "clipped": True}),
                         ((329, 788), ["resolve", "scroll", "resolve"]))

    def test_shown_whole_is_clicked_at_once(self):
        self.assertEqual(self.point({"point": [329, 500], "clipped": False}), ((329, 500), ["resolve"]))
        self.assertEqual(self.point({"bbox": [300, 490, 40, 20], "showing": True}), ((320, 500), ["resolve"]))


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

    def test_the_name_as_written_wins_over_another_case(self):
        from desk.a11y import pick

        menu = node("e1", 0, "link", "Registry")
        repository = node("e2", 0, "link", "registry")
        with mock.patch("sys.stderr") as err:
            self.assertIs(pick({"nodes": [menu, repository]}, [menu, repository], "registry"), repository)
            self.assertIs(pick({"nodes": [menu, repository]}, [menu, repository], "Registry"), menu)
        err.write.assert_not_called()

    def test_elements_alike_are_named_in_a_warning(self):
        from desk.a11y import pick

        first = node("e1", 0, "push button", "Delete")
        second = dict(node("e2", 0, "push button", "Delete"), bbox=[10, 80, 100, 40])
        with mock.patch("sys.stderr") as err:
            self.assertIs(pick({"nodes": [first, second]}, [first, second], "Delete"), first)
        said = "".join(c.args[0] for c in err.write.call_args_list)
        self.assertIn("2 elements are named 'Delete' (push button at (60,40), push button at (60,100))", said)

    def test_only_the_best_ranked_count(self):
        from desk.a11y import pick

        shown = {"pid": 1, "path": [0, 1], "role": "button", "name": "Delete task"}
        hidden = {"pid": 1, "path": [0, 2], "role": "button", "name": "Delete all", "offscreen": True}
        self.assertIs(pick({"nodes": [shown, hidden]}, [shown, hidden], "Delete"), shown)


class Roles(unittest.TestCase):
    def setUp(self):
        self.snap = {"nodes": [node("e1", 0, "page tab", "Source"), node("e2", 0, "toggle button", "Form"),
                               node("e3", 0, "push button", "Save", covered="alert"),
                               node("e4", 0, "push button", "Save")]}

    def test_aria_names_work(self):
        self.assertEqual([n["ref"] for n in _matches(self.snap, "Source", "tab", True)], ["e1"])

    def test_hint_names_what_the_name_belongs_to(self):
        self.assertIn("there is: page tab 'Source'", role_hint(self.snap, "Source", "button", True))
        self.assertEqual(role_hint(self.snap, "Source", None, True), "")

    def test_uncovered_preferred(self):
        self.assertEqual(pick(self.snap, _matches(self.snap, "Save", None, True), "Save")["ref"], "e4")


KEYPAD = {"tops": [{"title": "Keypad", "app": "keypad", "focus": True, "start": 0}],
            "nodes": [node("e1", 0, "button", "Choose an action for Key 1"), node("e2", 0, "label", "Volume")]}


class ExplainMissing(unittest.TestCase):
    def test_names_focus_and_similar_names(self):
        run = SimpleNamespace(agent_port=47011)  # Windows: no shell to ask
        out = explain_missing(run, KEYPAD, "Choose an action for Key 2").splitlines()
        self.assertEqual(out[1:], ['focused window: "Keypad" (keypad)',
                                   'similar: button "Choose an action for Key 1"'])

    def test_names_what_the_shell_shows(self):
        shell = {"overview": True, "banner": {"title": "Critical Updates Available", "body": "", "source": "Software"}}
        with mock.patch("desk.a11y._helper", return_value=shell):
            out = explain_missing(SimpleNamespace(agent_port=0), None, "OK")
        self.assertEqual(out.splitlines()[1:], ["the overview is open over the windows",
                                                'a notification of Software is on screen: "Critical Updates Available"'])


if __name__ == "__main__":
    unittest.main()


class FoundAgain(unittest.TestCase):
    """What the guest gets to find a listed node again."""

    def node(self, **kw):
        return {"pid": 7, "path": [0, 3], "role": "menu item", "name": "", "states": [], **kw}

    def test_one_without_a_name_goes_with_the_text_it_was_found_by(self):
        self.assertEqual(a11y.ref_spec(self.node(value="Quit"))["value"], "Quit")
        self.assertEqual(a11y.ref_spec(self.node(role="label", text="3 items"))["text"], "3 items")

    def test_not_the_text_of_a_field_that_typing_changes_nor_of_a_named_node(self):
        self.assertNotIn("value", a11y.ref_spec(self.node(role="entry", value="abc", states=["editable"])))
        self.assertEqual(set(a11y.ref_spec(self.node(name="Quit", value="Quit now"))), {"pid", "path", "role", "name"})

    def test_messages_name_it_by_that_text(self):
        self.assertEqual(a11y.called(a11y.ref_spec(self.node(value="Quit"))), "menu item 'Quit'")
