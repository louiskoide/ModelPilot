"""Edge cases for composed annotations: other parameter kinds, compose_left, partials and namespaces."""
from functools import partial
import inspect
import typing
import unittest

from toolz import compose, compose_left, curry
from toolz.functoolz import Compose


def g(y: str) -> float:
    return float(len(y))


def f(x: int) -> str:
    return str(x)


def kinds(a: int, /, b: str, *args: bytes, c: float = 1.0, **kwargs: complex) -> list:
    return [a, b, args, c, kwargs]


def pair(a: int, b: str) -> str:
    return b * a


def signature_view(c):
    sig = inspect.signature(c)
    out = {k: p.annotation for k, p in sig.parameters.items() if p.annotation is not p.empty}
    if sig.return_annotation is not sig.empty:
        out['return'] = sig.return_annotation
    return out


class ComposeAnnotationEdgeTests(unittest.TestCase):
    def test_every_parameter_kind_is_included(self):
        c = compose(g, kinds)
        expected = {'a': int, 'b': str, 'args': bytes, 'c': float, 'kwargs': complex, 'return': float}
        self.assertEqual(c.__annotations__, expected)
        self.assertEqual(typing.get_type_hints(c), expected)

    def test_compose_left_matches_compose(self):
        self.assertEqual(compose_left(f, g).__annotations__, {'x': int, 'return': float})
        self.assertEqual(compose_left(f, g).__annotations__, compose(g, f).__annotations__)

    def test_partial_and_curried_first_functions_follow_their_signature(self):
        for first in (partial(pair, 3), curry(pair)(3), curry(pair)):
            with self.subTest(first=first):
                c = compose(g, first)
                self.assertEqual(c.__annotations__, signature_view(c))
        self.assertEqual(compose(g, partial(pair, 3)).__annotations__, {'b': str, 'return': float})

    def test_inspect_get_annotations_sees_the_combined_view(self):
        self.assertEqual(inspect.get_annotations(compose(g, f)), {'x': int, 'return': float})
        self.assertEqual(inspect.get_annotations(Compose), {})

    def test_string_annotations_resolve_in_the_first_functions_namespace(self):
        ns = {}
        exec('from __future__ import annotations\n'
             'class Local: pass\n'
             'def s(x: Local) -> str: return str(x)', ns)
        c = compose(g, ns['s'])
        self.assertEqual(c.__annotations__, {'x': 'Local', 'return': float})
        self.assertEqual(typing.get_type_hints(c), {'x': ns['Local'], 'return': float})

    def test_long_chains_take_the_ends(self):
        def h(z):
            return z
        self.assertEqual(compose(g, h, h, f).__annotations__, {'x': int, 'return': float})
        self.assertEqual(compose(compose(g, h), compose(h, f)).__annotations__, {'x': int, 'return': float})


if __name__ == '__main__':
    unittest.main()
