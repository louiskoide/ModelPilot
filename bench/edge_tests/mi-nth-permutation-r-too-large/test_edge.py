"""Edge cases for nth_permutation's r: too large, zero, None, negative, and agreement with itertools."""
import itertools
import unittest

import more_itertools as mi


class NthPermutationEdgeTests(unittest.TestCase):
    def test_r_larger_than_the_pool_is_an_index_error(self):
        for r in (4, 5, 10):
            for index in (0, -1, 3):
                with self.assertRaises(IndexError):
                    mi.nth_permutation('abc', r, index)
        with self.assertRaises(IndexError):
            mi.nth_permutation([], 1, 0)

    def test_negative_r_is_a_value_error(self):
        for r in (-1, -3):
            with self.assertRaises(ValueError):
                mi.nth_permutation('abc', r, 0)

    def test_agrees_with_itertools(self):
        pool = 'abcd'
        for r in (None, 0, 1, 2, 4):
            perms = list(itertools.permutations(pool, r))
            for index in range(-len(perms), len(perms)):
                self.assertEqual(mi.nth_permutation(pool, r, index), perms[index])
            with self.assertRaises(IndexError):
                mi.nth_permutation(pool, r, len(perms))


if __name__ == '__main__':
    unittest.main()
