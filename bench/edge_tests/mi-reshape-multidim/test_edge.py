"""Edge cases for multidimensional reshape: iterators, deeper inputs, list shapes and the int form."""
from itertools import count
import unittest

import more_itertools as mi


class ReshapeEdgeTests(unittest.TestCase):
    matrix = [(0, 1), (2, 3), (4, 5)]

    def test_int_shape_is_unchanged(self):
        self.assertEqual(list(mi.reshape(self.matrix, 2)), [(0, 1), (2, 3), (4, 5)])
        self.assertEqual(list(mi.reshape(self.matrix, 4)), [(0, 1, 2, 3), (4, 5)])

    def test_shape_may_be_any_iterable_of_ints(self):
        self.assertEqual(list(mi.reshape(self.matrix, [3, 2])), [(0, 1), (2, 3), (4, 5)])
        self.assertEqual(list(mi.reshape(self.matrix, iter((2, 3)))), [(0, 1, 2), (3, 4, 5)])

    def test_deeper_inputs_and_one_pass_iterators(self):
        cube = [[[1, 2], [3, 4]], [[5, 6], [7, 8]]]
        self.assertEqual(list(mi.reshape(cube, (4, 2))), [(1, 2), (3, 4), (5, 6), (7, 8)])
        self.assertEqual(list(mi.reshape(cube, (2, 2, 2))), [((1, 2), (3, 4)), ((5, 6), (7, 8))])
        nested = iter([iter([1, 2]), iter([3, 4])])
        self.assertEqual(list(mi.reshape(nested, (4,))), [1, 2, 3, 4])

    def test_scalars_include_strings_and_none(self):
        self.assertEqual(list(mi.reshape([['ab', 'cd'], ['ef', 'gh']], (4,))), ['ab', 'cd', 'ef', 'gh'])
        self.assertEqual(list(mi.reshape([[None, 1], [2, 3]], (2, 2))), [(None, 1), (2, 3)])

    def test_infinite_flat_input_is_consumed_lazily(self):
        self.assertEqual(list(mi.reshape(count(), (2, 3))), [(0, 1, 2), (3, 4, 5)])


if __name__ == '__main__':
    unittest.main()
