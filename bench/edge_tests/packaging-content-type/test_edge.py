"""Edge cases for Description-Content-Type: more malformed values, both entry points, unsupported and valid types."""
import unittest

from packaging import metadata

MALFORMED = [
    'text/markdown; charset="UTF-8',
    'text/markdown; variant="gfm',
    'text/plain; a="b',
    'text/x-rst; a}b',
    'text/plain; a]b',
    'text/plain; @',
    'text/markdown; variant=gfm; x*',
    'text/plain; charset="UTF-8"; x*',
    'text/plain; x*=',
    'text/plain; a b',
    'text/plain; (comment',
    'text/plain; =',
]


def email_with(value):
    return f'Metadata-Version: 2.6\nName: packaging\nVersion: 1.0\nDescription-Content-Type: {value}\n'


class ContentTypeEdgeTests(unittest.TestCase):
    def test_from_email_reports_one_error_for_the_field(self):
        for value in MALFORMED:
            with self.subTest(value=value):
                with self.assertRaises(ExceptionGroup) as caught:
                    metadata.Metadata.from_email(email_with(value))
                (exc,) = caught.exception.exceptions
                self.assertIsInstance(exc, metadata.InvalidMetadata)
                self.assertEqual(exc.field, 'description-content-type')
                self.assertIn(repr(value), str(exc))
                self.assertIn('is not a valid content type', str(exc))

    def test_reading_the_attribute(self):
        for value in MALFORMED:
            with self.subTest(value=value):
                meta = metadata.Metadata.from_raw({'description_content_type': value}, validate=False)
                with self.assertRaises(metadata.InvalidMetadata) as caught:
                    meta.description_content_type  # noqa: B018
                self.assertEqual(caught.exception.field, 'description-content-type')
                self.assertIn(repr(value), str(caught.exception))
                self.assertIn('is not a valid content type', str(caught.exception))

    def test_well_formed_but_unsupported(self):
        for value in ('application/json', 'text/html; charset=UTF-8', 'image/png', 'application/octet-stream'):
            with self.subTest(value=value):
                meta = metadata.Metadata.from_raw({'description_content_type': value}, validate=False)
                with self.assertRaises(metadata.InvalidMetadata) as caught:
                    meta.description_content_type  # noqa: B018
                self.assertIn('must be one of', str(caught.exception))
                with self.assertRaises(ExceptionGroup) as group:
                    metadata.Metadata.from_email(email_with(value))
                (exc,) = group.exception.exceptions
                self.assertEqual(exc.field, 'description-content-type')

    def test_valid_values_still_accepted(self):
        for value in ('text/plain', 'text/x-rst', 'text/markdown; charset=UTF-8; variant=GFM',
                      'text/markdown; variant=CommonMark', 'TEXT/PLAIN; charset=UTF-8', 'text/markdown; charset="UTF-8"'):
            with self.subTest(value=value):
                meta = metadata.Metadata.from_email(email_with(value))
                self.assertEqual(meta.description_content_type, value)


if __name__ == '__main__':
    unittest.main()
