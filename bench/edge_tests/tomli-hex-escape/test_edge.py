"""Edge cases for the \\xHH escape: multi-line strings, keys, digit case, exact length, invalid forms."""
import unittest

import tomli


class HexEscapeEdgeTests(unittest.TestCase):
    def test_basic_and_multiline_strings(self):
        self.assertEqual(tomli.loads(r's = "\x41\x62c"'), {'s': 'Abc'})
        self.assertEqual(tomli.loads('s = """\\x41\n\\x09end"""'), {'s': 'A\n\tend'})
        self.assertEqual(tomli.loads(r's = "\xfF\x00"'), {'s': '\xff\x00'})

    def test_exactly_two_digits_are_used(self):
        self.assertEqual(tomli.loads(r's = "\x414"'), {'s': 'A4'})

    def test_quoted_keys(self):
        self.assertEqual(tomli.loads(r'"\x41" = 1'), {'A': 1})

    def test_literal_strings_are_unaffected(self):
        self.assertEqual(tomli.loads(r"s = '\x41'"), {'s': r'\x41'})
        self.assertEqual(tomli.loads("s = '''\\x41'''"), {'s': r'\x41'})

    def test_invalid_forms(self):
        for text in (r's = "\x4"', r's = "\xg0"', r's = "\x"', r's = "\X41"', 's = """\\x4"""'):
            with self.subTest(text=text):
                with self.assertRaises(tomli.TOMLDecodeError):
                    tomli.loads(text)


if __name__ == '__main__':
    unittest.main()
