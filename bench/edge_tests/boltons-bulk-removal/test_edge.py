"""Edge cases for BarrelList bounds and IndexedSet bulk removal: several sublists, both ends, gaps before a bulk update."""
import random
import unittest
from unittest import mock

from boltons.listutils import BarrelList
from boltons.setutils import IndexedSet


def several_sublists(n=40000):
    bl = BarrelList(range(n))
    bl.insert(0, -1)  # an insert balances the sublist, splitting it
    bl.pop(0)
    assert len(bl.lists) > 2, bl.lists
    return bl


def no_empty_sublists(bl):
    return len(bl.lists) == 1 or all(bl.lists)


class BarrelListEdgeTests(unittest.TestCase):
    def test_indexing_at_and_beyond_both_ends(self):
        bl = several_sublists()
        ref = list(bl)
        n = len(ref)
        for i in (n, n + 1, n + 10**6, -n - 1, -n - 10**6):
            with self.subTest(index=i):
                with self.assertRaises(IndexError):
                    bl[i]
        for i in (0, n - 1, -1, -n):
            with self.subTest(index=i):
                self.assertEqual(bl[i], ref[i])
        edge, total = [], 0
        for sub in bl.lists[:-1]:
            total += len(sub)
            edge += [total - 1, total, -total, -total - 1]
        for i in edge:
            with self.subTest(index=i):
                self.assertEqual(bl[i], ref[i])

    def test_insert_clamps_like_list(self):
        bl = several_sublists()
        ref = list(bl)
        for k, offset in enumerate((0, 1, 7, 10**6)):
            for index in (len(ref) + offset, -len(ref) - offset):
                ref.insert(index, ('x', k, index))
                bl.insert(index, ('x', k, index))
                self.assertGreater(len(bl.lists), 1)
                self.assertEqual(len(bl), len(ref))
        self.assertEqual(list(bl), ref)
        self.assertEqual(bl[0], ref[0])
        self.assertEqual(bl[-1], ref[-1])

    def test_pop_at_and_beyond_both_ends(self):
        bl = several_sublists()
        ref = list(bl)
        for i in (len(ref), len(ref) + 3, -len(ref) - 1, -len(ref) - 3):
            with self.subTest(index=i):
                with self.assertRaises(IndexError):
                    bl.pop(i)
        self.assertEqual(list(bl), ref)
        self.assertEqual(bl.pop(-len(bl)), ref.pop(-len(ref)))
        self.assertEqual(bl.pop(len(bl) - 1), ref.pop(len(ref) - 1))
        self.assertEqual(bl.pop(0), ref.pop(0))
        self.assertEqual(list(bl), ref)

    def test_draining_sublists_leaves_none_empty(self):
        bl = several_sublists()
        ref = list(bl)
        # Empty the last two sublists by indexed pops at the end, then a middle one from its start.
        for _ in range(len(bl.lists[-1]) + len(bl.lists[-2])):
            self.assertEqual(bl.pop(len(bl) - 1), ref.pop())
            self.assertTrue(no_empty_sublists(bl))
        self.assertEqual(bl.pop(), ref.pop())
        self.assertEqual(bl.pop(-1), ref.pop(-1))
        start = len(bl.lists[0])
        for _ in range(len(bl.lists[1])):
            self.assertEqual(bl.pop(start), ref.pop(start))
            self.assertTrue(no_empty_sublists(bl))
        self.assertEqual(list(bl), ref)
        self.assertEqual(bl[len(bl) - 1], ref[-1])
        with self.assertRaises(IndexError):
            bl[len(bl)]

    def test_random_operations_match_list(self):
        rng = random.Random(11)
        bl = several_sublists()
        ref = list(bl)
        for step in range(3000):
            op = rng.random()
            n = len(ref)
            if op < 0.3:
                i = rng.randrange(-n - 3, n + 3)
                if -n <= i < n:
                    self.assertEqual(bl.pop(i), ref.pop(i))
                else:
                    with self.assertRaises(IndexError):
                        bl.pop(i)
            elif op < 0.4:
                self.assertEqual(bl.pop(), ref.pop())
            elif op < 0.7:
                i = rng.randrange(-n - 3, n + 3)
                ref.insert(i, step)
                bl.insert(i, step)
            else:
                i = rng.randrange(-n - 3, n + 3)
                if -n <= i < n:
                    self.assertEqual(bl[i], ref[i])
                else:
                    with self.assertRaises(IndexError):
                        bl[i]
            self.assertEqual(len(bl), len(ref))
            self.assertTrue(no_empty_sublists(bl))
        self.assertEqual(list(bl), ref)


class IndexedSetBulkEdgeTests(unittest.TestCase):
    def with_gaps(self, n):
        # Scattered discards leave dead slots below the compaction thresholds.
        iset = IndexedSet(range(n))
        for x in range(5, n, n // 6):
            iset.discard(x)
        self.assertTrue(iset.dead_indices)
        return iset

    def check_like_list(self, iset, expected):
        self.assertEqual(list(iset), expected)
        self.assertEqual(len(iset), len(expected))
        for i in (0, 1, len(expected) // 2, -1, -len(expected)):
            self.assertEqual(iset[i], expected[i])
        for x in expected[::97]:
            self.assertEqual(iset.index(x), expected.index(x))
        self.assertEqual(list(iset[10:20]), expected[10:20])
        with self.assertRaises(IndexError):
            iset[len(expected)]

    def test_bulk_difference_update_after_gaps(self):
        rng = random.Random(3)
        iset = self.with_gaps(5000)
        before = list(iset)
        remove = set(rng.sample(range(6000), 3000))
        iset.difference_update(remove)
        expected = [x for x in before if x not in remove]
        self.check_like_list(iset, expected)
        iset.add('new')
        iset.discard(expected[1])
        expected = [x for x in expected if x != expected[1]] + ['new']
        self.check_like_list(iset, expected)
        self.assertEqual(iset.pop(), 'new')

    def test_bulk_intersection_update_after_gaps(self):
        rng = random.Random(4)
        iset = self.with_gaps(5000)
        before = list(iset)
        keep = set(rng.sample(range(5000), 1500))
        iset.intersection_update(keep)
        expected = [x for x in before if x in keep]
        self.check_like_list(iset, expected)
        iset.add(-1)
        self.assertEqual(iset[-1], -1)
        self.assertEqual(iset.index(-1), len(expected))

    def test_bulk_removal_of_everything(self):
        iset = IndexedSet(range(2000))
        iset.difference_update(set(range(2000)))
        self.assertEqual(len(iset), 0)
        self.assertEqual(list(iset), [])
        with self.assertRaises(IndexError):
            iset[0]
        iset.add('a')
        self.assertEqual(list(iset), ['a'])
        other = IndexedSet(range(2000))
        other.intersection_update(set())
        self.assertEqual(list(other), [])

    def test_bulk_removal_does_not_compact_per_item(self):
        rng = random.Random(5)
        for method, arg in (('difference_update', set(range(0, 20000, 2))),
                            ('intersection_update', set(rng.sample(range(20000), 4000)))):
            with self.subTest(method=method):
                iset = IndexedSet(range(20000))
                before = list(iset)
                with mock.patch.object(IndexedSet, '_compact', autospec=True,
                                       side_effect=IndexedSet._compact) as compact:
                    getattr(iset, method)(arg)
                self.assertLessEqual(compact.call_count, 1)
                keep = (lambda x: x not in arg) if method == 'difference_update' else (lambda x: x in arg)
                self.assertEqual(list(iset), [x for x in before if keep(x)])


if __name__ == '__main__':
    unittest.main()
