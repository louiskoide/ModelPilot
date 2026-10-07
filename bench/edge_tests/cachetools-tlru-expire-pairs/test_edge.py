"""Edge cases for TLRUCache.expire() returning what it removed: staggered expiry, updates and deletions."""
import unittest

from cachetools import TLRUCache


class Timer:
    def __init__(self):
        self.time = 0

    def __call__(self):
        return self.time


def ttu(key, value, now):
    return now + key  # key k lives k time units


class TLRUExpirePairsEdgeTests(unittest.TestCase):
    def make(self):
        timer = Timer()
        return TLRUCache(maxsize=10, ttu=ttu, timer=timer), timer

    def test_staggered_expiry_returns_each_item_once(self):
        cache, timer = self.make()
        for k in (1, 2, 3, 5):
            cache[k] = str(k)
        self.assertEqual(set(cache.expire(0)), set())
        self.assertEqual(set(cache.expire(2)), {(1, '1'), (2, '2')})
        self.assertEqual(set(cache.expire(2)), set())
        timer.time = 4
        self.assertEqual(set(cache.expire()), {(3, '3')})  # default time: the cache's timer
        self.assertEqual(sorted(cache), [5])

    def test_updated_value_is_reported(self):
        cache, timer = self.make()
        cache[2] = 'old'
        cache[2] = 'new'
        self.assertEqual(list(cache.expire(10)), [(2, 'new')])

    def test_deleted_and_popped_items_are_not_reported(self):
        cache, timer = self.make()
        cache[1], cache[2], cache[3] = 'a', 'b', 'c'
        del cache[1]
        cache.pop(2)
        self.assertEqual(list(cache.expire(10)), [(3, 'c')])
        self.assertEqual(len(cache), 0)


if __name__ == '__main__':
    unittest.main()
