"""Edge cases for serialize: exactly-once delivery across threads, iterator protocol and exhaustion."""
from collections import Counter
import threading
import unittest

import more_itertools as mi


class SerializeEdgeTests(unittest.TestCase):
    def test_every_item_once_across_threads(self):
        def gen():
            yield from range(50000)
        shared = mi.serialize(gen())
        seen, lock = Counter(), threading.Lock()

        def consume():
            mine = Counter(shared)
            with lock:
                seen.update(mine)
        workers = [threading.Thread(target=consume) for _ in range(8)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        self.assertEqual(seen, Counter(range(50000)))

    def test_iterator_protocol(self):
        s = mi.serialize([1, 2])
        self.assertIs(iter(s), s)
        self.assertEqual((next(s), next(s)), (1, 2))
        with self.assertRaises(StopIteration):
            next(s)
        with self.assertRaises(StopIteration):
            next(s)

    def test_wraps_any_iterable(self):
        self.assertEqual(list(mi.serialize('abc')), ['a', 'b', 'c'])
        self.assertEqual(list(mi.serialize(x * x for x in range(4))), [0, 1, 4, 9])
        from more_itertools import serialize
        self.assertEqual(list(serialize({})), [])


if __name__ == '__main__':
    unittest.main()
