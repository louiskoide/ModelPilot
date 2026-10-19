"""Edge cases for JSONLIterator rel_seek: every seek position, negative seeks, lines longer than a read block."""
import json
import os
import tempfile
import unittest

from boltons.jsonutils import JSONLIterator


class CappedReads:
    """A file object that fails fast instead of hanging when read() loops."""

    def __init__(self, fo, max_reads=200):
        self._fo, self._reads, self._max_reads = fo, 0, max_reads

    def read(self, size=-1):
        self._reads += 1
        if self._reads > self._max_reads:
            raise RuntimeError('read() called %d times: a read loop that never ends' % self._reads)
        return self._fo.read(size)

    def __iter__(self):
        return iter(self._fo)

    def __getattr__(self, name):
        return getattr(self._fo, name)


class JSONLSeekEdgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, records, trailing_newline):
        path = os.path.join(self.tmp.name, 'data%d.jsonl' % len(os.listdir(self.tmp.name)))
        lines = [json.dumps(r) for r in records]
        text = '\n'.join(lines) + ('\n' if trailing_newline else '')
        with open(path, 'w') as f:
            f.write(text)
        starts, pos = [], 0
        for line in lines:
            starts.append(pos)
            pos += len(line) + 1
        return path, len(text), starts

    def read_from(self, path, rel_seek):
        with open(path) as fo:
            return list(JSONLIterator(CappedReads(fo), rel_seek=rel_seek))

    def test_every_seek_position_without_trailing_newline(self):
        records = [{'i': i, 'pad': 'x' * (i % 7)} for i in range(30)]
        path, size, starts = self.write(records, trailing_newline=False)
        for k in range(1, 200):
            rel = k / 200
            target = int(size * rel)
            with self.subTest(rel_seek=rel, target=target):
                got = self.read_from(path, rel)
                self.assertEqual(got, records[len(records) - len(got):])
                if target not in starts:  # strictly inside a line: start at the next one
                    expected = records[sum(1 for s in starts if s <= target):]
                    self.assertEqual(got, expected)
                if target > starts[-1]:
                    self.assertEqual(got, [])

    def test_negative_seek_is_measured_from_the_end(self):
        records = [{'n': n} for n in range(100)]
        for trailing in (False, True):
            path, _, _ = self.write(records, trailing_newline=trailing)
            for r in (0.01, 0.05, 0.25, 0.3, 0.5, 0.75, 0.9, 0.99):
                with self.subTest(trailing_newline=trailing, rel_seek=-r):
                    got = self.read_from(path, -r)
                    self.assertEqual(got, self.read_from(path, 1.0 - r))
                    self.assertEqual(got, records[len(records) - len(got):])
                    self.assertLess(abs(len(got) - r * len(records)), 3)

    def test_lines_longer_than_a_read_block(self):
        records = [{'a': 1}, {'big': 'y' * 20000}, {'b': 2}, {'tail': 'z' * 15000}]
        path, size, starts = self.write(records, trailing_newline=False)
        inside_big = (starts[1] + 5000) / size
        inside_tail = (starts[3] + 9000) / size
        self.assertEqual(self.read_from(path, inside_big), records[2:])
        self.assertEqual(self.read_from(path, inside_tail), [])
        self.assertEqual(self.read_from(path, inside_tail - 1.0), [])

    def test_single_line_file_without_newline(self):
        path, _, _ = self.write([{'only': 'one line'}], trailing_newline=False)
        for rel in (0.1, 0.5, 0.9, -0.1, -0.5, -0.9):
            with self.subTest(rel_seek=rel):
                self.assertEqual(self.read_from(path, rel), [])


if __name__ == '__main__':
    unittest.main()
