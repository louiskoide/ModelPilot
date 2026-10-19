"""Edge cases for Table.to_text: empty tables, header cells rendered like data cells, short headers, no headers."""
import unittest

from boltons.tableutils import Table


def cells(line):
    return [c.strip() for c in line.split(' | ')]


class TableToTextEdgeTests(unittest.TestCase):
    def test_empty_tables(self):
        for table in (Table(), Table([]), Table([[]]), Table([[]], headers=None), Table([[], []])):
            with self.subTest(table=repr(table)):
                self.assertEqual(table.to_text(), '')
                self.assertEqual(table.to_text(with_headers=False), '')

    def test_no_headers_means_no_header_row_or_separator(self):
        table = Table([[1, 'two'], ['three', None], [4.5, 6]], headers=None)
        lines = table.to_text().split('\n')
        self.assertEqual([cells(line) for line in lines], [['1', 'two'], ['three', 'None'], ['4.5', '6']])
        self.assertEqual(table.to_text(), table.to_text(with_headers=False))
        self.assertEqual(Table([[], ['only']]).to_text(), 'only')

    def test_header_cells_render_like_data_cells(self):
        values = [None, 3, 4.5, ('t', 1), 'abcdefghijkl']
        as_headers = Table([list(range(5))], headers=None)
        as_headers.headers = list(values)
        as_data = Table([values, list(range(5))], headers=None)
        for maxlen in (None, 6):
            with self.subTest(maxlen=maxlen):
                header_line = as_headers.to_text(maxlen=maxlen).split('\n')[0]
                self.assertEqual(header_line, as_data.to_text(maxlen=maxlen).split('\n')[0])
        lines = as_headers.to_text().split('\n')
        self.assertEqual(len(lines), 3)
        self.assertEqual(set(lines[1]) - {'-', '|'}, set())

    def test_short_headers_are_padded_with_none(self):
        for given in (['a'], ['a', 'b'], [None]):
            with self.subTest(headers=given):
                short = Table([[1, 2, 3], [4, 5, 6]], headers=None)
                short.headers = list(given)
                padded = Table([[1, 2, 3], [4, 5, 6]], headers=None)
                padded.headers = list(given) + [None] * (3 - len(given))
                self.assertEqual(short.to_text(), padded.to_text())
                self.assertEqual(cells(short.to_text().split('\n')[0]),
                                 [str(h) for h in padded.headers])
                self.assertEqual(short.to_text(with_headers=False).split('\n'), ['1 | 2 | 3', '4 | 5 | 6'])


if __name__ == '__main__':
    unittest.main()
