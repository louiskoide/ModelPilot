"""Edge cases for numeric_range consistency: many float ranges, and exact types left unchanged."""
from datetime import datetime, timedelta
from decimal import Decimal
from fractions import Fraction
import random
import unittest

import more_itertools as mi


def float_ranges(count=300, seed=20260929):
    rng = random.Random(seed)
    out = []
    while len(out) < count:
        start = round(rng.uniform(-50, 50), rng.choice([1, 2, 3, 6]))
        step = round(rng.uniform(0.01, 5), rng.choice([1, 2, 3, 6])) * rng.choice([1, -1])
        n = rng.randint(0, 40)
        stop = start + step * (n + rng.choice([0, 0.5, 0.999, 1e-9]))
        if step:
            out.append((start, stop, step))
    return out


class NumericRangeEdgeTests(unittest.TestCase):
    def check_consistent(self, r):
        items = list(r)
        self.assertEqual(len(items), len(r))
        self.assertEqual(items, [r[i] for i in range(len(r))])
        for i, v in enumerate(items):
            self.assertIn(v, r)
            self.assertEqual(r.index(v), i)
            self.assertEqual(r.count(v), 1)
        return items

    def test_many_float_ranges_agree_with_their_items(self):
        for args in float_ranges():
            with self.subTest(args=args):
                r = mi.numeric_range(*args)
                items = self.check_consistent(r)
                for a, b in zip(items, items[1:]):  # values between items are not members
                    middle = (a + b) / 2
                    self.assertNotIn(middle, r)
                    self.assertEqual(r.count(middle), 0)
                if items:
                    self.assertNotIn(args[1], r)  # the stop is never a member

    def test_reversed_ranges_are_consistent(self):
        for args in [(0.0, 1.0, 0.1), (1.0, 0.0, -0.1), (-8.732, -13.532, -2.4), (0.3, 2.9, 0.13)]:
            with self.subTest(args=args):
                self.check_consistent(mi.numeric_range(*args)[::-1])

    def test_exact_types_are_unchanged(self):
        r = mi.numeric_range(Fraction(0), Fraction(1), Fraction(1, 10))
        self.assertEqual((len(r), r.index(Fraction(3, 10)), r.count(Fraction(3, 10))), (10, 3, 1))
        self.assertNotIn(Fraction(1, 3), r)
        self.assertNotIn(Fraction(1), r)
        d = mi.numeric_range(Decimal('0.0'), Decimal('1.0'), Decimal('0.1'))
        self.assertEqual((len(d), d.index(Decimal('0.3')), list(d)[-1]), (10, 3, Decimal('0.9')))
        self.assertNotIn(Decimal('0.35'), d)
        i = mi.numeric_range(0, 10, 3)
        self.assertEqual((len(i), list(i), i.index(6)), (4, [0, 3, 6, 9], 2))
        self.assertNotIn(10, i)
        self.assertNotIn(4, i)
        n = mi.numeric_range(10, 0, -4)
        self.assertEqual((len(n), list(n), n.index(2)), (3, [10, 6, 2], 2))
        for exact in (r, d, i, n):
            self.check_consistent(exact)

    def test_datetimes_are_unchanged(self):
        start = datetime(2020, 1, 1)
        r = mi.numeric_range(start, datetime(2020, 1, 2), timedelta(hours=5))
        self.assertEqual(len(r), 5)
        self.assertEqual(r.index(start + timedelta(hours=10)), 2)
        self.assertNotIn(start + timedelta(hours=11), r)
        self.check_consistent(r)


if __name__ == '__main__':
    unittest.main()
