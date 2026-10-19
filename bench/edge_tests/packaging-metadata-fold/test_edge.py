"""Edge cases for line boundaries in metadata: every boundary written and rejected, several in one value, non-boundaries."""
import unittest

from packaging import metadata

BOUNDARIES = ['\r\n', '\n', '\r', '\v', '\f', '\x1c', '\x1d', '\x1e', '\x85', ' ', ' ']
HEAD = 'metadata-version: 2.3\nname: packaging\nversion: 1.2.3\n'


def written(**fields):
    raw = {'metadata_version': '2.3', 'name': 'packaging', 'version': '1.2.3', **fields}
    return metadata.Metadata.from_raw(raw).as_rfc822().as_string()


class MetadataFoldEdgeTests(unittest.TestCase):
    def test_every_boundary_folds_when_written(self):
        for boundary in BOUNDARIES:
            with self.subTest(boundary=boundary):
                self.assertEqual(written(author=f'Hello{boundary}World'), HEAD + 'author: Hello\n        World\n\n')
                self.assertEqual(written(maintainer=f'A{boundary}B'), HEAD + 'maintainer: A\n            B\n\n')

    def test_several_boundaries_in_one_value(self):
        self.assertEqual(written(author='a\r\nb\rc d'),
                         HEAD + 'author: a\n        b\n        c\n        d\n\n')
        # "\r\n" is one boundary, not two.
        self.assertEqual(written(author='x\r\ny'), HEAD + 'author: x\n        y\n\n')

    def test_every_boundary_rejected_in_summary(self):
        for boundary in BOUNDARIES:
            with self.subTest(boundary=boundary):
                meta = metadata.Metadata.from_raw({'summary': f'Hello{boundary}Again'}, validate=False)
                with self.assertRaises(metadata.InvalidMetadata) as caught:
                    meta.summary  # noqa: B018
                self.assertEqual(caught.exception.field, 'summary')

    def test_other_whitespace_is_not_a_boundary(self):
        for char in ('\t', ' ', '\xa0', '\u2003'):
            with self.subTest(char=char):
                summary = f'Hello{char}Again'
                meta = metadata.Metadata.from_raw({'summary': summary}, validate=False)
                self.assertEqual(meta.summary, summary)


if __name__ == '__main__':
    unittest.main()
