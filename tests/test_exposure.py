"""Plan item 7's exposure measure on a synthetic repository: what a change touches, who refers to it and which
existing tests run it. $0, no model request; needs git."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from modelpilot import exposure

INIT = "from ._core import Box, shout\n\n__all__ = ['Box', 'shout']\n"
CORE = ('def shout(text):\n    return _upper(text) + "!"\n\n\n'
        'def _upper(text):\n    return text.upper()\n\n\n'
        'class Box:\n    def __init__(self, value):\n        self.value = value\n\n'
        '    def __eq__(self, other):\n        return self.value == other.value\n')
TESTS = ('import unittest\nfrom pkg import shout\n\n\nclass Shout(unittest.TestCase):\n'
         '    def test_word(self):\n        self.assertEqual(shout("hi"), "HI!")\n\n'
         '    def test_empty(self):\n        self.assertEqual(shout(""), "!")\n\n'
         '    def test_unrelated(self):\n        self.assertTrue(True)\n')
SUITE = ['{python}', '-m', 'unittest', 'discover', '-q', '-s', 'tests', '-t', '.']


def git(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@localhost', GIT_COMMITTER_NAME='t',
               GIT_COMMITTER_EMAIL='t@localhost')
    return subprocess.run(['git', *args], cwd=cwd, env=env, capture_output=True, text=True, check=True).stdout.strip()


class ParseDiffTests(unittest.TestCase):
    def test_hunk_lines_are_counted_not_guessed_from_their_first_character(self):
        diff = ('diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n'
                '@@ -3,2 +3,1 @@ def f():\n--- a removed line that looks like a header\n-x = 1\n+++ y = 2\n'
                '@@ -9 +8,0 @@\n-gone = True\n\\ No newline at end of file\n'
                'diff --git a/new.py b/new.py\nnew file mode 100644\n--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,2 @@\n+a\n+b\n'
                'diff --git a/old.py b/old.py\ndeleted file mode 100644\n--- a/old.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-a\n-b\n')
        self.assertEqual(exposure.parse_diff(diff), {
            'm.py': {'added': [3], 'deleted_at': [8], 'deleted': 3},
            'new.py': {'added': [1, 2], 'deleted_at': [], 'deleted': 0},
            'old.py': {'added': [], 'deleted_at': [], 'deleted': 2, 'deleted_file': True}})

    def test_definitions_module_names_and_privacy(self):
        defs = exposure.definitions(CORE)
        self.assertEqual([d['name'] for d in defs], ['shout', '_upper', 'Box', 'Box.__init__', 'Box.__eq__'])
        self.assertEqual(exposure.enclosing(defs, 11)['name'], 'Box.__init__')
        self.assertIsNone(exposure.enclosing(defs, 3))
        self.assertIsNone(exposure.definitions('def broken(:\n'))
        nested = exposure.definitions('class A:\n    def f(self):\n        def g():\n            pass\n\n'
                                      '    class B:\n        def h(self):\n            pass\n')
        self.assertEqual([d['name'] for d in nested], ['A', 'A.f', 'A.B', 'A.B.h'])  # g is part of A.f
        self.assertEqual(exposure.enclosing(nested, 4)['name'], 'A.f')
        self.assertEqual(exposure.module_name('src/pkg/_core.py', {'pythonpath': 'src'}), 'pkg._core')
        self.assertEqual(exposure.module_name('pkg/__init__.py', {'pythonpath': None}), 'pkg')
        self.assertIsNone(exposure.module_name('setup.py', {'pythonpath': 'src'}))
        self.assertEqual([exposure.is_private(n) for n in ('_upper', '__init__', 'shout', '__x')],
                         [True, False, False, True])


class ExposureTests(unittest.TestCase):
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
        base = git(self.repo, 'rev-parse', 'HEAD')
        (self.repo/'pkg'/'_core.py').write_text(CORE.replace('text.upper()', 'text.strip().upper()'))
        (self.repo/'tests'/'test_core.py').write_text(TESTS + '\n    def test_strip(self):\n'
                                                      '        self.assertEqual(shout(" a "), "A!")\n')
        git(self.repo, 'commit', '-qam', 'reference')
        self.task = {'id': 'synthetic', 'repo': 'synthetic', 'base': base, 'reference': git(self.repo, 'rev-parse', 'HEAD'),
                     'test_dir': 'tests', 'pythonpath': None, 'suite_command': SUITE}
        self.scratch = root/'scratch'

    def tearDown(self):
        self.tmp.cleanup()

    def measure(self, name, diff=None):
        return exposure.exposure(self.task, sys.executable, self.scratch/name, diff, repo=self.repo)

    def test_the_reference_fix_a_private_helper_every_shout_test_runs(self):
        out = self.measure('reference')
        self.assertEqual(out['files'], {'source': ['pkg/_core.py'], 'other': [], 'test_edits_dropped': ['tests/test_core.py']})
        [helper] = out['definitions']
        self.assertEqual((helper['name'], helper['import_path'], helper['public']), ('_upper', 'pkg._core._upper', False))
        self.assertEqual({k: helper['references'][k] for k in ('source', 'tests')}, {'source': 1, 'tests': 0})
        self.assertEqual(helper['coverage'], {'executable': 1, 'run_in_tests': 1, 'run_outside_tests_only': 0,
                                              'not_run': 0, 'tests': 2})
        self.assertEqual(out['coverage']['suite']['tests_run'], 3)  # the base's tests: the reference's new one dropped
        self.assertEqual(out['public_api'], [])
        self.assertEqual(out['summary']['changed_lines_run_in_tests'], 1.0)

    def test_a_fix_to_public_api_no_existing_test_runs(self):
        work = self.scratch/'agent'
        git(Path(self.tmp.name), 'clone', '-q', str(self.repo), str(work))
        git(work, 'checkout', '-q', self.task['base'])
        (work/'pkg'/'_core.py').write_text(
            'LIMIT = 3\n' + CORE.replace('self.value = value', 'self.value = int(value)')
            .replace('return self.value == other.value', 'same = self.value == other.value\n        return same'))
        (work/'tests'/'test_box.py').write_text('import unittest\nfrom pkg import Box\n\n\nclass B(unittest.TestCase):\n'
                                                '    def test_box(self):\n        self.assertEqual(Box("1"), Box(1))\n')
        git(work, 'add', '-A')
        diff = self.scratch/'agent.diff'
        diff.write_text(git(work, 'diff', '--binary', '--cached') + '\n')
        out = self.measure('fix', diff)
        self.assertEqual(out['files']['test_edits_dropped'], ['tests/test_box.py'])
        self.assertEqual(out['public_api'], ['pkg.Box.__eq__', 'pkg.Box.__init__'])
        by = {d['name']: d for d in out['definitions']}
        self.assertFalse(by['Box.__init__']['implicit'])
        self.assertTrue(by['Box.__eq__']['implicit'])  # runs on ==, so its class's references stand in
        self.assertEqual(by['Box.__init__']['references']['identifier'], 'Box')
        self.assertEqual(by['Box.__init__']['coverage']['tests'], 0)
        self.assertEqual(by['Box.__init__']['coverage']['not_run'], 1)
        self.assertEqual(by[None]['coverage'], {'executable': 1, 'run_in_tests': 0, 'run_outside_tests_only': 1,
                                                'not_run': 0})  # LIMIT runs at import only
        self.assertEqual(out['summary']['changed_lines_run_in_tests'], 0.0)
        self.assertEqual(out['summary']['tests_running_changed_definitions'], 0)

    def test_a_diff_that_does_not_apply_is_an_error(self):
        bad = self.scratch/'bad.diff'
        self.scratch.mkdir(parents=True)
        bad.write_text('diff --git a/pkg/_core.py b/pkg/_core.py\n--- a/pkg/_core.py\n+++ b/pkg/_core.py\n'
                       '@@ -1,1 +1,1 @@\n-nothing like this\n+x\n')
        self.assertEqual(self.measure('bad', bad), {'error': 'diff_did_not_apply'})


class LineTraceTests(unittest.TestCase):
    def test_lines_are_attributed_to_the_test_that_ran_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            (tree/'m.py').write_text('X = 1\n\n\ndef f(a):\n    if a:\n        return 1\n    return 2\n')
            (tree/'tests').mkdir()
            (tree/'tests'/'__init__.py').write_text('')
            (tree/'tests'/'test_m.py').write_text('import unittest\nimport m\n\n\nclass T(unittest.TestCase):\n'
                                                  '    def test_true(self):\n        m.f(True)\n')
            spec, out = tree/'spec.json', tree/'out.json'
            spec.write_text(json.dumps({'files': {str(tree/'m.py'): [1, 5, 6, 7]}}))
            run = subprocess.run([sys.executable, str(exposure.TRACER), str(spec), str(out), '-m', 'unittest',
                                  'discover', '-q', '-s', 'tests', '-t', '.'], cwd=tree, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            [traced] = json.loads(out.read_text())['files'].values()
        self.assertEqual(traced['hits'], {'1': [''], '5': ['tests.test_m.T.test_true'], '6': ['tests.test_m.T.test_true']})
        self.assertTrue({1, 4, 5, 6, 7} <= set(traced['executable']))
        self.assertNotIn(2, traced['executable'])


if __name__ == '__main__':
    unittest.main()
