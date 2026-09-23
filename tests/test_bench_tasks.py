import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from modelpilot import bench_tasks
from modelpilot.bench_tasks import (candidates, check_lock, clean_env, grade, make_splits, parse_results,
                                     run_tests, validate, workspace)

ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t'}
BUGGY = 'def last(items):\n    return items[0]\n'
FIXED = 'def last(items):\n    return items[-1]\n'
OLD_TEST = 'import unittest\nfrom pkg import last\n\nclass T(unittest.TestCase):\n    def test_one(self):\n        self.assertEqual(last([1]), 1)\n'
NEW_TEST = OLD_TEST + '\n    def test_many(self):\n        self.assertEqual(last([1, 2]), 2)\n'


class BenchTaskTests(unittest.TestCase):
    """A synthetic two-commit repository stands in for a mined one."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root/'repo'
        (self.repo/'pkg').mkdir(parents=True)
        (self.repo/'tests').mkdir()
        (self.repo/'tests'/'__init__.py').write_text('')
        self.commit('pkg/__init__.py', BUGGY, 'tests/test_pkg.py', OLD_TEST, init=True)
        self.base = self.head()
        self.commit('pkg/__init__.py', FIXED, 'tests/test_pkg.py', NEW_TEST)
        self.reference = self.head()
        self.task = {'id': 'synthetic', 'repo': 'repo', 'url': 'local', 'license': 'MIT', 'type': 'bug_fix',
                     'base': self.base, 'reference': self.reference, 'instruction': 'Fix last().',
                     'test_dir': 'tests', 'hidden_tests': ['tests/test_pkg.py'], 'source': 'synthetic',
                     'hidden_command': ['{python}', '-m', 'unittest', '-q', 'tests.test_pkg'],
                     'suite_command': ['{python}', '-m', 'unittest', 'discover', '-q', '-s', 'tests', '-t', '.']}

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.repo, env=dict(ENV, PATH='/usr/bin:/bin'),
                              capture_output=True, text=True, check=True).stdout

    def commit(self, *files, init=False):
        if init:
            self.git('init', '-q')
        for path, text in zip(files[::2], files[1::2]):
            (self.repo/path).write_text(text)
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'Fix last() for longer lists' if not init else 'Initial')

    def head(self):
        return self.git('rev-parse', 'HEAD').strip()

    def test_candidates_find_the_fix_commit(self):
        found = candidates(self.repo, 'pkg/', 'tests/', since='2000-01-01')
        self.assertEqual([c['commit'] for c in found], [self.reference])
        self.assertEqual(found[0]['test_files'], ['tests/test_pkg.py'])

    def test_valid_task_fails_on_base_and_passes_on_reference(self):
        result = validate(self.task, self.repo, sys.executable, self.root/'scratch', repeats=2)
        self.assertTrue(result['valid'], result)
        self.assertEqual(result['base_failing_tests'], ['test_many (tests.test_pkg.T)'])

    def test_workspace_has_the_base_tree_and_no_history(self):
        work = workspace(self.task, self.repo, self.root/'work')
        self.assertEqual((work/'pkg'/'__init__.py').read_text(), BUGGY)
        log = subprocess.run(['git', 'log', '--format=%H'], cwd=work, capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(len(log), 1)
        self.assertNotIn(self.reference, log)
        self.assertNotIn(NEW_TEST, (work/'tests'/'test_pkg.py').read_text())

    def test_grader_restores_tests_so_editing_them_cannot_pass(self):
        work = workspace(self.task, self.repo, self.root/'work')
        (work/'tests'/'test_pkg.py').write_text('import unittest\n')  # agent guts the tests
        self.assertFalse(grade(self.task, work, self.repo, sys.executable, self.root/'g1')['passed'])
        (work/'pkg'/'__init__.py').write_text(FIXED)
        result = grade(self.task, work, self.repo, sys.executable, self.root/'g2')
        self.assertEqual((result['passed'], result['reason']), (True, 'passed'))

    def test_usage_errors_are_not_counted_as_test_failures(self):
        work = workspace(self.task, self.repo, self.root/'work')
        broken = run_tests(['{python}', '-m', 'unittest', '-q', 'discover', '-s', 'tests'], work, sys.executable)
        self.assertEqual((broken['exit_code'], broken['tests_failed'], broken['tests_run']), (2, False, 0))
        bad = dict(self.task, suite_command=['{python}', '-m', 'unittest', '-q', 'discover', '-s', 'tests'])
        self.assertFalse(validate(bad, self.repo, sys.executable, self.root/'scratch', repeats=1)['valid'])

    def test_grading_does_not_pass_provider_credentials(self):
        work = workspace(self.task, self.repo, self.root/'work')
        (work/'tests'/'test_env.py').write_text('')
        probe = ['{python}', '-c', 'import os; print("KEY" if any(k.startswith("ANTHROPIC") for k in os.environ) else "CLEAN")']
        import os
        os.environ['ANTHROPIC_API_KEY'] = 'sk-ant-should-not-leak'
        try:
            self.assertIn('CLEAN', run_tests(probe, work, sys.executable)['output_tail'])
        finally:
            del os.environ['ANTHROPIC_API_KEY']

    def test_committed_task_specs_are_complete(self):
        specs = sorted((Path(__file__).resolve().parents[1]/'bench'/'tasks').glob('*/task.json'))
        self.assertGreaterEqual(len(specs), 5)
        for path in specs:
            spec = json.loads(path.read_text())
            self.assertEqual(spec['id'], path.parent.name)
            self.assertNotEqual(spec['base'], spec['reference'])
            self.assertIn(spec['license'], ('MIT', 'BSD-2-Clause', 'BSD-3-Clause', 'Apache-2.0'))


PYTEST = ['{python}', '-m', 'pytest', '-q']


class ParseResultTests(unittest.TestCase):
    def test_pytest_failures_passes_and_collection_errors(self):
        failed = ('..F.\n=== short test summary info ===\nFAILED tests/test_a.py::test_x - AssertionError\n'
                  'took in 3s of setup\n1 failed, 3 passed in 0.12s\n')
        self.assertEqual(parse_results(PYTEST, 1, failed), (4, True, ['tests/test_a.py::test_x']))
        self.assertEqual(parse_results(PYTEST, 0, '....\n4 passed, 1 skipped in 0.05s\n'), (5, False, []))
        collection = ('ERROR tests/test_b.py\n!!! Interrupted: 1 error during collection !!!\n1 error in 0.20s\n')
        self.assertEqual(parse_results(PYTEST, 2, collection), (1, True, ['tests/test_b.py']))

    def test_colored_pytest_output_is_parsed(self):
        colored = '\x1b[31mFAILED\x1b[0m tests/t.py::x\n\x1b[31m1 failed\x1b[0m, \x1b[32m2 passed\x1b[0m in 0.1s\n'
        self.assertEqual(parse_results(PYTEST, 1, colored), (3, True, ['tests/t.py::x']))

    def test_pytest_usage_errors_and_empty_runs_are_not_failures(self):
        self.assertEqual(parse_results(PYTEST, 4, 'ERROR: usage: pytest [options]\n')[:2], (0, False))
        self.assertEqual(parse_results(PYTEST, 5, 'no tests ran in 0.01s\n')[:2], (0, False))

    def test_unittest_output(self):
        output = 'FAIL: test_x (tests.test_a.T)\n---\nRan 3 tests in 0.1s\n\nFAILED (failures=1)\n'
        self.assertEqual(parse_results(['{python}', '-m', 'unittest'], 1, output), (3, True, ['test_x (tests.test_a.T)']))

    def test_interpreter_directory_leads_path(self):
        env = clean_env('/opt/bench/venv/bin/python')
        self.assertTrue(env['PATH'].startswith('/opt/bench/venv/bin' + os.pathsep))
        self.assertFalse(any(k.startswith('ANTHROPIC') for k in env))



class SplitTests(unittest.TestCase):
    TASKS = [{'id': 'a1', 'repo': 'a'}, {'id': 'a2', 'repo': 'a'}, {'id': 'b1', 'repo': 'b'}]

    def test_split_is_by_repository_and_locks_the_final_set(self):
        splits = make_splits(self.TASKS, final_repos=('b',))
        self.assertEqual((sorted(splits['tuning']), sorted(splits['final'])), (['a1', 'a2'], ['b1']))
        self.assertEqual(check_lock(self.TASKS, splits), [])
        edited = [dict(t, instruction='changed') if t['id'] == 'b1' else t for t in self.TASKS]
        self.assertEqual(check_lock(edited, splits), ['b1'])
        tuned = [dict(t, instruction='changed') if t['id'] == 'a1' else t for t in self.TASKS]
        self.assertEqual(check_lock(tuned, splits), [])  # tuning tasks may still be edited

    @unittest.skipUnless(bench_tasks.SPLITS.exists(), 'no committed split yet')
    def test_committed_split_covers_every_task_and_final_specs_are_unchanged(self):
        splits = json.loads(bench_tasks.SPLITS.read_text())
        tasks = bench_tasks.load()
        self.assertEqual(sorted(list(splits['tuning']) + list(splits['final'])), sorted(t['id'] for t in tasks))
        self.assertFalse(set(splits['tuning']) & set(splits['final']))
        repos = {split: {t['repo'] for t in tasks if t['id'] in splits[split]} for split in ('tuning', 'final')}
        self.assertFalse(repos['tuning'] & repos['final'])
        self.assertEqual(check_lock(tasks, splits), [], 'a final task spec changed after the lock')
        self.assertTrue(all(t['instruction'] != 'TODO' for t in tasks))


if __name__ == '__main__': unittest.main()
