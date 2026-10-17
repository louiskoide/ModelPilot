import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from modelpilot.governor import Governor
from modelpilot.hooks import REVIEW_AT_STOP, channel_declaration, handle

FIXTURES = Path(__file__).parent/'fixtures'/'hooks-2.1.280'
# A Bash call that runs unittest and passes, then one that fails: recorded from the pinned 2.1.284 against a loopback
# fake API (October 16), workspace paths as {cwd} and the interpreter as python3.
BASH_FIXTURES = Path(__file__).parent/'fixtures'/'hooks-2.1.284-bash'
ROOT = Path(__file__).resolve().parents[1]
SONNET_5M = {'output': 10, 'write': 2.5, 'read': .1}  # Sonnet 5.5, 5m writes


def payload(name, cwd, fixtures=FIXTURES, **changes):
    """A payload recorded from Claude Code 2.1.280 (or 2.1.284's Bash test runs), re-rooted at a test workspace."""
    text = (fixtures/f'{name}.json').read_text().replace('{cwd}', str(cwd)).replace('{config}', str(cwd))
    data = json.loads(text)
    data.update(changes)
    return data


class HookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.work = self.root/'work'
        self.work.mkdir()
        self.db = self.root/'state.db'
        self.now = [1000.]
        self.gov = Governor(self.db, 's', 1)
        self.task = self.gov.state.create('t', 'Report the first token')['id']
        self.rev = self.gov.state.claim(self.task, 1, 'client', seconds=3600)['revision']
        self.code = self.gov.declare_channel()
        self.env = {'MODELPILOT_DB': str(self.db), 'MODELPILOT_SESSION': 's', 'MODELPILOT_LIMIT_USD': '1',
                    'MODELPILOT_TASK': self.task, 'MODELPILOT_OWNER': 'client'}

    def tearDown(self):
        self.gov.close()
        self.tmp.cleanup()

    def run_hook(self, name, **changes):
        data = payload(name, self.work, **changes)
        return handle(data['hook_event_name'], data, self.env, clock=lambda: self.now[0])

    def context(self, output):
        return output.get('hookSpecificOutput', {}).get('additionalContext')

    def test_recorded_payloads_have_the_fields_hooks_rely_on(self):
        self.assertEqual(payload('SessionStart', self.work)['source'], 'startup')
        write = payload('PostToolUse-Write', self.work)
        self.assertEqual((write['tool_name'], write['tool_input']['file_path']), ('Write', f'{self.work}/a.txt'))
        failure = payload('PostToolUseFailure', self.work)
        self.assertEqual((failure['tool_name'], failure['is_interrupt']), ('Bash', False))
        self.assertIn('Exit code 3', failure['error'])
        self.assertEqual(payload('UserPromptSubmit', self.work)['cwd'], str(self.work))

    def test_correction_is_delivered_once_into_model_context_and_acknowledged(self):
        self.gov.state.correct(self.task, self.rev, 'Report the SECOND token instead')
        output = self.run_hook('PostToolUse-Read')
        self.assertEqual(output['hookSpecificOutput']['hookEventName'], 'PostToolUse')
        self.assertIn('Report the SECOND token instead', self.context(output))
        self.assertIn(f'revision {self.rev+1}', self.context(output))
        self.assertTrue(self.context(output).startswith(f'[ModelPilot ledger update, code {self.code}]'))
        self.assertNotIn('supersede', self.context(output).lower())  # no self-asserted authority
        self.assertEqual(self.gov.state.get(self.task)['ack_revision'], self.rev+1)
        self.assertIsNone(self.context(self.run_hook('PostToolUse-Read')))
        delivered = self.gov.journal('deliver_correction')
        self.assertEqual([e['payload']['revision'] for e in delivered], [self.rev+1])

    def test_channel_is_declared_once_per_session_and_kept_out_of_the_environment(self):
        self.assertEqual(self.gov.declare_channel(), self.code)  # stable for the session
        self.assertRegex(self.code, r'^[0-9A-F]{16}$')
        self.assertNotIn(self.code, json.dumps(self.env))
        declaration = channel_declaration(self.task, self.code)
        self.assertIn(f'[ModelPilot ledger update, code {self.code}]', declaration)
        self.assertIn(self.task, declaration)
        self.assertNotIn(self.code, json.dumps(self.gov.journal()))  # the journal never holds the code

    def test_undeclared_channel_delivers_nothing_and_tells_the_user(self):
        other = Governor(self.db, 'undeclared', 1)
        try:
            task = other.state.create('t', 'x')['id']
            rev = other.state.claim(task, 1, 'client', seconds=3600)['revision']
            other.state.correct(task, rev, 'Unannounced change')
            env = dict(self.env, MODELPILOT_SESSION='undeclared', MODELPILOT_TASK=task)
            data = payload('PostToolUse-Read', self.work)
            output = handle('PostToolUse', data, env, clock=lambda: self.now[0])
            self.assertIsNone(self.context(output))
            self.assertIn('no declared correction channel', output['systemMessage'])
            self.assertIsNone(other.state.get(task)['ack_revision'])
        finally:
            other.close()

    def test_correction_is_delivered_on_prompt_submit(self):
        self.gov.state.correct(self.task, self.rev, 'Use the new plan')
        output = self.run_hook('UserPromptSubmit')
        self.assertEqual(output['hookSpecificOutput']['hookEventName'], 'UserPromptSubmit')
        self.assertIn('Use the new plan', self.context(output))

    def test_written_file_is_observed_with_the_hash_on_disk(self):
        (self.work/'a.txt').write_text('ON DISK\n')  # differs from the recorded tool_input content
        self.run_hook('PostToolUse-Write')
        events = self.gov.db.execute("SELECT payload FROM events WHERE task=? AND kind='observation'", (self.task,)).fetchall()
        self.assertEqual([json.loads(e['payload']) for e in events],
                         [{'file': 'a.txt', 'content_hash': hashlib.sha256(b'ON DISK\n').hexdigest()}])
        self.assertEqual(len(self.gov.journal('stuck')), 1)

    def test_files_outside_the_workspace_or_missing_are_not_observed(self):
        outside = self.root/'outside.txt'
        outside.write_text('x')
        for path in (str(outside), f'{self.work}/missing.txt', f'{self.work}/../outside.txt'):
            data = payload('PostToolUse-Write', self.work)
            data['tool_input'] = dict(data['tool_input'], file_path=path)
            handle('PostToolUse', data, self.env, clock=lambda: self.now[0])
        self.assertEqual(self.gov.journal('stuck'), [])

    def test_repeated_failures_raise_a_user_only_notice_once(self):
        outputs = [self.run_hook('PostToolUseFailure') for _ in range(4)]
        self.assertNotIn('systemMessage', outputs[1])
        self.assertIn('increase_effort', outputs[2]['systemMessage'])
        self.assertIn('dry-run', outputs[2]['systemMessage'])
        self.assertIsNone(self.context(outputs[2]))  # recommendations never enter model context
        self.assertNotIn('systemMessage', outputs[3])  # same recommendation is not repeated
        self.assertEqual(self.gov.state.get(self.task)['level'], 0)  # nothing applied

    def observed(self):
        rows = self.gov.db.execute("SELECT payload FROM events WHERE task=? AND kind='observation' ORDER BY seq",
                                   (self.task,)).fetchall()
        return [json.loads(r['payload']) for r in rows]

    def bash(self, name, **changes):
        data = payload(name, self.work, BASH_FIXTURES, **changes)
        return handle(data['hook_event_name'], data, self.env, clock=lambda: self.now[0])

    def test_the_agents_bash_test_runs_are_observed_as_the_agents(self):
        passed, failed = payload('PostToolUse-Bash', self.work, BASH_FIXTURES), payload('PostToolUseFailure-Bash', self.work, BASH_FIXTURES)
        self.assertEqual((passed['tool_response']['stdout'][-2:], passed['tool_response']['stderr']), ('OK', ''))
        self.assertTrue(failed['error'].startswith('Exit code 1\n'))
        self.bash('PostToolUse-Bash')
        self.bash('PostToolUseFailure-Bash')
        first, second = self.observed()
        self.assertEqual(first, {'agent_suite': 'python3 -m unittest -q test_ok', 'agent_failures': 0})
        self.assertEqual({k: second.get(k) for k in ('agent_suite', 'agent_failures')},
                         {'agent_suite': 'python3 -m unittest test_bad', 'agent_failures': 1})
        self.assertNotIn('error_hash', second)  # the agent's run only, never also a scored error (heuristic-v3)
        self.assertRegex(second['agent_failure_hash'], '^[0-9a-f]{64}$')
        self.assertNotIn('agent_failure_hash', first)  # a passing run has no failure to compare
        self.assertFalse({'suite', 'failures'} & (set(first) | set(second)))  # never a host suite's result

    def test_other_bash_calls_and_runs_without_an_outcome_are_not_test_runs(self):
        response = payload('PostToolUse-Bash', self.work, BASH_FIXTURES)['tool_response']
        for command, stdout in (('ls -la', 'Ran 1 test in 0.000s\n\nOK'),  # not a test runner
                                ("cat <<'EOF' > t.sh\npython3 -m pytest -q\nEOF", '3 passed'),  # a here-document body
                                ('grep -rn pytest .', '3 passed'),
                                ('python3 -m pytest -q', 'collecting ...')):  # cut off: no summary
            self.bash('PostToolUse-Bash', tool_input={'command': command, 'description': 'd'},
                      tool_response=dict(response, stdout=stdout))
        self.assertEqual(self.observed(), [])
        self.run_hook('PostToolUseFailure')  # 2.1.280's failing Bash: exit 3, no test runner
        self.assertEqual(list(self.observed()[0]), ['error_hash'])

    def test_a_test_run_failing_alike_on_new_paths_is_observed_without_a_notice(self):
        error = payload('PostToolUseFailure-Bash', self.work, BASH_FIXTURES)['error']
        outputs = [self.bash('PostToolUseFailure-Bash', error=error.replace(str(self.work), f'{self.work}/pytest-{n}')
                             .replace('0.000s', f'0.00{n}s')) for n in range(3)]
        self.assertTrue(all('systemMessage' not in o for o in outputs))  # v2 escalated here, but not when piped (exit 0)
        result = self.gov.state.recommend(self.task)
        self.assertEqual({s: result['signals'][s] for s in ('repeated_error', 'stalled_agent_tests', 'repeated_agent_failure')},
                         {'repeated_error': False, 'stalled_agent_tests': True, 'repeated_agent_failure': True})
        self.assertEqual(result['score'], 0)

    def test_another_failure_repeated_on_new_temporary_paths_is_one_repeated_error(self):
        check = {'command': 'python3 check.py', 'description': 'check'}
        error = ('Exit code 1\nvalue {n}\nTraceback (most recent call last):\n  File "{d}/check.py", line {n}, in <module>\n'
                 "    assert parse('1,000') == 1000\nAssertionError: got None\n")
        outputs = [self.bash('PostToolUseFailure-Bash', tool_input=check, error=error.format(d=f'{self.work}/tmp{n}', n=n + 3))
                   for n in range(3)]
        self.assertNotIn('systemMessage', outputs[1])
        self.assertIn("stuck signals (repeated_error) suggest 'increase_effort'", outputs[2]['systemMessage'])

    def test_stalled_agent_runs_alone_are_journaled_without_a_notice(self):
        response = payload('PostToolUse-Bash', self.work, BASH_FIXTURES)['tool_response']
        failing = dict(response, stdout='FAIL: test_b (test_bad.T.test_b)\n\nRan 2 tests in 0.001s\n\nFAILED (failures=1)')
        outputs = [self.bash('PostToolUse-Bash', tool_response=failing) for _ in range(3)]  # through | tail: exit 0
        self.assertTrue(all('systemMessage' not in o for o in outputs))
        stuck = self.gov.journal('stuck')[-1]['payload']
        self.assertEqual((stuck['signals']['stalled_agent_tests'], stuck['recommendation']), (True, 'hold'))

    def repo(self, files):
        """self.work as a one-commit repository, as a bench workspace is."""
        for name, text in files.items():
            (self.work/name).write_text(text)
        for args in (['init', '-q'], ['add', '-A'], ['commit', '-q', '-m', 'base']):
            subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@localhost', *args], cwd=self.work,
                           check=True, capture_output=True)

    def shell(self, command='true'):
        """A Bash call that ran no test, as PostToolUse sends it."""
        return self.bash('PostToolUse-Bash', tool_input={'command': command, 'description': 'd'},
                         tool_response={'stdout': '', 'stderr': '', 'interrupted': False, 'isImage': False})

    @unittest.skipUnless(shutil.which('git'), 'needs git')
    def test_files_a_bash_call_changes_are_observed_as_file_evidence(self):
        self.repo({'a.py': 'x = 1\n', '.gitignore': 'build/\n'})
        digest = lambda text: hashlib.sha256(text.encode()).hexdigest()
        (self.work/'a.py').write_text('x = 2\n')
        (self.work/'new.py').write_text('n\n')
        for ignored in ('build/out.py', 'venv/lib/site.py'):  # ignored, and a new directory: never hashed
            (self.work/ignored).parent.mkdir(parents=True)
            (self.work/ignored).write_text('skip\n')
        (self.root/'outside.py').write_text('x\n')
        self.shell("sed -i '' 's/1/2/' a.py")
        self.assertEqual(self.observed(), [{'file': 'a.py', 'content_hash': digest('x = 2\n')},
                                           {'file': 'new.py', 'content_hash': digest('n\n')}])
        self.shell('ls')
        self.assertEqual(len(self.observed()), 2)  # nothing changed since it was last observed
        (self.work/'a.py').write_text('x = 1\n')  # back as at the base: git no longer lists it
        self.shell()
        self.assertEqual(self.observed()[2:], [{'file': 'a.py', 'content_hash': digest('x = 1\n')}])
        (self.work/'a.txt').write_text('ON DISK\n')
        self.run_hook('PostToolUse-Write')  # the Write tool's own evidence, then a Bash call: observed once
        self.shell()
        self.assertEqual([e['file'] for e in self.observed()[3:]], ['a.txt'])

    @unittest.skipUnless(shutil.which('git'), 'needs git')
    def test_edits_going_back_and_forth_through_bash_raise_edit_oscillation(self):
        self.repo({'a.py': 'x = 1\n'})
        outputs = []
        for text in ('x = 2\n', 'x = 3\n', 'x = 2\n', 'x = 3\n'):
            (self.work/'a.py').write_text(text)
            outputs.append(self.shell("python3 - <<'E'\nopen('a.py', 'w').write(...)\nE"))
        self.assertNotIn('systemMessage', outputs[2])
        self.assertIn("stuck signals (edit_oscillation) suggest 'increase_effort'", outputs[3]['systemMessage'])

    def test_bash_calls_outside_a_repository_observe_no_files(self):
        inside = subprocess.run(['git', 'rev-parse'], cwd=self.work, capture_output=True) if shutil.which('git') else None
        if inside and inside.returncode == 0:
            self.skipTest('the temporary directory is inside a repository')
        (self.work/'a.py').write_text('x\n')
        self.shell()
        self.assertEqual(self.observed(), [])

    def test_interrupted_tools_are_not_failures(self):
        for _ in range(3):
            self.run_hook('PostToolUseFailure', is_interrupt=True)
        self.assertEqual(self.gov.journal('stuck'), [])

    def test_cancelled_task_gets_a_stop_notice_once(self):
        self.gov.state.cancel(self.task, self.rev)
        output = self.run_hook('PostToolUse-Read')
        self.assertIn('cancelled', self.context(output))
        self.assertIn(self.code, self.context(output))
        self.assertIsNone(self.context(self.run_hook('PostToolUse-Read')))

    def test_expired_lease_delivers_nothing_and_tells_the_user(self):
        self.gov.state.correct(self.task, self.rev, 'Too late')
        with self.gov.db:
            self.gov.db.execute('UPDATE tasks SET lease_until=0 WHERE id=?', (self.task,))
        output = self.run_hook('PostToolUse-Read')
        self.assertIsNone(self.context(output))
        self.assertIn('lease', output['systemMessage'])
        self.assertIsNone(self.gov.state.get(self.task)['ack_revision'])

    def test_lease_is_renewed_while_the_client_is_active(self):
        with self.gov.db:
            self.gov.db.execute('UPDATE tasks SET lease_until=lease_until-3500 WHERE id=?', (self.task,))
        before = self.gov.state.get(self.task)['lease_until']
        self.run_hook('PostToolUse-Read')
        self.assertGreater(self.gov.state.get(self.task)['lease_until'], before + 3000)

    def test_idle_gap_surfaces_a_rebase_plan_to_the_user_only(self):
        self.gov.queue_change('model', 'claude-opus-4-6', self.rev)
        self.gov.observe_context('claude-sonnet-5-5', 4_000, SONNET_5M, '5m')  # the proxy saw an API-key session
        self.run_hook('Stop')
        self.now[0] += 100
        self.assertNotIn('systemMessage', self.run_hook('UserPromptSubmit'))
        self.run_hook('Stop')
        self.now[0] += 400
        output = self.run_hook('UserPromptSubmit')
        plan = self.gov.journal('plan_rebase')[-1]['payload']
        self.assertEqual((plan['action'], plan['trigger']), ('would_rebase', 'idle'))
        self.assertIn(plan['plan_id'], output['systemMessage'])
        self.assertIn('/model claude-opus-4-6', output['systemMessage'])
        self.assertIn('acknowledged once this session is seen running it', output['systemMessage'])
        self.assertIsNone(self.context(output))
        self.assertEqual(len(self.gov.pending_changes()), 1)  # suggested, not applied

    def test_an_idle_compaction_is_suggested_only_where_it_pays_unless_the_check_is_off(self):
        self.gov.queue_change('compact', True, self.rev)
        self.gov.observe_context('claude-sonnet-5-5', 4_000, SONNET_5M, '5m')
        self.gov.observe_context('claude-sonnet-5-5', 6_000, SONNET_5M, '5m')
        self.run_hook('Stop')
        self.now[0] += 400
        self.assertNotIn('systemMessage', self.run_hook('UserPromptSubmit'))
        plan = self.gov.journal('plan_rebase')[-1]['payload']
        self.assertEqual((plan['action'], plan['compaction_check']['verdict']), ('defer', 'does_not_pay'))
        self.run_hook('Stop')
        self.now[0] += 400
        self.env['MODELPILOT_COMPACTION_CHECK'] = 'off'
        self.assertIn('/compact', self.run_hook('UserPromptSubmit')['systemMessage'])

    def test_a_rebuild_waits_out_a_1h_entry_and_an_unknown_one(self):
        self.gov.queue_change('model', 'claude-opus-4-6', self.rev)
        self.run_hook('Stop')
        self.now[0] += 400
        self.assertNotIn('systemMessage', self.run_hook('UserPromptSubmit'))  # no proxy evidence: lifetime unknown
        self.gov.observe_context('claude-sonnet-5-5', 4_000, dict(SONNET_5M, write=4), '1h')  # a subscription session
        self.run_hook('Stop')
        self.now[0] += 600
        self.assertNotIn('systemMessage', self.run_hook('UserPromptSubmit'))  # a 1h entry is still read at 10 minutes
        self.run_hook('Stop')
        self.now[0] += 3_630
        self.assertIn('/model claude-opus-4-6', self.run_hook('UserPromptSubmit')['systemMessage'])

    def test_compaction_acknowledges_only_a_compact_only_plan(self):
        self.gov.queue_change('compact', True, self.rev)
        self.gov.queue_change('model', 'claude-opus-4-6', self.rev)
        self.gov.plan_rebase(self.rev, explicit_boundary=True)
        self.run_hook('SessionStart', source='compact')
        self.assertEqual(len(self.gov.pending_changes()), 2)  # the model change was never seen on the wire
        self.gov.acknowledge_rebase(self.gov.plan_rebase(self.rev, explicit_boundary=True)['plan_id'])
        self.gov.queue_change('compact', True, self.rev)
        self.gov.plan_rebase(self.rev, explicit_boundary=True)
        self.run_hook('SessionStart', source='startup')
        self.assertEqual(len(self.gov.pending_changes()), 1)
        self.run_hook('SessionStart', source='compact')
        self.assertEqual(self.gov.pending_changes(), [])

    def test_correction_issued_during_the_last_model_call_blocks_the_stop_once(self):
        self.gov.state.correct(self.task, self.rev, 'Report the SECOND token instead')
        output = self.run_hook('Stop')
        self.assertEqual(output['decision'], 'block')
        self.assertTrue(output['reason'].startswith(f'[ModelPilot ledger update, code {self.code}]'))
        self.assertIn('Report the SECOND token instead', output['reason'])
        self.assertNotIn('hookSpecificOutput', output)  # a Stop hook's additionalContext never reaches the model
        self.assertEqual(self.gov.state.get(self.task)['ack_revision'], self.rev+1)
        self.assertEqual([e['payload']['event'] for e in self.gov.journal('deliver_correction')], ['Stop'])
        # The client continues; its next stop has nothing new and ends the turn.
        self.assertNotIn('decision', self.run_hook('Stop', stop_hook_active=True))

    def test_stop_with_nothing_pending_is_not_blocked(self):
        self.assertEqual(self.run_hook('Stop'), {})
        self.gov.state.correct(self.task, self.rev, 'x')
        self.run_hook('PostToolUse-Read')  # delivered mid-turn already
        self.assertEqual(self.run_hook('Stop'), {})

    def test_stop_blocks_at_most_once_per_revision_even_if_acknowledgment_fails(self):
        self.gov.state.correct(self.task, self.rev, 'Use the new plan')
        with unittest.mock.patch.object(type(self.gov.state), 'acknowledge', side_effect=ValueError('stale')):
            first = self.run_hook('Stop')
            second = self.run_hook('Stop', stop_hook_active=True)
        self.assertEqual(first['decision'], 'block')
        self.assertNotIn('decision', second)  # never holds the client in a loop
        self.assertEqual(len(self.gov.journal('stop_block')), 1)

    def test_cancelled_task_ends_at_stop_without_another_model_call(self):
        self.gov.state.cancel(self.task, self.rev)
        output = self.run_hook('Stop')
        self.assertNotIn('decision', output)
        self.assertEqual(self.gov.journal('deliver_cancel'), [])

    def test_undeclared_channel_never_blocks_the_stop(self):
        other = Governor(self.db, 'undeclared', 1)
        try:
            task = other.state.create('t', 'x')['id']
            rev = other.state.claim(task, 1, 'client', seconds=3600)['revision']
            other.state.correct(task, rev, 'Unannounced change')
            env = dict(self.env, MODELPILOT_SESSION='undeclared', MODELPILOT_TASK=task)
            output = handle('Stop', payload('Stop', self.work), env, clock=lambda: self.now[0])
            self.assertNotIn('decision', output)
            self.assertIn('no declared correction channel', output['systemMessage'])
        finally:
            other.close()

    # The review at the agent's finish (MODELPILOT_REVIEW_AT_STOP, set by the ModelPilot arm when its policy reviews there).

    def reviewing(self):
        self.env[REVIEW_AT_STOP] = '1'

    def test_a_review_holds_the_finish_once_per_turn(self):
        self.reviewing()
        output = self.run_hook('Stop')
        self.assertEqual(output['decision'], 'block')
        self.assertTrue(output['reason'].startswith(f'[ModelPilot ledger update, code {self.code}] Task {self.task} is '
                                                    'unchanged.'))
        self.assertIn('the review follows this message under the same marker', output['reason'])
        held, = self.gov.journal('review_block')
        self.assertEqual((held['task'], held['revision']), (self.task, self.rev))
        self.assertEqual(self.gov.journal('client_stop'), [])  # held: the turn goes on
        # The client continues the turn; its next stop ends it, and a later turn's finish is held again.
        self.assertEqual(self.run_hook('Stop', stop_hook_active=True), {})
        self.assertEqual(len(self.gov.journal('client_stop')), 1)
        self.assertEqual(self.run_hook('Stop')['decision'], 'block')
        self.assertEqual(len(self.gov.journal('review_block')), 2)

    def test_without_the_flag_the_finish_is_never_held(self):
        self.assertEqual(self.run_hook('Stop'), {})
        self.assertEqual(self.gov.journal('review_block'), [])

    def test_a_pending_correction_goes_first_and_the_review_follows_it(self):
        self.reviewing()
        self.gov.state.correct(self.task, self.rev, 'Report the SECOND token instead')
        first = self.run_hook('Stop')
        self.assertIn('Report the SECOND token instead', first['reason'])
        self.assertEqual(self.gov.journal('review_block'), [])
        second = self.run_hook('Stop', stop_hook_active=True)
        self.assertIn('the review follows', second['reason'])
        self.assertEqual(self.gov.journal('review_block')[0]['revision'], self.rev + 1)
        self.assertEqual(self.run_hook('Stop', stop_hook_active=True), {})

    def test_no_review_once_the_limit_is_spent_or_cost_is_unknown(self):
        self.reviewing()
        self.gov.admit('mp-1', .5, self.task, self.rev, gate='spent')
        self.gov.settle('mp-1', 1.0)  # the continuation would be refused
        self.assertEqual(self.run_hook('Stop'), {})
        other = Governor(self.db, 'unknown', 1)
        try:
            task = other.state.create('t', 'x')['id']
            rev = other.state.claim(task, 1, 'client', seconds=3600)['revision']
            other.declare_channel()
            other.admit('mp-2', .1, task, rev, gate='spent')
            other.settle('mp-2', None)
            env = dict(self.env, MODELPILOT_SESSION='unknown', MODELPILOT_TASK=task)
            self.assertEqual(handle('Stop', payload('Stop', self.work), env, clock=lambda: self.now[0]), {})
        finally:
            other.close()
        self.assertEqual(self.gov.journal('review_block'), [])

    def test_no_review_after_the_policy_stopped_the_task(self):
        self.reviewing()
        self.gov.note('policy_stop', {'status': 'stop', 'reason': 'policy_stop:no_stronger_setting'}, self.task, self.rev)
        self.assertEqual(self.run_hook('Stop'), {})
        self.assertEqual(self.gov.journal('review_block'), [])

    def test_no_review_without_a_declared_channel_or_for_a_task_that_ended(self):
        self.reviewing()
        other = Governor(self.db, 'undeclared', 1)
        try:
            task = other.state.create('t', 'x')['id']
            other.state.claim(task, 1, 'client', seconds=3600)
            env = dict(self.env, MODELPILOT_SESSION='undeclared', MODELPILOT_TASK=task)
            self.assertNotIn('decision', handle('Stop', payload('Stop', self.work), env, clock=lambda: self.now[0]))
        finally:
            other.close()
        self.gov.state.cancel(self.task, self.rev)
        self.assertNotIn('decision', self.run_hook('Stop'))
        self.assertEqual(self.gov.journal('review_block'), [])

    def test_hooks_never_record_applied_actions(self):
        self.gov.state.correct(self.task, self.rev, 'x')
        (self.work/'a.txt').write_text('x')
        for name in ('SessionStart', 'UserPromptSubmit', 'PostToolUse-Write', 'PostToolUseFailure', 'Stop'):
            self.run_hook(name)
        entries = self.gov.journal()
        self.assertGreater(len(entries), 5)
        self.assertTrue(all(e['payload'].get('applied') is not True for e in entries))


class HookProcessTests(unittest.TestCase):
    """The hook command itself: always exits 0; errors are logged by type, never with stdin content."""
    def test_errors_exit_zero_and_log_no_payload_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            errors = Path(tmp)/'hook-errors.jsonl'
            env = dict(os.environ, MODELPILOT_DB=str(Path(tmp)/'state.db'), MODELPILOT_SESSION='s',
                       MODELPILOT_LIMIT_USD='1', MODELPILOT_TASK='missing-task', MODELPILOT_OWNER='client',
                       MODELPILOT_HOOK_ERRORS=str(errors))
            for stdin in ('{"hook_event_name":"PostToolUse","prompt":"SECRET_TEXT"}', 'not json SECRET_TEXT'):
                result = subprocess.run([sys.executable, '-m', 'modelpilot.hooks', 'PostToolUse'], input=stdin, env=env,
                                        cwd=ROOT, capture_output=True, text=True, timeout=30)
                self.assertEqual((result.returncode, result.stdout), (0, ''))
            logged = errors.read_text()
            self.assertEqual(len(logged.splitlines()), 2)
            self.assertNotIn('SECRET_TEXT', logged)
            self.assertIn('error_type', logged)

    def test_coordinator_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp)/'state.db'
            gov = Governor(db, 's', 1)
            task = gov.state.create('t', 'x')['id']
            rev = gov.state.claim(task, 1, 'client')['revision']
            gov.close()
            base = [sys.executable, '-m', 'modelpilot.hooks']
            flags = ['--db', str(db), '--session', 's', '--limit-usd', '1']
            run = lambda *args: json.loads(subprocess.run(base + list(args) + flags, cwd=ROOT, capture_output=True,
                                                         text=True, timeout=30, check=True).stdout)
            run('queue-change', '--kind', 'effort', '--value', '"high"', '--revision', str(rev))
            status = run('status')
            self.assertEqual([c['kind'] for c in status['pending_changes']], ['effort'])
            gov = Governor(db, 's', 1)
            plan = gov.plan_rebase(rev, explicit_boundary=True)['plan_id']
            gov.close()
            self.assertEqual(run('ack-rebase', plan)['pending'], [])


if __name__ == '__main__': unittest.main()
