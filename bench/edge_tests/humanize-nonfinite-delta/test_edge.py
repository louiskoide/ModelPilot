"""Edge cases for naturaldelta() with non-finite and huge values: options, signs, ints, finite values unchanged."""
import unittest

import humanize


class NonFiniteDeltaEdgeTests(unittest.TestCase):
    def test_non_finite_with_every_option(self):
        options = [{}, {'months': False}, {'minimum_unit': 'milliseconds'}, {'minimum_unit': 'microseconds'}]
        for value in (float('inf'), float('-inf'), float('nan')):
            for kwargs in options:
                with self.subTest(value=value, kwargs=kwargs):
                    out = humanize.naturaldelta(value, **kwargs)
                    self.assertIsInstance(out, str)
                    self.assertEqual(out, str(value))

    def test_too_large_finite_values_still_raise(self):
        for value in (1e30, -1e30, 1e300, -1e300, 1e15, -1e15, 10**30, -(10**30)):
            with self.subTest(value=value):
                with self.assertRaises(OverflowError):
                    humanize.naturaldelta(value)

    def test_finite_values_unchanged(self):
        cases = {0: 'a moment', 1.0: 'a second', 30.5: '30 seconds', 3600.0: 'an hour', -3600.0: 'an hour',
                 86400 * 3: '3 days'}
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(humanize.naturaldelta(value), expected)


if __name__ == '__main__':
    unittest.main()
