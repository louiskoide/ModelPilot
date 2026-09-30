"""Edge cases for ',' and '_' grouping in integer fields: search, named fields, several fields, no separators."""
import unittest

import parse


class DecimalGroupingEdgeTests(unittest.TestCase):
    def test_numbers_without_separators(self):
        self.assertEqual(parse.parse('{:,d}', '999')[0], 999)
        self.assertEqual(parse.parse('{:_d}', '0')[0], 0)
        self.assertEqual(parse.parse('{:,d}', '-7')[0], -7)

    def test_named_and_several_fields(self):
        result = parse.parse('{a:,d} and {b:_d}', '12,345 and 6_789_000')
        self.assertEqual((result['a'], result['b']), (12345, 6789000))
        self.assertIsInstance(result['a'], int)

    def test_search_and_findall(self):
        self.assertEqual(parse.search('total {:,d} items', 'the total 1,234,567 items here')[0], 1234567)
        found = [r[0] for r in parse.findall('<{:,d}>', '<1,000> <22> <3,333,333>')]
        self.assertEqual(found, [1000, 22, 3333333])

    def test_round_trip_with_format(self):
        for value in (0, 7, 1000, -1000, 123456789, -98765432):
            for spec in (',d', '_d'):
                text = format(value, spec)
                self.assertEqual(parse.parse('x{:%s}y' % spec, 'x%sy' % text)[0], value, (spec, text))

    def test_plain_d_is_unchanged(self):
        self.assertEqual(parse.parse('{:d}', '1000')[0], 1000)
        self.assertIsNone(parse.parse('{:d}', '1,000'))


if __name__ == '__main__':
    unittest.main()
