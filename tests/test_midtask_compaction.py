import http.client
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock
from modelpilot import bench, bench_report, fixtures, midtask_compaction, proxy
from modelpilot.fixtures import fixture_server
from tests import test_bench_tasks as synthetic

S55, H55, O55 = 'claude-sonnet-5-5', 'claude-haiku-5-5', 'claude-opus-5-5'
CONTROL, SESSION, HAIKU = 'sonnet-5.5-low-concise', 'sonnet-5.5-low-concise-compact', 'sonnet-5.5-low-concise-compact-haiku'
SUMMARY = 'Summary: last() in pkg/__init__.py returns the wrong item; return items[-1], then run the tests.'


def compaction_body(model=S55, thinking_from=None):
    """A compaction request as Claude Code 2.1.284 sends it (checked at $0 against the fixture, October 10)."""
    assistant = [{'type': 'text', 'text': 'Plan: return items[-1].'}]
    if thinking_from:
        assistant.insert(0, {'type': 'thinking', 'thinking': 'from ' + thinking_from, 'signature': 'sig'})
    return {'model': model, 'max_tokens': 128000, 'stream': False, 'thinking': {'type': 'adaptive'},
            'output_config': {'effort': 'low'}, 'tools': [{'name': 'Read', 'input_schema': {'type': 'object'}}],
            'context_management': {'edits': [{'type': 'clear_thinking_20251015', 'keep': 'all'}]},
            'messages': [{'role': 'user', 'content': 'Fix last().'},
                         {'role': 'system', 'content': [{'type': 'text', 'text': 'client note'}]},
                         {'role': 'assistant', 'content': assistant},
                         {'role': 'user', 'content': proxy.COMPACTION_MARKER + '\n\nSummarize the conversation.'}]}


class StepTests(unittest.TestCase):
    def test_only_arms_with_a_compaction_entry_compact_between_the_turns(self):
        self.assertEqual(midtask_compaction.steps(bench.ARMS[CONTROL]), ['plan', 'fix'])
        self.assertEqual(midtask_compaction.steps(bench.ARMS[SESSION]), ['plan', 'compact', 'fix'])
        self.assertEqual(midtask_compaction.steps(bench.ARMS[HAIKU]), ['plan', 'compact', 'fix'])
        # The compaction arms differ from their control only in who writes the summary.
        for arm in (SESSION, HAIKU):
            self.assertEqual({k: v for k, v in bench.ARMS[arm].items() if k != 'compaction'}, bench.ARMS[CONTROL])
        self.assertEqual((bench.compaction_model(bench.ARMS[SESSION]), bench.compaction_model(bench.ARMS[HAIKU])),
                         (None, H55))

    def test_the_split_shape_runs_fixed_arms_back_to_back(self):
        with self.assertRaisesRegex(ValueError, 'fixed arms only'):
            midtask_compaction.SplitTrial({'id': 't'}, 'modelpilot', '/nonexistent', 'claude', None, 'http://127.0.0.1:1', {})
        with self.assertRaisesRegex(ValueError, 'back to back'):
            midtask_compaction.SplitTrial({'id': 't'}, CONTROL, '/nonexistent', 'claude', None, 'http://127.0.0.1:1', {},
                                          gap=330)
        with tempfile.TemporaryDirectory() as out, self.assertRaisesRegex(ValueError, 'fixed arms only'):
            bench.run_bench([{'id': 't'}], [CONTROL, 'modelpilot'], 1, 0, Path(out)/'run', 'claude', None,
                            'http://127.0.0.1:1', {}, client_version='2.1.284', shape='split')


class CheckTests(unittest.TestCase):
    def rows(self, *models, rewrite=None):
        return ([{'kind': 'messages', 'tool_count': 6, 'model': S55}] +
                [dict({'kind': 'messages', 'tool_count': 6, 'model': m, 'compaction': True},
                      **({'compaction_model': rewrite} if rewrite else {})) for m in models])

    def test_the_summary_must_come_from_the_arms_model(self):
        self.assertEqual(bench.compaction_check(self.rows(H55, rewrite={'model': H55, 'applied': True}), H55),
                         {'requested': H55, 'served': {H55: 1}, 'not_rewritten': [], 'applied': True})
        refused = bench.compaction_check(self.rows(S55, rewrite={'model': H55, 'applied': False, 'reason': 'gate'}), H55)
        self.assertEqual((refused['applied'], refused['not_rewritten']), (False, ['gate']))
        self.assertTrue(bench.compaction_check(self.rows(S55), S55)['applied'])  # 'session': the client's own model
        self.assertIsNone(bench.compaction_check(self.rows(), H55)['applied'])  # no compaction: the stop says why

    def test_a_control_that_compacts_fails_its_check(self):
        self.assertTrue(bench.compaction_check(self.rows(), None)['applied'])
        self.assertFalse(bench.compaction_check(self.rows(S55), None)['applied'])

    def test_a_failed_check_excludes_the_trial(self):
        self.assertEqual(bench_report.ineligible_reason({'compaction_check': {'applied': False}}), 'compaction_not_applied')
        self.assertIsNone(bench_report.ineligible_reason({'compaction_check': {'applied': None}}))
        self.assertIsNone(bench_report.ineligible_reason({'compaction_check': {'applied': True}}))


class ProxyRewriteTests(unittest.TestCase):
    """Plain HTTP through the proxy to the owned fixture: $0."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.log = Path(self.temp.name)/'log.jsonl'
        self.upstream = fixture_server()
        self.upstream.keep_bodies = True
        self.proxy = proxy.ProxyServer(('127.0.0.1', 0), f'http://127.0.0.1:{self.upstream.server_port}', self.log,
                                       bench.rates(), compaction_model=H55)
        self.threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (self.upstream, self.proxy)]
        for t in self.threads:
            t.start()

    def tearDown(self):
        for s in (self.proxy, self.upstream):
            s.shutdown()
            s.server_close()
        for t in self.threads:
            t.join()
        self.temp.cleanup()

    def send(self, body):
        conn = http.client.HTTPConnection('127.0.0.1', self.proxy.server_port, timeout=5)
        conn.request('POST', '/v1/messages', json.dumps(body).encode(),
                     {'Content-Type': 'application/json', 'x-api-key': 'sk-ant-offline-not-a-key'})
        conn.getresponse().read()
        conn.close()
        self.proxy.wait_idle(5)
        return json.loads(self.upstream.bodies[-1]), json.loads(self.log.read_text().splitlines()[-1])

    def test_only_the_compaction_request_goes_to_the_compaction_model(self):
        body = compaction_body(thinking_from=S55)
        sent, row = self.send(body)
        self.assertEqual((sent['model'], sent['output_config'], sent['max_tokens']), (H55, {'effort': 'low'}, 128000))
        self.assertEqual(sent['messages'], body['messages'])  # Sonnet 5.5 -> Haiku 5.5 history: verified, kept as is
        self.assertEqual((row['model'], row['compaction']), (H55, True))
        self.assertEqual(row['compaction_model'], {'model': H55, 'client_model': S55, 'applied': True})
        self.assertIn('client_request_sha256', row)
        self.assertIsNotNone(row['cost_usd'])  # priced as Haiku 5.5, the model that answered
        work = dict(body, messages=body['messages'][:3] + [{'role': 'user', 'content': 'Go on.'}])
        sent, row = self.send(work)
        self.assertEqual((sent['model'], row['model']), (S55, S55))
        self.assertNotIn('compaction_model', row)
        self.assertNotIn('compaction', row)

    def test_a_rewrite_the_thinking_gate_refuses_goes_as_sent_and_says_why(self):
        # Opus 5.5 -> Haiku 5.5 with thinking history is not probed (policy_actions.THINKING_HISTORY_VERIFIED).
        body = compaction_body(model=O55, thinking_from=O55)
        sent, row = self.send(body)
        self.assertEqual(sent, body)
        self.assertEqual(row['model'], O55)
        self.assertFalse(row['compaction_model']['applied'])
        self.assertIn('Thinking history', row['compaction_model']['reason'])
        self.assertNotIn('client_request_sha256', row)

    def test_the_compaction_model_must_be_priced_and_policy_free(self):
        with self.assertRaisesRegex(ValueError, 'priced model'):
            proxy.ProxyServer(('127.0.0.1', 0), 'http://127.0.0.1:1', Path(self.temp.name)/'x.jsonl', bench.rates(),
                              compaction_model='claude-unknown')
        with self.assertRaisesRegex(ValueError, 'without a policy'):
            proxy.ProxyServer(('127.0.0.1', 0), 'http://127.0.0.1:1', Path(self.temp.name)/'x.jsonl', bench.rates(),
                              policy=object(), compaction_model=H55)


def phased(plan, fix):
    """A scripted agent by phase: plan steps after the plan prompt, a summary for the compaction request, fix steps
    after the fix prompt, each counted from its own prompt (fixtures.scripted_response counts from the start)."""
    original = fixtures.scripted_response

    def respond(request, script):
        messages = request['messages']
        if proxy.compaction_request(request):
            return original(dict(request, messages=messages[:1]), [{'text': SUMMARY}])
        marker = midtask_compaction.FIX_PROMPT if midtask_compaction.FIX_PROMPT in json.dumps(messages) else 'two turns'
        start = max(i for i, m in enumerate(messages) if m['role'] == 'user' and marker in json.dumps(m))
        return original(dict(request, messages=messages[start:]), fix if marker != 'two turns' else plan)
    return mock.patch.object(fixtures, 'scripted_response', respond)


@unittest.skipUnless(shutil.which('claude'), 'Claude Code CLI not installed')
class OfflineSplitTests(unittest.TestCase):
    """Real client, fake key, scripted loopback upstream, synthetic repository: $0, no provider traffic."""
    def setUp(self):
        self.repo_case = synthetic.BenchTaskTests('test_valid_task_fails_on_base_and_passes_on_reference')
        self.repo_case.setUp()
        self.task = dict(self.repo_case.task, repo_path=str(self.repo_case.repo), pythonpath='.')
        self.upstream = fixture_server()
        self.upstream.keep_bodies = True
        self.upstream.script = [{'text': 'unused'}]  # replaced per phase by phased()
        self.thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.thread.start()
        self.out = self.repo_case.root/'bench'
        self.cli, self.version = bench.resolve_client(shutil.which('claude'))

    def tearDown(self):
        self.upstream.shutdown()
        self.upstream.server_close()
        self.thread.join()
        self.repo_case.tearDown()

    def run_split(self, arm, edit_in_plan=False):
        path = str(self.out/arm/'workspace'/'pkg'/'__init__.py')
        write = {'tool': 'Write', 'input': {'file_path': path, 'content': synthetic.FIXED}}
        plan = [{'tool': 'Read', 'input': {'file_path': path}}] + ([write] if edit_in_plan else []) + [
            {'text': 'Plan: return items[-1] in last(), then run the tests.'}]
        fix = [write, {'tool': 'Bash', 'input': {'command': 'python3 -m unittest -q', 'description': 'tests'}},
               {'text': 'Done.'}]
        self.upstream.bodies.clear()
        trial = midtask_compaction.SplitTrial(self.task, arm, self.out/arm, self.cli, 'sk-ant-offline-not-a-key',
                                              f'http://127.0.0.1:{self.upstream.server_port}', bench.rates(),
                                              python=sys.executable, max_turns=8, client_version=self.version)
        with phased(plan, fix):
            try:
                while trial.step():
                    pass
            finally:
                trial.close()
        bodies = [json.loads(b) for b in self.upstream.bodies if json.loads(b).get('tools')]
        return trial.record, bodies

    def test_the_haiku_arm_compacts_on_haiku_between_plan_and_fix(self):
        record, bodies = self.run_split(HAIKU)
        self.assertEqual([s['prompt'] for s in record['sessions']], ['plan', 'compact', 'fix'])
        self.assertEqual([s['stop'] for s in record['sessions']], ['success'] * 3)
        self.assertEqual((record['shape'], record['steps'], record['compaction']), ('split', ['plan', 'compact', 'fix'], H55))
        self.assertTrue(record['passed'], record.get('grade'))
        self.assertEqual(record['compaction_check'], {'requested': H55, 'served': {H55: 1}, 'not_rewritten': [],
                                                      'applied': True})
        self.assertFalse(record['plan_turn_edits']['edited'])
        # Only the compaction request went to Haiku, at the client's effort; the concise prompt stayed in it.
        compaction = [b for b in bodies if proxy.compaction_request(b)]
        self.assertEqual([(b['model'], b['output_config']) for b in compaction], [(H55, {'effort': 'low'})])
        self.assertEqual({b['model'] for b in bodies if not proxy.compaction_request(b)}, {S55})
        for check in ('effort_check', 'prompt_check'):
            self.assertTrue(record[check]['applied'], record[check])
        self.assertIsNone(bench_report.ineligible_reason(record))
        self.assertTrue(record['accounting']['tokens_match'], record['accounting'])
        # The premise: after compaction the fix turn has the summary, not the plan turn's conversation.
        fix = [b for b in bodies if midtask_compaction.FIX_PROMPT in json.dumps(b['messages'])]
        self.assertTrue(fix)
        self.assertTrue(all(SUMMARY in json.dumps(b['messages']) for b in fix))
        self.assertFalse(any('two turns' in json.dumps(b['messages']) for b in fix))
        rows = [json.loads(l) for l in (self.out/HAIKU/'observations.jsonl').read_text().splitlines()]
        turns = midtask_compaction.breakdown(record, rows, bench.rates())
        self.assertEqual(set(turns['turn_cost_usd']), {'plan', 'compact', 'fix'})
        self.assertEqual(turns['compaction_model'], {H55: 1})

    def test_the_session_arm_compacts_on_its_own_model_and_the_control_keeps_the_conversation(self):
        record, bodies = self.run_split(SESSION)
        self.assertEqual([s['prompt'] for s in record['sessions']], ['plan', 'compact', 'fix'])
        self.assertEqual(record['compaction_check']['served'], {S55: 1})
        self.assertTrue(record['compaction_check']['applied'])
        self.assertEqual({b['model'] for b in bodies}, {S55})
        record, bodies = self.run_split(CONTROL)
        self.assertEqual([s['prompt'] for s in record['sessions']], ['plan', 'fix'])
        self.assertTrue(record['passed'], record.get('grade'))
        self.assertEqual(record['compaction_check'], {'requested': None, 'served': {}, 'not_rewritten': [],
                                                      'applied': True})
        fix = [b for b in bodies if midtask_compaction.FIX_PROMPT in json.dumps(b['messages'])]
        self.assertTrue(fix and all('two turns' in json.dumps(b['messages']) for b in fix))

    def test_an_edit_during_the_plan_turn_is_recorded(self):
        record, _ = self.run_split(CONTROL, edit_in_plan=True)
        self.assertTrue(record['plan_turn_edits']['edited'])
        self.assertGreater(record['plan_turn_edits']['diff_bytes'], 0)
        self.assertTrue(record['passed'])  # recorded, not stopped: the trial is graded as the session left it


if __name__ == '__main__':
    unittest.main()
