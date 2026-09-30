"""Edge cases for loads() given a non-str: several types and their messages, str subclasses, load()."""
import io
import unittest

import tomli


class LoadsTypeErrorEdgeTests(unittest.TestCase):
    def test_several_types_and_their_messages(self):
        for value, name in ((b'a = 1', 'bytes'), (bytearray(b'a = 1'), 'bytearray'), (None, 'NoneType'),
                            (1, 'int'), (True, 'bool'), (1.5, 'float'), (['a = 1'], 'list'), ({}, 'dict')):
            with self.subTest(name=name):
                with self.assertRaises(TypeError) as caught:
                    tomli.loads(value)
                self.assertEqual(str(caught.exception), f"Expected str object, not '{name}'")

    def test_str_subclasses_are_accepted(self):
        class Text(str):
            pass

        self.assertEqual(tomli.loads(Text('a = 1')), {'a': 1})

    def test_load_still_takes_binary_files(self):
        self.assertEqual(tomli.load(io.BytesIO(b'a = 1')), {'a': 1})


if __name__ == '__main__':
    unittest.main()
