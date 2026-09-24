import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from modelpilot.governor import Governor
from modelpilot.hooks import channel_declaration, handle

FIXTURES = Path(__file__).parent/'fixtures'/'hooks-2.1.280'
ROOT = Path(__file__).resolve().parents[1]


def payload(name, cwd, **changes):
    """A payload recorded from Claude Code 2.1.280, re-rooted at a test workspace."""
    text = (FIXTURES/f'{name}.json').read_text().replace('{cwd}', str(cwd)).replace('{config}', str(cwd))
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
        self.assertIsNone(self.context(output))
        self.assertEqual(len(self.gov.pending_changes()), 1)  # suggested, not applied

    def test_compaction_acknowledges_only_a_compact_only_plan(self):
        self.gov.queue_change('compact', True, self.rev)
        self.gov.queue_change('model', 'claude-opus-4-6', self.rev)
        self.gov.plan_rebase(self.rev, explicit_boundary=True)
        self.run_hook('SessionStart', source='compact')
        self.assertEqual(len(self.gov.pending_changes()), 2)  # the model change needs an explicit ack
        self.gov.acknowledge_rebase(self.gov.plan_rebase(self.rev, explicit_boundary=True)['plan_id'])
        self.gov.queue_change('compact', True, self.rev)
        self.gov.plan_rebase(self.rev, explicit_boundary=True)
        self.run_hook('SessionStart', source='startup')
        self.assertEqual(len(self.gov.pending_changes()), 1)
        self.run_hook('SessionStart', source='compact')
        self.assertEqual(self.gov.pending_changes(), [])

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
