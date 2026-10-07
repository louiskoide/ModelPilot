"""Edge cases for bucket keys: misses of every kind, keys seen only through b[key], and 'in' against list(b)."""
import unittest

import more_itertools as mi


def tens(x):
    return 10 * (x // 10)


class BucketKeyEdgeTests(unittest.TestCase):
    def test_in_agrees_with_the_listed_keys_after_any_lookups(self):
        b = mi.bucket(['apple', 'avocado', 'banana', 'cherry'], key=lambda s: s[0])
        for probe in ('z', 'a', 'q', 'c', 'z'):
            self.assertEqual(probe in b, probe in list(b), probe)
        self.assertEqual(sorted(b), ['a', 'b', 'c'])

    def test_a_key_seen_only_through_its_bucket_is_listed(self):
        b = mi.bucket([10, 11, 20], key=tens)
        self.assertEqual(next(iter(b[10])), 10)  # handed straight to the caller
        self.assertEqual(sorted(b), [10, 20])

    def test_many_misses_leave_no_trace(self):
        b = mi.bucket([1, 2, 3], key=lambda x: x % 2)
        for k in range(2, 50):
            self.assertFalse(k in b)
            self.assertEqual(list(b[k]), [])
        self.assertEqual(sorted(b), [0, 1])

    def test_keys_the_validator_rejects_never_appear(self):
        b = mi.bucket([10, 20, 30], key=tens, validator=lambda k: k != 20)
        self.assertFalse(20 in b)
        self.assertEqual(list(b[20]), [])
        self.assertEqual(sorted(b), [10, 30])

    def test_buckets_still_yield_their_items_in_order(self):
        b = mi.bucket([10, 20, 11, 21, 12], key=tens)
        self.assertFalse(40 in b)
        self.assertEqual(list(b[10]), [10, 11, 12])
        self.assertEqual(list(b[20]), [20, 21])
        self.assertEqual(sorted(b), [10, 20])


if __name__ == '__main__':
    unittest.main()
