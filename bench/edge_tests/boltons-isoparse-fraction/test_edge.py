"""Edge cases for isoparse fractions: round trips over many microseconds, every fraction length, truncation not rounding."""
import random
import unittest
from datetime import datetime

from boltons.timeutils import isoparse


class IsoparseFractionEdgeTests(unittest.TestCase):
    def test_round_trips_for_both_timespecs(self):
        rng = random.Random(8)
        micros = [0, 1, 9, 10, 99, 100, 999, 1000, 1001, 5000, 51000, 100000, 500000, 999000, 999999]
        micros += [rng.randrange(10**6) for _ in range(200)]
        for us in micros:
            dt = datetime(rng.randrange(1, 9999), rng.randrange(1, 13), rng.randrange(1, 29),
                          rng.randrange(24), rng.randrange(60), rng.randrange(60), us)
            with self.subTest(dt=dt):
                self.assertEqual(isoparse(dt.isoformat(timespec='microseconds')), dt)
                self.assertEqual(isoparse(dt.isoformat(sep=' ', timespec='microseconds')), dt)
                self.assertEqual(isoparse(dt.isoformat()), dt)
                ms = dt.replace(microsecond=us // 1000 * 1000)
                self.assertEqual(isoparse(ms.isoformat(timespec='milliseconds')), ms)

    def test_every_fraction_length(self):
        cases = {'5': 500000, '05': 50000, '005': 5000, '123': 123000, '0001': 100, '12345': 123450,
                 '000000': 0, '000010': 10, '9': 900000, '999999': 999999}
        for digits, expected in cases.items():
            with self.subTest(digits=digits):
                self.assertEqual(isoparse(f'2001-02-03T04:05:06.{digits}'),
                                 datetime(2001, 2, 3, 4, 5, 6, expected))

    def test_extra_digits_are_truncated_not_rounded(self):
        cases = {'1234567': 123456, '9999999': 999999, '999999999': 999999, '000000999': 0,
                 '123456789123': 123456, '0000005': 0}
        for digits, expected in cases.items():
            with self.subTest(digits=digits):
                self.assertEqual(isoparse(f'2001-02-03T04:05:59.{digits}'),
                                 datetime(2001, 2, 3, 4, 5, 59, expected))

    def test_without_a_fraction(self):
        self.assertEqual(isoparse('2001-02-03T04:05:06'), datetime(2001, 2, 3, 4, 5, 6))
        self.assertEqual(isoparse('2001-02-03T04:05'), datetime(2001, 2, 3, 4, 5))
        self.assertEqual(isoparse('2001-02-03'), datetime(2001, 2, 3))


if __name__ == '__main__':
    unittest.main()
