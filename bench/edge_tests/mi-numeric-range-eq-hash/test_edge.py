"""Edge cases for numeric_range equality and hashing: integer ranges against range, other types, sets."""
from datetime import datetime, timedelta
from decimal import Decimal
from fractions import Fraction
import itertools
import unittest

import more_itertools as mi


class NumericRangeEqHashEdgeTests(unittest.TestCase):
    def test_integer_ranges_compare_like_range(self):
        args = [(a, b, c) for a in range(-2, 3) for b in range(-2, 4) for c in (-3, -1, 1, 2, 4)]
        for x, y in itertools.combinations(args, 2):
            with self.subTest(x=x, y=y):
                self.assertEqual(mi.numeric_range(*x) == mi.numeric_range(*y), range(*x) == range(*y))
                if range(*x) == range(*y):
                    self.assertEqual(hash(mi.numeric_range(*x)), hash(mi.numeric_range(*y)))

    def test_one_element_ranges_ignore_the_step(self):
        for x, y in [((1.0, 1.1, 5.0), (1.0, 1.2, 3.0)), ((Decimal('2'), Decimal('3'), Decimal('7')),
                                                           (Decimal('2'), Decimal('2.5'), Decimal('1'))),
                     ((Fraction(1, 2), Fraction(1), Fraction(9)), (Fraction(1, 2), Fraction(3, 4), Fraction(1, 2)))]:
            self.assertEqual(mi.numeric_range(*x), mi.numeric_range(*y))
            self.assertEqual(hash(mi.numeric_range(*x)), hash(mi.numeric_range(*y)))

    def test_empty_ranges_of_any_type_are_equal(self):
        empties = [mi.numeric_range(0), mi.numeric_range(1.5, 1.0), mi.numeric_range(Decimal('3'), Decimal('1')),
                   mi.numeric_range(datetime(2026, 1, 2), datetime(2026, 1, 1), timedelta(hours=1))]
        for x, y in itertools.combinations(empties, 2):
            self.assertEqual(x, y)
            self.assertEqual(hash(x), hash(y))

    def test_ranges_differing_in_length_first_value_or_step_differ(self):
        for x, y in [((0.0, 3.0, 1.0), (0.0, 4.0, 1.0)), ((0.0, 3.0, 1.0), (0.5, 3.5, 1.0)),
                     ((0.0, 3.0, 1.0), (0.0, 6.0, 2.0)),
                     ((datetime(2026, 1, 1), datetime(2026, 1, 2), timedelta(hours=10)),
                      (datetime(2026, 1, 1), datetime(2026, 1, 2), timedelta(hours=11)))]:
            self.assertNotEqual(mi.numeric_range(*x), mi.numeric_range(*y))

    def test_sets_and_other_types(self):
        self.assertEqual(len({mi.numeric_range(2, 3, 1), mi.numeric_range(2, 3, 5), mi.numeric_range(2, 3)}), 1)
        r = mi.numeric_range(0, 3)
        self.assertEqual(r, r)
        self.assertNotEqual(r, [0, 1, 2])
        self.assertNotEqual(r, None)
        self.assertFalse(r == range(0, 3))


if __name__ == '__main__':
    unittest.main()
