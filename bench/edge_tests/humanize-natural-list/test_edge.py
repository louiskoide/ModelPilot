"""Edge cases for natural_list() on iterables: one-shot iterators of every length, other containers, type hints."""
import itertools
import unittest
from collections import deque

import humanize


class OnlyIter:
    """An iterable with nothing but __iter__: no len(), no indexing, no truth value of its own."""

    def __init__(self, items):
        self._items = list(items)

    def __iter__(self):
        return iter(self._items)


class NaturalListEdgeTests(unittest.TestCase):
    def test_one_shot_iterators_of_every_length(self):
        for n in range(0, 6):
            words = [f'w{i}' for i in range(n)]
            expected = humanize.natural_list(list(words))
            makers = {
                'generator': lambda: (w for w in words),
                'iter': lambda: iter(words),
                'map': lambda: map(str, words),
                'filter': lambda: filter(None, words),
                'chain': lambda: itertools.chain(words[:1], words[1:]),
                'only __iter__': lambda: OnlyIter(words),
            }
            for name, make in makers.items():
                with self.subTest(n=n, kind=name):
                    self.assertEqual(humanize.natural_list(make()), expected)

    def test_other_containers(self):
        cases = [
            ((), ''), ((1, 2), '1 and 2'), (deque(['a', 'b', 'c']), 'a, b and c'), (range(0), ''), (range(5, 6), '5'),
            ({'x': 1}.values(), '1'), ({'one': 1, 'two': 2}.items(), "('one', 1) and ('two', 2)"),
            ({'a': 1, 'b': 2, 'c': 3}, 'a, b and c'), (frozenset(['solo']), 'solo'), (iter([]), ''),
        ]
        for value, expected in cases:
            with self.subTest(value=repr(value)):
                self.assertEqual(humanize.natural_list(value), expected)

    def test_mixed_items_in_a_generator(self):
        self.assertEqual(humanize.natural_list(x for x in [1, 'two', 3.5, None]), '1, two, 3.5 and None')

    def test_type_hint_accepts_iterables(self):
        hint = humanize.natural_list.__annotations__['items']
        self.assertIn('Iterable', hint if isinstance(hint, str) else repr(hint))


if __name__ == '__main__':
    unittest.main()
