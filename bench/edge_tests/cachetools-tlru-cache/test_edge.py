"""Edge cases for TLRUCache: datetime timers, sizes, expiry before LRU, re-insertion and frozen time."""
from datetime import datetime, timedelta
import math
import unittest

from cachetools import TLRUCache


class Clock:
    def __init__(self, start=0, step=0):
        self.now, self.step = start, step

    def __call__(self):
        value = self.now
        if self.step:
            self.now += self.step
        return value


class TLRUEdgeTests(unittest.TestCase):
    def test_datetime_timer_and_timedelta_ttu(self):
        clock = Clock(datetime(2026, 1, 1))
        cache = TLRUCache(maxsize=10, ttu=lambda k, hours, now: now + timedelta(hours=hours), timer=clock)
        cache['short'], cache['long'] = 1, 5
        clock.now += timedelta(hours=1)
        self.assertNotIn('short', cache)
        self.assertEqual((cache['long'], len(cache)), (5, 1))

    def test_ttu_gets_key_value_and_current_time(self):
        seen = []
        cache = TLRUCache(maxsize=3, ttu=lambda k, v, t: seen.append((k, v, t)) or t + 10, timer=Clock(7))
        cache['a'] = 'b'
        self.assertEqual(seen, [('a', 'b', 7)])

    def test_getsizeof_counts_only_live_items(self):
        clock = Clock()
        cache = TLRUCache(maxsize=10, ttu=lambda k, v, t: t + 100, timer=clock, getsizeof=len)
        cache['a'], cache['b'] = 'xxxx', 'yyyyyy'
        self.assertEqual(cache.currsize, 10)
        cache['c'] = 'zz'
        self.assertNotIn('a', cache)
        self.assertEqual(set(cache), {'b', 'c'})
        with self.assertRaises(ValueError):
            cache['huge'] = 'x' * 11

    def test_expired_items_go_before_least_recently_used_ones(self):
        clock = Clock()
        cache = TLRUCache(maxsize=2, ttu=lambda k, v, t: t + v, timer=clock)
        cache['old'] = 100
        cache['brief'] = 2
        clock.now = 2  # 'brief' expired; 'old' is still the least recently used
        cache['new'] = 100
        self.assertEqual(set(cache), {'old', 'new'})
        clock.now = 0
        cache = TLRUCache(maxsize=3, ttu=lambda k, v, t: t + v, timer=clock)
        cache['brief'] = 2  # least recently used, but expired by the time popitem() runs
        cache['keep'] = 100
        clock.now = 2
        self.assertEqual(cache.popitem(), ('keep', 100))

    def test_reinsertion_recomputes_the_expiration(self):
        clock = Clock()
        cache = TLRUCache(maxsize=2, ttu=lambda k, v, t: t + 2, timer=clock)
        cache['k'] = 1
        clock.now = 1
        cache['k'] = 2
        clock.now = 2
        self.assertEqual(cache['k'], 2)
        clock.now = 3
        self.assertNotIn('k', cache)

    def test_expire_in_the_past_removes_nothing_and_time_can_be_frozen(self):
        clock = Clock(10)
        cache = TLRUCache(maxsize=3, ttu=lambda k, v, t: t + 1, timer=clock)
        cache['a'] = 1
        cache.expire(5)
        self.assertEqual(list(cache), ['a'])
        ticking = Clock(0, step=1)
        cache = TLRUCache(maxsize=3, ttu=lambda k, v, t: math.inf, timer=ticking)
        with cache.timer as now:
            self.assertEqual((cache.timer(), cache.timer()), (now, now))
        self.assertGreater(cache.timer(), now)


if __name__ == '__main__':
    unittest.main()
