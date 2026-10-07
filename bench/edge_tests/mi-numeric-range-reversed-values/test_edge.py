"""Edge cases for reversed(numeric_range): exact values and length across number types, steps and limits."""
from datetime import date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
import unittest

import more_itertools as mi


class NumericRangeReversedEdgeTests(unittest.TestCase):
    def check(self, *args):
        r = mi.numeric_range(*args)
        backwards = list(reversed(r))
        self.assertEqual(backwards, list(r)[::-1], args)
        self.assertEqual(len(backwards), len(r), args)

    def test_float_ranges_in_both_directions(self):
        for args in [(0.0, 1.0, 0.1), (1.0, 0.0, -0.1), (0.1, 0.5, 0.1), (-1.0, 1.0, 0.3), (0.0, 0.95, 0.05),
                     (5.5,), (2.5, 7.25), (0.0, 1e-9, 1e-10)]:
            self.check(*args)

    def test_exact_number_types(self):
        self.check(Decimal('0'), Decimal('1'), Decimal('0.3'))
        self.check(Fraction(1, 3), Fraction(7, 3), Fraction(1, 3))
        self.check(10, 0, -3)

    def test_empty_and_one_element_ranges(self):
        for args in [(0.0, 0.0, 0.5), (1.0, 0.0, 0.5), (0.0, 1.0, -0.5), (0.0, 0.1, 1.0)]:
            self.check(*args)

    def test_datetime_ranges_at_the_limits(self):
        day = timedelta(days=1)
        self.check(datetime.min, datetime.min + 3 * day, day)
        self.check(datetime.max, datetime.max - 3 * day, -day)
        self.check(date.min, date.min + 2 * day, day)

    def test_ordinary_datetime_ranges(self):
        start = datetime(2026, 1, 1)
        self.check(start, start + timedelta(hours=5), timedelta(minutes=45))


if __name__ == '__main__':
    unittest.main()
