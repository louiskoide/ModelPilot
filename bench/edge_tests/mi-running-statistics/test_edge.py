"""Edge cases for running windows and statistics: random windows, exact types, laziness and exports."""
import dataclasses
from decimal import Decimal
from fractions import Fraction
from itertools import count, islice
import random
from statistics import mean, median
import unittest

import more_itertools as mi


def windows(data, maxlen):
    for j in range(1, len(data) + 1):
        yield data[max(j - maxlen, 0):j]


class RunningStatisticsEdgeTests(unittest.TestCase):
    def test_min_and_max_match_brute_force_on_floats(self):
        rng = random.Random(20260929)
        data = [rng.choice([-2.5, -1.0, 0.0, 0.5, 3.25, 7.0]) for _ in range(300)]
        for maxlen in (1, 2, 3, 7, 50, 1000):
            with self.subTest(maxlen=maxlen):
                self.assertEqual(list(mi.running_min(data, maxlen=maxlen)), [min(w) for w in windows(data, maxlen)])
                self.assertEqual(list(mi.running_max(data, maxlen=maxlen)), [max(w) for w in windows(data, maxlen)])

    def test_statistics_are_exact_for_fractions(self):
        rng = random.Random(7)
        data = [Fraction(rng.randint(-20, 20), rng.randint(1, 6)) for _ in range(60)]
        for maxlen in (None, 1, 4, 9):
            size = len(data) if maxlen is None else maxlen
            expected = [mi.Stats(size=len(w), minimum=min(w), median=median(w), maximum=max(w), mean=mean(w))
                        for w in windows(data, size)]
            with self.subTest(maxlen=maxlen):
                self.assertEqual(list(mi.running_statistics(data, maxlen=maxlen)), expected)

    def test_windowed_mean_of_complex_and_decimal(self):
        self.assertEqual(list(mi.running_mean([1 + 1j, 3 + 3j, 5 + 5j], maxlen=2)), [1 + 1j, 2 + 2j, 4 + 4j])
        self.assertEqual(list(mi.running_mean([Decimal('1'), Decimal('2'), Decimal('4')], maxlen=2)),
                         [Decimal('1'), Decimal('1.5'), Decimal('3')])

    def test_lazy_on_infinite_input(self):
        first = list(islice(mi.running_statistics(count(), maxlen=3), 5))
        self.assertEqual([s.size for s in first], [1, 2, 3, 3, 3])
        self.assertEqual([s.maximum for s in first], [0, 1, 2, 3, 4])
        self.assertEqual(list(islice(mi.running_min(count(10), maxlen=2), 3)), [10, 10, 11])

    def test_bad_windows_and_exports(self):
        for func in (mi.running_min, mi.running_max, mi.running_mean):
            for maxlen in (0, -1):
                with self.subTest(func=func.__name__, maxlen=maxlen), self.assertRaises(ValueError):
                    list(func([1, 2, 3], maxlen=maxlen))
        with self.assertRaises(ValueError):
            mi.running_statistics([1, 2], maxlen=-3)
        self.assertEqual([f.name for f in dataclasses.fields(mi.Stats)], ['size', 'minimum', 'median', 'maximum', 'mean'])
        exported = set(mi.more.__all__) | set(mi.recipes.__all__)
        self.assertLessEqual({'Stats', 'running_min', 'running_max', 'running_statistics'}, exported)


if __name__ == '__main__':
    unittest.main()
