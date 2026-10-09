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
