"""Edge cases for running_min/running_max stability: identity against min()/max() over many tied windows."""
import random
import unittest

import more_itertools as mi


class Tagged:
    """Compares by value only, so equal values from different positions are distinct objects."""
    def __init__(self, value, tag):
        self.value, self.tag = value, tag

    def __lt__(self, other):
        return self.value < other.value

    def __le__(self, other):
        return self.value <= other.value

    def __gt__(self, other):
        return self.value > other.value

    def __ge__(self, other):
        return self.value >= other.value

    def __eq__(self, other):
        return self.value == other.value

    __hash__ = None

    def __repr__(self):
        return f'Tagged({self.value}, {self.tag})'


def windows(data, maxlen):
    for end in range(1, len(data) + 1):
        yield data[max(0, end - maxlen) if maxlen else 0:end]


class RunningMinMaxEdgeTests(unittest.TestCase):
    def check(self, data, maxlen):
        for running, builtin in ((mi.running_min, min), (mi.running_max, max)):
            got = list(running(data, maxlen=maxlen))
            want = [builtin(w) for w in windows(data, maxlen)]
            self.assertEqual(len(got), len(want))
            for i, (g, w) in enumerate(zip(got, want)):
                self.assertIs(g, w, (running.__name__, maxlen, i, g, w))

    def test_many_tied_windows(self):
        rng = random.Random(7)
        for trial in range(60):
            data = [Tagged(rng.randint(0, 3), i) for i in range(rng.randint(0, 25))]
            for maxlen in (1, 2, 3, 4, 7, None):
                with self.subTest(trial=trial, maxlen=maxlen):
                    self.check(data, maxlen)

    def test_all_equal(self):
        data = [Tagged(1, i) for i in range(6)]
        for maxlen in (1, 2, 3, None):
            self.check(data, maxlen)

    def test_mixed_numeric_types(self):
        from fractions import Fraction
        data = [1, 1.0, True, Fraction(1), 0, 0.0, 2, 2.0]
        for maxlen in (2, 3, None):
            for running, builtin in ((mi.running_min, min), (mi.running_max, max)):
                got = [type(v) for v in running(data, maxlen=maxlen)]
                want = [type(builtin(w)) for w in windows(data, maxlen)]
                self.assertEqual(got, want, (running.__name__, maxlen))


if __name__ == '__main__':
    unittest.main()
