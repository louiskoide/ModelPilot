"""Edge cases for seekable indexing: agreement with elements() after seeks, maxlen windows and errors."""
import unittest

import more_itertools as mi


class SeekableGetitemEdgeTests(unittest.TestCase):
    def assert_matches_elements(self, s):
        cached = list(s.elements())
        for i in range(-len(cached), len(cached)):
            self.assertEqual(s[i], cached[i], i)
        for i in (len(cached), -len(cached) - 1):
            with self.assertRaises(IndexError):
                s[i]

    def test_matches_elements_through_seeks(self):
        s = mi.seekable(iter('abcdefg'))
        self.assert_matches_elements(s)
        mi.take(4, s)
        self.assert_matches_elements(s)
        s.seek(1)
        self.assert_matches_elements(s)
        self.assertEqual((s[0], s[-1]), ('a', 'd'))  # seeking back doesn't change what was produced
        mi.take(5, s)
        self.assert_matches_elements(s)
        s.relative_seek(-3)
        self.assert_matches_elements(s)

    def test_maxlen_window(self):
        s = mi.seekable(range(100), maxlen=3)
        for count in (1, 1, 1, 7):  # 1, 2, 3 and then 10 items taken
            mi.take(count, s)
            self.assert_matches_elements(s)
        self.assertEqual((s[0], s[-1]), (7, 9))

    def test_indexing_does_not_advance(self):
        s = mi.seekable(range(5))
        next(s)
        next(s)
        self.assertEqual(s[-1], 1)
        self.assertEqual(next(s), 2)
        self.assertEqual(s[-1], 2)


if __name__ == '__main__':
    unittest.main()
