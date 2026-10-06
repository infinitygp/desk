import unittest

from desk import browser

JOURNAL = """\
2026-10-06T09:58:01+02:00 desk-fedora bash[4612]: console.log: "hello from log" ({a:1})
2026-10-06T09:58:01+02:00 desk-fedora bash[4612]: console.error: "an error"
2026-10-06T09:58:01+02:00 desk-fedora bash[4612]: JavaScript error: file:///tmp/t.html, line 5: ReferenceError: x
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
