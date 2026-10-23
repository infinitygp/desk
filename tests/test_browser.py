import unittest

from desk import browser
from desk.config import DeskError

JOURNAL = """\
2026-10-06T09:58:01.250311+02:00 desk-fedora bash[4612]: console.log: "hello from log" ({a:1})
2026-10-06T09:58:01.250311+02:00 desk-fedora bash[4612]: console.error: "an error"
2026-10-06T09:58:01.250311+02:00 desk-fedora bash[4612]: JavaScript error: file:///tmp/t.html, line 5: ReferenceError: x
2026-10-06T09:58:02+02:00 desk-fedora bash[4612]: console.warn: RSLoader: Recipe new-tab-1 not returned
2026-10-06T09:58:02+02:00 desk-fedora bash[4612]: console.log: "Note: with a colon"
2026-10-06T09:58:03+02:00 desk-fedora bash[4612]: JavaScript error: resource://autofill/FormAutofillChild.sys.mjs, line 639: TypeError
"""


class Parse(unittest.TestCase):
    def test_pages_only(self):
        lines = browser.parse(JOURNAL)
        self.assertEqual(lines, [
            '09:58:01 console.log: "hello from log" ({a:1})',
            '09:58:01 console.error: "an error"',
            "09:58:01 JavaScript error: file:///tmp/t.html, line 5: ReferenceError: x",
            '09:58:02 console.log: "Note: with a colon"',
        ])
        self.assertEqual(len(browser.errors(lines)), 2)

    def test_everything(self):
        self.assertEqual(len(browser.parse(JOURNAL, everything=True)), 6)


DETAILED = """\
2026-10-06T09:58:01+02:00 desk-fedora firefox[4612]: JavaScript error: , line 0: uncaught exception: Object
2026-10-06T09:58:01+02:00 desk-fedora firefox[4612]: JavaScript error: https://localhost:9090/cockpit/@localhost/podman/index.js, line 2: uncaught exception: Object
2026-10-06T09:58:01+02:00 desk-fedora firefox[4612]: JavaScript error detail: {"message":"uncaught exception: Object","value":"{\\"problem\\":\\"not-found\\"}","stack":["load@https://localhost:9090/cockpit/@localhost/podman/index.js:2:41","(anonymous)@https://localhost:9090/cockpit/@localhost/podman/index.js:3:20"]}
2026-10-06T09:58:02+02:00 desk-fedora firefox[4612]: JavaScript error detail: {"message":"TypeError: x is null","value":null,"stack":[]}
"""


class Detail(unittest.TestCase):
    def test_attached_to_the_latest_matching_error(self):
        lines = browser.parse(DETAILED)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], "09:58:01 JavaScript error: , line 0: uncaught exception: Object")
        self.assertEqual(lines[1], '09:58:01 JavaScript error: https://localhost:9090/cockpit/@localhost/podman/index.js, '
                         'line 2: uncaught exception: Object = {"problem":"not-found"} '
                         "(at load@https://localhost:9090/cockpit/@localhost/podman/index.js:2:41 < "
                         "(anonymous)@https://localhost:9090/cockpit/@localhost/podman/index.js:3:20)")
        self.assertEqual(len(browser.errors(lines)), 2)


class Open(unittest.TestCase):
    """`desk browser open` tells a page that loaded from one that did not."""

    def open(self, pages: list[dict], target: str = "https://app.test/", opened: list | None = None) -> str:
        from types import SimpleNamespace
        from unittest import mock

        run = SimpleNamespace(image="fedora-44-gnome", ssh_port=1, user="tester", dom=lambda: None)
        answers = iter(pages)
        last = pages[-1]
        started = SimpleNamespace(returncode=0, stderr=b"")

        def ssh_run(port, user, command, **kw):
            (opened if opened is not None else []).append(command)
            return started

        with mock.patch("desk.browser.a11y.page_url", side_effect=lambda run: next(answers, last)), \
                mock.patch("desk.browser.a11y.focus"), mock.patch("desk.browser.guest.ssh_run", side_effect=ssh_run), \
                mock.patch("desk.browser.display.wait_stable"), mock.patch("desk.browser.time.sleep"):
            return browser.open_url(run, target, timeout=0.05)

    def test_loaded(self):
        self.assertEqual(self.open([{"url": "about:home"}, {"url": "https://app.test/"}]),
                         ("https://app.test/", "https://app.test/"))

    def test_sent_elsewhere_by_the_site(self):
        self.assertEqual(self.open([{"url": "about:home"}, {"url": "https://app.test/login"}], "https://app.test/x"),
                         ("https://app.test/login", "https://app.test/x"))
        # Back to the page it was on: a redirect to the login it came from is not a failed load.
        self.assertEqual(self.open([{"url": "https://app.test/login"}], "https://app.test/dashboard")[0],
                         "https://app.test/login")

    def test_the_browsers_own_error_page(self):
        with self.assertRaisesRegex(browser.NotLoaded, "did not load: Firefox shows 'Server Not Found'"):
            self.open([{"url": "about:home"}, {"url": "https://app.test/", "problem": "Server Not Found"}])

    def test_no_page_while_the_server_does_not_answer(self):
        with self.assertRaisesRegex(browser.NotLoaded, "did not load within 0s: Firefox shows no page"):
            self.open([{"url": "about:home"}, {"url": None}])

    def test_a_page_that_arrived_and_cannot_be_read_a_moment_later_has_loaded(self):
        self.assertEqual(self.open([{"url": "about:home"}, {"url": "https://app.test/"}, {"url": None}])[0],
                         "https://app.test/")

    def test_a_new_part_after_hash_of_the_page_shown_is_loaded_through_about_blank(self):
        # Firefox leaves an empty "New Tab" for such an address from another program.
        opened = []
        self.assertEqual(self.open([{"url": "https://app.test/example"}, {"url": "about:blank"},
                                    {"url": "https://app.test/example#/tasks"}], "/example#/tasks", opened)[0],
                         "https://app.test/example#/tasks")
        self.assertEqual(["about:blank" in c for c in opened], [True, False])
        self.assertIn("https://app.test/example#/tasks", opened[1])
        # Another page, or the same one without "#", loads whole by itself.
        for before, target in (("https://app.test/system", "/example#/tasks"),
                               ("https://app.test/example#/x", "/example")):
            opened = []
            self.open([{"url": before}, {"url": "https://app.test" + target}], target, opened)
            self.assertEqual(len(opened), 1)

    def test_about_blank_that_does_not_come_is_desks_failure(self):
        with self.assertRaisesRegex(DeskError, "did not leave the page") as e:
            self.open([{"url": "https://app.test/example"}], "/example#/tasks")
        self.assertNotIsInstance(e.exception, browser.NotLoaded)

    def test_addresses_as_firefox_writes_them(self):
        for shown, asked in (("https://app.test/", "https://App.Test"), ("https://app.test/", "http://app.test/"),
                             ("file:///home/tester/x.html", "file:/home/tester/x.html"),
                             ("https://app.test/system/services#/x", "https://app.test/system"),
                             ("https://app.test/a?b=1", "https://app.test/a?b=1")):
            with self.subTest(shown=shown, asked=asked):
                self.assertTrue(browser.under(shown, asked))
        for shown, asked in (("https://app.test/login", "https://app.test/dashboard"),
                             ("https://other.test/", "https://app.test/"), ("about:home", "https://app.test/"),
                             ("https://app.test/example", "https://app.test/example#/tasks"),
                             ("https://app.test/example#/overview", "https://app.test/example#/tasks")):
            with self.subTest(shown=shown, asked=asked):
                self.assertFalse(browser.under(shown, asked))
        self.assertTrue(browser.under("https://app.test/example#/tasks/1", "https://app.test/example#/tasks"))
        self.assertTrue(browser.under("https://app.test/example#/overview", "https://app.test/example#/tasks",
                                      fragment=False))


class Viewport(unittest.TestCase):
    """The first time in a Firefox session the mode's toolbar comes before its size fields."""

    def viewport(self, fields: list):
        from types import SimpleNamespace
        from unittest import mock

        run = SimpleNamespace(image="fedora-44-gnome", dom=lambda: None, scale=100)
        clock = iter(range(0, 1000, 5))
        with mock.patch("desk.browser._linux", return_value=SimpleNamespace(layout="us")), \
                mock.patch("desk.browser.a11y.focus"), mock.patch("desk.browser._rdm", return_value={"open": True}), \
                mock.patch("desk.browser._rdm_fields", side_effect=fields) as looked, \
                mock.patch("desk.browser.a11y.replace_text", return_value=""), mock.patch("desk.browser.display"), \
                mock.patch("desk.browser.time.monotonic", side_effect=lambda: next(clock)), \
                mock.patch("desk.browser.time.sleep"):
            browser.viewport(run, "390x700")
        return looked.call_count

    def test_waits_for_the_fields(self):
        field = {"pid": 1, "path": [0, 1], "role": "spin button", "name": ""}
        self.assertEqual(self.viewport([[], [], [field, field]]), 3)

    def test_gives_up_when_they_do_not_come(self):
        with self.assertRaisesRegex(DeskError, "cannot find the width and height fields"):
            self.viewport([[]] * 100)
