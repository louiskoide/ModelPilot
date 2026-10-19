"""Edge cases for fractional(): more values that round to a whole number, strings, signs, unchanged mixed fractions."""
import unittest

import humanize


class FractionalEdgeTests(unittest.TestCase):
    def test_values_that_round_to_a_whole_number(self):
        cases = {4.99999999: '5', 9.9999: '10', -9.9999: '-10', -0.9999999: '-1', 1.0000001: '1',
                 -1.0000001: '-1', 0.00001: '0', 1e-9: '0', 99.99999: '100', 7: '7', -7: '-7', 7.0: '7',
                 12.0004: '12'}
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(humanize.fractional(value), expected)

    def test_strings(self):
        for value, expected in (('2.9999999', '3'), ('0', '0'), ('0.0', '0'), ('-0.9999999', '-1'), ('5', '5')):
            with self.subTest(value=value):
                self.assertEqual(humanize.fractional(value), expected)

    def test_no_degenerate_fraction_for_near_integers(self):
        for whole in range(-20, 21):
            for delta in (0, 1e-7, -1e-7, 1e-5, -1e-5):
                value = whole + delta
                with self.subTest(value=value):
                    out = humanize.fractional(value)
                    self.assertNotIn('/', out)
                    self.assertEqual(int(out), whole)

    def test_real_fractions_unchanged(self):
        cases = {0.5: '1/2', 2.5: '2 1/2', -2.5: '-2 1/2', 0.3: '3/10', 1.3: '1 3/10', 1 / 3: '1/3',
                 2.75: '2 3/4', 0.999: '999/1000'}
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(humanize.fractional(value), expected)


if __name__ == '__main__':
    unittest.main()
