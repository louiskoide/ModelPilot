import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from modelpilot import bench, fixtures, policy_actions, switch_policy
from modelpilot.bench_tools import OWNER, ToolServer, excerpt
from modelpilot.governor import Governor
from modelpilot.modelpilot_adapter import ModelPilotAdapter
from tests import test_bench_tasks as synthetic
from tests import test_bench
from tests.test_bench_jev import ACCOUNT_CATALOG, POLICY_TIERS

BROKEN = 'def last(items):\n    return None\n'
EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max']


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.repo_case = synthetic.BenchTaskTests('test_valid_task_fails_on_base_and_passes_on_reference')
        self.repo_case.setUp()
        root = self.repo_case.root
        self.work = synthetic.bench_tasks.workspace(self.repo_case.task, self.repo_case.repo, root/'work')
        for name in ('home', 'tmp'):
            (root/name).mkdir()
        self.gov = Governor(root/'ledger.sqlite3', 's', 1)
        self.task = self.gov.state.create('t', 'fix')['id']
        self.gov.state.claim(self.task, 1, OWNER, seconds=3600)
        self.server = ToolServer(self.gov, self.task, self.work, self.repo_case.task, sys.executable,
                                 root/'home', root/'tmp', threshold=256)
        self.server.dispatch({'jsonrpc': '2.0', 'id': 0, 'method': 'initialize', 'params': {}})

    def tearDown(self):
        self.gov.close()
        self.repo_case.tearDown()

    def call(self, name, **args):
        reply = self.server.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                      'params': {'name': name, 'arguments': args}})
        if 'error' in reply:
            return reply['error']
        return reply['result']['isError'], json.loads(reply['result']['content'][0]['text']) \
            if not reply['result']['isError'] else reply['result']['content'][0]['text']

    def test_excerpt_keeps_head_and_tail_within_threshold(self):
        text = 'A' * 1000 + 'MIDDLE' + 'Z' * 1000
        shown, truncated = excerpt(text, 256)
        self.assertTrue(truncated)
        self.assertTrue(shown.startswith('A' * 128) and shown.endswith('Z' * 128))
        self.assertNotIn('MIDDLE', shown)
        self.assertEqual(excerpt('short', 256), ('short', False))

    def test_long_search_returns_an_excerpt_and_a_handle_that_pages_the_full_text(self):
        (self.work/'notes.txt').write_text(''.join(f'needle {i}\n' for i in range(200)))  # new, untracked
        (self.work/'.hidden').mkdir()
        error, result = self.call('search', query='needle')
        self.assertFalse(error)
        self.assertEqual(result['matches'], 200)
        self.assertTrue(result['truncated'])
        self.assertLess(len(result['output'].encode()), 400)
        self.assertNotIn('notes.txt:100:', result['output'])
        error, page = self.call('expand_output', handle=result['handle'], offset=0, limit=32000)
        self.assertIn('notes.txt:100: needle 99', page['text'])
        calls = [e['payload'] for e in self.gov.journal('bench_tool')]
        self.assertEqual([(c['tool'], c.get('matches'), c.get('truncated')) for c in calls],
                         [('search', 200, True), ('expand_output', None, None)])
        self.assertEqual((calls[1]['characters'], calls[1]['offset'], calls[1]['more']), (len(page['text']), 0, False))

    def test_run_tests_reports_counts_and_feeds_the_stuck_ladder(self):
        (self.work/'pkg'/'__init__.py').write_text(BROKEN)
        for _ in range(3):
            error, result = self.call('run_tests')
            self.assertFalse(error)
            self.assertEqual((result['tests_run'], result['tests_passed'], result['failing_count']), (1, 0, 1))
        recommendation = self.gov.state.recommend(self.task)
        self.assertTrue(recommendation['signals']['stalled_tests'])
        self.assertEqual(recommendation['recommendation'], 'increase_effort')
        (self.work/'pkg'/'__init__.py').write_text(synthetic.FIXED)
        error, result = self.call('run_tests')
        self.assertEqual((result['exit_code'], result['tests_passed'], result['truncated']), (0, 1, False))
        self.assertIn('OK', result['output'])

    def test_tests_run_without_provider_credentials(self):
        (self.work/'tests'/'test_env.py').write_text(
            'import os, unittest\nclass E(unittest.TestCase):\n'
            '    def test_env(self):\n        self.assertNotIn("ANTHROPIC_API_KEY", os.environ)\n')
        with mock.patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'sk-ant-secret'}):
            error, result = self.call('run_tests')
        self.assertEqual((result['tests_run'], result['failing_count']), (2, 0), result)

    def test_invalid_arguments_are_refused(self):
        self.assertEqual(self.call('run_tests', command='rm -rf /')['code'], -32602)
        self.assertEqual(self.call('search', query=5)['code'], -32602)
        self.assertTrue(self.call('search', query='')[0])
        self.assertTrue(self.call('expand_output', handle='../../etc/passwd')[0])

    def test_correction_refuses_stale_observations(self):
        self.gov.state.correct(self.task, 1, 'new instruction')
        (self.work/'pkg'/'__init__.py').write_text(BROKEN)
        self.call('run_tests')
        self.assertEqual(len(self.gov.journal('observe_refused')), 1)


def handle_from_last_result(request):
    """What a model would do: read the handle from the latest tool result."""
    results = [b for m in request['messages'] if m['role'] == 'user' and isinstance(m['content'], list)
               for b in m['content'] if b.get('type') == 'tool_result']
    text = json.dumps(results[-1]['content'])
    return {'handle': re.search(r'[0-9a-f]{64}', text).group(0), 'offset': 0, 'limit': 32000}


@unittest.skipUnless(shutil.which('claude'), 'Claude Code CLI not installed')
class ToolTrialTests(unittest.TestCase):
    """Real client in a bench trial with the ModelPilot arm's tools; fixture upstream, $0."""
    def setUp(self):
        self.fixture = test_bench.OfflineTrialTests('test_fixing_agent_passes_and_idle_agent_fails_with_exact_accounting')
        self.fixture.setUp()
        for name in ('upstream', 'out', 'task', 'cli', 'version'):
            setattr(self, name, getattr(self.fixture, name))

    def tearDown(self):
        self.fixture.tearDown()

    def modelpilot_trial(self, name, script, adapter, max_turns=12, rates=test_bench.RATES):
        work = self.out/name/'workspace'
        self.upstream.keep_bodies = True
        self.upstream.script = [dict(step, input={k: (str(work/v) if k == 'file_path' else v)
                                                  for k, v in step['input'].items()})
                                if 'tool' in step and isinstance(step['input'], dict) else step for step in script]
        return bench.run_trial(self.task, adapter.arm_id, self.out/name, self.cli, 'sk-ant-offline-not-a-key',
                               f'http://127.0.0.1:{self.upstream.server_port}', rates, python=sys.executable,
                               max_turns=max_turns, client_version=self.version, adapter=adapter)

    def test_long_output_stays_out_of_context_until_expanded(self):
        needles = ''.join(f'needle {i}\n' for i in range(200))
        script = [{'tool': 'Write', 'input': {'file_path': 'notes.txt', 'content': needles}},
                  {'tool': 'mcp__modelpilot__search', 'input': {'query': 'needle'}},
                  {'tool': 'mcp__modelpilot__expand_output', 'input': handle_from_last_result},
                  {'tool': 'Write', 'input': {'file_path': 'pkg/__init__.py', 'content': synthetic.FIXED}},
                  {'tool': 'mcp__modelpilot__run_tests', 'input': {}},
                  {'text': 'Done.'}]
        record = self.modelpilot_trial('tools', script, ModelPilotAdapter(limit_usd=1, tools=True, threshold=256))
        self.assertTrue(record['passed'], record['grade'])
        routing = record['routing']
        self.assertEqual([c['tool'] for c in routing['tools']['calls']], ['search', 'expand_output', 'run_tests'])
        self.assertTrue(routing['tools']['calls'][0]['truncated'])
        self.assertTrue(routing['accounting_matches'], routing)
        self.assertTrue(record['accounting']['tokens_match'], record['accounting'])
        bodies = [b.decode() for b in self.upstream.bodies if b'"tools"' in b]
        tool_names = {t['name'] for t in json.loads(bodies[0])['tools']}
        self.assertTrue({'mcp__modelpilot__run_tests', 'mcp__modelpilot__search',
                         'mcp__modelpilot__expand_output'} <= tool_names, tool_names)
        after_search = next(i for i, b in enumerate(bodies) if 'excerpt omitted' in b)
        self.assertNotIn('notes.txt:100: needle 99', bodies[after_search])  # only the excerpt entered context
        self.assertIn('notes.txt:100: needle 99', bodies[after_search + 1])  # paged in on request
        self.assertFalse(routing['applied'])
        self.assertFalse(routing['benchmark_eligible'])

    def test_stalled_tests_escalate_through_the_fixture_policy(self):
        script = ([{'tool': 'Write', 'input': {'file_path': 'pkg/__init__.py', 'content': BROKEN}}] +
                  [{'tool': 'mcp__modelpilot__run_tests', 'input': {}}] * 3 +
                  [{'tool': 'Write', 'input': {'file_path': 'pkg/__init__.py', 'content': synthetic.FIXED}},
                   {'tool': 'mcp__modelpilot__run_tests', 'input': {}},
                   {'text': 'Done.'}])
        # The fixture policy reserves the rung's output allowance: 2.1.284 sends Sonnet 5.5 max_tokens 128000 ($1.28).
        adapter = ModelPilotAdapter(limit_usd=2, tools=True, fixture_policy=self.upstream)
        record = self.modelpilot_trial('policy', script, adapter)
        self.assertTrue(record['passed'], record['grade'])
        main = [(json.loads(b)['model'], json.loads(b)['output_config']['effort'])
                for b in self.upstream.bodies if b'"tools"' in b]
        # Three stalled suite runs (host evidence from run_tests) -> one effort rung, then kept.
        self.assertEqual(main, [('claude-sonnet-5-5', 'medium')]*4 + [('claude-sonnet-5-5', 'high')]*3, main)
        routing = record['routing']
        self.assertEqual(routing['fixture_policy']['escalations'],
                         [{'action': 'increase_effort', 'status': 'fixture_confirmed',
                           'target_model': 'claude-sonnet-5-5', 'target_effort': 'high'}])
        self.assertEqual(routing['fixture_policy']['kept_requests'], 2)
        self.assertTrue(routing['applied'])
        self.assertTrue(routing['accounting_matches'], routing)
        self.assertTrue(record['accounting']['cost_complete'], record['accounting'])
        self.assertFalse(routing['benchmark_eligible'])

    def main_loop(self):
        """(model, effort in force) per main-loop request: the latest effort message, else the top-level effort."""
        return [(json.loads(b)['model'], policy_actions.effective_effort(json.loads(b)))
                for b in self.upstream.bodies if b'"tools"' in b]

    OPUS_RATES = dict(test_bench.RATES, **{'claude-opus-5-5': dict(input=4, output=20, read=.2, write_5m=5, write_1h=8)})

    def advised_trial(self, name, script, model, effort, limit=1, config=None, **kwargs):
        """The active arm with Jev's real bridge and code, its TypeSafe answer stubbed ($0, never eligible). The policy
        runs with calibration off unless a config is given: the stub's answer is what should drive its moves."""
        if config is None:
            config = switch_policy.load()
            config['calibration']['enabled'] = False
        stub = {'model': {'choice': model, 'confidence': .9,
                          'probabilities': {m: .9 if m == model else .05 for m in POLICY_TIERS}},
                'effort': {'choice': effort, 'confidence': .85,
                           'probabilities': {e: .85 if e == effort else .0375 for e in EFFORTS}}}
        with mock.patch.object(switch_policy, 'load', return_value=config):
            adapter = bench.arm_adapter('modelpilot', limit, 1, advisor_stub=stub)
            with mock.patch.object(fixtures, 'CATALOG', ACCOUNT_CATALOG):
                return self.modelpilot_trial(name, script, adapter, **kwargs)

    STALLED = ([{'tool': 'Write', 'input': {'file_path': 'pkg/__init__.py', 'content': BROKEN}}] +
               [{'tool': 'mcp__modelpilot__run_tests', 'input': {}}] * 3)

    def test_the_active_arm_jumps_straight_to_jevs_setting_and_again_on_stuck_evidence(self):
        script = self.STALLED + [{'tool': 'Write', 'input': {'file_path': 'pkg/__init__.py', 'content': synthetic.FIXED}},
                                 {'tool': 'mcp__modelpilot__run_tests', 'input': {}}, {'text': 'Done.'}]
        record = self.advised_trial('active', script, 'claude-sonnet-5-5', 'high', rates=self.OPUS_RATES)
        self.assertTrue(record['passed'], record['grade'])
        # The first request (a turn start) jumps from the client's Sonnet 5.5 medium straight to Jev's Sonnet 5.5 high;
        # three stalled suite runs are evidence it isn't enough, and inside the turn only the model can move: Opus 5.5,
        # at the turn's effort.
        self.assertEqual(self.main_loop(), [('claude-sonnet-5-5', 'high')]*4 + [('claude-opus-5-5', 'high')]*3)
        routing = record['routing']
        self.assertEqual((routing['mode'], routing['benchmark_eligible'], routing['ineligible_reason']),
                         ('active', False, 'advisor_stub'))
        policy = routing['policy']
        self.assertEqual([(e['action'], e['trigger'], e['status'], e['target_model'], e['target_effort'])
                          for e in policy['escalations']],
                         [('jump', 'turn_start', 'confirmed', 'claude-sonnet-5-5', 'high'),
                          ('jump', 'stuck_evidence', 'confirmed', 'claude-opus-5-5', 'high')])
        self.assertEqual([(d['trigger'], d['action']) for d in policy['decisions']],
                         [('turn_start', 'jump'), ('stuck_evidence', 'jump')])
        self.assertEqual((routing['advisor']['calls'], routing['advisor']['failures'], routing['advisor']['live']), (2, 0, False))
        self.assertEqual(record['catalog'], {'status': 200, 'models': ['claude-opus-5-5', 'claude-sonnet-5-5',
                                                                      'claude-haiku-4-5-20251001']})
        self.assertTrue(routing['accounting_matches'], routing)
        self.assertTrue(record['accounting']['cost_complete'], record['accounting'])
        self.assertEqual(record['accounting']['cost_scope'], 'complete')  # a stub costs nothing; live Jev is unpriced

    def test_per_message_effort_through_the_real_client(self):
        """With per-message effort on, the jump to Jev's effort rides in an effort-only system message: every main-loop
        request keeps the client's top-level effort, and the message stays where it was first sent."""
        cfg = switch_policy.load()
        cfg['per_message_effort']['enabled'] = True
        cfg['calibration']['enabled'] = False
        script = [{'tool': 'Write', 'input': {'file_path': 'pkg/__init__.py', 'content': synthetic.FIXED}},
                  {'tool': 'mcp__modelpilot__run_tests', 'input': {}}, {'text': 'Done.'}]
        record = self.advised_trial('per-message', script, 'claude-sonnet-5-5', 'xhigh', config=cfg, rates=self.OPUS_RATES)
        self.assertTrue(record['passed'], record['grade'])
        bodies = [json.loads(b) for b in self.upstream.bodies if b'"tools"' in b]
        self.assertEqual({b['output_config']['effort'] for b in bodies}, {'medium'})
        self.assertEqual({policy_actions.effective_effort(b) for b in bodies}, {'xhigh'})
        ours = [[i for i, m in enumerate(b['messages']) if m.get('output_config') and m.get('content') == []] for b in bodies]
        clients = [[i for i, m in enumerate(b['messages']) if m.get('output_config') and m.get('content') != []] for b in bodies]
        self.assertEqual(ours, [ours[0]] * len(bodies))  # put back where it was first sent
        self.assertEqual(len(ours[0]), 1)
        self.assertLess(clients[0][-1], ours[0][0])  # after Claude Code's own effort message, so it holds
        self.assertEqual(record['path']['steps'], [{'setting': ['claude-sonnet-5-5', 'xhigh'], 'requests': len(bodies),
                                                    'cost_usd': record['path']['steps'][0]['cost_usd']}])
        self.assertTrue(record['accounting']['cost_complete'], record['accounting'])

    def test_a_budget_refusal_ends_the_session_as_a_budget_stop(self):
        script = ([{'tool': 'Write', 'input': {'file_path': 'pkg/__init__.py', 'content': synthetic.FIXED}}] +
                  [{'tool': 'mcp__modelpilot__run_tests', 'input': {}}] * 4 + [{'text': 'Done.'}])
        # Jev says the client's own setting: stay; three scripted Sonnet 5.5 replies reach the limit.
        record = self.advised_trial('budget', script, 'claude-sonnet-5-5', 'medium', limit=2.5 * (100*2 + 4*10) / 1e6)
        session, = record['sessions']
        self.assertEqual((session['stop'], session['requests']), ('budget_stop', 3), session)
        self.assertEqual((record['accounting']['refused_requests'], record['accounting']['refusal_reasons']),
                         (1, {'insufficient_budget': 1}))
        self.assertEqual(self.main_loop(), [('claude-sonnet-5-5', 'medium')]*3)  # the refused one never left
        self.assertEqual(record['routing']['policy']['refusals'], ['insufficient_budget'])

    def test_a_stuck_task_with_nothing_stronger_is_stopped_unfinished(self):
        script = self.STALLED + [{'tool': 'mcp__modelpilot__run_tests', 'input': {}}] * 9 + [{'text': 'Done.'}]
        record = self.advised_trial('stuck', script, 'claude-opus-5-5', 'max', max_turns=20, rates=self.OPUS_RATES)
        self.assertFalse(record['passed'])
        self.assertEqual(record['sessions'][0]['stop'], 'policy_stop', record['sessions'])
        self.assertEqual(self.main_loop(), [('claude-opus-5-5', 'max')]*4)
        policy = record['routing']['policy']
        self.assertEqual((len(policy['stops']), policy['refusals']), (1, ['policy_stop:no_stronger_setting']))
        self.assertTrue(record['accounting']['cost_complete'], record['accounting'])

    def test_without_a_typesafe_key_the_arm_stays_at_its_start_and_is_ineligible(self):
        script = [{'tool': 'Write', 'input': {'file_path': 'pkg/__init__.py', 'content': synthetic.FIXED}},
                  {'tool': 'mcp__modelpilot__run_tests', 'input': {}}, {'text': 'Done.'}]
        with mock.patch.object(fixtures, 'CATALOG', ACCOUNT_CATALOG):
            record = self.modelpilot_trial('no-key', script, bench.arm_adapter('modelpilot', 1, 1))
        self.assertTrue(record['passed'], record['grade'])
        self.assertEqual(set(self.main_loop()), {('claude-sonnet-5-5', 'medium')})
        self.assertEqual((record['routing']['benchmark_eligible'], record['routing']['ineligible_reason']),
                         (False, 'no_advisor'))

    def test_policy_refuses_a_live_upstream(self):
        with self.assertRaises(ValueError):
            ModelPilotAdapter(fixture_policy='https://api.anthropic.com')


if __name__ == '__main__':
    unittest.main()
