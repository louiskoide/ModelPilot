"""Edge cases for growing an existing key with getsizeof, beyond grow-that-fits, same size and shrink."""
import unittest

from cachetools import Cache, FIFOCache, LRUCache, TTLCache


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now


def caches(maxsize=10):
    size = dict(maxsize=maxsize, getsizeof=lambda x: x)
    return [Cache(**size), FIFOCache(**size), LRUCache(**size), TTLCache(ttl=100, timer=Clock(), **size)]


class SetitemEvictEdgeTests(unittest.TestCase):
    def assertConsistent(self, cache):
        self.assertEqual(cache.currsize, sum(cache[k] for k in list(cache)))
        self.assertLessEqual(cache.currsize, cache.maxsize)

    def test_growth_that_needs_room_evicts_only_what_the_difference_needs(self):
        for cache in caches():
            with self.subTest(type(cache).__name__):
                cache[2] = 2  # the oldest and least recently used entry
                cache[3] = 3
                cache[4] = 4  # currsize 9
                cache[4] = 6  # needs 2 more: evicting 2 is enough, 3 stays
                self.assertEqual(set(cache), {3, 4})
                self.assertEqual((cache[3], cache[4], cache.currsize), (3, 6, 9))
                self.assertConsistent(cache)

    def test_the_key_being_updated_may_be_evicted_and_is_still_stored(self):
        for cache in caches():
            with self.subTest(type(cache).__name__):
                cache[4] = 4  # the oldest and least recently used entry
                cache[3] = 3
                cache[4] = 8  # evicting 4 itself frees 4, but 8 still needs 3 evicted too
                self.assertEqual(list(cache), [4])
                self.assertEqual((cache[4], cache.currsize, len(cache)), (8, 8, 1))
                self.assertConsistent(cache)

    def test_growing_to_the_whole_cache_evicts_everything_else(self):
        for cache in caches():
            with self.subTest(type(cache).__name__):
                for key in (1, 2, 3):
                    cache[key] = key
                cache[2] = 10
                self.assertEqual((list(cache), cache[2], cache.currsize), ([2], 10, 10))

    def test_a_value_too_large_still_raises_and_keeps_the_old_value(self):
        for cache in caches():
            with self.subTest(type(cache).__name__):
                cache[3] = 3
                cache[4] = 4
                with self.assertRaises(ValueError):
                    cache[4] = 11
                self.assertEqual((cache[3], cache[4], cache.currsize), (3, 4, 7))

    def test_repeated_growth_and_shrinking_keeps_currsize_exact(self):
        for cache in caches(maxsize=20):
            with self.subTest(type(cache).__name__):
                for step, (key, value) in enumerate([(1, 5), (2, 5), (1, 9), (3, 4), (2, 1), (1, 12), (3, 7)]):
                    cache[key] = value
                    self.assertEqual(cache[key], value, step)
                    self.assertConsistent(cache)


if __name__ == '__main__':
    unittest.main()
