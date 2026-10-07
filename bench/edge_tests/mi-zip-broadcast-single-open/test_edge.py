"""Edge cases for zip_broadcast opening each input once: counted opens, positions, scalar handling and strict."""
import unittest

import more_itertools as mi


class Counted:
    def __init__(self, values):
        self.values, self.opens = values, 0

    def __iter__(self):
        self.opens += 1
        return iter(self.values)


class ZipBroadcastOpenEdgeTests(unittest.TestCase):
    def test_every_iterable_argument_is_opened_once(self):
        for strict in (False, True):
            a, b = Counted([1, 2, 3]), Counted('xyz')
            out = list(mi.zip_broadcast(a, 0, b, strict=strict, scalar_types=None))
            self.assertEqual(out, [(1, 0, 'x'), (2, 0, 'y'), (3, 0, 'z')])
            self.assertEqual((a.opens, b.opens), (1, 1), strict)

    def test_generators_and_iterators_work(self):
        out = list(mi.zip_broadcast((i * i for i in range(3)), 'k', iter([7, 8, 9])))
        self.assertEqual(out, [(0, 'k', 7), (1, 'k', 8), (4, 'k', 9)])

    def test_scalars_are_still_repeated(self):
        self.assertEqual(list(mi.zip_broadcast('ab', [1, 2], 5)), [('ab', 1, 5), ('ab', 2, 5)])
        self.assertEqual(list(mi.zip_broadcast(b'x', [1])), [(b'x', 1)])
        self.assertEqual(list(mi.zip_broadcast(1, 2)), [(1, 2)])

    def test_scalar_types_none_iterates_strings(self):
        self.assertEqual(list(mi.zip_broadcast('ab', [1, 2], scalar_types=None)), [('a', 1), ('b', 2)])

    def test_strict_still_rejects_unequal_lengths(self):
        source = Counted([1, 2])
        with self.assertRaises(ValueError):
            list(mi.zip_broadcast(source, [1, 2, 3], strict=True))
        self.assertEqual(source.opens, 1)
        self.assertEqual(list(mi.zip_broadcast([1, 2], [1, 2, 3])), [(1, 1), (2, 2)])


if __name__ == '__main__':
    unittest.main()
