"""Edge cases for dir_info file URLs: more relative forms, letter case, empty paths, validate(), absolute forms kept."""
import unittest

from packaging.direct_url import DirectUrl, DirectUrlValidationError, DirInfo

MESSAGE = 'File URL must be absolute when dir_info is present'
RELATIVE = ['file:relative/dir', 'file:../up', 'FILE:relative', 'File:./x', 'file:', 'FILE:', 'file://',
            'FILE://LOCALHOST', 'file://host', 'file:?x=1', 'file:#frag', 'file://localhost?x=1']
ABSOLUTE = ['file:///', 'file:/abs', 'FILE:///abs/path', 'fIlE:///abs', 'file://localhost/abs', 'FILE://LOCALHOST/abs',
            'file:///C:/Users/x', 'file:///abs%20with%20space']


class DirInfoFileUrlEdgeTests(unittest.TestCase):
    def test_relative_or_empty_paths_rejected(self):
        for url in RELATIVE:
            for dir_info in ({}, {'editable': True}):
                with self.subTest(url=url, dir_info=dir_info):
                    with self.assertRaises(DirectUrlValidationError) as caught:
                        DirectUrl.from_dict({'url': url, 'dir_info': dir_info})
                    self.assertIn(MESSAGE, str(caught.exception))

    def test_validate_rejects_them_too(self):
        for url in RELATIVE:
            with self.subTest(url=url):
                with self.assertRaises(DirectUrlValidationError) as caught:
                    DirectUrl(url=url, dir_info=DirInfo()).validate()
                self.assertIn(MESSAGE, str(caught.exception))

    def test_absolute_file_urls_accepted(self):
        for url in ABSOLUTE:
            with self.subTest(url=url):
                parsed = DirectUrl.from_dict({'url': url, 'dir_info': {'editable': False}})
                self.assertEqual(parsed.url, url)
                DirectUrl(url=url, dir_info=DirInfo(editable=True)).validate()

    def test_other_schemes_keep_their_error(self):
        for url in ('https://example.com/project', 'relative/path', '/abs/path'):
            with self.subTest(url=url):
                with self.assertRaises(DirectUrlValidationError) as caught:
                    DirectUrl.from_dict({'url': url, 'dir_info': {}})
                self.assertIn('URL scheme must be file:// when dir_info is present', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
