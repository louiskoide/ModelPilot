"""Edge cases for TOMLDecodeError attributes: errors from the parser, positions, keywords and deprecation."""
import io
import unittest
import warnings

import tomli

INVALID = ['a = 1\nb = ', 'x = 1 2', 'a = "unterminated', '[table\nk = 1', 'k = [1, 2', '= 1', 'a = 1\na = 2',
           'a = 1\n\n\n  b = @']


class DecodeErrorEdgeTests(unittest.TestCase):
    def check(self, e, doc):
        self.assertEqual(e.doc, doc)
        self.assertIsInstance(e.pos, int)
        self.assertEqual(e.lineno, doc.count('\n', 0, e.pos) + 1)
        self.assertEqual(e.colno, e.pos + 1 if e.lineno == 1 else e.pos - doc.rindex('\n', 0, e.pos))
        where = 'end of document' if e.pos >= len(doc) else f'line {e.lineno}, column {e.colno}'
        self.assertEqual(str(e), f'{e.msg} (at {where})')
        self.assertEqual(e.args, (str(e),))

    def test_parser_errors_carry_consistent_attributes(self):
        for doc in INVALID:
            with self.subTest(doc=doc), warnings.catch_warnings():
                warnings.simplefilter('error')  # the parser must not use a deprecated form
                with self.assertRaises(tomli.TOMLDecodeError) as caught:
                    tomli.loads(doc)
                self.check(caught.exception, doc)

    def test_messages_and_positions_are_unchanged(self):
        with self.assertRaises(tomli.TOMLDecodeError) as caught:
            tomli.loads('x = 1 2')
        self.assertEqual(str(caught.exception),
                         'Expected newline or end of document after a statement (at line 1, column 7)')
        with self.assertRaises(tomli.TOMLDecodeError) as caught:
            tomli.load(io.BytesIO(b'a = 1\nb = '))
        self.assertEqual(str(caught.exception), 'Invalid value (at end of document)')
        self.assertEqual((caught.exception.msg, caught.exception.lineno), ('Invalid value', 2))

    def test_line_starts_and_keywords(self):
        with warnings.catch_warnings():
            warnings.simplefilter('error')
            e = tomli.TOMLDecodeError('m', 'a\nb', 2)
            self.assertEqual((e.lineno, e.colno, str(e)), (2, 1, 'm (at line 2, column 1)'))
            e = tomli.TOMLDecodeError(msg='m', doc='ab', pos=0)
            self.assertEqual((e.lineno, e.colno), (1, 1))
            e = tomli.TOMLDecodeError('m', '', 0)
            self.assertEqual(str(e), 'm (at end of document)')

    def test_other_argument_types_are_deprecated_but_kept(self):
        for args in [('m', 'doc', 1.5), ('m', b'doc', 1), (ValueError('x'),)]:
            with self.subTest(args=args):
                with self.assertWarns(DeprecationWarning):
                    e = tomli.TOMLDecodeError(*args)
                self.assertEqual(e.args, args)
                self.assertIsInstance(e, ValueError)


if __name__ == '__main__':
    unittest.main()
