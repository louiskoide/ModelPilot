"""Re-grading saved fixes and frozen edge-case tests on a synthetic repository, $0."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
from modelpilot import bench_tasks, regrade
from tests import test_bench_tasks as synthetic

# Passes the hidden tests (lists) but not an edge case the agent never saw (a string).
LISTS_ONLY = 'def last(items):\n    return items[len(items) - 1] if isinstance(items, list) else items[0]\n'
EDGE = ('import unittest\nfrom pkg import last\n\nclass Edge(unittest.TestCase):\n'
        '    def test_three(self):\n        self.assertEqual(last([1, 2, 3]), 3)\n'
        '    def test_string(self):\n        self.assertEqual(last("ab"), "b")\n')
# Behaviour the real fix never had: a suite with this test is not used.
BEYOND_REFERENCE = ('import unittest\nfrom pkg import last\n\nclass Edge(unittest.TestCase):\n'
                    '    def test_empty(self):\n        self.assertIsNone(last([]))\n')


class RegradeTests(unittest.TestCase):
    def setUp(self):
        self.case = synthetic.BenchTaskTests('test_candidates_find_the_fix_commit')
        self.case.setUp()
        self.root = self.case.root
        self.task = dict(self.case.task, repo_path=str(self.case.repo))
        self.edge = self.root/'edge'
        (self.edge/'synthetic').mkdir(parents=True)
        (self.edge/'synthetic'/'test_edge.py').write_text(EDGE)

    def tearDown(self):
        self.case.tearDown()

    def trial(self, arm, source, passed):
        """A finished trial as bench saves it: its fix as a diff from the base, and its record."""
        work = bench_tasks.workspace(self.task, self.case.repo, self.root/'ws'/arm)
        if source is not None:
            (work/'pkg'/'__init__.py').write_text(source)
        diff = subprocess.run(['git', 'diff', '--binary', 'HEAD'], cwd=work, capture_output=True, check=True).stdout
        out = self.root/'run'/'synthetic'/arm/'0'
        out.mkdir(parents=True)
        (out/'agent.diff').write_bytes(diff)
        (out/'trial.json').write_text(json.dumps({'task': 'synthetic', 'arm': arm, 'trial': 0, 'passed': passed,
                                                  'expected_hidden_passed': 2, 'grade': {'reason': 'recorded'}}))
        return out

    def regrade(self, arm, source, passed, edge=False):
        return regrade.regrade_trial(self.trial(arm, source, passed), self.task, self.case.repo, sys.executable,
                                     self.root/'scratch'/arm, edge=edge, root=self.edge)

    def test_saved_fixes_regrade_to_their_recorded_verdicts(self):
        fixed = self.regrade('fixed', synthetic.FIXED, True)
        idle = self.regrade('idle', None, False)
        self.assertEqual((fixed['regraded_passed'], fixed['matches']), (True, True))
        self.assertEqual((idle['regraded_passed'], idle['regraded_reason'], idle['matches']),
                         (False, 'hidden_tests_failed', True))
        self.assertEqual((fixed['scope']['source_files'], fixed['scope']['source_lines'], fixed['scope']['test_added']),
                         (1, 2, 0))

    def test_an_empty_diff_on_a_passing_trial_is_a_fix_not_saved(self):
        committed = self.regrade('committed', None, True)  # the agent committed, and the diff was taken against HEAD
        self.assertEqual(committed['error'], 'fix_not_saved')

    def test_edge_tests_see_past_pass_fail_and_count_only_if_the_reference_passes_them(self):
        suite = regrade.check_edge(self.task, self.case.repo, sys.executable, self.root/'check', self.edge)
        self.assertTrue(suite['usable'])
        self.assertEqual((suite['reference']['tests_passed'], suite['base']['tests_passed']), (2, 0))
        partial = self.regrade('partial', LISTS_ONLY, True, edge=True)
        fixed = self.regrade('fixed', synthetic.FIXED, True, edge=True)
        self.assertTrue(partial['regraded_passed'] and fixed['regraded_passed'])  # pass/fail can't tell them apart
        self.assertEqual((partial['edge']['tests_passed'], partial['edge']['tests_run']), (1, 2))
        self.assertEqual(partial['edge']['failing_tests'], ['test_string (test_edge.Edge)'])
        self.assertEqual((fixed['edge']['tests_passed'], fixed['edge']['tests_run']), (2, 2))
        summary = regrade.summarize([partial, fixed], {'synthetic': suite}, ['partial', 'fixed'])
        self.assertEqual((summary['arms']['partial']['edge_all_passed'], summary['arms']['fixed']['edge_all_passed']), (0, 1))
        self.assertEqual(summary['paired']['tasks'][0]['partial']['edge'], '1/2')
        (self.edge/'synthetic'/'test_beyond.py').write_text(BEYOND_REFERENCE)
        beyond = regrade.check_edge(self.task, self.case.repo, sys.executable, self.root/'check2', self.edge)
        self.assertFalse(beyond['usable'])

    def test_test_paths(self):
        self.assertTrue(regrade.is_test_path('tests/test_x.py', 'tests'))
        self.assertTrue(regrade.is_test_path('src/pkg/tests/helpers.py', 'tests'))
        self.assertTrue(regrade.is_test_path('test_cli.py', 'tests'))
        self.assertFalse(regrade.is_test_path('src/pkg/core.py', 'tests'))


if __name__ == '__main__':
    unittest.main()
