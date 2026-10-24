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


# Captured from Chromium 155 on fedora-44-gnome (journalctl -o short-iso-precise).
CHROMIUM = """\
2026-10-23T21:39:21.105619+02:00 desk-fedora bash[3045]: [3037:3037:1023/213921.105340:INFO:CONSOLE:5] "plain log [object Object]", source: file:///tmp/t.html (5)
2026-10-23T21:39:21.105619+02:00 desk-fedora bash[3045]: [3037:3037:1023/213921.105517:INFO:CONSOLE:8] "an error", source: file:///tmp/t.html (8)
2026-10-23T21:39:21.159266+02:00 desk-fedora bash[3045]: [3037:3037:1023/213921.159132:INFO:CONSOLE:10] "Uncaught (in promise) #<Object>", source: file:///tmp/t.html (10)
2026-10-23T21:39:21.167389+02:00 desk-fedora bash[3045]: [3037:3037:1023/213921.167246:INFO:CONSOLE:1] "from iframe 42", source: about:srcdoc (1)
2026-10-23T21:39:21.203227+02:00 desk-fedora bash[3045]: [3037:3037:1023/213921.202940:INFO:CONSOLE:11] "Uncaught TypeError: boom here", source: file:///tmp/t.html (11)
2026-10-23T21:52:13.388880+02:00 desk-fedora bash[2166]: [2069:2069:1023/215213.388112:INFO:CONSOLE:4] "two
2026-10-23T21:52:14+02:00 desk-fedora bash[2166]: [2069:2069:1023/215214.000001:INFO:CONSOLE:3] "said "hi"", source: chrome://newtab/ (3)
"""


class Chromium(unittest.TestCase):
    """Chromium logs every console call at one level: only "Uncaught" tells an error."""

    def test_in_the_shape_of_firefoxs(self):
        lines = browser.parse(CHROMIUM)
        self.assertEqual(lines, [
            '21:39:21 Chromium console: "plain log [object Object]" (file:///tmp/t.html, line 5)',
            '21:39:21 Chromium console: "an error" (file:///tmp/t.html, line 8)',
            "21:39:21 JavaScript error: file:///tmp/t.html, line 10: Uncaught (in promise) #<Object>",
            '21:39:21 Chromium console: "from iframe 42" (about:srcdoc, line 1)',
            "21:39:21 JavaScript error: file:///tmp/t.html, line 11: Uncaught TypeError: boom here",
            # The first line of a message of several: the rest carry no mark to find them by.
            '21:52:13 Chromium console: "two …"',
        ])
        self.assertEqual(len(browser.errors(lines)), 2)
        # console.error among them: nothing tells which, so none is counted as clean.
        self.assertEqual(len(browser.unlevelled(lines)), 4)

    def test_its_own_pages_only_with_everything(self):
        lines = browser.parse(CHROMIUM, everything=True)
        self.assertEqual(lines[-1], '21:52:14 Chromium console: "said "hi"" (chrome://newtab/, line 3)')

    def test_firefox_has_none_without_a_level(self):
        self.assertEqual(browser.unlevelled(browser.parse(JOURNAL)), [])


class Open(unittest.TestCase):
    """`desk browser open` tells a page that loaded from one that did not."""

    def open(self, pages: list[dict], target: str = "https://app.test/", opened: list | None = None,
             rc: int = 0, focused: list | None = None, **kw) -> str:
        from types import SimpleNamespace
        from unittest import mock

        run = SimpleNamespace(image="fedora-44-gnome", ssh_port=1, user="tester", dom=lambda: None)
        answers = iter(pages)
        last = pages[-1]
        started = SimpleNamespace(returncode=rc, stderr=b"")

        def ssh_run(port, user, command, **kw):
            (opened if opened is not None else []).append(command)
            return started

        def page_url(run):
            page = next(answers, last)
            if isinstance(page, Exception):
                raise page
            return page

        with mock.patch("desk.browser.a11y.page_url", side_effect=page_url), \
                mock.patch("desk.browser.a11y.focus",
                           side_effect=lambda run, name: (focused if focused is not None else []).append(name)), \
                mock.patch("desk.browser.guest.ssh_run", side_effect=ssh_run), \
                mock.patch("desk.browser.display.wait_stable"), mock.patch("desk.browser.time.sleep"):
            return browser.open_url(run, target, timeout=0.05, **kw)

    def test_loaded(self):
        self.assertEqual(self.open([{"url": "about:home"}, {"url": "https://app.test/"}]),
                         ("https://app.test/", "https://app.test/"))

    def test_a_browser_busy_loading_is_asked_again(self):
        slow = DeskError("the focused window did not answer in time while its accessibility tree was read")
        self.assertEqual(self.open([{"url": "about:home"}, slow, {"url": "https://app.test/"}])[0], "https://app.test/")

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

    def test_the_browser_in_the_focused_window_or_the_one_asked_for(self):
        for pages, kw, expected in (([{"url": "about:home", "app": "Chromium"}], {}, "Chromium"),
                                    ([{"url": "about:home", "app": "gnome-terminal-server"}], {}, "Firefox"),
                                    ([{"url": "about:home", "app": "Firefox"}], {"browser": "chromium"}, "Chromium")):
            opened, focused = [], []
            with self.subTest(expected=expected):
                self.open(pages + [{"url": "https://app.test/", "app": expected}], opened=opened, focused=focused, **kw)
                # Brought to the front first when another window was, to see what it shows now.
                self.assertEqual(focused, [expected] * (1 if pages[0]["app"] == expected else 2))
                self.assertIn("chromium-browser" if expected == "Chromium" else "firefox https://app.test/", opened[0])

    def test_an_error_page_left_from_before_in_a_browser_behind_is_not_the_new_page(self):
        stale = {"url": "chrome-error://chromewebdata/", "problem": "This site can’t be reached", "app": "Chromium"}
        self.assertEqual(self.open([{"url": "https://localhost:9090/", "app": "Firefox"}, stale, stale, stale,
                                    {"url": "https://app.test/", "app": "Chromium"}], browser="chromium")[0],
                         "https://app.test/")

    def test_a_path_on_the_page_of_another_browser_is_not_opened(self):
        with self.assertRaisesRegex(DeskError, "no page in a focused Chromium window"):
            self.open([{"url": "https://app.test/", "app": "Firefox"}], "/system", browser="chromium")

    def test_an_image_without_chromium_says_so(self):
        with self.assertRaisesRegex(DeskError, "fedora-44-gnome has no Chromium.*desk maintain --only fedora-44-gnome"):
            self.open([{"url": None}], browser="chromium", rc=127)
        with self.assertRaisesRegex(DeskError, "fedora-44-gnome has no Firefox: --browser chromium"):
            self.open([{"url": None}], browser="firefox", rc=127)

    def test_chromiums_error_page_is_not_waited_out(self):
        # Its address is Chromium's own, so the page asked for never "arrives".
        answers = [{"url": "https://app.test/old", "app": "Chromium"},
                   {"url": "chrome-error://chromewebdata/", "app": "Chromium",
                    "problem": "This site can’t be reached (ERR_CONNECTION_REFUSED)"}]
        with self.assertRaisesRegex(browser.NotLoaded, "Chromium shows 'This site can’t be reached"):
            self.open(answers)

    def test_chromium_still_on_the_page_before_while_the_server_does_not_answer(self):
        waiting = {"url": "https://example.com/", "address": "127.0.0.1:8099/x", "app": "Chromium"}
        with self.assertRaisesRegex(browser.NotLoaded, "still shows https://example.com/ while its address bar has "
                                    "127.0.0.1:8099/x"):
            self.open([waiting], "http://127.0.0.1:8099/x")
        # Sent back to where it was: the address bar shows that page too.
        back = {"url": "https://app.test/login", "address": "app.test/login", "app": "Chromium"}
        self.assertEqual(self.open([back], "https://app.test/dashboard")[0], "https://app.test/login")

    def test_addresses_as_chromiums_address_bar_writes_them(self):
        for url, bar in (("https://example.com/", "example.com"), ("file:///tmp/t.html", "/tmp/t.html"),
                         ("https://www.example.com/a%20b#x", "example.com/a b#x"),
                         ("http://127.0.0.1:8099/x", "127.0.0.1:8099/x")):
            with self.subTest(url=url):
                self.assertEqual(browser._bare(url), browser._bare(bar))

    def test_addresses_as_firefox_writes_them(self):
        for shown, asked in (("https://app.test/", "https://App.Test"), ("https://app.test/", "http://app.test/"),
                             ("file:///home/tester/x.html", "file:/home/tester/x.html"),
                             ("https://app.test/system/services#/x", "https://app.test/system"),
                             ("https://app.test/a?b=1", "https://app.test/a?b=1"),
                             ("https://app.test/system?x=1", "https://app.test/system"),
                             ("https://app.test/a?b=1&c=2", "https://app.test/a?b=1")):
            with self.subTest(shown=shown, asked=asked):
                self.assertTrue(browser.under(shown, asked))
        for shown, asked in (("https://app.test/login", "https://app.test/dashboard"),
                             ("https://other.test/", "https://app.test/"), ("about:home", "https://app.test/"),
                             ("https://app.test/example", "https://app.test/example#/tasks"),
                             ("https://app.test/example#/overview", "https://app.test/example#/tasks"),
                             ("https://app.test/systemd", "https://app.test/system"),
                             ("https://app.test/a?b=10", "https://app.test/a?b=1")):
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
                mock.patch("desk.browser.a11y.page_url", return_value={"app": "Firefox"}), \
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

    def test_the_page_area_is_the_box_firefox_gives(self):
        # Measured with Firefox 158 on Fedora 44: a 390x600 page is 390x600 screen pixels from the
        # box's corner, its frame drawn around the box.
        from types import SimpleNamespace
        from unittest import mock

        run = SimpleNamespace(image="fedora-44-gnome", dom=lambda: None, scale=100)
        field = {"pid": 1, "path": [0, 1], "role": "spin button", "name": ""}
        page = {"open": True, "bbox": [453, 157, 390, 600]}
        with mock.patch("desk.browser._linux", return_value=SimpleNamespace(layout="us")), \
                mock.patch("desk.browser.a11y.page_url", return_value={"app": "Firefox"}), \
                mock.patch("desk.browser.a11y.focus"), \
                mock.patch("desk.browser._rdm", side_effect=lambda run, focus=None, zoom=False:
                           page if focus == "page" else {"open": True}), \
                mock.patch("desk.browser._rdm_fields", return_value=[field, field]), \
                mock.patch("desk.browser.a11y.replace_text", return_value=""), \
                mock.patch("desk.browser.display") as display:
            display.png_size.return_value = (1280, 800)
            self.assertEqual(browser.viewport(run, "390x600"),
                             "page area 390x600 CSS pixels; on screen: desk shot --region 453,157,390,600; "
                             "desk browser viewport reset to close the mode")


class ChromiumDevice(unittest.TestCase):
    """Chromium's device mode is DevTools': opened, its sizes typed, closed with them."""

    def viewport(self, states: list[dict], size: str = "390x700", box=None, shows=None,
                 zooms: bool = True) -> tuple[str, list, list]:
        import json
        from types import SimpleNamespace
        from unittest import mock

        run = SimpleNamespace(image="fedora-44-gnome", dom=lambda: None, scale=100)
        answers, keys, typed, shows = iter(states), [], [], shows or {}
        last = states[-1]

        def rdm(run, focus=None, zoom=False):
            if zoom:
                keys.append("zoom 100%")
                return {**last, "zoomed": zooms}
            return {**last, "bbox": box} if focus == "page" else next(answers, last)

        def read(run, cmd, spec):
            # What the field shows: a value of `shows` first, else what was typed into it last.
            name = json.loads(spec)["name"]
            return {"text": (shows.get(name) or [dict(typed).get(name)]).pop(0)}

        display = mock.Mock()
        display.key.side_effect = lambda dom, combo: keys.append(combo)
        display.png_size.return_value = (1280, 800)
        with mock.patch("desk.browser._linux", return_value=SimpleNamespace(layout="us")), \
                mock.patch("desk.browser.a11y.focus"), mock.patch("desk.browser._rdm", side_effect=rdm), \
                mock.patch("desk.browser.a11y.replace_text", side_effect=lambda run, spec, text, layout:
                           typed.append((spec["name"], text)) or ""), \
                mock.patch("desk.browser.a11y._helper", side_effect=read), \
                mock.patch("desk.browser.display", display), mock.patch("desk.browser.time.sleep"), \
                mock.patch("desk.browser.time.monotonic", side_effect=iter(range(0, 10000, 5)).__next__):
            return browser.viewport(run, size, browser="chromium"), keys, typed

    FIELDS = [{"pid": 1, "path": [0, 3], "role": "spin button", "name": "Width"},
              {"pid": 1, "path": [0, 5], "role": "spin button", "name": "Height (leave empty for full)"}]

    def test_opens_devtools_then_the_device_toolbar(self):
        closed = {"open": False, "devtools": False, "fields": []}
        tools = {"open": False, "devtools": True, "fields": []}
        device = {"open": True, "devtools": True, "fields": self.FIELDS}
        out, keys, typed = self.viewport([closed, tools, device], box=[48, 64, 390, 700])
        # At 100 %: fitted to the window the page would be smaller on screen than its CSS pixels.
        self.assertEqual(keys, ["ctrl+shift+i", "ctrl+shift+m", "zoom 100%", "ret", "ret"])
        self.assertEqual(typed, [("Width", "390"), ("Height (leave empty for full)", "700")])
        self.assertIn("page area 390x700 CSS pixels; on screen: desk shot --region 48,64,390,700", out)

    def test_says_when_it_runs_past_the_bottom(self):
        device = {"open": True, "devtools": True, "fields": self.FIELDS}
        out, keys, _ = self.viewport([device], box=[168, 193, 390, 700])
        self.assertEqual(keys, ["zoom 100%", "ret", "ret"])
        self.assertIn("runs past the bottom of the screen (desk display --resolution 1280x1024", out)
        # Chromium's box is the part its window shows.
        out, _, _ = self.viewport([device], box=[168, 193, 390, 582])
        self.assertTrue(out.startswith("page area 390x700 CSS pixels and runs past the bottom of the window (desk "
                                       "display --resolution 1280x1024 for a taller one); on screen: desk shot "
                                       "--region 168,193,390,582"), out)

    def test_a_size_the_toolbar_lost_is_typed_again_once(self):
        device = {"open": True, "devtools": True, "fields": self.FIELDS}
        out, keys, typed = self.viewport([device], box=[168, 193, 390, 582],
                                         shows={"Height (leave empty for full)": [""]})
        self.assertEqual(len(typed), 4)
        self.assertIn("page area 390x700 CSS pixels", out)
        with self.assertRaisesRegex(DeskError, "device mode's fields show 390 x  afterwards"):
            self.viewport([device], box=[168, 193, 390, 582], shows={"Height (leave empty for full)": ["", ""]})

    def test_a_box_a_pixel_off_is_the_size_asked_for(self):
        device = {"open": True, "devtools": True, "fields": self.FIELDS}
        out, _, _ = self.viewport([device], box=[168, 186, 390, 777], size="390x776")
        self.assertTrue(out.startswith("page area 390x776 CSS pixels and runs past the bottom of the screen"), out)

    def test_a_zoom_that_does_not_come_is_desks_failure(self):
        device = {"open": True, "devtools": True, "fields": self.FIELDS}
        with self.assertRaisesRegex(DeskError, "did not take the zoom 100 %"):
            self.viewport([device], box=[168, 193, 390, 700], zooms=False)

    def test_reset_closes_devtools(self):
        out, keys, _ = self.viewport([{"open": True, "devtools": True}, {"open": False, "devtools": False}], "reset")
        self.assertEqual(keys, ["f12"])
        self.assertEqual(out, "DevTools closed: pages get the whole window again")
