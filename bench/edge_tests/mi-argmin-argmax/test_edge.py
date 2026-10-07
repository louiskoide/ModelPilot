"""Edge cases for argmin/argmax: ties, generators, keys, single items and empty input."""
import random
import unittest

import more_itertools as mi


class ArgMinMaxEdgeTests(unittest.TestCase):
    def test_matches_first_occurrence_on_random_lists(self):
        rng = random.Random(3)
        for _ in range(100):
            data = [rng.randrange(5) for _ in range(rng.randrange(1, 12))]
            self.assertEqual(mi.argmin(data), data.index(min(data)))
            self.assertEqual(mi.argmax(data), data.index(max(data)))

    def test_generators_and_keys(self):
        self.assertEqual(mi.argmin(x * x for x in [-3, 2, -1, 1]), 2)
        words = ['pear', 'fig', 'banana', 'kiwi', 'plum']
        self.assertEqual(mi.argmin(words, key=len), 1)
        self.assertEqual(mi.argmax(words, key=len), 2)
        self.assertEqual(mi.argmax(iter(words), key=lambda w: w[1]), 4)  # second letters e, i, a, i, l

    def test_single_item(self):
        self.assertEqual((mi.argmin([7]), mi.argmax('z')), (0, 0))

    def test_empty_raises_value_error(self):
        for func in (mi.argmin, mi.argmax):
            with self.assertRaises(ValueError):
                func([])
            with self.assertRaises(ValueError):
                func(iter(()), key=abs)

    def test_exported(self):
        from more_itertools import argmax, argmin
        self.assertEqual((argmin([2, 1]), argmax([2, 1])), (1, 0))


if __name__ == '__main__':
    unittest.main()
