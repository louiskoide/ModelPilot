"""Edge cases for @cached(condition=...): real threads, independent keys, failures and the func decorators."""
import threading
import unittest
import warnings

import cachetools
import cachetools.func

WAIT = 10  # seconds; a deadlock fails the test instead of hanging it


class ConditionEdgeTests(unittest.TestCase):
    def test_concurrent_calls_for_one_key_compute_it_once(self):
        entered, release, calls, results = threading.Event(), threading.Event(), [], []

        @cachetools.cached(cachetools.LRUCache(10), condition=threading.Condition())
        def slow(x):
            calls.append(x)
            entered.set()
            release.wait(WAIT)
            return x * 2

        first = threading.Thread(target=lambda: results.append(slow(3)))
        first.start()
        self.assertTrue(entered.wait(WAIT))
        waiters = [threading.Thread(target=lambda: results.append(slow(3))) for _ in range(3)]
        for t in waiters:
            t.start()
        release.set()
        for t in [first] + waiters:
            t.join(WAIT)
            self.assertFalse(t.is_alive())
        self.assertEqual((calls, results), ([3], [6, 6, 6, 6]))

    def test_other_keys_are_not_blocked(self):
        entered, release = threading.Event(), threading.Event()

        lock = threading.RLock()

        @cachetools.cached({}, lock=lock, condition=threading.Condition(lock))
        def value(x):
            if x == 'slow':
                entered.set()
                release.wait(WAIT)
            return x

        slow = threading.Thread(target=value, args=('slow',))
        slow.start()
        self.assertTrue(entered.wait(WAIT))
        done = []
        fast = threading.Thread(target=lambda: done.append(value('fast')))
        fast.start()
        fast.join(WAIT)
        self.assertEqual(done, ['fast'])  # finished while 'slow' was still being computed
        release.set()
        slow.join(WAIT)
        self.assertFalse(slow.is_alive())

    def test_a_failed_computation_releases_the_key(self):
        attempts = []

        @cachetools.cached(cachetools.LRUCache(10), condition=threading.Condition(), info=True)
        def flaky(x):
            attempts.append(x)
            if len(attempts) == 1:
                raise RuntimeError('first call fails')
            return x

        with self.assertRaises(RuntimeError):
            flaky(1)
        result = []
        t = threading.Thread(target=lambda: result.append(flaky(1)))
        t.start()
        t.join(WAIT)
        self.assertEqual((result, attempts), ([1], [1, 1]))
        self.assertEqual(flaky.cache_info()[:2], (0, 2))

    def test_keyword_info_does_not_warn(self):
        with warnings.catch_warnings():
            warnings.simplefilter('error')
            wrapper = cachetools.cached(cachetools.LRUCache(2), lock=threading.Lock(), info=True)(lambda x: x)
        self.assertEqual(wrapper(1), 1)
        self.assertEqual(wrapper.cache_info(), (0, 1, 2, 1))

    def test_func_decorators_still_prevent_stampedes(self):
        entered, release, calls = threading.Event(), threading.Event(), []

        @cachetools.func.lru_cache(maxsize=8)
        def slow(x):
            calls.append(x)
            entered.set()
            release.wait(WAIT)
            return x

        threads = [threading.Thread(target=slow, args=(5,))]
        threads[0].start()
        self.assertTrue(entered.wait(WAIT))
        threads.append(threading.Thread(target=slow, args=(5,)))
        threads[1].start()
        release.set()
        for t in threads:
            t.join(WAIT)
        self.assertEqual(calls, [5])


if __name__ == '__main__':
    unittest.main()
