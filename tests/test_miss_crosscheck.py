"""The miss detector's cross-run test check on a synthetic task: peers' tests, kept only if they pass on their own
tree, run against each fix. $0, no model request; needs git and an interpreter with pytest (skips without one)."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from modelpilot import bench_tasks, miss_crosscheck as mx
from tests.test_exposure import git

CORE = 'def shout(text):\n    return text.upper() + "!"\n'
STRIPPED = 'def shout(text):\n    return text.strip().upper() + "!"\n'
TESTS = ('import unittest\nfrom pkg import shout\n\n\nclass Shout(unittest.TestCase):\n'
         '    def test_word(self):\n        self.assertEqual(shout("hi"), "HI!")\n')


def has_pytest(python):
    return subprocess.run([python, '-c', 'import pytest'], capture_output=True).returncode == 0


PYTHON = next((p for p in (bench_tasks.interpreter(), sys.executable) if has_pytest(p)), None)


class ParsingTests(unittest.TestCase):
    def test_test_functions_are_named_as_pytest_names_them(self):
        found = mx.test_functions('import x\n\ndef test_a():\n    pass\n\ndef helper():\n    pass\n\n'
                                  'class T(unittest.TestCase):\n    def test_b(self):\n        pass\n\n    def setUp(self):\n'
                                  '        pass\n')
        self.assertEqual(sorted(found), ['T::test_b', 'test_a'])
        self.assertIsNone(mx.test_functions('def broken(:\n'))
        self.assertNotEqual(mx.test_functions('def test_a():\n    assert 1\n')['test_a'],
                            mx.test_functions('def test_a():\n    assert 2\n')['test_a'])
        self.assertEqual(mx.test_functions('def test_a():\n    assert 1\n'),
                         mx.test_functions('def test_a():\n\n    assert 1  # same code\n'))

    def test_junit_outcomes_fold_parameters_and_find_collection_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            xml = Path(tmp)/'j.xml'
            xml.write_text(
                '<testsuites><testsuite>'
                '<testcase classname="tests.test_x.T" name="test_a[1]"/>'
                '<testcase classname="tests.test_x.T" name="test_a[2]"><failure message="AssertionError: no">E   assert 1 == 2'
                '</failure></testcase>'
                '<testcase classname="tests.test_x" name="test_b"><skipped message="later"/></testcase>'
                '<testcase classname="" name="tests.test_y"><error message="collection failure">'
                "E   ImportError: cannot import name 'whisper' from 'pkg'</error></testcase>"
                '</testsuite></testsuites>')
            results = mx.junit_outcomes(xml)
        self.assertEqual(mx.outcome_of(results, 'tests/test_x.py', 'T::test_a')['outcome'], 'failed')
        self.assertEqual(mx.outcome_of(results, 'tests/test_x.py', 'test_b')['outcome'], 'skipped')
        self.assertEqual(mx.outcome_of(results, 'tests/test_y.py', 'test_c')['outcome'], 'error')
        self.assertEqual(mx.outcome_of(results, 'tests/test_z.py', 'test_d')['outcome'], 'not_run')

    def test_failures_naming_what_the_base_lacks_are_api_noise(self):
        known = {'shout', 'pkg', 'upper', 'group'}
        cases = [("ImportError: cannot import name 'whisper' from 'pkg'", 'api_noise'),
                 ("ModuleNotFoundError: No module named 'pkg.loud'", 'api_noise'),
                 ("AttributeError: module 'pkg' has no attribute 'shout'", 'other_error'),  # the base has it: A lost it
                 ("TypeError: shout() got an unexpected keyword argument 'times'", 'api_noise'),
                 ("fixture 'tmp_repo' not found", 'api_noise'),
                 ('AssertionError: assert "HI !" == "HI!"', 'assertion'),
                 ("TypeError: 'NoneType' object is not subscriptable", 'other_error')]
        for text, label in cases:
            self.assertEqual(mx.classify({'outcome': 'failed', 'text': text}, known), label, text)
        self.assertEqual(mx.classify({'outcome': 'passed', 'text': ''}, known), 'passed')


@unittest.skipUnless(PYTHON, 'needs an interpreter with pytest (the bench venv has one)')
class CrossCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root/'repo'
        (self.repo/'pkg').mkdir(parents=True)
        (self.repo/'tests').mkdir()
        (self.repo/'pkg'/'__init__.py').write_text('from ._core import shout\n')
        (self.repo/'pkg'/'_core.py').write_text(CORE)
        (self.repo/'tests'/'__init__.py').write_text('')
        (self.repo/'tests'/'test_core.py').write_text(TESTS)
        git(self.repo, 'init', '-q')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', 'base')
        base = git(self.repo, 'rev-parse', 'HEAD')
        (self.repo/'pkg'/'_core.py').write_text(STRIPPED)
        git(self.repo, 'commit', '-q', '-am', 'reference')
        self.task = {'id': 'synthetic', 'repo': 'synthetic', 'base': base, 'reference': git(self.repo, 'rev-parse', 'HEAD'),
                     'test_dir': 'tests', 'pythonpath': None}
        self.trials = [
            self.trial('pass', 'a', True, .1, {'pkg/_core.py': STRIPPED, 'tests/test_core.py': TESTS +
                       '\n    def test_strips(self):\n        self.assertEqual(shout(" hi "), "HI!")\n'}),
            self.trial('miss', 'a', False, .05, {'pkg/_core.py': CORE.replace('text.upper()', 'text.lstrip().upper()'),
                       'tests/test_left.py': 'from pkg import shout\n\n\ndef test_left():\n    assert shout(" hi") == "HI!"\n'}),
            self.trial('noise', 'b', True, .3, {'pkg/_core.py': STRIPPED + '\n\ndef whisper(text):\n    return text.lower()\n',
                       'tests/test_whisper.py': 'from pkg._core import whisper\n\n\ndef test_whisper():\n'
                                                '    assert whisper("A") == "a"\n'}, model=mx.OPUS),
            self.trial('bad', 'a', True, .08, {'pkg/_core.py': STRIPPED,
                       'tests/test_bad.py': 'from pkg import shout\n\n\ndef test_wrong():\n    assert shout("x") == "X!!"\n'})]

    def tearDown(self):
        self.tmp.cleanup()

    def trial(self, name, arm, strict, cost, files, model='claude-sonnet-5-5'):
        work = self.root/f'agent-{name}'
        git(self.root, 'clone', '-q', str(self.repo), str(work))
        git(work, 'checkout', '-q', self.task['base'])
        for path, text in files.items():
            (work/path).write_text(text)
        git(work, 'add', '-A')
        diff = self.root/'diffs'/f'{name}.diff'
        diff.parent.mkdir(exist_ok=True)
        diff.write_text(git(work, 'diff', '--binary', '--cached') + '\n')
        return {'key': name, 'run': 'bench-x', 'task': 'synthetic', 'arm': arm, 'model': model, 'trial': 0,
                'strict_pass': strict, 'cost': cost, 'diff': diff}

    def test_a_peer_keeps_only_the_tests_it_changed_that_pass_on_its_own_tree(self):
        found = {t['key']: mx.peer_tests(t, self.task, self.repo, PYTHON, self.root/'s'/t['key']) for t in self.trials}
        self.assertEqual(found['pass']['files']['tests/test_core.py']['names'], ['Shout::test_strips'])  # not test_word
        self.assertEqual(found['pass']['kept'], {'tests/test_core.py': ['Shout::test_strips']})
        self.assertEqual(found['pass']['reference_kept'], {'tests/test_core.py': ['Shout::test_strips']})
        self.assertEqual(found['bad']['kept'], {})  # fails on its own tree: never used
        self.assertEqual(found['noise']['reference_kept'], {'tests/test_whisper.py': []})  # the reference has no whisper

    def test_a_peers_test_flags_the_miss_and_its_invented_api_is_noise(self):
        tests = {t['key']: mx.peer_tests(t, self.task, self.repo, PYTHON, self.root/'s'/t['key']) for t in self.trials}
        known = mx.base_names(self.task, self.repo, self.root/'names')
        miss = self.trials[1]
        crossed = mx.against_peers(miss, self.task, self.repo, PYTHON, self.root/'a', [t for t in self.trials if t is not miss],
                                   tests, known)
        self.assertEqual(crossed['pass']['failing'], [['tests/test_core.py', 'Shout::test_strips', 'assertion']])
        self.assertEqual(crossed['noise']['classes'], {'api_noise': 1})
        self.assertNotIn('bad', crossed)  # no kept tests, nothing run
        self.assertTrue(mx.flags(crossed['pass']))
        self.assertFalse(mx.flags(crossed['noise']))

    def test_the_whole_check_flags_the_miss_and_no_pass(self):
        report = mx.crosscheck(self.trials, {'synthetic': self.task}, self.root/'out', PYTHON, lambda task: self.repo, jobs=2)
        self.assertEqual((report['trials'], report['misses'], report['errors']), (4, 1, 0))
        readings = report['summary']['readings']
        self.assertEqual(readings['any/all']['overall'], {'misses_flagged': 1, 'misses': 1, 'passes_flagged': 0, 'passes': 3})
        self.assertEqual(readings['same_arm/all']['overall'], {'misses_flagged': 1, 'misses': 1, 'passes_flagged': 0,
                                                               'passes': 2})  # arm a: pass, miss, bad
        self.assertEqual(readings['opus/all']['overall']['misses_flagged'], 0)  # Opus's test only used its own API
        bound = report['summary']["reference_bound (an upper bound a host can't reach)"]
        self.assertEqual(bound['any/all']['overall']['misses_flagged'], 1)
        self.assertEqual(readings['any/all']['leave_one_task_out']['synthetic']['misses'], 0)
        miss = next(r for r in report['rows'] if r['key'] == 'miss')
        self.assertAlmostEqual(miss['peer_cost']['same_arm'], (.1 + .08) / 2)
        self.assertEqual(report['summary']['price']['redo_usd'], .25)
        saved = json.loads((self.root/'out'/'peer_tests.json').read_text())
        self.assertNotIn('text', json.dumps(saved['pass']['files']))  # file contents stay out of the record
        with self.assertRaises(FileExistsError):
            mx.crosscheck(self.trials, {'synthetic': self.task}, self.root/'out', PYTHON, lambda task: self.repo)

    def test_the_estimate_counts_the_runs_and_times_one_peer_phase_per_task(self):
        plan = mx.estimate(self.trials, {'synthetic': self.task}, PYTHON, lambda task: self.repo, self.root/'est', jobs=2)
        self.assertEqual((plan['trials'], plan['misses'], plan['tasks'], plan['pytest_runs']), (4, 1, 1, 12))
        self.assertEqual(plan['peers_per_trial'], {'synthetic': 3})
        timed = plan['timing_per_task']['synthetic']
        self.assertEqual((timed['trials'], timed['error']), (4, None))
        self.assertGreater(timed['sample_tests_kept'], 0)  # the largest diff (the invented-API one) carries a test
        self.assertGreater(plan['estimated_seconds'], 0)

    def test_the_peer_sample_is_seeded_and_capped(self):
        peers = [{'key': f'p{i}'} for i in range(10)]
        one = mx.sample_peers({'key': 'a'}, peers, 4, seed=0)
        self.assertEqual(one, mx.sample_peers({'key': 'a'}, peers, 4, seed=0))
        self.assertEqual(len(one), 4)
        self.assertEqual(len(mx.sample_peers({'key': 'a'}, peers, 16, seed=0)), 10)



class CliTests(unittest.TestCase):
    def test_the_cli_needs_the_dev_runs(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(mx, 'dev_trials', return_value=[]), \
                mock.patch('sys.argv', ['miss_crosscheck', 'run', '--out', str(Path(tmp)/'x')]):
            with self.assertRaises(SystemExit) as stop:
                mx.main()
            self.assertFalse((Path(tmp)/'x').exists())
        self.assertIn('dev split', str(stop.exception))


if __name__ == '__main__':
    unittest.main()
