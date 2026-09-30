"""Edge cases for TTLCache.expire() returning what it removed: order, current values, sizes."""
from datetime import datetime, timedelta
import unittest

from cachetools import TTLCache


class Clock:
    def __init__(self, now=0):
        self.now = now

    def __call__(self):
        return self.now


class TtlExpireEdgeTests(unittest.TestCase):
    def test_several_items_come_back_in_expiry_order(self):
        clock = Clock()
        cache = TTLCache(maxsize=10, ttl=10, timer=clock)
        for key in ('c', 'a', 'b'):  # insertion order, not sorted order
            cache[key] = key.upper()
            clock.now += 1
        self.assertEqual(list(cache.expire(100)), [('c', 'C'), ('a', 'A'), ('b', 'B')])
        self.assertEqual(len(cache), 0)

    def test_an_updated_item_moves_back_and_returns_its_current_value(self):
        clock = Clock()
        cache = TTLCache(maxsize=10, ttl=10, timer=clock)
        cache[1] = 'old'
        clock.now = 1
        cache[2] = 'two'
        clock.now = 2
        cache[1] = 'new'  # now expires after 2
        self.assertEqual(list(cache.expire(11.5)), [(2, 'two')])
        self.assertEqual(list(cache.expire(12.5)), [(1, 'new')])

    def test_only_the_expired_prefix_is_returned(self):
        clock = Clock()
        cache = TTLCache(maxsize=10, ttl=10, timer=clock)
        for key in range(5):
            cache[key] = key * 10
            clock.now += 1
        self.assertEqual(list(cache.expire(12)), [(0, 0), (1, 10), (2, 20)])
        self.assertEqual(sorted(cache.items()), [(3, 30), (4, 40)])

    def test_nothing_expired_is_an_empty_iterable(self):
        cache = TTLCache(maxsize=10, ttl=10, timer=Clock())
        self.assertEqual(list(cache.expire()), [])
        cache[1] = 1
        self.assertEqual(list(cache.expire(5)), [])
        self.assertEqual(list(cache.expire()), [])

    def test_sizes_are_released(self):
        clock = Clock()
        cache = TTLCache(maxsize=10, ttl=10, timer=clock, getsizeof=len)
        cache['a'] = 'xxx'
        clock.now = 5
        cache['b'] = 'yy'
        self.assertEqual(list(cache.expire(12)), [('a', 'xxx')])
        self.assertEqual(cache.currsize, 2)

    def test_datetime_timer_with_several_items(self):
        start = datetime(2025, 1, 1)
        clock = Clock(start)
        cache = TTLCache(maxsize=10, ttl=timedelta(minutes=10), timer=clock)
        cache[1] = 'a'
        clock.now = start + timedelta(minutes=1)
        cache[2] = 'b'
        clock.now = start + timedelta(minutes=2)
        cache[3] = 'c'
        self.assertEqual(list(cache.expire(start + timedelta(minutes=11, seconds=30))), [(1, 'a'), (2, 'b')])
        self.assertEqual(list(cache.expire(start + timedelta(minutes=11, seconds=30))), [])


if __name__ == '__main__':
    unittest.main()
