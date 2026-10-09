import unittest

try:
    from desk.guest_a11y import placement, stacked_base
except (ImportError, ValueError):  # no AT-SPI typelib on this host
    placement = stacked_base = None


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
    def __init__(self, parent=None):
        self.parent = parent

    def get_parent(self):
        return self.parent


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


if __name__ == "__main__":
    unittest.main()
