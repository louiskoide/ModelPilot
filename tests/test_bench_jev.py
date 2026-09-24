import http.client
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock
from modelpilot import bench, bench_jev, jev_route_check
from modelpilot.fixtures import USAGE, fixture_server
from modelpilot.jev_route_check import load_decisions
from tests import test_bench_tasks as synthetic
from tests.test_bench import OfflineTrialTests

ROOT = Path(__file__).resolve().parents[1]
SONNET, OPUS, HAIKU = 'claude-sonnet-5', 'claude-opus-5', 'claude-haiku-4-5-20251001'
RATES = {SONNET: dict(input=2, output=10, read=.2, write_5m=2.5, write_1h=4)}
AUTH = '[jev] routing failed, keeping claude-opus-5: 401 Cannot authenticate with the server.'


def decision(model, at, *, current=OPUS, reason='jev', stub=False, usage=None):
    request = dict({'stub': True} if stub else {}, state={'session': {'current_model': current}})
    response = {'stub': True} if stub else {'model': 'jev-1.13.0', 'usage': usage or {'input_tokens': 893, 'output_tokens': 100}}
    return {'model': model, 'confidence': .9, 'reason': reason, 'at': at, 'jev': {'request': request, 'response': response}}


def message(model, started, *, tools=6, status=200):
    return {'kind': 'messages', 'model': model, 'tool_count': tools, 'http_status': status, 'started_unix': started}


def lines(*items):
    return '\n'.join(items) + '\n'


ONE_SESSION = [{'rows': [0, 3], 'started_unix': 100.0, 'ended_unix': 200.0}]
ROUTED = lines('[jev] 1a2b3c4d5e6f 412ms p=0.91 opus -> sonnet (jev) ctx~50 | Task',
               '[jev] 1a2b3c4d5e6f rewrite jev-router -> claude-sonnet-5', '[jev] 200 served by claude-sonnet-5',
               '[jev] 1a2b3c4d5e6f rewrite jev-router -> claude-sonnet-5', '[jev] 200 served by claude-sonnet-5')


class RoutingTests(unittest.TestCase):
    def test_a_genuine_decision_is_routing(self):
        rows = [message(SONNET, 101.0), message(HAIKU, 101.5, tools=0), message(SONNET, 102.0)]
        result = bench_jev.routing([decision(SONNET, 100500)], ROUTED, rows, ONE_SESSION)
        self.assertTrue(result['routed'])
        self.assertEqual((result['router'], result['decisions'], result['extra_decisions']), ('typesafe', 1, 0))
        self.assertEqual(result['choices'], [{'model': SONNET, 'reason': 'jev', 'confidence': .9, 'current_model': OPUS}])
        self.assertEqual(result['served_models'], [SONNET])  # Claude Code's own helper calls carry no tools
        self.assertTrue(result['continuations_on_selection'])
        self.assertEqual((result['fail_open'], result['auth_failure']), ([], False))
        self.assertEqual(result['router_usage'], [{'input_tokens': 893, 'output_tokens': 100}])
        self.assertIsNone(result['state_carried'])

    def test_stock_jev_rewrites_the_sentinel_without_a_decision(self):
        stderr = lines('[jev] 1a2b3c4d5e6f rewrite jev-router -> claude-opus-5', '[jev] 200 served by claude-opus-5')
        result = bench_jev.routing([], stderr, [message(OPUS, 101.0), message(OPUS, 102.0)], ONE_SESSION)
        self.assertFalse(result['routed'])
        self.assertEqual((result['router'], result['decisions'], result['extra_decisions']), (None, 0, 0))
        self.assertEqual(result['fail_open'], ['unrouted_sentinel'])
        self.assertEqual(result['served_models'], [OPUS])
        self.assertIsNone(result['continuations_on_selection'])

    def test_an_authentication_failure_is_fail_open_not_routing(self):
        stderr = lines(AUTH, '[jev] 1a2b3c4d5e6f no-jev opus -> opus (jev-unavailable/no-change) ctx~50 | Task',
                       '[jev] 1a2b3c4d5e6f rewrite jev-router -> claude-opus-5')
        unavailable = dict(decision(OPUS, 100500, reason='jev-unavailable/no-change'), jev=None)
        result = bench_jev.routing([unavailable], stderr, [message(OPUS, 101.0)], ONE_SESSION)
        self.assertFalse(result['routed'])
        self.assertTrue(result['auth_failure'])
        self.assertIn('routing failed', result['fail_open'])
        self.assertIn('no-jev', result['fail_open'])

    def test_a_timeout_is_fail_open_but_not_an_authentication_failure(self):
        stderr = lines('[jev] routing failed, keeping claude-opus-5: The operation was aborted',
                       '[jev] 1a2b3c4d5e6f no-jev opus -> opus (jev-unavailable/no-change) ctx~50 | Task')
        result = bench_jev.routing([dict(decision(OPUS, 100500), jev=None)], stderr, [message(OPUS, 101.0)], ONE_SESSION)
        self.assertFalse(result['routed'])
        self.assertFalse(result['auth_failure'])

    def test_a_resend_after_a_rejection_is_an_extra_decision(self):
        # Haiku rejects Claude Code's system-role message; the resend is a new conversation to Jev.
        rows = [message(HAIKU, 101.0, status=400), message(HAIKU, 102.0), message(HAIKU, 103.0)]
        result = bench_jev.routing([decision(HAIKU, 100500), decision(HAIKU, 101500)], ROUTED, rows, ONE_SESSION)
        self.assertEqual((result['decisions'], result['extra_decisions']), (2, 1))
        self.assertTrue(result['routed'])
        self.assertTrue(result['continuations_on_selection'])

    def test_a_continuation_on_another_model_is_flagged(self):
        rows = [message(SONNET, 101.0), message(OPUS, 102.0)]
        result = bench_jev.routing([decision(SONNET, 100500)], ROUTED, rows, ONE_SESSION)
        self.assertFalse(result['continuations_on_selection'])
        self.assertEqual(result['served_models'], [OPUS, SONNET])

    def test_the_follow_up_decision_must_see_the_first_selection(self):
        sessions = [{'rows': [0, 1], 'started_unix': 100.0, 'ended_unix': 200.0},
                    {'rows': [1, 2], 'started_unix': 300.0, 'ended_unix': 400.0}]
        rows = [message(SONNET, 101.0), message(SONNET, 301.0)]
        carried = [decision(SONNET, 100500), decision(SONNET, 300500, current=SONNET)]
        result = bench_jev.routing(carried, ROUTED, rows, sessions)
        self.assertTrue(result['state_carried'])
        self.assertEqual(result['extra_decisions'], 0)
        reset = [decision(SONNET, 100500), decision(SONNET, 300500, current=OPUS)]
        self.assertFalse(bench_jev.routing(reset, ROUTED, rows, sessions)['state_carried'])

    def test_stub_decisions_are_labeled(self):
        result = bench_jev.routing([decision(SONNET, 100500, stub=True)], ROUTED, [message(SONNET, 101.0)], ONE_SESSION)
        self.assertEqual((result['router'], result['router_usage']), ('stub', []))
        self.assertTrue(result['routed'])


class DecisionFileTests(unittest.TestCase):
    def test_decisions_come_from_every_session_file_in_time_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)/'jev-claude'
            folder.mkdir()
            first, second, third = decision(SONNET, 1000), decision(SONNET, 2000), decision(OPUS, 1500)
            (folder/'a.json').write_text(json.dumps(dict(second, history=[first, second])))
            (folder/'b.json').write_text(json.dumps(third))
            (folder/'c.json').write_text('not json')
            (folder/'d.json').write_text(json.dumps({'manual': True, 'at': 5}))
            self.assertEqual(load_decisions(tmp), [first, third, second])
            self.assertEqual(load_decisions(Path(tmp)/'missing'), [])


class ArmTests(unittest.TestCase):
    def test_both_jev_arms_are_configured_and_runnable(self):
        self.assertIn('jev', bench.RUNNABLE)
        stock, compat = bench.ARMS['jev-stock'], bench.ARMS['jev-compat']
        self.assertEqual((stock['model'], stock['checkout'], stock['patch']), ('jev-router', 'work/jev-router-baseline', None))
        self.assertEqual((compat['model'], compat['checkout'], compat['patch']),
                         ('jev-router', 'work/jev-router-compat', jev_route_check.PATCH))

    def test_every_model_jev_can_serve_has_rates(self):
        self.assertEqual(set(bench_jev.JEV_MODELS), {HAIKU, SONNET, OPUS})
        table = bench.rates()
        self.assertTrue(all(m in table for m in bench_jev.JEV_MODELS))

    def test_the_router_process_gets_the_router_key_and_never_the_anthropic_key(self):
        dirs = {'home': Path('/t/home'), 'tmp': Path('/t/tmp'), 'config': Path('/t/config')}
        base = {'PATH': '/usr/bin:/bin', 'LANG': 'C', 'ANTHROPIC_API_KEY': 'sk-ant-secret', 'ANTHROPIC_MODEL': OPUS,
                'ANTHROPIC_BASE_URL': 'http://elsewhere'}
        env = bench_jev.router_env(base, dirs, 'ts-secret')
        self.assertNotIn('sk-ant-secret', env.values())
        for name in ('ANTHROPIC_API_KEY', 'ANTHROPIC_MODEL', 'ANTHROPIC_BASE_URL'):
            self.assertNotIn(name, env)
        self.assertEqual((env['JEV_API_KEY'], env['JEV_DEBUG'], env['JEV_NO_STATUSLINE']), ('ts-secret', '1', '1'))
        self.assertEqual((env['HOME'], env['TMPDIR'], env['LANG']), ('/t/home', '/t/tmp', 'C'))
        self.assertNotIn('JEV_API_KEY', bench_jev.router_env(base, dirs, None))

    def test_a_missing_or_modified_checkout_is_a_problem(self):
        with tempfile.TemporaryDirectory() as tmp:
            router = bench_jev.JevRouter(bench.ARMS['jev-compat'], root=Path(tmp))
            self.assertIn('No Jev checkout', router.problem())
            with self.assertRaises(bench_jev.JevCheckoutChanged):
                router.verify()
            (Path(tmp)/'work/jev-router-compat/.git').mkdir(parents=True)
            with mock.patch.object(bench_jev, 'verify_checkout', return_value=False):
                self.assertIn('pinned revision', router.problem())
            with mock.patch.object(bench_jev, 'verify_checkout', return_value=True):
                self.assertIsNone(router.problem())


class FakeRouter:
    """Stands in for bench_jev.JevRouter: passes requests straight to the trial's proxy."""
    def __init__(self, stderr='', decisions=(), problem=None):
        self.stderr, self.decisions, self.problem_text = stderr, list(decisions), problem
        self.started, self.stopped, self.key, self.live, self.pid = 0, 0, None, True, 4242

    def verify(self):
        if self.problem_text:
            raise bench_jev.JevCheckoutChanged(self.problem_text)

    def describe(self):
        return {'variant': 'compat', 'jev_commit': 'abc123', 'patch_sha256': 'def456', 'launcher': 'fake'}

    def start(self, upstream_url, dirs, jev_key, stderr_path, stub=None):
        self.started += 1
        self.key = jev_key
        Path(stderr_path).write_text(self.stderr)
        return {'env': {'ANTHROPIC_BASE_URL': upstream_url, 'ANTHROPIC_MODEL': 'jev-router'}, 'add_dir': '/jev',
                'router': 'stub' if stub else 'typesafe'}

    def alive(self):
        return self.live

    def stop(self):
        self.stopped += 1

    def collect(self, tmp_dir, dest):
        Path(dest).write_text(json.dumps(self.decisions))
        return self.decisions


class JevTrialTests(unittest.TestCase):
    """bench.Trial on a Jev arm with a fake router and a stub client (one request per session)."""
    def setUp(self):
        self.repo_case = synthetic.BenchTaskTests('test_valid_task_fails_on_base_and_passes_on_reference')
        self.repo_case.setUp()
        self.task = dict(self.repo_case.task, repo_path=str(self.repo_case.repo))
        self.upstream = fixture_server()
        self.thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.thread.start()
        self.calls, self.envs = [], []

    def tearDown(self):
        self.upstream.shutdown()
        self.upstream.server_close()
        self.thread.join()
        self.repo_case.tearDown()

    def stub(self):
        def run_client(command, env, cwd, timeout, **_):
            self.calls.append(command)
            self.envs.append(env)
            host, port = env['ANTHROPIC_BASE_URL'].rsplit('/', 1)[1].split(':')
            conn = http.client.HTTPConnection(host, int(port), timeout=5)
            conn.request('POST', '/v1/messages', json.dumps({'model': SONNET, 'max_tokens': 8, 'messages': [],
                                                             'tools': [{'name': 'Read'}]}),
                         {'Content-Type': 'application/json'})
            conn.getresponse().read()
            conn.close()
            final = {'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 1, 'total_cost_usd': 1.0,
                     'modelUsage': {'jev-router': {'inputTokens': USAGE['input_tokens'], 'outputTokens': USAGE['output_tokens'],
                                                   'cacheReadInputTokens': USAGE['cache_read_input_tokens']}}}
            return {'status': 'completed', 'returncode': 0, 'stdout': json.dumps(final) + '\n', 'stderr': ''}
        return mock.patch.object(bench, 'run_client', run_client)

    def trial(self, router, **options):
        return bench.Trial(self.task, 'jev-compat', self.repo_case.root/'trial', '/fake/claude', 'sk-ant-client',
                           f'http://127.0.0.1:{self.upstream.server_port}', RATES, python=sys.executable,
                           router=router, jev_key='ts-secret', **options)

    def test_a_jev_session_is_routed_without_a_model_and_keeps_keys_apart(self):
        router = FakeRouter(ROUTED, [decision(SONNET, 1)])
        trial = self.trial(router)
        with self.stub():
            self.assertFalse(trial.step())
        command, env = self.calls[0], self.envs[0]
        self.assertNotIn('--model', command)
        self.assertEqual(command[command.index('--add-dir') + 1], '/jev')
        self.assertEqual(env['ANTHROPIC_MODEL'], 'jev-router')
        self.assertEqual(env['ANTHROPIC_API_KEY'], 'sk-ant-client')
        self.assertNotIn('JEV_API_KEY', env)
        self.assertNotIn('ts-secret', env.values())
        self.assertEqual((router.key, router.started), ('ts-secret', 1))
        self.assertGreaterEqual(router.stopped, 1)
        record = trial.record
        self.assertEqual((record['model'], record['variant'], record['jev_commit']), ('jev-router', 'compat', 'abc123'))
        self.assertEqual(record['routing']['decisions'], 1)
        accounting = record['accounting']
        self.assertEqual(accounting['cost_scope'], 'provider_only_router_unpriced')
        self.assertIsNone(accounting['router_cost_usd'])
        self.assertIsNone(accounting['client_cost_matches'])
        self.assertEqual(accounting['client_cost_basis'], 'sentinel_model_unknown_price')
        self.assertTrue(accounting['tokens_match'], accounting)
        self.assertTrue((self.repo_case.root/'trial'/'decisions.json').exists())
        self.assertTrue((self.repo_case.root/'trial'/'jev.stderr.txt').exists())
        self.assertTrue(record['complete'])

    def test_keys_are_redacted_from_the_router_log(self):
        trial = self.trial(FakeRouter(ROUTED + 'echo ts-secret sk-ant-client\n', [decision(SONNET, 1)]))
        with self.stub():
            trial.step()
        text = (self.repo_case.root/'trial'/'jev.stderr.txt').read_text()
        self.assertNotIn('ts-secret', text)
        self.assertNotIn('sk-ant-client', text)
        self.assertIn('echo [REDACTED] [REDACTED]', text)

    def test_a_changed_checkout_stops_the_trial_before_any_client_call(self):
        router = FakeRouter(problem='modified')
        with self.stub(), self.assertRaises(bench_jev.JevCheckoutChanged):
            self.trial(router).step()
        self.assertEqual((self.calls, router.started), ([], 0))

    def test_a_dead_router_stops_the_trial_before_the_next_session(self):
        router = FakeRouter(ROUTED, [decision(SONNET, 1)])
        trial = self.trial(router, shape='followup', gap=0)
        with self.stub():
            self.assertTrue(trial.step())
            router.live = False
            with self.assertRaises(bench_jev.JevRouterDown):
                trial.step()
        self.assertEqual(len(self.calls), 1)
        trial.close()
        self.assertGreaterEqual(router.stopped, 1)

    def test_an_authentication_failure_marks_the_router_unavailable(self):
        trial = self.trial(FakeRouter(lines(AUTH), [dict(decision(OPUS, 1), jev=None)]))
        with self.stub():
            trial.step()
        self.assertTrue(trial.router_unavailable)
        self.assertTrue(trial.record['routing']['auth_failure'])


JEV_READY = all((ROOT/f'work/{name}/src/proxy.mjs').exists() for name in ('jev-router-baseline', 'jev-router-compat'))


@unittest.skipUnless(shutil.which('claude') and shutil.which('node') and JEV_READY, 'needs claude, node and both Jev checkouts')
class OfflineJevTrialTests(unittest.TestCase):
    """Real client and Jev's real proxy, fake key, stub route, scripted loopback upstream: $0."""
    setUp = OfflineTrialTests.setUp
    tearDown = OfflineTrialTests.tearDown
    FIX = OfflineTrialTests.FIX

    def trial(self, arm, name, script, **options):
        work = self.out/name/'workspace'
        self.upstream.script = [dict(step, input={k: (str(work/v) if k == 'file_path' else v) for k, v in step['input'].items()})
                                if 'tool' in step else step for step in script]
        options = dict(dict(python=sys.executable, max_turns=8, client_version=self.version, jev_stub=SONNET), **options)
        return bench.run_trial(self.task, arm, self.out/name, self.cli, 'sk-ant-offline-not-a-key',
                               f'http://127.0.0.1:{self.upstream.server_port}', bench.rates(), **options)

    def assert_gone(self, pid):
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_compat_jev_routes_a_fixing_session_with_exact_wire_accounting(self):
        record = self.trial('jev-compat', 'compat', self.FIX)
        self.assertTrue(record['passed'], record['grade'])
        routing = record['routing']
        self.assertTrue(routing['routed'], routing)
        self.assertEqual((routing['router'], routing['decisions'], routing['served_models']), ('stub', 1, [SONNET]))
        self.assertTrue(routing['continuations_on_selection'])
        accounting = record['accounting']
        self.assertTrue(accounting['tokens_match'], accounting)
        self.assertAlmostEqual(accounting['cost_usd'], 4 * (100*2 + 4*10) / 1e6)
        self.assertIsNone(accounting['client_cost_matches'])
        self.assertIsInstance(accounting['client_cost_usd'], float)  # the client's own price for the sentinel
        trial_dir = self.out/'compat'
        self.assertEqual(len(json.loads((trial_dir/'decisions.json').read_text())), 1)
        self.assertIn('rewrite jev-router -> claude-sonnet-5', (trial_dir/'jev.stderr.txt').read_text())
        self.assertFalse((trial_dir/'workspace').exists())
        self.assert_gone(record['sessions'][0]['router_pid'])

    def test_compat_jev_carries_its_routing_state_into_the_follow_up(self):
        script = self.FIX + [{'tool': 'Bash', 'input': {'command': 'python3 -m unittest discover -q -s tests -t .',
                                                        'description': 'suite'}},
                             {'text': 'All tests pass.'}]
        record = self.trial('jev-compat', 'followup', script, shape='followup', gap=0)
        self.assertTrue(record['passed'], record['grade'])
        self.assertEqual([s['stop'] for s in record['sessions']], ['success', 'success'])
        routing = record['routing']
        self.assertEqual(routing['decisions'], 2, routing)
        self.assertTrue(routing['state_carried'], routing['choices'])
        first, second = record['sessions']
        self.assertEqual(first['router_pid'], second['router_pid'])  # one Jev proxy for the whole trial
        self.assertTrue(record['accounting']['tokens_match'], record['accounting'])

    def test_stock_jev_on_this_client_serves_everything_on_opus_without_routing(self):
        record = self.trial('jev-stock', 'stock', self.FIX)
        self.assertTrue(record['passed'], record['grade'])
        routing = record['routing']
        self.assertFalse(routing['routed'])
        self.assertEqual((routing['decisions'], routing['served_models']), (0, [OPUS]))
        self.assertIn('unrouted_sentinel', routing['fail_open'])
        self.assertAlmostEqual(record['accounting']['cost_usd'], 4 * (100*5 + 4*25) / 1e6)


if __name__ == '__main__': unittest.main()
