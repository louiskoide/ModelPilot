"""The behavioural vote (`behavior_vote`, `vote_plugin`): peer tests with their expected values dropped, run on every
fix's tree, and recorded Haiku 5.5 fixes voting. $0, no model request; the plugin and pipeline tests need git and an
interpreter with pytest (they skip without one)."""
import ast
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from modelpilot import behavior_vote as bv, bench_tasks, miss_crosscheck as mx
from tests.test_exposure import git


def has_pytest(python):
    return subprocess.run([python, '-c', 'import pytest'], capture_output=True).returncode == 0


PYTHON = next((p for p in (bench_tasks.interpreter(), sys.executable) if has_pytest(p)), None)


def transformed(source):
    return bv.probe_source(source)


class ProbeSourceTests(unittest.TestCase):
    def test_an_assertion_records_its_operands_and_drops_literal_expected_values(self):
        out = transformed('def test_a():\n    assert f(1) == 2\n    assert g() == h()\n    assert not k()\n'
                          '    assert isinstance(m(), int)\n    assert x is None, "message"\n')
        self.assertNotIn('assert ', out)
        self.assertIn('_cbv_v = f(1)\n', out)  # 2 is the test's expected value: dropped
        self.assertIn('_cbv_v = (g(), h())', out)  # neither side is a literal: both recorded
        self.assertIn('_cbv_v = k()\n', out)
        self.assertIn('_cbv_v = isinstance(m(), int)', out)
        self.assertIn('_cbv_v = x\n', out)
        self.assertNotIn('message', out)
        self.assertIn("_cbv_value('2:4', _cbv_v)", out)  # the site is the original line:column
        self.assertIn("_cbv_raised('2:4', _cbv_e)", out)

    def test_literals_include_containers_and_negative_numbers(self):
        for text in ('1', '-7', "('a', -1.5)", '[1, (2, None)]', "{'k': [1]}", '{1, 2}'):
            self.assertTrue(bv.is_literal(ast.parse(text, mode='eval').body), text)
        for text in ('x', '(x,)', 'f()', "{'k': v}", '-x'):
            self.assertFalse(bv.is_literal(ast.parse(text, mode='eval').body), text)

    def test_unittest_assertions_record_values_and_drop_messages(self):
        out = transformed('import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n'
                          '        self.assertEqual(f(), "x", msg="m")\n        self.assertTrue(g())\n'
                          '        self.assertIsInstance(e, ValueError)\n        self.assertIn(1, s())\n'
                          '        self.assertCountEqual(["b", "a"], t())\n        self.assertRegex(u(), "^a")\n')
        self.assertNotIn('self.assert', out)
        self.assertIn('_cbv_v = f()\n', out)
        self.assertIn('_cbv_v = g()\n', out)
        self.assertIn('_cbv_v = isinstance(e, ValueError)', out)
        self.assertIn('_cbv_v = s()\n', out)
        self.assertIn('_cbv_v = t()\n', out)
        self.assertIn('_cbv_v, sort=True)', out)
        self.assertIn('_cbv_v = u()\n', out)
        self.assertNotIn('"m"', out)
        self.assertNotIn("'^a'", out)

    def test_raises_warns_and_fail_become_recorders(self):
        out = transformed('import pytest\ndef test_a(self):\n'
                          '    with pytest.raises(ValueError, match="bad") as info:\n        f()\n'
                          '    pytest.raises(KeyError, g, 1, key=2)\n'
                          '    with self.assertRaises(TypeError) as cm:\n        h()\n'
                          '    self.assertRaisesRegex(TypeError, "x", h, 3)\n'
                          '    with pytest.warns(UserWarning), open("f") as fh:\n        w()\n'
                          '    with self.assertWarns(DeprecationWarning):\n        d()\n'
                          '    with pytest.deprecated_call():\n        d()\n'
                          '    pytest.fail("no")\n    self.fail("no")\n')
        self.assertIn("with _cbv_raises('3:9') as info:", out)
        self.assertIn("_cbv_raises_call('5:4', g, 1, key=2)", out)
        self.assertIn("with _cbv_raises('6:9') as cm:", out)
        self.assertIn("_cbv_raises_call('8:4', h, 3)", out)
        self.assertIn("with _cbv_warns('9:9'), open('f') as fh:", out)
        self.assertIn("with _cbv_warns('11:9'):", out)
        self.assertIn("with _cbv_warns('13:9'):", out)
        self.assertIn("_cbv_reached('15:4')", out)
        self.assertIn("_cbv_reached('16:4')", out)
        for gone in ('ValueError', 'KeyError', 'UserWarning', 'bad', 'pytest.fail', 'self.fail'):
            self.assertNotIn(gone, out)

    def test_the_recorder_import_follows_the_docstring_and_future_imports(self):
        out = transformed('"""Doc."""\nfrom __future__ import annotations\nimport os\n\ndef test_a():\n    assert os\n')
        lines = out.splitlines()
        self.assertEqual(lines[0], '"""Doc."""')
        self.assertEqual(lines[1], 'from __future__ import annotations')
        self.assertTrue(lines[2].startswith('from vote_plugin import _cbv_value'))
        self.assertIsNone(transformed('def broken(:\n'))

    def test_a_walrus_in_an_assertion_still_binds_in_the_test(self):
        out = transformed('def test_a():\n    assert (m := f()) is not None\n    assert m.x == 1\n')
        self.assertIn('_cbv_v = (m := f())', out)
        compile(out, 'probe.py', 'exec')


class VoteTests(unittest.TestCase):
    OK = ['ok', [['1:0', 'value', 'aa']], []]
    OTHER = ['ok', [['1:0', 'value', 'bb']], []]

    def test_the_target_is_flagged_when_it_differs_from_the_voters_majority(self):
        voters = [{'p': self.OK}, {'p': self.OK}, {'p': self.OTHER}]
        self.assertTrue(bv.vote({'p': self.OTHER}, voters)['flagged'])
        self.assertFalse(bv.vote({'p': self.OK}, voters)['flagged'])

    def test_no_majority_no_vote(self):
        result = bv.vote({'p': self.OK}, [{'p': self.OK}, {'p': self.OTHER}])
        self.assertEqual((result['usable'], result['voting'], result['flagged']), (1, 0, False))

    def test_a_probe_a_voter_doesnt_run_cleanly_is_not_used(self):
        for bad in (['error:call:TypeError', [], []], ['skipped', [], []], ['assertion', [], []], None):
            voters = [{'p': self.OK}, {'p': self.OK}, {'p': bad} if bad else {}]
            result = bv.vote({'p': self.OTHER}, voters)
            self.assertEqual((result['usable'], result['flagged']), (0, False), bad)

    def test_the_targets_crash_or_missing_test_is_a_difference_but_its_assertion_is_unreadable(self):
        voters = [{'p': self.OK}, {'p': self.OK}]
        self.assertTrue(bv.vote({'p': ['call:TypeError', [], []]}, voters)['flagged'])
        self.assertTrue(bv.vote({}, voters)['flagged'])
        self.assertEqual(bv.vote({'p': ['assertion', [], []]}, voters)['usable'], 0)

    def test_unstable_and_unselected_probes_are_left_out(self):
        voters = [{'p': self.OK, 'q': self.OK}, {'p': self.OK, 'q': self.OK}]
        target = {'p': self.OTHER, 'q': self.OTHER}
        self.assertEqual(bv.vote(target, voters, unstable={'p'})['divergent'], ['q'])
        self.assertEqual(bv.vote(target, voters, probes={'p'})['divergent'], ['p'])
        self.assertEqual(bv.unstable_probes([{'p': self.OK, 'q': self.OK}], [{'p': self.OK, 'q': self.OTHER}]), {'q'})

    def test_warnings_are_part_of_a_reading(self):
        warned = ['ok', [['1:0', 'value', 'aa']], ['DeprecationWarning']]
        self.assertTrue(bv.vote({'p': warned}, [{'p': self.OK}, {'p': self.OK}])['flagged'])


class StatisticsTests(unittest.TestCase):
    def test_within_task_null_is_exact(self):
        # One task: 4 trials, 2 misses, 2 flags. P(both flags on misses) = 1/6.
        rows = [('t', True, True), ('t', True, True), ('t', False, False), ('t', False, False)]
        null = bv.within_task(rows)
        self.assertEqual(null['misses_flagged'], 2)
        self.assertAlmostEqual(null['p'], 1 / 6)
        self.assertEqual(null['expected'], 1.0)
        # A task whose every trial is flagged carries no evidence: p 1.
        self.assertEqual(bv.within_task([('u', True, True), ('u', False, True)])['p'], 1.0)

    def test_the_clustered_bound_resamples_tasks(self):
        rows = [('a', True, True)] * 5 + [('b', True, False)] * 5
        lower = bv.recall_lower(rows, resamples=2000)
        self.assertEqual(lower, 0.0)  # drawing b twice is likely enough
        self.assertEqual(bv.recall_lower([('a', True, True)] * 3), 1.0)
        self.assertIsNone(bv.recall_lower([('a', False, True)]))

    def test_the_verdict_needs_every_check(self):
        tasks = ['a', 'b', 'c', 'd']
        rows = [(t, True, True) for t in tasks for _ in range(4)] + [(t, False, False) for t in tasks for _ in range(10)]
        result = bv.verdict(rows)
        self.assertEqual(result['verdict'], 'pass')
        self.assertEqual(result['tasks_caught'], tasks)
        noisy = rows + [(t, False, True) for t in tasks for _ in range(3)]  # 12 of 52 passes flagged: 23%
        self.assertEqual(bv.verdict(noisy)['checks']['false_flag_rate'], False)
        self.assertEqual(bv.verdict(noisy)['verdict'], 'fail')
        one_task = [('a', True, True)] * 10 + [('a', False, False)] * 30
        self.assertEqual(bv.verdict(one_task)['checks']['tasks_caught'], False)


@unittest.skipUnless(PYTHON, 'needs an interpreter with pytest (the bench venv has one)')
class PluginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.tree = self.root/'tree'
        (self.tree/'pkg').mkdir(parents=True)
        (self.tree/'tests').mkdir()
        (self.tree/'pkg'/'__init__.py').write_text(
            'import warnings\n\n\ndef half(x):\n    if x == 3:\n        warnings.warn("odd", DeprecationWarning)\n'
            '    return x / 2\n\n\nclass Box:\n    pass\n')
        (self.tree/'tests'/'__init__.py').write_text('')
        (self.tree/'tests'/'helpers.py').write_text('def check(x):\n    assert x == 1\n')

    def tearDown(self):
        self.tmp.cleanup()

    def run_probe(self, files, select):
        plugin = bv.plugin_dir(self.root/'scratch')
        files = {p: bv.probe_source(t) for p, t in files.items()}
        task = {'id': 'synthetic', 'test_dir': 'tests', 'pythonpath': None}
        return bv.run_probes(self.tree, task, PYTHON, self.root/'run', files, select, plugin)

    def test_readings_statuses_and_normalization(self):
        source = ('import pytest\nfrom pkg import half, Box\nfrom tests.helpers import check\n\n\n'
                  'def test_values(tmp_path):\n    assert half(4) == 99\n    assert dict(b=1, a=2) == {}\n'
                  '    assert 0.1 + 0.2 == 0\n    assert Box() is None\n    assert str(tmp_path) == ""\n'
                  '    assert half("x") == 0\n\n\n'
                  'def test_raises_and_warns():\n    with pytest.raises(ValueError):\n        half(2)\n'
                  '    with pytest.raises(ZeroDivisionError):\n        1 / 0\n'
                  '    with pytest.warns(UserWarning):\n        half(3)\n\n\n'
                  'def test_helper_assertion():\n    check(2)\n\n\n'
                  'def test_crash():\n    half(None)\n\n\n'
                  '@pytest.mark.parametrize("x", [1, 2])\ndef test_param(x):\n    assert half(x) == 0\n\n\n'
                  'def test_not_selected():\n    1 / 0\n')
        readings = self.run_probe({'tests/test_p.py': source, 'tests/test_broken.py': 'import nowhere\n\ndef test_b():\n    pass\n'},
                                  [['tests/test_p.py', n] for n in ('test_values', 'test_raises_and_warns',
                                                                    'test_helper_assertion', 'test_crash', 'test_param')]
                                  + [['tests/test_broken.py', 'test_b']])
        probes = readings['probes']
        self.assertNotIn('tests/test_p.py::test_not_selected', probes)
        values = probes['tests/test_p.py::test_values']
        self.assertEqual(values['status'], 'ok')
        self.assertEqual([o[3] for o in values['obs']],
                         ['2', "dict{'a': 2, 'b': 1}", '0.3', values['obs'][3][3], "'<TMP>/test_values0'", 'TypeError'])
        self.assertIn(' at 0x?>', values['obs'][3][3])  # memory addresses normalized
        self.assertEqual([o[1] for o in values['obs']], ['value'] * 5 + ['raised'])
        self.assertEqual([o[0] for o in values['obs']][:2], ['7:4', '8:4'])
        raises = probes['tests/test_p.py::test_raises_and_warns']
        self.assertEqual([o[1:2] + o[3:] for o in raises['obs']],
                         [['raises', 'none'], ['raises', 'ZeroDivisionError'], ['warns', 'DeprecationWarning']])
        self.assertEqual(probes['tests/test_p.py::test_helper_assertion']['status'], 'assertion')
        self.assertEqual(probes['tests/test_p.py::test_crash']['status'], 'call:TypeError')
        self.assertEqual(sorted(k for k in probes if 'test_param' in k),
                         ['tests/test_p.py::test_param[1]', 'tests/test_p.py::test_param[2]'])
        self.assertEqual(readings['collect'], {'tests/test_broken.py': 'collect:ModuleNotFoundError'})
        signatures = bv.signatures(readings, [['tests/test_p.py', 'test_values'], ['tests/test_broken.py', 'test_b']])
        self.assertEqual(signatures['tests/test_broken.py::test_b'][0], 'collect:ModuleNotFoundError')

    def test_warnings_emitted_by_the_test_are_recorded(self):
        readings = self.run_probe({'tests/test_w.py': 'from pkg import half\n\ndef test_w():\n    half(3)\n'},
                                  [['tests/test_w.py', 'test_w']])
        self.assertEqual(readings['probes']['tests/test_w.py::test_w']['warnings'], ['DeprecationWarning'])

    def test_unittest_cases_run_as_probes(self):
        source = ('import unittest\nfrom pkg import half\n\n\nclass T(unittest.TestCase):\n'
                  '    def test_a(self):\n        self.assertEqual(half(2), 5)\n'
                  '        with self.assertRaises(TypeError) as cm:\n            half(None)\n'
                  '        self.assertIsInstance(cm.exception, KeyError)\n'
                  '        with self.assertWarns(DeprecationWarning) as w:\n            half(3)\n'
                  '        self.assertEqual(str(w.warning), "x")\n')
        readings = self.run_probe({'tests/test_u.py': source}, [['tests/test_u.py', 'T::test_a']])
        probe = readings['probes']['tests/test_u.py::T::test_a']
        self.assertEqual(probe['status'], 'ok')
        self.assertEqual([o[3] for o in probe['obs']], ['1', 'TypeError', 'False', 'DeprecationWarning', "'odd'"])


SHOUT = 'def shout(text):\n    return text.strip().upper() + "!"\n'
LEFT = 'def shout(text):\n    return text.lstrip().upper() + "!"\n'
LOUD = 'def shout(text):\n    return text.strip().upper() + "!!"\n'
BASE = 'def shout(text):\n    return text.upper() + "!"\n'


@unittest.skipUnless(PYTHON, 'needs an interpreter with pytest (the bench venv has one)')
class PipelineTests(unittest.TestCase):
    """A synthetic task: three Haiku fixes vote; a peer test's wrong expected value doesn't matter, only behaviour."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root/'repo'
        (self.repo/'pkg').mkdir(parents=True)
        (self.repo/'tests').mkdir()
        (self.repo/'pkg'/'__init__.py').write_text('from ._core import shout\n')
        (self.repo/'pkg'/'_core.py').write_text(BASE)
        (self.repo/'tests'/'__init__.py').write_text('')
        (self.repo/'tests'/'test_core.py').write_text('from pkg import shout\n\n\ndef test_word():\n'
                                                      '    assert shout("hi") == "HI!"\n')
        git(self.repo, 'init', '-q')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', 'base')
        base = git(self.repo, 'rev-parse', 'HEAD')
        (self.repo/'pkg'/'_core.py').write_text(SHOUT)
        git(self.repo, 'commit', '-q', '-am', 'reference')
        self.task = {'id': 'synthetic', 'repo': 'synthetic', 'base': base, 'reference': git(self.repo, 'rev-parse', 'HEAD'),
                     'test_dir': 'tests', 'pythonpath': None}
        strips = 'from pkg import shout\n\n\ndef test_strips():\n    assert shout(" hi ") == "HI!"\n'
        # The loud peer's test expects its own wrong output; as a probe only its input counts.
        loud = 'from pkg import shout\n\n\ndef test_loud():\n    assert shout("x") == "X!!"\n'
        self.trials = [
            self.trial('h1', bv.HAIKU, True, {'pkg/_core.py': SHOUT, 'tests/test_strips.py': strips}),
            self.trial('h2', bv.HAIKU, True, {'pkg/_core.py': SHOUT}),
            self.trial('h3', bv.HAIKU, True, {'pkg/_core.py': SHOUT}),
            self.trial('miss', 'claude-sonnet-5-5', False, {'pkg/_core.py': LEFT}),
            self.trial('pass', 'claude-sonnet-5-5', True, {'pkg/_core.py': SHOUT}),
            self.trial('loud', 'claude-sonnet-5-5', False, {'pkg/_core.py': LOUD, 'tests/test_loud.py': loud})]

    def tearDown(self):
        self.tmp.cleanup()

    def trial(self, name, model, strict, files):
        work = self.root/f'agent-{name}'
        git(self.root, 'clone', '-q', str(self.repo), str(work))
        git(work, 'checkout', '-q', self.task['base'])
        for path, text in files.items():
            (work/path).write_text(text)
        git(work, 'add', '-A')
        diff = self.root/'diffs'/f'{name}.diff'
        diff.parent.mkdir(exist_ok=True)
        diff.write_text(git(work, 'diff', '--binary', '--cached') + '\n')
        return {'key': name, 'run': 'bench-x', 'task': 'synthetic', 'arm': 'a' if model != bv.HAIKU else 'haiku-5.5',
                'model': model, 'trial': 0, 'strict_pass': strict, 'cost': 0.03, 'diff': diff}

    def recorded(self):
        out = {}
        for t in self.trials:
            found = mx.peer_tests(t, self.task, self.repo, PYTHON, self.root/'peer'/t['key'], reference=False)
            out[t['key']] = {'kept': found['kept'], 'files': {p: {'names': v['names'], 'sha256': v['sha256']}
                                                              for p, v in found['files'].items()}}
        return out

    def test_the_vote_flags_both_misses_and_no_pass(self):
        recorded = self.recorded()
        self.assertEqual(recorded['loud']['kept'], {'tests/test_loud.py': ['test_loud']})  # passes on its own tree
        report = bv.collect(self.trials, {'synthetic': self.task}, self.root/'out', PYTHON, lambda task: self.repo,
                            recorded, jobs=2)
        self.assertEqual(report['pool_errors'], {})
        self.assertEqual(report['probes_selected'], {'synthetic': 2})
        self.assertTrue(json.loads((self.root/'out'/'readings.json').read_text())['readings'])
        rows, summary = bv.score_report(report)
        by_key = {r['key']: r for r in rows}
        self.assertTrue(by_key['miss']['votes']['all']['flagged'])  # " hi " -> "HI !" against "HI!"
        self.assertTrue(by_key['loud']['votes']['all']['flagged'])  # "X!!" against "X!": its own test, its own reading
        self.assertFalse(by_key['pass']['votes']['all']['flagged'])
        self.assertEqual(by_key['h1']['voters'], 2)  # its own run left out
        self.assertEqual(by_key['miss']['voters'], 3)
        self.assertEqual(by_key['miss']['unstable_probes'], 0)
        self.assertEqual(by_key['miss']['voters_rerun'], 3)
        # Without its own test the loud fix still differs, on the strip probe: " hi " -> "HI!!" against "HI!".
        self.assertTrue(by_key['loud']['votes']['not_own']['flagged'])
        primary = summary['primary (pre-registered)']
        self.assertEqual((primary['misses_flagged'], primary['misses'], primary['passes_flagged'], primary['passes']),
                         (2, 2, 0, 4))

    def test_a_changed_test_file_since_the_crosscheck_is_an_error(self):
        recorded = self.recorded()
        recorded['h1']['files']['tests/test_strips.py']['sha256'] = '0' * 64
        found = bv.pool_of(self.trials[0], self.task, recorded, self.repo, self.root/'pool')
        self.assertEqual(found, {'error': 'changed_since_crosscheck:tests/test_strips.py'})


if __name__ == '__main__':
    unittest.main()
