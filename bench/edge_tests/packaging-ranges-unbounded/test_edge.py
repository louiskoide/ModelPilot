"""Edge cases for unbounded range ends: every spelling, keys and sorting beside bounded ends, bounded ends unchanged."""
import unittest

from packaging._ranges import NEG_INF, POS_INF, LowerBound, UpperBound
from packaging.version import Version


class UnboundedEndsEdgeTests(unittest.TestCase):
    def test_every_spelling_is_one_value(self):
        for cls, inf in ((LowerBound, NEG_INF), (UpperBound, POS_INF)):
            for inclusive in (True, False, 1, 0):
                with self.subTest(cls=cls.__name__, inclusive=inclusive):
                    for bound in (cls(None, inclusive), cls(version=None, inclusive=inclusive)):
                        self.assertFalse(bound.inclusive)
                        self.assertEqual(bound, inf)
                        self.assertEqual(hash(bound), hash(inf))
                        self.assertEqual({bound: 'v'}[inf], 'v')

    def test_sorting_beside_bounded_ends(self):
        v1, v2 = Version('1.0'), Version('2.0')
        lowers = [LowerBound(v2, False), LowerBound(None, True), LowerBound(v1, True), NEG_INF, LowerBound(v1, False)]
        ordered = sorted(lowers)
        self.assertEqual(ordered[:2], [NEG_INF, NEG_INF])
        self.assertEqual(ordered[2:], [LowerBound(v1, True), LowerBound(v1, False), LowerBound(v2, False)])
        self.assertEqual(len(set(lowers)), 4)
        uppers = [UpperBound(None, True), UpperBound(v2, True), POS_INF, UpperBound(v1, False), UpperBound(v2, False)]
        ordered = sorted(uppers)
        self.assertEqual(ordered[-2:], [POS_INF, POS_INF])
        self.assertEqual(ordered[:3], [UpperBound(v1, False), UpperBound(v2, False), UpperBound(v2, True)])
        self.assertEqual(len(set(uppers)), 4)
        self.assertEqual(min(LowerBound(None, True), LowerBound(Version('0'), True)), NEG_INF)
        self.assertEqual(max(UpperBound(None, True), UpperBound(Version('999'), True)), POS_INF)

    def test_comparisons_between_the_two_spellings(self):
        for cls in (LowerBound, UpperBound):
            a, b = cls(None, True), cls(None, False)
            with self.subTest(cls=cls.__name__):
                self.assertEqual((a < b, a > b, a <= b, a >= b, a == b, a != b), (False, False, True, True, True, False))

    def test_bounded_ends_keep_their_flag(self):
        v = Version('1.0')
        for cls in (LowerBound, UpperBound):
            with self.subTest(cls=cls.__name__):
                self.assertTrue(cls(v, True).inclusive)
                self.assertFalse(cls(v, False).inclusive)
                self.assertNotEqual(cls(v, True), cls(v, False))


if __name__ == '__main__':
    unittest.main()
