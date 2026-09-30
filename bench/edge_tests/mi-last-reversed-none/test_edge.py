"""Edge cases for last() with `__reversed__ = None`: several items, defaults, inherited markers."""
import unittest

import more_itertools as mi


class NotReversible:
    __reversed__ = None

    def __init__(self, items):
        self.items = items

    def __iter__(self):
        return iter(self.items)


class Reversible:
    def __init__(self, items):
        self.items = items

    def __iter__(self):
        return iter(self.items)

    def __reversed__(self):
        return reversed(self.items)


class MarkedNotReversible(Reversible):
    __reversed__ = None


class LastReversedNoneEdgeTests(unittest.TestCase):
    def test_the_final_item_of_several(self):
        self.assertEqual(mi.last(NotReversible([1, 2, 3])), 3)
        self.assertEqual(mi.last(NotReversible('abc')), 'c')

    def test_empty_uses_the_default_or_raises(self):
        self.assertEqual(mi.last(NotReversible([]), default='none'), 'none')
        self.assertIsNone(mi.last(NotReversible([]), None))
        with self.assertRaises(ValueError):
            mi.last(NotReversible([]))

    def test_a_subclass_that_marks_itself_not_reversible(self):
        self.assertEqual(mi.last(MarkedNotReversible([4, 5, 6])), 6)

    def test_ordinary_reversible_iterables_still_work(self):
        self.assertEqual(mi.last(Reversible([4, 5, 6])), 6)
        self.assertEqual(mi.last(iter([7, 8])), 8)
        self.assertEqual(mi.last({'a': 1, 'b': 2}), 'b')


if __name__ == '__main__':
    unittest.main()
