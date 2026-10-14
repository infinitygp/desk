import unittest

from desk import browser

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

    def open(self, pages: list[dict], target: str = "https://app.test/") -> str:
        from types import SimpleNamespace
        from unittest import mock

        run = SimpleNamespace(image="fedora-44-gnome", ssh_port=1, user="tester", dom=lambda: None)
        answers = iter(pages)
        last = pages[-1]
        started = SimpleNamespace(returncode=0, stderr=b"")
        with mock.patch("desk.browser.a11y.page_url", side_effect=lambda run: next(answers, last)), \
                mock.patch("desk.browser.a11y.focus"), mock.patch("desk.browser.guest.ssh_run", return_value=started), \
                mock.patch("desk.browser.display.wait_stable"), mock.patch("desk.browser.time.sleep"):
            return browser.open_url(run, target, timeout=0.05)

    def test_loaded(self):
        self.assertEqual(self.open([{"url": "about:home"}, {"url": "https://app.test/"}]), "https://app.test/")

    def test_sent_elsewhere_by_the_site(self):
        self.assertEqual(self.open([{"url": "about:home"}, {"url": "https://app.test/login"}], "https://app.test/x"),
                         "https://app.test/login")

    def test_the_browsers_own_error_page(self):
        with self.assertRaisesRegex(browser.NotLoaded, "did not load: Firefox shows 'Server Not Found'"):
            self.open([{"url": "about:home"}, {"url": "https://app.test/", "problem": "Server Not Found"}])

    def test_still_the_page_from_before(self):
        with self.assertRaisesRegex(browser.NotLoaded, "did not load within 0s: Firefox shows about:home"):
            self.open([{"url": "about:home"}])
        with self.assertRaisesRegex(browser.NotLoaded, "Firefox shows no page"):
            self.open([{"url": None}])
