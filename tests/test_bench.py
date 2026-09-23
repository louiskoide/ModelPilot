import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from modelpilot import bench
from modelpilot.fixtures import fixture_server
from tests import test_bench_tasks as synthetic

RATES = {'claude-sonnet-5': dict(input=2, output=10, read=.2, write_5m=2.5, write_1h=4)}


class ScheduleTests(unittest.TestCase):
    def test_order_is_complete_randomized_and_reproducible(self):
        tasks = [{'id': 'a'}, {'id': 'b'}]
        order = bench.schedule(tasks, ['opus-5', 'sonnet-5'], 3, seed=7)
        self.assertEqual(sorted(order), sorted((t, a, n) for t in 'ab' for a in ('opus-5', 'sonnet-5') for n in range(3)))
        self.assertEqual(order, bench.schedule(tasks, ['opus-5', 'sonnet-5'], 3, seed=7))
        self.assertNotEqual(order, bench.schedule(tasks, ['opus-5', 'sonnet-5'], 3, seed=8))

    def test_unpriced_requests_leave_trial_cost_unknown(self):
        rows = [{'kind': 'messages', 'http_status': 200, 'cost_usd': .01, 'usage': {'input_tokens': 5}},
                {'kind': 'messages', 'http_status': 400, 'cost_usd': None}]
        result = bench.accounting(rows, {})
        self.assertEqual((result['cost_usd'], result['known_cost_usd'], result['rejected_requests']), (None, .01, 1))
        summary = bench.summarize([{'arm': 'x', 'passed': True, 'wall_seconds': 1, 'accounting': result}], ['x'])[0]
        self.assertEqual((summary['cost_per_pass_usd'], summary['unpriced_trials']), (None, 1))

    def test_unimplemented_arms_refuse_instead_of_pretending(self):
        for arm in ('jev-stock', 'jev-compat', 'modelpilot'):
            with self.assertRaises(NotImplementedError):
                bench.run_trial({'id': 't'}, arm, '/nonexistent', 'claude', 'k', 'http://127.0.0.1:1', RATES)

    def test_every_fixed_arm_model_has_rates(self):
        table = bench.rates()
        self.assertTrue(all(arm['model'] in table for arm in bench.ARMS.values() if arm['kind'] == 'fixed'))


@unittest.skipUnless(shutil.which('claude'), 'Claude Code CLI not installed')
class OfflineTrialTests(unittest.TestCase):
    """Real client, fake key, scripted loopback upstream, synthetic repository: $0, no provider traffic."""
    def setUp(self):
        self.repo_case = synthetic.BenchTaskTests('test_valid_task_fails_on_base_and_passes_on_reference')
        self.repo_case.setUp()
        self.task = dict(self.repo_case.task, repo_path=str(self.repo_case.repo))
        self.upstream = fixture_server()
        self.thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.thread.start()
        self.out = self.repo_case.root/'bench'

    def tearDown(self):
        self.upstream.shutdown()
        self.upstream.server_close()
        self.thread.join()
        self.repo_case.tearDown()

    def trial(self, name, script):
        work = self.out/name/'workspace'
        self.upstream.script = [dict(step, input={k: (str(work/v) if k == 'file_path' else v) for k, v in step['input'].items()})
                                if 'tool' in step else step for step in script]
        return bench.run_trial(self.task, 'sonnet-5', self.out/name, shutil.which('claude'), 'sk-ant-offline-not-a-key',
                               f'http://127.0.0.1:{self.upstream.server_port}', RATES, python=sys.executable, max_turns=8)

    def test_fixing_agent_passes_and_idle_agent_fails_with_exact_accounting(self):
        fixed = self.trial('fixes', [{'tool': 'Read', 'input': {'file_path': 'pkg/__init__.py'}},
                                     {'tool': 'Write', 'input': {'file_path': 'pkg/__init__.py', 'content': synthetic.FIXED}},
                                     {'tool': 'Bash', 'input': {'command': 'python3 -m unittest -q', 'description': 'tests'}},
                                     {'text': 'Done.'}])
        self.assertTrue(fixed['passed'], fixed['grade'])
        self.assertEqual(fixed['client']['subtype'], 'success')
        accounting = fixed['accounting']
        self.assertEqual(accounting['requests'], 4)
        self.assertTrue(accounting['tokens_match'], accounting)
        self.assertTrue(accounting['client_cost_matches'], accounting)
        self.assertAlmostEqual(accounting['cost_usd'], 4 * (100*2 + 4*10) / 1e6)
        self.assertIn(b'items[-1]', (self.out/'fixes'/'agent.diff').read_bytes())
        self.assertFalse((self.out/'fixes'/'workspace').exists())  # only records are kept
        idle = self.trial('idle', [{'text': 'I could not find the problem.'}])
        self.assertEqual((idle['passed'], idle['grade']['reason']), (False, 'hidden_tests_failed'))
        self.assertEqual(idle['grade']['failing_tests'], ['test_many (tests.test_pkg.T)'])
        saved = json.loads((self.out/'idle'/'trial.json').read_text())
        self.assertEqual(saved['spec_sha256'], idle['spec_sha256'])


if __name__ == '__main__': unittest.main()
