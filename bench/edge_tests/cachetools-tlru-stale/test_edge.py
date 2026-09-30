"""Edge cases for assigning an already-expired value in TLRUCache, beyond a single key without sizes."""
import unittest

from cachetools import TLRUCache


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now


def ttu(_key, value, now):
    return now + value[0]  # value = (time to use, size)


def make(maxsize=10, clock=None):
    return TLRUCache(maxsize=maxsize, ttu=ttu, timer=clock or Clock(), getsizeof=lambda value: value[1])


class TlruStaleEdgeTests(unittest.TestCase):
    def test_other_keys_and_sizes_are_kept_exact(self):
        cache = make()
        cache[1] = (5, 3)
        cache[2] = (5, 4)
        cache[1] = (0, 2)  # already expired: key 1 goes
        self.assertNotIn(1, cache)
        self.assertEqual((list(cache), cache[2], len(cache), cache.currsize), ([2], (5, 4), 1, 4))

    def test_an_expired_value_for_a_new_key_is_not_stored(self):
        cache = make()
        cache[2] = (5, 4)
        cache[1] = (0, 2)
        self.assertEqual((list(cache), len(cache), cache.currsize), ([2], 1, 4))
        self.assertIsNone(cache.get(1))

    def test_a_later_value_for_the_key_survives_the_old_values_expiry(self):
        clock = Clock()
        cache = make(clock=clock)
        cache[1] = (5, 1)   # would have expired at 5
        cache[1] = (0, 1)   # removed
        cache[1] = (10, 1)  # stored again, expires at 10
        clock.now = 6
        cache.expire()
        self.assertEqual((cache[1], len(cache), cache.currsize), ((10, 1), 1, 1))
        clock.now = 11
        cache.expire()
        self.assertEqual((len(cache), cache.currsize), (0, 0))
        self.assertNotIn(1, cache)

    def test_the_cache_keeps_working_after_the_removal(self):
        clock = Clock()
        cache = make(maxsize=3, clock=clock)
        cache[1] = (5, 1)
        cache[2] = (5, 1)
        cache[1] = (-1, 1)  # expired in the past
        cache[3] = (5, 1)
        cache[4] = (5, 1)  # fits without evicting: key 1's size was released
        self.assertEqual((sorted(cache), cache.currsize), ([2, 3, 4], 3))
        cache[5] = (5, 1)  # now one entry has to go
        self.assertEqual((len(cache), cache.currsize), (3, 3))
        self.assertIn(5, cache)


if __name__ == '__main__':
    unittest.main()
