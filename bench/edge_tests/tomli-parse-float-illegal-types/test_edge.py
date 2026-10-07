"""Edge cases for parse_float return types: subclasses, positions in the document, legal types and load()."""
from decimal import Decimal
import io
import unittest

import tomli

MESSAGE = 'parse_float must not return dicts or lists'


class Mapping(dict):
    pass


class Seq(list):
    pass


class ParseFloatTypesEdgeTests(unittest.TestCase):
    def test_subclasses_are_refused(self):
        for bad in (lambda s: Mapping(), lambda s: Seq([1])):
            with self.assertRaisesRegex(ValueError, MESSAGE):
                tomli.loads('x = 1.5', parse_float=bad)

    def test_every_position_in_the_document(self):
        for doc in ('[t]\nf = 2.5', 'a = [1.0, 2.0]', 'i = {f = 3.25}', 'a = [[0.5]]', '[[aot]]\nf = 1e3'):
            with self.subTest(doc=doc):
                with self.assertRaisesRegex(ValueError, MESSAGE):
                    tomli.loads(doc, parse_float=lambda s: [])

    def test_legal_types_still_used(self):
        self.assertEqual(tomli.loads('x = 0.1', parse_float=Decimal), {'x': Decimal('0.1')})
        self.assertEqual(tomli.loads('x = [0.1, inf]', parse_float=str), {'x': ['0.1', 'inf']})
        self.assertEqual(tomli.loads('x = 1', parse_float=lambda s: []), {'x': 1})  # no float, never called

    def test_load_from_binary_file(self):
        with self.assertRaisesRegex(ValueError, MESSAGE):
            tomli.load(io.BytesIO(b'v = 1.0'), parse_float=lambda s: {})


if __name__ == '__main__':
    unittest.main()
