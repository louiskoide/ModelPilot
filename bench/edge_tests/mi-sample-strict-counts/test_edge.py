"""Edge cases for sample(..., counts=..., strict=True) when the population is too small."""
import unittest

import more_itertools as mi


class SampleStrictCountsEdgeTests(unittest.TestCase):
    def test_too_small_by_any_amount_raises(self):
        for k, counts in ((6, [1, 2, 2]), (7, [2, 2, 2]), (2, [0, 1, 0]), (4, [3]), (1, [0, 0])):
            with self.subTest(k=k, counts=counts):
                with self.assertRaises(ValueError):
                    mi.sample('abc'[:len(counts)], k, counts=counts, strict=True)

    def test_exactly_the_population_is_allowed(self):
        for _ in range(20):
            self.assertEqual(sorted(mi.sample('ab', 3, counts=[1, 2], strict=True)), ['a', 'b', 'b'])
        self.assertEqual(mi.sample('ab', 0, counts=[1, 2], strict=True), [])

    def test_without_strict_a_small_population_is_returned_whole(self):
        for _ in range(20):
            self.assertEqual(sorted(mi.sample('abcde', 10, counts=[1, 1, 1, 1, 1])), list('abcde'))
            self.assertEqual(sorted(mi.sample('ab', 5, counts=[2, 1])), ['a', 'a', 'b'])

    def test_the_other_modes_still_raise(self):
        with self.assertRaises(ValueError):
            mi.sample('abc', 4, strict=True)
        with self.assertRaises(ValueError):
            mi.sample('abc', 4, weights=[1, 1, 1], strict=True)


if __name__ == '__main__':
    unittest.main()
