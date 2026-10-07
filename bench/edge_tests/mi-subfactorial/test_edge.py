"""Edge cases for subfactorial: the recurrence far past float range, small values, types and errors."""
import math
import unittest

import more_itertools as mi


class SubfactorialEdgeTests(unittest.TestCase):
    def test_recurrence_holds_exactly_for_large_n(self):
        values = [1, 0]
        for n in range(2, 160):
            values.append((n - 1) * (values[-1] + values[-2]))
        for n in (0, 1, 2, 30, 60, 100, 159):
            with self.subTest(n=n):
                self.assertEqual(mi.subfactorial(n), values[n])

    def test_matches_the_rounding_formula_where_floats_are_exact_enough(self):
        for n in range(1, 18):
            self.assertEqual(mi.subfactorial(n), round(math.factorial(n) / math.e))

    def test_returns_int(self):
        self.assertIs(type(mi.subfactorial(7)), int)
        self.assertIs(type(mi.subfactorial(0)), int)

    def test_errors(self):
        for bad in (-1, -10):
            with self.assertRaises(ValueError):
                mi.subfactorial(bad)
        for bad in (2.5, None, '3', [3]):
            with self.assertRaises(TypeError):
                mi.subfactorial(bad)

    def test_exported(self):
        from more_itertools import subfactorial
        from more_itertools.more import __all__ as names
        self.assertIn('subfactorial', names)
        self.assertEqual(subfactorial(4), 9)


if __name__ == '__main__':
    unittest.main()
