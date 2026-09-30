"""Edge cases for multi-line inline tables: comments, nesting, arrays, dotted keys, and what stays invalid."""
import unittest

import tomli


class InlineTableNewlinesEdgeTests(unittest.TestCase):
    def test_comments_between_pairs(self):
        doc = 'x = {\n  a = 1, # first\n  # a whole line\n  b = 2,\n}\n'
        self.assertEqual(tomli.loads(doc), {'x': {'a': 1, 'b': 2}})

    def test_nested_and_inside_arrays(self):
        self.assertEqual(tomli.loads('x = {\n  y = {\n    z = 1,\n  },\n}'), {'x': {'y': {'z': 1}}})
        self.assertEqual(tomli.loads('arr = [\n  {a = 1,\n  },\n  {b = 2},\n]'), {'arr': [{'a': 1}, {'b': 2}]})

    def test_dotted_keys_across_lines(self):
        self.assertEqual(tomli.loads('x = {\n  a.b = 1,\n  a.c = 2\n}'), {'x': {'a': {'b': 1, 'c': 2}}})

    def test_trailing_comma_on_one_line_and_empty_tables(self):
        self.assertEqual(tomli.loads('x = {a = 1,}'), {'x': {'a': 1}})
        self.assertEqual(tomli.loads('x = {\n  # nothing here\n}'), {'x': {}})
        self.assertEqual(tomli.loads('x = {\n\n}'), {'x': {}})

    def test_what_stays_invalid(self):
        for doc in ('x = {a = 1,,}', 'x = {,}', 'x = {, a = 1}', 'x = {\n  a = 1,\n  a = 2,\n}',
                    'x = {a = 1 b = 2}', 'x = {a =\n 1}', 'x = {\n  a = 1,\n'):
            with self.subTest(doc=doc):
                with self.assertRaises(tomli.TOMLDecodeError):
                    tomli.loads(doc)

    def test_single_line_tables_are_unchanged(self):
        self.assertEqual(tomli.loads('x = {a = 1, b = {c = "d"}}'), {'x': {'a': 1, 'b': {'c': 'd'}}})
        self.assertEqual(tomli.loads('x = {}'), {'x': {}})


if __name__ == '__main__':
    unittest.main()
