import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from modelpilot.bench_tasks import candidates, grade, run_tests, validate, workspace

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


if __name__ == '__main__': unittest.main()
