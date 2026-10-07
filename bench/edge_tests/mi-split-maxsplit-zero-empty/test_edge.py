"""Edge cases for maxsplit=0 on empty input: every kind of empty iterable, non-empty input and split_at's rule."""
import unittest

import more_itertools as mi

FUNCS = [(mi.split_before, lambda x: x == 0), (mi.split_after, lambda x: x == 0), (mi.split_when, lambda a, b: a > b)]


class SplitMaxsplitZeroEdgeTests(unittest.TestCase):
    def test_every_empty_iterable_gives_nothing(self):
        for func, pred in FUNCS:
            for empty in ([], (), '', iter([]), (x for x in [])):
                with self.subTest(func=func.__name__, empty=type(empty).__name__):
                    self.assertEqual(list(func(empty, pred, maxsplit=0)), [])

    def test_non_empty_input_with_maxsplit_zero_is_one_group(self):
        for func, pred in FUNCS:
            with self.subTest(func=func.__name__):
                self.assertEqual(list(func([3, 0, 2, 0, 1], pred, maxsplit=0)), [[3, 0, 2, 0, 1]])
                self.assertEqual(list(func(iter([5]), pred, maxsplit=0)), [[5]])

    def test_empty_input_matches_other_maxsplits(self):
        for func, pred in FUNCS:
            for maxsplit in (-1, 0, 1, 5):
                with self.subTest(func=func.__name__, maxsplit=maxsplit):
                    self.assertEqual(list(func([], pred, maxsplit=maxsplit)), [])

    def test_split_at_still_follows_str_split(self):
        for maxsplit in (-1, 0, 1, 3):
            with self.subTest(maxsplit=maxsplit):
                self.assertEqual(list(mi.split_at('', lambda c: c == ',', maxsplit=maxsplit)), [[]])
                self.assertEqual([''.join(p) for p in mi.split_at('a,b', lambda c: c == ',', maxsplit=maxsplit)],
                                 'a,b'.split(',', maxsplit))


if __name__ == '__main__':
    unittest.main()
