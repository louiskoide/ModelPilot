"""The miss detector's post-run facts and executed checks on synthetic inputs. $0, no model request; needs git."""
from pathlib import Path
import sys
import tempfile
import unittest
from modelpilot import miss_detector
from tests.test_exposure import CORE, INIT, SUITE, TESTS, git


def tool_use(id_, name, **input_):
    return {'type': 'tool_use', 'id': id_, 'name': name, 'input': input_}


def result(id_, text):
    return {'type': 'tool_result', 'tool_use_id': id_, 'content': text}


class FactTests(unittest.TestCase):
    def test_diff_facts_split_source_from_tests(self):
        diff = ('diff --git a/pkg/core.py b/pkg/core.py\n--- a/pkg/core.py\n+++ b/pkg/core.py\n@@ -1,2 +1,2 @@\n'
                '-old\n+new\n+more\n'
                'diff --git a/tests/test_core.py b/tests/test_core.py\n--- a/tests/test_core.py\n+++ b/tests/test_core.py\n'
                '@@ -1,1 +1,3 @@\n+    def test_new(self):\n+        pass\n')
        facts = miss_detector.diff_facts(diff, 'tests')
        self.assertEqual({k: facts[k] for k in ('source_files', 'test_files', 'source_added', 'source_deleted',
                                                'test_added', 'tests_defined', 'source_lines')},
                         {'source_files': 1, 'test_files': 1, 'source_added': 2, 'source_deleted': 1, 'test_added': 2,
                          'tests_defined': 1, 'source_lines': 3})

    def test_tool_facts_see_a_test_run_after_the_last_edit(self):
        messages = [
            {'role': 'assistant', 'content': [tool_use('a', 'Read', file_path='x.py'), tool_use('b', 'Edit')]},
            {'role': 'user', 'content': [result('a', 'x'), result('b', 'ok')]},
            {'role': 'assistant', 'content': [tool_use('c', 'Bash', command='python -m pytest -q')]},
            {'role': 'user', 'content': [result('c', '3 passed in 0.1s')]},
            {'role': 'assistant', 'content': [{'type': 'text', 'text': 'Done. Note that I assumed UTF-8.'}]}]
        facts = miss_detector.tool_facts(messages)
        self.assertEqual((facts['edits'], facts['reads'], facts['bash'], facts['test_calls']), (1, 1, 1, 1))
        self.assertTrue(facts['tested_after_last_edit'])
        self.assertTrue(facts['last_tests_passed'])
        self.assertEqual(miss_detector.final_text(messages), 'Done. Note that I assumed UTF-8.')

    def test_an_edit_after_the_last_test_run_is_untested(self):
        messages = [{'role': 'assistant', 'content': [tool_use('c', 'Bash', command='pytest')]},
                    {'role': 'user', 'content': [result('c', '1 failed, 2 passed in 0.1s')]},
                    {'role': 'assistant', 'content': [tool_use('d', 'Write')]}]
        facts = miss_detector.tool_facts(messages)
        self.assertFalse(facts['tested_after_last_edit'])
        self.assertEqual(facts['last_test_failures'], 1)


class SplitTests(unittest.TestCase):
    def test_the_holdout_needs_a_preregistration(self):
        argv = sys.argv
        with tempfile.TemporaryDirectory() as tmp:
            sys.argv = ['miss_detector', 'facts', '--split', 'holdout', '--out', str(Path(tmp)/'x.json')]
            try:
                with self.assertRaises(SystemExit), open('/dev/null', 'w') as null:
                    stderr, sys.stderr = sys.stderr, null
                    try:
                        miss_detector.main()
                    finally:
                        sys.stderr = stderr
            finally:
                sys.argv = argv
            self.assertFalse((Path(tmp)/'x.json').exists())


class ExecutedCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root/'repo'
        (self.repo/'pkg').mkdir(parents=True)
        (self.repo/'tests').mkdir()
        (self.repo/'pkg'/'__init__.py').write_text(INIT)
        (self.repo/'pkg'/'_core.py').write_text(CORE)
        (self.repo/'tests'/'__init__.py').write_text('')
        (self.repo/'tests'/'test_core.py').write_text(TESTS)
        git(self.repo, 'init', '-q')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', 'base')
        self.task = {'id': 'synthetic', 'repo': 'synthetic', 'base': git(self.repo, 'rev-parse', 'HEAD'),
                     'test_dir': 'tests', 'pythonpath': None, 'suite_command': SUITE}
        self.scratch = root/'scratch'
        self.work = root/'agent'
        git(root, 'clone', '-q', str(self.repo), str(self.work))

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, name):
        git(self.work, 'add', '-A')
        diff = self.scratch/f'{name}.diff'
        diff.parent.mkdir(parents=True, exist_ok=True)
        diff.write_text(git(self.work, 'diff', '--binary', '--cached') + '\n')
        out = miss_detector.executed_checks(self.task, sys.executable, self.scratch/name, diff, repo=self.repo)
        return out, miss_detector.check_facts(out)

    def test_a_new_feature_only_the_agents_new_test_runs(self):
        (self.work/'pkg'/'_core.py').write_text(CORE + '\n\ndef whisper(text):\n    return text.lower()\n')
        (self.work/'tests'/'test_core.py').write_text(
            TESTS + '\n    def test_whisper(self):\n        from pkg._core import whisper\n'
            '        self.assertEqual(whisper("A"), "a")\n')
        _, facts = self.check('new')
        self.assertEqual(facts, {'suite_failed': False, 'agent_tests_added_run': 1, 'base_suite_failed': False,
                                 'changed_executable': 2, 'coverage_all_tests': 0.5, 'coverage_agent_tests': 0.5,
                                 'coverage_base_tests': 0.0, 'check_error': None})  # the def line runs at import only

    def test_a_change_that_breaks_an_existing_test(self):
        (self.work/'pkg'/'_core.py').write_text(CORE.replace('+ "!"', '+ "?"'))
        out, facts = self.check('broken')
        self.assertTrue(facts['suite_failed'])
        self.assertTrue(facts['base_suite_failed'])
        self.assertEqual(facts['agent_tests_added_run'], 0)
        self.assertEqual(out['whole']['agent_tests_hitting'], 0)

    def test_a_change_without_source_lines(self):
        (self.work/'tests'/'test_core.py').write_text(TESTS + '\n    def test_more(self):\n        pass\n')
        out, facts = self.check('tests-only')
        self.assertEqual(out['error'], 'no_source_lines')
        self.assertEqual(facts['check_error'], 'no_source_lines')


if __name__ == '__main__':
    unittest.main()
