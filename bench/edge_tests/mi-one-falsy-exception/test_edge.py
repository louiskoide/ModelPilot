"""Edge cases for one()/only() with caller-supplied exceptions: identity, falsy plus unrepresentable items."""
from itertools import count
import unittest

import more_itertools as mi


class FalsyError(Exception):
    def __bool__(self):
        return False


class NoRepr:
    def __repr__(self):
        raise RuntimeError('repr should not be called')


class OneFalsyEdgeTests(unittest.TestCase):
    def test_the_same_instance_is_raised(self):
        for exc in (FalsyError('x'), LookupError('y')):
            with self.assertRaises(type(exc)) as caught:
                mi.one([1, 2], too_long=exc)
            self.assertIs(caught.exception, exc)
            with self.assertRaises(type(exc)) as caught:
                mi.one([], too_short=exc)
            self.assertIs(caught.exception, exc)
            with self.assertRaises(type(exc)) as caught:
                mi.only([1, 2], too_long=exc)
            self.assertIs(caught.exception, exc)

    def test_a_falsy_too_long_with_items_whose_repr_raises(self):
        self.assertRaises(FalsyError, lambda: mi.one((NoRepr() for _ in count()), too_long=FalsyError()))
        self.assertRaises(FalsyError, lambda: mi.only(iter([NoRepr(), NoRepr()]), too_long=FalsyError()))

    def test_a_falsy_too_short_leaves_the_other_cases_alone(self):
        self.assertEqual(mi.one([5], too_short=FalsyError(), too_long=FalsyError()), 5)
        self.assertRaises(ValueError, lambda: mi.one([1, 2], too_short=FalsyError()))

    def test_without_custom_exceptions_the_defaults_still_apply(self):
        with self.assertRaises(ValueError) as caught:
            mi.one([1, 2, 3])
        self.assertIn('1', str(caught.exception))
        self.assertIn('2', str(caught.exception))
        self.assertRaises(ValueError, lambda: mi.one([]))
        self.assertRaises(ValueError, lambda: mi.only([1, 2]))
        self.assertIsNone(mi.only([]))
        self.assertEqual(mi.only([], default='d'), 'd')
        self.assertEqual(mi.only([7]), 7)

    def test_exception_classes_are_accepted(self):
        self.assertRaises(KeyError, lambda: mi.one([], too_short=KeyError))
        self.assertRaises(KeyError, lambda: mi.only([1, 2], too_long=KeyError))


if __name__ == '__main__':
    unittest.main()
