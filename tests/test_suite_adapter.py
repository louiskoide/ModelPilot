"""The governor's test adapter on a tiny local suite: a pass, a failure, a timeout, a stale result after an edit, and a
revision fenced out by a correction; the Stop hook's opt-in run. $0, no model request, no network."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock
from modelpilot import suite_adapter as sa
from modelpilot.governor import Governor
from modelpilot.hooks import SUITE, handle

# A suite the test controls through files: it reads verdict.json and writes a JUnit report (or only exits), and can
# sleep or edit a watched file while it runs.
SUITE_SCRIPT = '''
import json, os, sys, time
verdict = json.load(open('verdict.json'))
time.sleep(verdict.get('sleep', 0))
if verdict.get('edit'):
    open('src/app.py', 'a').write('# edited during the run\\n')
if verdict.get('report', True):
    with open(verdict.get('junit', '.mp-junit.xml'), 'w') as f:
        f.write('<testsuites><testsuite name="s" tests="%d" failures="%d" errors="%d" skipped="%d"/></testsuites>'
                % (verdict['tests'], verdict['failures'], verdict.get('errors', 0), verdict.get('skipped', 0)))
open('env-seen.txt', 'w').write('ANTHROPIC_API_KEY' in os.environ and 'leaked' or 'clean')
sys.exit(verdict.get('exit', 1 if verdict['failures'] or verdict.get('errors') else 0))
'''


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.work = self.root/'work'
        (self.work/'src').mkdir(parents=True)
        (self.work/'src'/'app.py').write_text('def f():\n    return 1\n')
        (self.work/'run_suite.py').write_text(SUITE_SCRIPT)
        self.gov = Governor(self.root/'state.db', 's', 1)
        self.task = self.gov.state.create('t', 'Make f return 2')['id']
        self.rev = self.gov.state.claim(self.task, 1, 'client', seconds=3600)['revision']
        self.gov.state.acknowledge(self.task, self.rev, 'client')
        self.spec = sa.load_spec({'name': 'unit', 'command': [sys.executable, 'run_suite.py'], 'junit': '.mp-junit.xml',
                                  'timeout_seconds': 5, 'watch': ['src/**/*.py']})

    def tearDown(self):
        self.gov.close()
        self.tmp.cleanup()

    def verdict(self, **values):
        (self.work/'verdict.json').write_text(json.dumps(values))

    def suites(self):
        return self.gov.state.observations(self.task, self.rev, 'suite', limit=10)

    def test_a_pass_lands_with_its_counts_and_the_tree_it_ran_on(self):
        self.verdict(tests=3, failures=0, skipped=1)
        with mock.patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'sk-ant-must-not-reach-the-suite'}):
            result = sa.run(self.spec, self.work)
        self.assertEqual((result['outcome'], result['failures'], result['tests'], result['exit_code']), ('pass', 0, 3, 0))
        self.assertEqual((self.work/'env-seen.txt').read_text(), 'clean')  # no inherited credentials
        landed = sa.land(self.gov, self.task, self.rev, 'client', self.spec, self.work, result)
        self.assertTrue(landed['landed'])
        observed, = self.suites()
        self.assertEqual((observed['suite'], observed['suite_outcome'], observed['failures']), ('unit', 'pass', 0))
        self.assertEqual(observed['tree_hash'], sa.tree_hash(self.work, ['src/**/*.py']))
        self.assertEqual(self.gov.journal('suite_result')[-1]['payload']['applied'], False)

    def test_a_failure_lands_as_a_failure_with_failures_and_errors_counted(self):
        self.verdict(tests=4, failures=1, errors=1)
        result = sa.run(self.spec, self.work)
        self.assertEqual((result['outcome'], result['failures'], result['exit_code']), ('fail', 2, 1))
        self.assertTrue(sa.land(self.gov, self.task, self.rev, 'client', self.spec, self.work, result)['landed'])
        self.assertEqual(self.suites()[0]['failures'], 2)

    def test_a_hung_suite_is_unknown_never_a_pass(self):
        self.verdict(tests=1, failures=0, sleep=30)
        spec = dict(self.spec, timeout_seconds=.5)
        started = time.monotonic()
        result = sa.run(spec, self.work)
        self.assertLess(time.monotonic() - started, 10)  # killed, process group and all
        self.assertEqual((result['outcome'], result['reason'], result['failures']), ('unknown', 'timeout', None))
        self.assertTrue(sa.land(self.gov, self.task, self.rev, 'client', spec, self.work, result)['landed'])
        observed, = self.suites()
        self.assertEqual(observed['suite_outcome'], 'unknown')
        self.assertNotIn('failures', observed)  # an unknown run never feeds the stalled-tests signal

    def test_other_unknowns(self):
        cases = {'no_junit': dict(tests=1, failures=0, report=False), 'no_tests_ran': dict(tests=2, failures=0, skipped=2),
                 'exit_code_without_failures': dict(tests=2, failures=0, exit=4),
                 'files_changed_during_run': dict(tests=2, failures=0, edit=True)}
        for reason, verdict in cases.items():
            with self.subTest(reason):
                self.verdict(**verdict)
                result = sa.run(self.spec, self.work)
                self.assertEqual((result['outcome'], result['reason']), ('unknown', reason))
        (self.work/'.mp-junit.xml').write_text('<testsuites')  # a stale, broken report is deleted before the run
        self.verdict(tests=1, failures=0, report=False)
        self.assertEqual(sa.run(self.spec, self.work)['reason'], 'no_junit')

    def test_without_a_report_the_exit_status_decides_and_a_failure_has_no_count(self):
        spec = dict(self.spec, junit=None)
        self.verdict(tests=1, failures=0, report=False)
        self.assertEqual(sa.run(spec, self.work)['outcome'], 'pass')
        self.verdict(tests=1, failures=0, report=False, exit=3)
        result = sa.run(spec, self.work)
        self.assertEqual((result['outcome'], result['failures']), ('fail', None))
        sa.land(self.gov, self.task, self.rev, 'client', spec, self.work, result)
        self.assertNotIn('failures', self.suites()[0])

    def test_a_result_for_a_tree_edited_since_the_run_is_refused(self):
        self.verdict(tests=2, failures=0)
        result = sa.run(self.spec, self.work)
        (self.work/'src'/'app.py').write_text('def f():\n    return 2\n')  # the agent edits after the run started
        landed = sa.land(self.gov, self.task, self.rev, 'client', self.spec, self.work, result)
        self.assertEqual(landed, {'landed': False, 'reason': 'files_changed_since_run'})
        self.assertEqual(self.suites(), [])
        self.assertEqual(self.gov.journal('suite_refused')[-1]['payload']['refusal'], 'files_changed_since_run')

    def test_a_revision_fenced_out_by_a_correction_is_refused(self):
        self.verdict(tests=2, failures=1)
        result = sa.run(self.spec, self.work)
        self.gov.state.correct(self.task, self.rev, 'Make f return 3')  # corrected while the suite ran
        landed = sa.land(self.gov, self.task, self.rev, 'client', self.spec, self.work, result)
        self.assertEqual(landed, {'landed': False, 'reason': 'stale_revision'})
        self.assertEqual(self.suites(), [])
        self.assertEqual(self.gov.state.observations(self.task, self.rev + 1, 'suite'), [])  # nor on the new one

    def test_an_unchanged_tree_is_not_run_again(self):
        self.verdict(tests=2, failures=0)
        first = sa.run_and_land(self.gov, self.task, 'client', self.spec, self.work)
        self.assertTrue(first['landed'])
        self.assertEqual(sa.run_and_land(self.gov, self.task, 'client', self.spec, self.work)['skipped'], 'unchanged')
        (self.work/'src'/'app.py').write_text('def f():\n    return 2\n')
        self.assertTrue(sa.run_and_land(self.gov, self.task, 'client', self.spec, self.work)['landed'])

    def test_declarations_are_checked(self):
        good = {'name': 'unit', 'command': ['python3', '-m', 'pytest']}
        self.assertEqual(sa.load_spec(json.dumps(good))['watch'], ['**/*.py'])
        for bad in ({'name': 'unit', 'command': 'pytest -q'}, {'name': 'unit', 'command': []},
                    {'name': 'a b', 'command': ['x']}, dict(good, timeout_seconds=0), dict(good, timeout_seconds=601),
                    dict(good, cwd='../elsewhere'), dict(good, junit='/tmp/j.xml'), dict(good, watch=['../**']),
                    dict(good, shell=True), [good]):
            with self.assertRaises(ValueError, msg=bad):
                sa.load_spec(bad)


class LedgerFieldTests(unittest.TestCase):
    def test_suite_observations_are_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            gov = Governor(Path(tmp)/'state.db', 's', 1)
            try:
                task = gov.state.create('t', 'x')['id']
                rev = gov.state.claim(task, 1, 'client', seconds=3600)['revision']
                gov.state.acknowledge(task, rev, 'client')
                for bad in ({'suite': 'u', 'suite_outcome': 'passed'}, {'suite': 'u', 'suite_outcome': 'unknown', 'failures': 0},
                            {'suite': 'u', 'tree_hash': 1}):
                    with self.assertRaises(ValueError, msg=bad):
                        gov.state.observe(task, rev, 'client', bad)
                gov.state.observe(task, rev, 'client', {'suite': 'u', 'suite_outcome': 'fail', 'failures': 2, 'tree_hash': 'ab'})
            finally:
                gov.close()


class StopHookTests(unittest.TestCase):
    """MODELPILOT_SUITE set: the Stop hook that really ends a turn runs the suite once and lands it (observation only);
    unset, nothing runs; a blocked stop runs nothing."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.work = self.root/'work'
        (self.work/'src').mkdir(parents=True)
        (self.work/'src'/'app.py').write_text('x = 1\n')
        (self.work/'run_suite.py').write_text(SUITE_SCRIPT)
        (self.work/'verdict.json').write_text(json.dumps({'tests': 2, 'failures': 1}))
        self.db = self.root/'state.db'
        self.gov = Governor(self.db, 's', 1)
        self.task = self.gov.state.create('t', 'x')['id']
        self.rev = self.gov.state.claim(self.task, 1, 'client', seconds=3600)['revision']
        self.gov.state.acknowledge(self.task, self.rev, 'client')
        self.env = {'MODELPILOT_DB': str(self.db), 'MODELPILOT_SESSION': 's', 'MODELPILOT_LIMIT_USD': '1',
                    'MODELPILOT_TASK': self.task, 'MODELPILOT_OWNER': 'client'}
        self.spec = json.dumps({'name': 'unit', 'command': [sys.executable, 'run_suite.py'], 'junit': '.mp-junit.xml',
                                'timeout_seconds': 5, 'watch': ['src/**/*.py']})

    def tearDown(self):
        self.gov.close()
        self.tmp.cleanup()

    def stop(self, env):
        return handle('Stop', {'hook_event_name': 'Stop', 'session_id': 'c', 'cwd': str(self.work),
                               'stop_hook_active': False}, env)

    def test_off_by_default(self):
        self.assertEqual(self.stop(self.env), {})
        self.assertEqual(self.gov.state.observations(self.task, self.rev, 'suite'), [])
        self.assertFalse((self.work/'.mp-junit.xml').exists())

    def test_a_stop_runs_the_declared_suite_once_and_only_observes(self):
        env = dict(self.env, **{SUITE: self.spec})
        started = time.monotonic()
        output = self.stop(env)
        self.assertNotIn('decision', output)  # never blocks the stop
        observed, = self.gov.state.observations(self.task, self.rev, 'suite', limit=5)
        self.assertEqual((observed['suite_outcome'], observed['failures']), ('fail', 1))
        self.assertEqual(self.gov.state.get(self.task)['revision'], self.rev)  # nothing else changed
        self.assertLess(time.monotonic() - started, 5)
        self.stop(env)  # the same tree: not run again
        self.assertEqual(len(self.gov.state.observations(self.task, self.rev, 'suite', limit=5)), 1)

    def test_a_blocked_stop_runs_nothing(self):
        from modelpilot import hooks
        with mock.patch.object(hooks, 'stop_delivery', return_value=('a correction for the model', None)):
            output = self.stop(dict(self.env, **{SUITE: self.spec}))
        self.assertEqual(output['decision'], 'block')
        self.assertEqual(self.gov.state.observations(self.task, self.rev, 'suite'), [])
        self.assertFalse((self.work/'.mp-junit.xml').exists())

    def test_a_task_with_an_undelivered_correction_is_not_run(self):
        self.gov.state.correct(self.task, self.rev, 'y')  # pending: not yet acknowledged by this client
        from modelpilot import suite_adapter
        result = suite_adapter.run_and_land(self.gov, self.task, 'client', suite_adapter.load_spec(self.spec), self.work)
        self.assertEqual(result, {'skipped': 'stale_revision', 'revision': self.rev + 1})
        self.assertFalse((self.work/'.mp-junit.xml').exists())  # the suite never ran

    def test_the_coordinator_command_runs_it_too(self):
        import contextlib
        import io
        from modelpilot.hooks import coordinator
        (self.root/'suite.json').write_text(self.spec)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            coordinator(['run-suite', '--db', str(self.db), '--session', 's', '--limit-usd', '1', '--task', self.task,
                         '--owner', 'client', '--spec', str(self.root/'suite.json'), '--workspace', str(self.work)])
        result = json.loads(out.getvalue())
        self.assertTrue(result['landed'])
        self.assertEqual((result['result']['outcome'], result['result']['failures']), ('fail', 1))

    def test_a_bad_declaration_runs_nothing_and_is_journaled(self):
        output = self.stop(dict(self.env, **{SUITE: '{"name": "unit", "command": "pytest"}'}))
        self.assertNotIn('decision', output)
        self.assertEqual(self.gov.state.observations(self.task, self.rev, 'suite'), [])
        self.assertEqual(self.gov.journal('suite_refused')[-1]['payload']['refusal'], 'bad_declaration')


if __name__ == '__main__':
    unittest.main()
