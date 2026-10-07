"""Edge cases for deprecating @cached(None): one warning at decoration, where it points, and real caches."""
import unittest
import warnings

import cachetools


def func(*args, **kwargs):
    return args + tuple(kwargs.items())


class CachedNoneDeprecatedEdgeTests(unittest.TestCase):
    def test_one_warning_at_decoration_only(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter('always')
            wrapper = cachetools.cached(None)(func)
            self.assertEqual(len(w), 1)
            self.assertIs(w[0].category, DeprecationWarning)
            self.assertIn('cache=None', str(w[0].message))
            self.assertEqual(w[0].filename, __file__)  # attributed to the decorating code
            wrapper(1)
            wrapper(1)
            self.assertEqual(len(w), 1)

    def test_info_variant_warns_and_counts_misses(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter('always')
            wrapper = cachetools.cached(None, info=True)(func)
        self.assertEqual([x.category for x in w], [DeprecationWarning])
        wrapper(0)
        wrapper(0)
        self.assertEqual(wrapper.cache_info(), (0, 2, 0, 0))
        self.assertIsNone(wrapper.cache)

    def test_real_caches_do_not_warn(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter('always')
            for cache in ({}, cachetools.LRUCache(maxsize=2)):
                wrapper = cachetools.cached(cache, info=True)(func)
                wrapper(1)
                wrapper(1)
                self.assertEqual(wrapper.cache_info().hits, 1)
        self.assertEqual(w, [])

    def test_still_usable_as_a_plain_decorator(self):
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            wrapper = cachetools.cached(cache=None)(func)
        self.assertEqual(wrapper(1, foo='bar'), (1, ('foo', 'bar')))
        wrapper.cache_clear()
        self.assertIs(wrapper.__wrapped__, func)


if __name__ == '__main__':
    unittest.main()
