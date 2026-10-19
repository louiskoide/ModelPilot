"""Edge cases for intword() carries: other formats, negatives, strings, values just short of a carry."""
import unittest

import humanize

UNITS = ['thousand', 'million', 'billion', 'trillion', 'quadrillion', 'quintillion', 'sextillion', 'septillion',
         'octillion', 'nonillion', 'decillion']


class IntwordCarryEdgeTests(unittest.TestCase):
    def test_carry_with_other_formats(self):
        for i, exponent in enumerate(range(9, 34, 3)):
            unit = UNITS[i + 2]
            with self.subTest(exponent=exponent):
                self.assertEqual(humanize.intword(10**exponent - 10**(exponent - 6), '%.2f'), f'1.00 {unit}')
                self.assertEqual(humanize.intword(10**exponent - 10**(exponent - 5), '%.1f'), f'1.0 {unit}')
                self.assertEqual(humanize.intword(10**exponent - 1, '%.3f'), f'1.000 {unit}')

    def test_negative_and_string_values(self):
        for i, exponent in enumerate(range(6, 34, 3)):
            unit = UNITS[i + 1]
            with self.subTest(exponent=exponent):
                self.assertEqual(humanize.intword(-(10**exponent - 1)), f'-1.0 {unit}')
                self.assertEqual(humanize.intword(str(10**exponent - 1)), f'1.0 {unit}')

    def test_no_carry_when_rounding_stays_below(self):
        for i, exponent in enumerate(range(9, 34, 3)):
            unit = UNITS[i + 1]
            with self.subTest(exponent=exponent):
                self.assertEqual(humanize.intword(999_940 * 10**(exponent - 6)), f'999.9 {unit}')
                self.assertEqual(humanize.intword(10**exponent), f'1.0 {UNITS[i + 2]}')

    def test_rendered_mantissa_stays_below_1000(self):
        for exponent in range(4, 34):
            for value in (10**exponent - 1, 10**exponent - 7, 10**exponent - 10**(exponent - 4)):
                with self.subTest(value=value):
                    mantissa = float(humanize.intword(value).split(' ', 1)[0])
                    self.assertLess(mantissa, 1000)


if __name__ == '__main__':
    unittest.main()
