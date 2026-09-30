"""Edge cases for cachedmethod's bound cache_key, beyond the default key the hidden tests check."""
import threading
import unittest

from cachetools import LRUCache, cachedmethod, keys


def tagged(self, *args, **kwargs):
    """A key function that uses self: a bound cache_key must pass the instance through."""
    return ('tagged', self.tag) + keys.hashkey(*args, **kwargs)


class Cached:
    def __init__(self, tag):
        self.tag = tag
        self.cache = LRUCache(maxsize=10)
        self.lock = threading.RLock()
        self.cond = threading.Condition()

    @cachedmethod(lambda self: self.cache)
    def plain(self, x, y=0):
        return x + y

    @cachedmethod(lambda self: self.cache, key=tagged)
    def custom(self, x, y=0):
        return x - y

    @cachedmethod(lambda self: self.cache, key=tagged, lock=lambda self: self.lock)
    def custom_lock(self, x, y=0):
        return x * 2

    @cachedmethod(lambda self: self.cache, key=tagged, condition=lambda self: self.cond)
    def custom_cond(self, x, y=0):
        return x * 3

    @cachedmethod(lambda self: self.cache, key=tagged, info=True)
    def custom_info(self, x, y=0):
        return x * 4

    @cachedmethod(lambda self: self.cache, info=True)
    def plain_info(self, x, y=0):
        return x * 5


class CacheKeyEdgeTests(unittest.TestCase):
    def test_keyword_arguments(self):
        obj = Cached('a')
        self.assertEqual(obj.plain.cache_key(1, y=2), keys.methodkey(obj, 1, y=2))
        self.assertEqual(obj.plain_info.cache_key(1, y=2), keys.methodkey(obj, 1, y=2))

    def test_a_key_function_that_uses_self(self):
        obj = Cached('a')
        for method in (obj.custom, obj.custom_lock, obj.custom_cond, obj.custom_info):
            self.assertEqual(method.cache_key(7, y=1), tagged(obj, 7, y=1))

    def test_the_key_is_the_one_the_decorator_stores(self):
        obj = Cached('a')
        for method in (obj.plain, obj.custom, obj.custom_lock, obj.custom_cond, obj.custom_info, obj.plain_info):
            obj.cache.clear()
            method(3, y=4)
            self.assertIn(method.cache_key(3, y=4), obj.cache)
            self.assertEqual(len(obj.cache), 1)

    def test_instances_get_their_own_keys_when_the_key_uses_self(self):
        a, b = Cached('a'), Cached('b')
        self.assertNotEqual(a.custom.cache_key(1), b.custom.cache_key(1))
        a.custom(1)
        self.assertIn(a.custom.cache_key(1), a.cache)
        self.assertNotIn(b.custom.cache_key(1), a.cache)

    def test_popping_by_key_forces_a_recompute(self):
        calls = []

        class Counting(Cached):
            @cachedmethod(lambda self: self.cache, lock=lambda self: self.lock)
            def value(self, x):
                calls.append(x)
                return x

        obj = Counting('a')
        obj.value(9)
        obj.value(9)
        with obj.value.cache_lock:
            obj.value.cache.pop(obj.value.cache_key(9))
        obj.value(9)
        self.assertEqual(calls, [9, 9])


if __name__ == '__main__':
    unittest.main()
