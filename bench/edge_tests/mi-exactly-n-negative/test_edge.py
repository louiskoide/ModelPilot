"""Edge cases for exactly_n: negative n everywhere, n = 0, predicates and early stopping."""
import itertools
import unittest

import more_itertools as mi


class ExactlyNEdgeTests(unittest.TestCase):
    def test_negative_n_is_always_false(self):
        for iterable in ([], [True], [False], [True] * 5, iter([1, 0, 1])):
            for n in (-1, -2, -100):
                self.assertIs(mi.exactly_n(iterable, n), False)
        self.assertIs(mi.exactly_n(itertools.repeat(True), -3), False)

    def test_zero(self):
        self.assertTrue(mi.exactly_n([], 0))
        self.assertTrue(mi.exactly_n([0, '', None], 0))
        self.assertFalse(mi.exactly_n([0, 1], 0))

    def test_counts_with_predicates(self):
        data = list(range(20))
        for n in range(0, 12):
            self.assertEqual(mi.exactly_n(data, n, lambda x: x < 10), n == 10, n)
        self.assertTrue(mi.exactly_n('aAbB', 2, str.isupper))

    def test_stops_early_on_infinite_input(self):
        self.assertFalse(mi.exactly_n(itertools.count(1), 5))
        self.assertTrue(mi.exactly_n(itertools.chain([1, 1], itertools.repeat(0, 10)), 2))


if __name__ == '__main__':
    unittest.main()
