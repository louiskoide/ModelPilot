"""Edge cases for IndexedSet index bounds: every index against a list after random removals, pop, empty sets."""
import random
import unittest

from boltons.setutils import IndexedSet


def with_gaps(rng, n, removals):
    iset = IndexedSet(range(n))
    for x in rng.sample(range(n), removals):
        iset.discard(x)
    return iset


class IndexedSetBoundsEdgeTests(unittest.TestCase):
    def test_every_index_matches_a_list_after_removals(self):
        rng = random.Random(1)
        for trial in range(40):
            n = rng.randrange(1, 60)
            iset = with_gaps(rng, n, rng.randrange(0, n))
            ref = list(iset)
            for i in range(-len(ref) - 4, len(ref) + 4):
                with self.subTest(trial=trial, index=i, gaps=len(iset.dead_indices)):
                    if -len(ref) <= i < len(ref):
                        self.assertEqual(iset[i], ref[i])
                    else:
                        with self.assertRaises(IndexError):
                            iset[i]

    def test_pop_matches_a_list_after_removals(self):
        rng = random.Random(2)
        for trial in range(40):
            n = rng.randrange(2, 60)
            iset = with_gaps(rng, n, rng.randrange(0, n - 1))
            ref = list(iset)
            with self.subTest(trial=trial):  # the first failure ends the trial
                while ref:
                    i = rng.randrange(-len(ref) - 5, len(ref) + 5)
                    if -len(ref) <= i < len(ref):
                        self.assertEqual(iset.pop(i), ref.pop(i), (i, len(ref)))
                    else:
                        with self.assertRaises(IndexError, msg=(i, len(ref))):
                            iset.pop(i)
                    self.assertEqual(list(iset), ref)

    def test_out_of_range_pop_removes_nothing(self):
        iset = IndexedSet(range(20))
        for x in (3, 7, 11, 15):
            iset.discard(x)
        ref = list(iset)
        for i in (len(ref), len(ref) + 1, -len(ref) - 1, -2 * len(ref), -100, 100):
            with self.subTest(index=i):
                with self.assertRaises(IndexError):
                    iset.pop(i)
                self.assertEqual(list(iset), ref)
                self.assertEqual(len(iset), len(ref))

    def test_empty_sets(self):
        emptied = IndexedSet('abc')
        for x in 'abc':
            emptied.discard(x)
        for iset in (IndexedSet(), emptied):
            for i in (0, -1, 1):
                with self.subTest(set=repr(iset), index=i):
                    with self.assertRaises(IndexError):
                        iset[i]
                    with self.assertRaises(IndexError):
                        iset.pop(i)
            with self.assertRaises(IndexError):
                iset.pop()


if __name__ == '__main__':
    unittest.main()
