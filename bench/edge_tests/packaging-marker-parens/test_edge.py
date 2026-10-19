"""Edge cases for str(Marker): random nested and/or groups with redundant parentheses, combined markers, outer wrappers."""
import itertools
import random
import unittest

from packaging.markers import Marker

ATOMS = {
    'os_name == "nt"': ('os_name', 'nt'),
    'sys_platform == "linux"': ('sys_platform', 'linux'),
    'python_version < "3.10"': ('python_version', '3.9'),
    'platform_machine == "x86_64"': ('platform_machine', 'x86_64'),
    'implementation_name == "cpython"': ('implementation_name', 'cpython'),
}
FALSE_VALUES = {'os_name': 'posix', 'sys_platform': 'darwin', 'python_version': '3.12',
                'platform_machine': 'arm64', 'implementation_name': 'pypy'}


def environments():
    names = list(ATOMS)
    for bits in itertools.product((False, True), repeat=len(names)):
        env = {}
        for name, bit in zip(names, bits):
            key, true_value = ATOMS[name]
            env[key] = true_value if bit else FALSE_VALUES[key]
        yield env, dict(zip(names, bits))


def random_expr(rng, depth):
    """A marker string and a Python evaluator for it."""
    if depth == 0 or rng.random() < 0.3:
        atom = rng.choice(list(ATOMS))
        text, fn = atom, (lambda truth, a=atom: truth[a])
    else:
        parts = [random_expr(rng, depth - 1) for _ in range(rng.randrange(2, 4))]
        ops = [rng.choice(['and', 'or']) for _ in parts[1:]]
        text = parts[0][0]
        for op, (t, _) in zip(ops, parts[1:]):
            text += f' {op} {t}'
        fns = [f for _, f in parts]

        def fn(truth, fns=fns, ops=ops):
            # Python's own precedence: and binds tighter than or, as in markers.
            terms, current = [], fns[0](truth)
            for op, f in zip(ops, fns[1:]):
                if op == 'and':
                    current = current and f(truth)
                else:
                    terms.append(current)
                    current = f(truth)
            terms.append(current)
            return any(terms)
        text = '(' + text + ')'
    wraps = rng.choice([0, 0, 1, 2, 3])
    return '(' * wraps + text + ')' * wraps, fn


class MarkerParensEdgeTests(unittest.TestCase):
    def test_random_nested_groups_round_trip(self):
        rng = random.Random(10)
        envs = list(environments())
        for i in range(300):
            text, fn = random_expr(rng, 3)
            marker = Marker(text)
            again = Marker(str(marker))
            with self.subTest(i=i, marker=text, written=str(marker)):
                for env, truth in envs:
                    expected = fn(truth)
                    self.assertEqual(marker.evaluate(env), expected)
                    self.assertEqual(again.evaluate(env), expected)

    def test_combined_markers_round_trip(self):
        a = Marker('os_name == "nt" or sys_platform == "linux"')
        b = Marker('python_version < "3.10" or implementation_name == "cpython"')
        c = Marker('((platform_machine == "x86_64" or os_name == "nt"))')
        envs = [env for env, _ in environments()]
        for combined in (a & b, (a & b) | c, c & a, (c | a) & b):
            again = Marker(str(combined))
            with self.subTest(written=str(combined)):
                for env in envs:
                    self.assertEqual(again.evaluate(env), combined.evaluate(env))

    def test_redundant_outer_wrapper_adds_nothing(self):
        cases = {
            '((os_name == "nt"))': 'os_name == "nt"',
            '(((os_name == "nt")))': 'os_name == "nt"',
            '((os_name == "nt" or sys_platform == "linux"))': 'os_name == "nt" or sys_platform == "linux"',
            '((os_name == "nt" and sys_platform == "linux"))': 'os_name == "nt" and sys_platform == "linux"',
        }
        for text, expected in cases.items():
            with self.subTest(marker=text):
                self.assertEqual(str(Marker(text)), expected)

    def test_nested_group_keeps_one_pair(self):
        m = Marker('python_version < "3.10" and (((sys_platform == "linux" or sys_platform == "darwin")))')
        self.assertEqual(str(m), 'python_version < "3.10" and (sys_platform == "linux" or sys_platform == "darwin")')


if __name__ == '__main__':
    unittest.main()
