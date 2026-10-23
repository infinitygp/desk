import unittest
from types import SimpleNamespace
from unittest import mock

try:
    from desk import guest_a11y
    from desk.guest_a11y import Atspi, placement, stacked_base, visible_point
except (ImportError, ValueError):  # no AT-SPI typelib on this host
    guest_a11y = Atspi = placement = stacked_base = visible_point = None


@unittest.skipIf(stacked_base is None, "AT-SPI GObject introspection not installed")
class StackedBase(unittest.TestCase):
    base = ("window", 460, 108)

    def test_child_as_large_as_parent_takes_offset_back(self):
        # gnome-calculator's keypad: a GtkStack page and its child both report (12, 12).
        parent, child = [472, 476, 336, 236], [484, 488, 336, 236]
        self.assertEqual(stacked_base(parent, child, self.base), ("window", 448, 96))

    def test_ordinary_layouts_untouched(self):
        self.assertEqual(stacked_base([472, 476, 336, 236], [472, 476, 336, 236], self.base), self.base)
        self.assertEqual(stacked_base([472, 476, 336, 236], [484, 488, 64, 44], self.base), self.base)
        self.assertEqual(stacked_base(None, [484, 488, 64, 44], self.base), self.base)
        self.assertEqual(stacked_base([472, 476, 336, 236], None, self.base), self.base)


class Node:
    def __init__(self, parent=None, labels=()):
        self.parent, self.labels = parent, list(labels)

    def get_parent(self):
        return self.parent

    def get_relation_set(self):
        return [Relation(Atspi.RelationType.LABELLED_BY, self.labels)] if self.labels else []


class Relation:
    def __init__(self, kind, targets):
        self.kind, self.targets = kind, targets

    def get_relation_type(self):
        return self.kind

    def get_n_targets(self):
        return len(self.targets)

    def get_target(self, i):
        return self.targets[i]


@unittest.skipIf(placement is None, "AT-SPI GObject introspection not installed")
class Placement(unittest.TestCase):
    # document > form > pane (scrolls) > button > label; footer is the form's sticky footer
    doc = Node()
    form = Node(doc)
    pane = Node(form)
    button = Node(pane)
    label = Node(button)
    footer = Node(form)
    footer_button = Node(footer)
    chain = ((doc, [0, 0, 800, 600]), (form, [0, 0, 800, 600]), (pane, [0, 100, 800, 300]))

    def test_the_element_or_what_it_contains(self):
        self.assertEqual(placement(self.button, self.button, self.chain, (10, 150)), "self")
        self.assertEqual(placement(self.button, self.label, self.chain, (10, 150)), "inside")

    def test_something_else_covers_it(self):
        self.assertEqual(placement(self.button, self.footer_button, self.chain, (10, 150)), "other")

    def test_an_ancestor_counts_only_when_the_point_is_inside_everything_between(self):
        # Firefox hit-tests a sticky footer's container but not its buttons.
        footer_chain = [(self.doc, [0, 0, 800, 600]), (self.form, [0, 0, 800, 600]), (self.footer, [0, 550, 800, 50])]
        self.assertEqual(placement(self.footer_button, self.footer, footer_chain, (10, 570)), "around")
        # Scrolled out of the pane: the form is there, the pane is not.
        self.assertEqual(placement(self.button, self.form, self.chain, (10, 50)), "other")

    def test_unknown(self):
        self.assertIsNone(placement(self.button, None, self.chain, (10, 150)))

    def test_its_own_label_over_it_is_where_to_click(self):
        # A PatternFly tile: the label over the whole card is what a click on the radio hits.
        card = Node(self.pane)
        label = Node(card)
        radio = Node(card, labels=[Node(card), label])
        chain = self.chain + ((card, [0, 100, 200, 80]),)
        self.assertEqual(placement(radio, label, chain, (100, 140)), "label")
        self.assertEqual(placement(radio, Node(label), chain, (100, 140)), "label")
        self.assertEqual(placement(radio, self.footer_button, chain, (100, 140)), "other")



@unittest.skipIf(visible_point is None, "AT-SPI GObject introspection not installed")
class CutOff(unittest.TestCase):
    # Cockpit's services list: the link of the last row reaches 12 px below the bottom of the
    # plugin's main area, which scrolls, and its middle with it.
    chain = [(Node(), [0, 32, 1280, 768]), (Node(), [240, 186, 1040, 614]), (Node(), [268, 190, 984, 582])]
    link = [312, 763, 34, 21]

    def test_the_point_listed_is_one_the_container_shows(self):
        self.assertEqual(visible_point(self.link, self.chain), (329, 768))
        self.assertEqual(visible_point([312, 700, 34, 21], self.chain), (329, 710))
        self.assertIsNone(visible_point([312, 780, 34, 21], self.chain))

    def test_a_click_goes_to_the_part_shown_and_says_when_none_is(self):
        root = (Node(), ("window", 0, 0), [0, 32, 1280, 768])
        link = Node()
        with mock.patch.object(guest_a11y, "hit_at", return_value=link):
            self.assertEqual(guest_a11y.clickable_point(link, self.link, self.chain, root, True),
                             ((329, 768), None, False))
            # All of it below the edge, yet hit-tested as there: clicked, but scrolled first.
            self.assertEqual(guest_a11y.clickable_point(link, [312, 780, 34, 16], self.chain, root, True),
                             ((329, 788), None, True))
        with mock.patch.object(guest_a11y, "hit_at", return_value=None):  # no hit test: what it shows
            self.assertEqual(guest_a11y.clickable_point(link, self.link, self.chain, root, True),
                             ((329, 768), None, False))
            self.assertEqual(guest_a11y.clickable_point(link, self.link, self.chain, root, False), (None, None, False))



class Acc:
    def __init__(self, role, name):
        self.role, self.name = role, name

    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.name

    def get_state_set(self):
        return SimpleNamespace(contains=lambda state: state == Atspi.StateType.SHOWING)

    def get_extents(self, coord):
        return SimpleNamespace(x=10, y=20, width=100, height=20)

    def get_interfaces(self):
        return ["Text", "Component"]


@unittest.skipIf(guest_a11y is None, "AT-SPI GObject introspection not installed")
class OwnText(unittest.TestCase):
    def node(self, role, name, text):
        with mock.patch.object(guest_a11y, "text_of", return_value=text):
            node, st = guest_a11y.describe(Acc(role, name), ("screen", 0, 0), 1, [0], 0)
        return node if guest_a11y.wanted(node, st, "") else None

    def test_a_named_group_keeps_its_text(self):
        # role="group" aria-label="Selection" around "2 tags selected": Firefox's panel.
        self.assertEqual(self.node("panel", "Selection", "2 tags selected")["text"], "2 tags selected")
        self.assertEqual(self.node("panel", "", "2 tags selected")["text"], "2 tags selected")

    def test_containers_without_text_of_their_own_stay_out(self):
        self.assertIsNone(self.node("panel", "Selection", ""))
        self.assertIsNone(self.node("section", "", ""))

    def test_a_named_label_is_its_name(self):
        self.assertNotIn("text", self.node("label", "User name", "User name"))


class Tree:
    """An accessible with children and object attributes, as a browser's page has."""

    def __init__(self, role, name="", attrs=None, children=(), toolkit=("", "0")):
        self.role, self.name, self.attrs, self.children, self.toolkit = role, name, attrs or {}, list(children), toolkit

    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.name

    def get_attributes(self):
        return self.attrs

    def get_child_count(self):
        return len(self.children)

    def get_child_at_index(self, i):
        return self.children[i]

    def get_toolkit_name(self):
        return self.toolkit[0]

    def get_toolkit_version(self):
        return self.toolkit[1]


@unittest.skipIf(guest_a11y is None, "AT-SPI GObject introspection not installed")
class ChromiumPages(unittest.TestCase):
    def test_window_coordinates_from_the_surface(self):
        # Chromium draws its own shadow: its Back button at (18, 56) of a window whose frame is
        # at (127, 39) and its surface at (115, 29) is on screen at (133, 85). Measured on GNOME 50.
        window = {"pid": 7, "title": "Example Domain - Chromium", "minimized": False, "x11": False,
                  "frame": [127, 39, 1026, 755], "buffer": [115, 29, 1050, 780]}
        mapper = guest_a11y.Mapper([window])
        top = Tree("frame", "Example Domain - Chromium")
        chromium = Tree("application", "Chromium", toolkit=("Chromium", "155.0.8059.84"))
        firefox = Tree("application", "Firefox", toolkit=("Gecko", "144.0"))
        self.assertEqual(mapper.for_toplevel("Chromium", 7, top, chromium), ("window", 115, 29))
        self.assertEqual(mapper.for_toplevel("Firefox", 7, top, firefox), ("window", 127, 39))

    def test_error_page_and_certificate_warning(self):
        def page(heading, code, code_role="static"):
            return Tree("document web", children=[Tree("section", attrs={"id": "main-content"}, children=[
                Tree("section", attrs={"id": "main-message"}, children=[
                    Tree("heading", heading, {"tag": "h1"}),
                    Tree("section", attrs={"id": "debugging"}, children=[
                        Tree(code_role, code, {"class": "error-code"})])])])])

        with mock.patch.object(guest_a11y, "text_of", return_value=""):
            self.assertEqual(guest_a11y.browser_error(page("This site can’t be reached", "ERR_CONNECTION_REFUSED"),
                                                      "chrome-error://chromewebdata/"),
                             "This site can’t be reached (ERR_CONNECTION_REFUSED)")
            self.assertEqual(guest_a11y.browser_error(page("Your connection is not private",
                                                           "NET::ERR_CERT_AUTHORITY_INVALID", "button"),
                                                      "chrome-error://chromewebdata/"),
                             "Your connection is not private (NET::ERR_CERT_AUTHORITY_INVALID)")
            # A page that only looks like one is the page asked for.
            self.assertIsNone(guest_a11y.browser_error(page("Offline", "E1"), "https://app.test/"))

    def test_a_walk_out_of_time_is_not_a_walk_that_found_nothing(self):
        import time

        top = Tree("frame", children=[Tree("panel", children=[Tree("embedded", "responsive")])])
        seen = []
        with mock.patch.object(guest_a11y, "states", return_value={"showing"}):
            guest_a11y.showing_walk(top, lambda a: seen.append(a.role), time.monotonic() + 5)
            self.assertEqual(seen, ["panel", "embedded"])
            with self.assertRaisesRegex(LookupError, "did not answer in time"):
                guest_a11y.showing_walk(top, lambda a: None, time.monotonic() - 1)

    def test_the_page_behind_docked_devtools(self):
        # The address bar first, then DevTools (desk browser viewport opens them), then the page.
        bar = Tree("entry", attrs={"class": guest_a11y.OMNIBOX})
        devtools, inspected = Tree("document web", "DevTools"), Tree("document web", "Example Domain")
        top = Tree("frame", "Example Domain - Chromium", children=[
            Tree("panel", children=[bar]), Tree("panel", children=[devtools, inspected])])
        app = Tree("application", "Chromium", toolkit=("Chromium", "155.0.8059.84"))
        urls = {devtools: "devtools://devtools/bundled/devtools_app.html", inspected: "https://example.com/"}
        with mock.patch.object(guest_a11y, "front_toplevel", return_value=({"title": top.name}, app, top)), \
                mock.patch.object(guest_a11y, "states", return_value={"showing"}), \
                mock.patch.object(guest_a11y, "doc_url", side_effect=urls.get), \
                mock.patch.object(guest_a11y, "text_of", return_value="example.com"), \
                mock.patch.object(guest_a11y, "browser_error", return_value=None):
            shown = guest_a11y.page({})
        self.assertEqual((shown["url"], shown["address"]), ("https://example.com/", "example.com"))


if __name__ == "__main__":
    unittest.main()
