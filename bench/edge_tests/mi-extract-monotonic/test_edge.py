"""Edge cases for extract(monotonic=True): agreement with the eager path, laziness, repeats and late errors."""
import itertools
import random
import unittest

import more_itertools as mi


class Counting:
    def __init__(self, values):
        self.values, self.read = iter(values), 0

    def __iter__(self):
        return self

    def __next__(self):
        value = next(self.values)
        self.read += 1
        return value


class ExtractMonotonicEdgeTests(unittest.TestCase):
    def test_agrees_with_the_eager_path_on_sorted_indices(self):
        rng = random.Random(5)
        data = list(range(100, 140))
        for _ in range(50):
            indices = sorted(rng.randrange(40) for _ in range(rng.randrange(0, 8)))
            with self.subTest(indices=indices):
                self.assertEqual(list(mi.extract(data, indices, monotonic=True)), list(mi.extract(data, indices)))

    def test_reads_only_as_far_as_needed(self):
        source = Counting(range(1000))
        it = mi.extract(source, iter([3, 3, 10, 11]), monotonic=True)
        self.assertEqual(next(it), 3)
        self.assertEqual(source.read, 4)
        self.assertEqual(next(it), 3)
        self.assertEqual(source.read, 4)  # a repeated index needs nothing new
        self.assertEqual(next(it), 10)
        self.assertEqual(source.read, 11)
        self.assertEqual(list(it), [11])
        self.assertEqual(source.read, 12)

    def test_infinite_indices_and_iterable(self):
        squares = mi.extract(itertools.count(), (n * n for n in itertools.count()), monotonic=True)
        self.assertEqual(mi.take(5, squares), [0, 1, 4, 9, 16])

    def test_errors_come_when_the_index_is_reached(self):
        it = mi.extract('abcdef', [1, 4, 2], monotonic=True)
        self.assertEqual(next(it), 'b')
        self.assertEqual(next(it), 'e')
        with self.assertRaises(ValueError):
            next(it)
        it = mi.extract('abc', [0, 5], monotonic=True)
        self.assertEqual(next(it), 'a')
        with self.assertRaises(IndexError):
            next(it)
        with self.assertRaises(ValueError):
            list(mi.extract('abc', [1, -1], monotonic=True))

    def test_empty_indices_read_nothing(self):
        source = Counting('abc')
        self.assertEqual(list(mi.extract(source, [], monotonic=True)), [])
        self.assertEqual(source.read, 0)


if __name__ == '__main__':
    unittest.main()
