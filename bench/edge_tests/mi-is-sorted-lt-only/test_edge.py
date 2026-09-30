"""Edge cases for is_sorted() needing only `<`: objects whose other comparisons raise, keys, duplicates."""
from itertools import product
import unittest

import more_itertools as mi


class OnlyLess:
    """Supports `<` and nothing else: >, <=, >= raise instead of falling back to a reflected `<`."""
    def __init__(self, value):
        self.value = value

    def __lt__(self, other):
        return self.value < other.value

    def _refuse(self, other):
        raise TypeError('only < is supported')

    __gt__ = __le__ = __ge__ = _refuse


def expected(values, reverse, strict):
    """What comparing the input with sorted() gives, for plain numbers."""
    pairs = list(zip(values, values[1:]))
    if reverse:
        pairs = [(b, a) for a, b in pairs]
    return all(a < b for a, b in pairs) if strict else all(not b < a for a, b in pairs)


CASES = [[], [1], [1, 2, 3], [1, 1, 2], [3, 2, 1], [3, 3, 1], [2, 2, 2], [1, 3, 2], [2, 1, 3], [5, 5]]


class IsSortedEdgeTests(unittest.TestCase):
    def test_items_with_only_less_than(self):
        for values, reverse, strict in product(CASES, (False, True), (False, True)):
            with self.subTest(values=values, reverse=reverse, strict=strict):
                result = mi.is_sorted(map(OnlyLess, values), reverse=reverse, strict=strict)
                self.assertEqual(result, expected(values, reverse, strict))

    def test_keys_with_only_less_than(self):
        for values, reverse, strict in product(CASES, (False, True), (False, True)):
            with self.subTest(values=values, reverse=reverse, strict=strict):
                items = [str(v) for v in values]
                result = mi.is_sorted(items, key=lambda s: OnlyLess(int(s)), reverse=reverse, strict=strict)
                self.assertEqual(result, expected(values, reverse, strict))

    def test_one_pass_iterators(self):
        self.assertTrue(mi.is_sorted(iter(map(OnlyLess, [1, 2, 2])), strict=False))
        self.assertFalse(mi.is_sorted(iter(map(OnlyLess, [1, 2, 2])), strict=True))
        self.assertTrue(mi.is_sorted(iter(map(OnlyLess, [3, 2, 1])), reverse=True, strict=True))


if __name__ == '__main__':
    unittest.main()
