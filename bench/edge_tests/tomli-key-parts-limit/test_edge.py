"""Edge cases for the key-part limit: headers, inline tables, the exact boundary and normal documents."""
import sys
import unittest

import tomli


class KeyPartsLimitEdgeTests(unittest.TestCase):
    def test_boundary(self):
        limit = sys.getrecursionlimit()
        doc = tomli.loads('a.' * (limit - 1) + 'a = 1')  # exactly limit parts
        depth, node = 0, doc
        while isinstance(node, dict):
            node, depth = node['a'], depth + 1
        self.assertEqual((depth, node), (limit, 1))
        with self.assertRaisesRegex(RecursionError, r'TOML key has more than the allowed [0-9]+ parts'):
            tomli.loads('a.' * limit + 'a = 1')

    def test_table_headers_and_inline_tables(self):
        limit = sys.getrecursionlimit()
        with self.assertRaises(RecursionError):
            tomli.loads('[' + 'b.' * (limit + 5) + 'b]\nx = 1')
        with self.assertRaises(RecursionError):
            tomli.loads('t = {' + 'c.' * (limit + 5) + 'c = 1}')
        self.assertEqual(tomli.loads('[x.y]\nz = 1\n[[arr.of]]\nq = 2')['x']['y']['z'], 1)

    def test_quoted_parts_count_too(self):
        limit = sys.getrecursionlimit()
        with self.assertRaises(RecursionError):
            tomli.loads('"q".' * (limit + 1) + '"q" = 1')
        self.assertEqual(tomli.loads('"a b"."c.d" = 1'), {'a b': {'c.d': 1}})


if __name__ == '__main__':
    unittest.main()
