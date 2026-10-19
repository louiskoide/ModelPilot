"""Edge cases for metric() carries: every SI prefix boundary in both directions, negatives, other precisions."""
import unittest

import humanize

UP = ['k', 'M', 'G', 'T', 'P', 'E', 'Z', 'Y', 'R']
DOWN = ['m', 'μ', 'n', 'p', 'f', 'a', 'z', 'y', 'r']


class MetricCarryEdgeTests(unittest.TestCase):
    def test_every_prefix_boundary_upwards(self):
        # 0.99999 x 10**(3k) rounds to 1000 of the prefix below, so it carries.
        self.assertEqual(humanize.metric(999.999, 'V'), '1.00 kV')
        for k, prefix in enumerate(UP[1:], start=2):
            with self.subTest(prefix=prefix):
                self.assertEqual(humanize.metric(0.99999 * 10**(3 * k), 'V'), f'1.00 {prefix}V')

    def test_every_prefix_boundary_downwards(self):
        self.assertEqual(humanize.metric(0.99999, 'V'), '1.00 V')
        for k, prefix in enumerate(DOWN, start=1):
            with self.subTest(prefix=prefix):
                self.assertEqual(humanize.metric(0.99999 * 10**(-3 * k), 'V'), f'1.00 {prefix}V')

    def test_negative_values(self):
        cases = {-999.9: '-1.00 kV', -999_999: '-1.00 MV', -0.0009999: '-1.00 mV', -999.4: '-999 V'}
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(humanize.metric(value, 'V'), expected)

    def test_same_precision_after_the_carry(self):
        cases = [((999.99, 'V', 4), '1.000 kV'), ((999_960, 'Hz', 4), '1.000 MHz'), ((99_999.9, 'V', 6), '99.9999 kV'),
                 ((999.9994, 'V', 6), '999.999 V'), ((999.99999999, 'V', 6), '1.00000 kV'), ((999.6, '', 3), '1.00 k')]
        for (value, unit, precision), expected in cases:
            with self.subTest(value=value, precision=precision):
                self.assertEqual(humanize.metric(value, unit, precision=precision), expected)

    def test_no_carry_below_1000(self):
        cases = {999.4: '999 V', 999.49: '999 V', 999_400: '999 kV', 0.0009994: '999 μV', 1000: '1.00 kV'}
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(humanize.metric(value, 'V'), expected)


if __name__ == '__main__':
    unittest.main()
