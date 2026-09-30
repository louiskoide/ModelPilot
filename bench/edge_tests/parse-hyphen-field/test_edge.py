"""Edge cases for hyphens in field names: several hyphens, types, full parse, findall, collision order."""
import unittest

import parse


class HyphenFieldEdgeTests(unittest.TestCase):
    def test_several_hyphens_and_a_full_match(self):
        result = parse.parse('{first-name} {last-name}', 'Ada Lovelace')
        self.assertEqual((result['first-name'], result['last-name']), ('Ada', 'Lovelace'))
        self.assertEqual(parse.parse('{a-b-c}!', 'x!')['a-b-c'], 'x')

    def test_a_typed_hyphen_field(self):
        result = parse.parse('/items/{item-id:d}/', '/items/42/')
        self.assertEqual(result['item-id'], 42)
        self.assertEqual(result.named, {'item-id': 42})

    def test_findall(self):
        found = [r['item-id'] for r in parse.findall('<{item-id}>', '<1> and <2>')]
        self.assertEqual(found, ['1', '2'])

    def test_collisions_in_either_order_keep_the_right_values(self):
        first = parse.parse('{a-b}/{a_b}', '1/2')
        self.assertEqual((first['a-b'], first['a_b']), ('1', '2'))
        second = parse.parse('{a_b}/{a-b}', '1/2')
        self.assertEqual((second['a_b'], second['a-b']), ('1', '2'))

    def test_repeated_hyphen_field_must_match_the_same_text(self):
        self.assertEqual(parse.parse('{user-id}/{user-id}', '7/7')['user-id'], '7')
        self.assertIsNone(parse.parse('{user-id}/{user-id}', '7/8'))


if __name__ == '__main__':
    unittest.main()
