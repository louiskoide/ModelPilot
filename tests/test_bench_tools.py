import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from modelpilot import bench
from modelpilot.bench_tools import OWNER, ToolServer, excerpt
from modelpilot.governor import Governor
from modelpilot.modelpilot_adapter import ModelPilotAdapter
from tests import test_bench_tasks as synthetic
from tests import test_bench

BROKEN = 'def last(items):\n    return None\n'


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
        self.assertEqual([(c['tool'], c['matches'], c['truncated']) for c in calls], [('search', 200, True)])

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

    def modelpilot_trial(self, name, script, adapter):
        work = self.out/name/'workspace'
        self.upstream.keep_bodies = True
        self.upstream.script = [dict(step, input={k: (str(work/v) if k == 'file_path' else v)
                                                  for k, v in step['input'].items()})
                                if 'tool' in step and isinstance(step['input'], dict) else step for step in script]
        return bench.run_trial(self.task, 'modelpilot', self.out/name, self.cli, 'sk-ant-offline-not-a-key',
                               f'http://127.0.0.1:{self.upstream.server_port}', test_bench.RATES, python=sys.executable,
                               max_turns=12, client_version=self.version, adapter=adapter)

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
        self.assertEqual([c['tool'] for c in routing['tools']['calls']], ['search', 'run_tests'])
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
        adapter = ModelPilotAdapter(limit_usd=1, tools=True, fixture_policy=self.upstream)
        record = self.modelpilot_trial('policy', script, adapter)
        self.assertTrue(record['passed'], record['grade'])
        main = [(json.loads(b)['model'], json.loads(b)['output_config']['effort'])
                for b in self.upstream.bodies if b'"tools"' in b]
        # Three stalled suite runs (host evidence from run_tests) -> one effort rung, then kept.
        self.assertEqual(main, [('claude-sonnet-5', 'medium')]*4 + [('claude-sonnet-5', 'high')]*3, main)
        routing = record['routing']
        self.assertEqual(routing['fixture_policy']['escalations'],
                         [{'action': 'increase_effort', 'status': 'fixture_confirmed',
                           'target_model': 'claude-sonnet-5', 'target_effort': 'high'}])
        self.assertEqual(routing['fixture_policy']['kept_requests'], 2)
        self.assertTrue(routing['applied'])
        self.assertTrue(routing['accounting_matches'], routing)
        self.assertTrue(record['accounting']['cost_complete'], record['accounting'])
        self.assertFalse(routing['benchmark_eligible'])

    def test_policy_refuses_a_live_upstream(self):
        with self.assertRaises(ValueError):
            ModelPilotAdapter(fixture_policy='https://api.anthropic.com')


if __name__ == '__main__':
    unittest.main()
