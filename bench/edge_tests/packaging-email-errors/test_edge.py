"""Edge cases for from_email() error collection: several problems of each kind, duplicated fields, no __context__."""
import unittest

from packaging import metadata

BASE = 'Metadata-Version: 2.6\nName: packaging\nVersion: 1.0\n'


class EmailErrorsEdgeTests(unittest.TestCase):
    def collect(self, data):
        with self.assertRaises(ExceptionGroup) as caught:
            metadata.Metadata.from_email(data, validate=True)
        group = caught.exception
        self.assertEqual(group.message, 'invalid or unparsed metadata')
        self.assertIsNone(group.__context__)
        for exc in group.exceptions:
            self.assertIsInstance(exc, metadata.InvalidMetadata)
        return sorted(exc.field for exc in group.exceptions)

    def test_several_unknown_and_several_invalid(self):
        data = ('Metadata-Version: 2.6\nName: packaging\nVersion: not a version\nRequires-Python: >=3.x\n'
                'First-Unknown: a\nSecond-Unknown: b\n')
        self.assertEqual(self.collect(data), ['first-unknown', 'requires-python', 'second-unknown', 'version'])

    def test_duplicated_optional_field_with_invalid_ones(self):
        data = BASE.replace('Version: 1.0', 'Version: bad') + 'Summary: one\nSummary: two\nOther-Field: x\n'
        self.assertEqual(self.collect(data), ['other-field', 'summary', 'version'])

    def test_duplicated_required_fields_reported_once(self):
        data = 'Metadata-Version: 2.6\nName: a\nName: b\nVersion: 1.0\nVersion: 2.0\n'
        self.assertEqual(self.collect(data), ['name', 'version'])
        data = 'Name: a\nName: b\nUnknown: z\n'
        self.assertEqual(self.collect(data), ['metadata-version', 'name', 'unknown', 'version'])

    def test_missing_required_fields_with_an_unknown_one(self):
        self.assertEqual(self.collect('Mystery: 1\n'), ['metadata-version', 'mystery', 'name', 'version'])
        self.assertEqual(self.collect('Name: packaging\nMystery: 1\n'), ['metadata-version', 'mystery', 'version'])

    def test_valid_metadata_and_validate_false_unchanged(self):
        meta = metadata.Metadata.from_email(BASE + 'Summary: fine\n', validate=True)
        self.assertEqual((meta.name, str(meta.version), meta.summary), ('packaging', '1.0', 'fine'))
        loose = metadata.Metadata.from_email(BASE + 'Unknown-Field: x\n', validate=False)
        self.assertEqual(loose.name, 'packaging')


if __name__ == '__main__':
    unittest.main()
