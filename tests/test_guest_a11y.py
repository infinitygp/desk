import unittest

try:
    from desk.guest_a11y import stacked_base
except (ImportError, ValueError):  # no AT-SPI typelib on this host
    stacked_base = None


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


if __name__ == "__main__":
    unittest.main()
