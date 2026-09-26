import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from modelpilot import bench, bench_tasks
from modelpilot.fixtures import fixture_server, scripted_response
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

    def test_unimplemented_arms_refuse_instead_of_pretending(self):
        with self.assertRaises(NotImplementedError):
            bench.run_trial({'id': 't'}, 'modelpilot', '/nonexistent', 'claude', 'k', 'http://127.0.0.1:1', RATES)

    def test_every_fixed_arm_model_has_rates(self):
        table = bench.rates()
        self.assertTrue(all(arm['model'] in table for arm in bench.ARMS.values() if arm['kind'] == 'fixed'))

    def test_session_end_reasons(self):
        ok = {'subtype': 'success', 'is_error': False}
        cases = [(('completed', ok, []), 'success'),
                 (('timeout', {}, []), 'timeout'),
                 (('completed', {'subtype': 'error_max_turns', 'is_error': True}, []), 'turn_limit'),
                 (('completed', {'subtype': 'error_max_budget_usd', 'is_error': True}, []), 'budget_stop'),
                 (('completed', {}, [{'kind': 'messages', 'status': 'transport_error'}]), 'transport_error'),
                 (('completed', {'subtype': 'error_during_execution', 'is_error': True, 'api_error_status': 400}, []), 'api_error'),
                 (('completed', {'subtype': 'error_during_execution', 'is_error': True}, []), 'client_error'),
                 (('completed', {}, []), 'client_error')]
        for args, expected in cases:
            self.assertEqual(bench.stop_reason(*args), expected, args)

    def test_diffs_touching_test_configuration_are_flagged(self):
        paths = ['pkg/core.py', 'conftest.py', 'tests/conftest.py', 'pyproject.toml', 'setup.cfg', 'src/sitecustomize.py',
                 'docs/pytest.ini.md', 'tox.ini']
        self.assertEqual(bench.test_config_changes(paths, 'tests'),
                         ['conftest.py', 'pyproject.toml', 'setup.cfg', 'src/sitecustomize.py', 'tox.ini'])

    def test_rejected_requests_get_a_labeled_sensitivity_figure_but_no_headline(self):
        rows = [{'kind': 'messages', 'http_status': 200, 'cost_usd': .01, 'usage': {'input_tokens': 5}},
                {'kind': 'messages', 'http_status': 400, 'cost_usd': None},
                {'kind': 'messages', 'http_status': 200, 'cost_usd': .02, 'usage': {'input_tokens': 5}}]
        result = bench.accounting(rows, {})
        self.assertIsNone(result['cost_usd'])
        self.assertAlmostEqual(result['cost_if_rejected_free_usd'], .03)
        self.assertEqual(result['cost_scope'], 'complete')
        self.assertEqual(result['client_cost_basis'], 'client_model_table')
        self.assertNotIn('router_cost_usd', result)
        # A transport failure or an unpriced success is not a rejection: nothing is assumed free.
        for bad in ({'kind': 'messages', 'http_status': None, 'cost_usd': None},
                    {'kind': 'messages', 'http_status': 200, 'cost_usd': None, 'usage': {}}):
            self.assertIsNone(bench.accounting(rows + [bad], {})['cost_if_rejected_free_usd'])
        failed = bench.accounting(rows + [{'kind': 'messages', 'http_status': None, 'cost_usd': None}], {})
        self.assertEqual((failed['rejected_requests'], failed['transport_failures']), (1, 1))

    def test_jev_accounting_is_provider_only_and_skips_the_client_price(self):
        rows = [{'kind': 'messages', 'http_status': 200, 'cost_usd': .01, 'usage': {'input_tokens': 5}}]
        final = {'total_cost_usd': .04, 'modelUsage': {'jev-router': {'inputTokens': 5}}}
        result = bench.accounting(rows, final, jev=True)
        self.assertEqual((result['cost_usd'], result['cost_scope']), (.01, 'provider_only_router_unpriced'))
        self.assertIsNone(result['router_cost_usd'])
        self.assertIsNone(result['client_cost_matches'])
        self.assertEqual(result['client_cost_basis'], 'sentinel_model_unknown_price')
        self.assertTrue(result['tokens_match'])

    def test_jev_commands_leave_the_model_to_the_router(self):
        command = bench.client_command('/c', 'do it', None, 30, 1.0, ['--session-id', 'x'], ['--add-dir', '/jev'])
        self.assertNotIn('--model', command)
        self.assertEqual(command[-2:], ['--add-dir', '/jev'])
        self.assertEqual(command[command.index('--tools') + 1], bench.TOOLS)

    def test_budget_threshold_is_passed_exactly(self):
        for budget, text in ((1.0, '1'), (.004, '0.004'), (.000001, '0.000001'), (2.5, '2.5')):
            command = bench.client_command('/c', 'do it', 'claude-sonnet-5', 30, budget, ['--session-id', 'x'])
            self.assertEqual(command[command.index('--max-budget-usd') + 1], text)


class FixtureScriptTests(unittest.TestCase):
    def test_follow_up_prompts_advance_the_script(self):
        script = [{'text': 'first'}, {'text': 'second'}, {'text': 'third'}]
        messages = [{'role': 'user', 'content': [{'type': 'text', 'text': 'task'}]},
                    {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 't0', 'name': 'Read', 'input': {}}]},
                    {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't0', 'content': 'x'}]},
                    {'role': 'assistant', 'content': [{'type': 'text', 'text': 'Done.'}]}]

        def reply(conversation):
            return json.loads(scripted_response({'model': 'm', 'messages': conversation}, script)[2])['content'][0]['text']
        self.assertEqual(reply(messages[:3]), 'second')  # one tool result, as before
        self.assertEqual(reply(messages + [{'role': 'user', 'content': 'Follow-up.'}]), 'third')


class Clock:
    def __init__(self):
        self.now, self.log, self.sleeps = 0.0, [], []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeTrial:
    """Stands in for bench.Trial on a fake clock: fixed sessions, duration and cost."""
    def __init__(self, name, clock, sessions=2, seconds=100, cost=.4, fail_on=None, unavailable_after=None):
        self.key, self.clock, self.sessions, self.seconds, self.cost, self.fail_on = (name, 'sonnet-5', 0), clock, sessions, seconds, cost, fail_on
        self.ran, self.started, self.finished, self.record = 0, False, False, None
        self.unavailable_after, self.router_unavailable, self.closed = unavailable_after, False, False

    def step(self):
        if self.fail_on == self.ran + 1:
            raise RuntimeError('harness bug')
        self.started = True
        self.clock.now += self.seconds
        self.clock.log.append((self.key[0], self.ran + 1))
        self.ran += 1
        self.router_unavailable = self.ran == self.unavailable_after
        if self.ran == self.sessions:
            self.finish()
        return self.ran < self.sessions

    def known_cost(self):
        return self.cost * self.ran

    def close(self):
        self.closed = True

    def finish(self, stopped=None):
        self.close()
        self.finished = True
        self.record = {'task': self.key[0], 'arm': 'sonnet-5', 'trial': 0, 'passed': True, 'wall_seconds': 1.0,
                       'complete': stopped is None, 'stopped': stopped, 'sessions': [{'stop': 'success'}] * self.ran,
                       'accounting': {'cost_usd': self.known_cost()}, 'cache': {'cold_equivalent_cost_usd': self.known_cost()}}


class InterleaveTests(unittest.TestCase):
    def test_parked_trials_resume_after_the_gap_while_new_trials_fill_the_wait(self):
        clock = Clock()
        trials = [FakeTrial(n, clock) for n in 'ABC']
        self.assertIsNone(bench.interleave(trials, 330, clock=clock, sleep=clock.sleep))
        self.assertEqual(clock.log, [('A', 1), ('B', 1), ('C', 1), ('A', 2), ('B', 2), ('C', 2)])
        self.assertEqual(clock.sleeps, [130])  # only when nothing else could run
        self.assertTrue(all(t.finished for t in trials))

    def test_zero_gap_resumes_at_once(self):
        clock = Clock()
        bench.interleave([FakeTrial(n, clock) for n in 'AB'], 0, clock=clock, sleep=clock.sleep)
        self.assertEqual((clock.log, clock.sleeps), ([('A', 1), ('A', 2), ('B', 1), ('B', 2)], []))

    def test_single_sessions_keep_the_given_order(self):
        clock = Clock()
        bench.interleave([FakeTrial(n, clock, sessions=1) for n in 'CAB'], 330, clock=clock, sleep=clock.sleep)
        self.assertEqual(clock.log, [('C', 1), ('A', 1), ('B', 1)])

    def test_a_stop_is_checked_before_every_session(self):
        clock = Clock()
        trials = [FakeTrial(n, clock) for n in 'AB']
        stops = iter([None, None, 'run_budget'])
        self.assertEqual(bench.interleave(trials, 0, clock=clock, sleep=clock.sleep, stop=lambda: next(stops)), 'run_budget')
        self.assertEqual(clock.log, [('A', 1), ('A', 2)])
        self.assertFalse(trials[1].started)


class RunBenchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)/'run'
        self.clock = Clock()
        self.made = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_fake(self, names, fake=None, **options):
        def factory(task, arm, trial_dir, trial):
            made = FakeTrial(task['id'], self.clock, **(fake or {}).get(task['id'], {}))
            self.made.append(made)
            return made
        return bench.run_bench([{'id': n} for n in names], ['sonnet-5'], 1, 0, self.out, '/fake/claude', 'k',
                               'http://127.0.0.1:1', RATES, client_version='9.9 (fake)', trial_factory=factory,
                               clock=self.clock, sleep=self.clock.sleep, python=sys.executable, **options)

    def test_run_budget_stops_scheduling_and_closes_parked_trials(self):
        summary = self.run_fake('ABC', shape='followup', gap=0, run_budget=1.0)
        self.assertEqual((summary['stopped'], summary['complete']), ('run_budget', False))
        self.assertAlmostEqual(summary['known_spend_usd'], 1.2)  # thresholds are checked between sessions
        self.assertEqual([t.ran for t in self.made], [2, 1])  # the third trial never started
        self.assertEqual(self.made[1].record['stopped'], 'run_budget')
        saved = json.loads((self.out/'summary.json').read_text())
        self.assertEqual((saved['arms'][0]['trials'], saved['arms'][0]['incomplete_trials']), (1, 1))
        manifest = json.loads((self.out/'manifest.json').read_text())
        self.assertEqual((manifest['shape'], manifest['gap_seconds'], manifest['run_budget_usd']), ('followup', 0, 1.0))
        self.assertEqual(manifest['follow_up_prompt'], bench.FOLLOW_UP)
        self.assertEqual(manifest['client_version'], '9.9 (fake)')

    def test_a_harness_crash_still_writes_the_summary(self):
        order = [task for task, _, _ in bench.schedule([{'id': n} for n in 'AB'], ['sonnet-5'], 1, 0)]
        with self.assertRaises(RuntimeError):
            self.run_fake('AB', fake={order[1]: {'fail_on': 1}}, shape='single')
        saved = json.loads((self.out/'summary.json').read_text())
        self.assertEqual((saved['complete'], saved['error'], saved['trials']), (False, 'RuntimeError', 1))
        self.assertTrue(self.made[1].closed)  # per-trial proxy and router are released on a crash

    def test_a_router_authentication_failure_stops_the_run(self):
        first = bench.schedule([{'id': n} for n in 'ABC'], ['sonnet-5'], 1, 0)[0][0]
        summary = self.run_fake('ABC', fake={first: {'unavailable_after': 1, 'sessions': 1}}, shape='single')
        self.assertEqual((summary['stopped'], summary['complete']), ('jev_router_unavailable', False))
        self.assertEqual(len(self.made), 1)
        saved = json.loads((self.out/'summary.json').read_text())
        self.assertEqual(saved['stopped'], 'jev_router_unavailable')


class TrialTests(unittest.TestCase):
    """bench.Trial with the client replaced by a stub that sends one request through the trial's proxy."""
    def setUp(self):
        self.repo_case = synthetic.BenchTaskTests('test_valid_task_fails_on_base_and_passes_on_reference')
        self.repo_case.setUp()
        self.task = dict(self.repo_case.task, repo_path=str(self.repo_case.repo))
        self.upstream = fixture_server()
        self.thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.thread.start()
        self.calls = []

    def tearDown(self):
        self.upstream.shutdown()
        self.upstream.server_close()
        self.thread.join()
        self.repo_case.tearDown()

    def stub(self, subtypes):
        results = iter(subtypes)

        def run_client(command, env, cwd, timeout, **_):
            self.calls.append(command)
            host, port = env['ANTHROPIC_BASE_URL'].rsplit('/', 1)[1].split(':')
            conn = http.client.HTTPConnection(host, int(port), timeout=5)
            conn.request('POST', '/v1/messages', json.dumps({'model': 'claude-sonnet-5', 'max_tokens': 8, 'messages': []}),
                         {'Content-Type': 'application/json'})
            conn.getresponse().read()
            conn.close()
            subtype = next(results)
            final = {'type': 'result', 'subtype': subtype, 'is_error': subtype != 'success', 'num_turns': 1}
            return {'status': 'completed', 'returncode': 0, 'stdout': json.dumps(final) + '\n', 'stderr': ''}
        return mock.patch.object(bench, 'run_client', run_client)

    def trial(self, **options):
        return bench.Trial(self.task, 'sonnet-5', self.repo_case.root/'trial', '/fake/claude', 'k',
                           f'http://127.0.0.1:{self.upstream.server_port}', RATES, python=sys.executable, **options)

    def test_follow_up_resumes_the_first_session(self):
        trial = self.trial(shape='followup', gap=0)
        with self.stub(['success', 'success']):
            self.assertTrue(trial.step())
            self.assertFalse(trial.step())
        first, second = self.calls
        session = first[first.index('--session-id') + 1]
        self.assertEqual(second[second.index('--resume') + 1], session)
        self.assertEqual(second[second.index('-p') + 1], bench.FOLLOW_UP)
        self.assertNotIn('--no-session-persistence', first + second)
        record = trial.record
        self.assertEqual([s['stop'] for s in record['sessions']], ['success', 'success'])
        self.assertEqual([s['requests'] for s in record['sessions']], [1, 1])
        self.assertEqual(record['accounting']['requests'], 2)
        self.assertTrue(record['complete'])
        self.assertGreaterEqual(record['gap_seconds'], 0)
        self.assertEqual(json.loads((self.repo_case.root/'trial'/'trial.json').read_text())['phase'], 'graded')

    def test_closing_keeps_the_row_of_a_request_still_in_flight(self):
        trial = self.trial(shape='followup', gap=0)
        with self.stub(['success']):
            self.assertTrue(trial.step())  # parked: the trial's proxy is still open
        self.upstream.script, self.upstream.delay = [{'text': 'late'}], .5
        port = trial.proxy.server_port

        def late():
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
            conn.request('POST', '/v1/messages', json.dumps({'model': 'claude-sonnet-5', 'max_tokens': 8, 'stream': True,
                                                             'tools': [{'name': 'Read'}], 'messages': []}),
                         {'Content-Type': 'application/json'})
            conn.getresponse().read()
            conn.close()
        client = threading.Thread(target=late)
        client.start()
        for _ in range(200):
            if trial.proxy.in_flight:
                break
            time.sleep(.005)
        trial.close()
        client.join()
        self.assertEqual(len(bench.read_rows(trial.log)), 2)
        self.assertIsNone(trial.proxy)

    def test_no_follow_up_after_an_unsuccessful_first_session(self):
        trial = self.trial(shape='followup', gap=0)
        with self.stub(['error_max_turns']):
            self.assertFalse(trial.step())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(trial.record['follow_up'], 'skipped_after_turn_limit')

    def test_the_record_keeps_its_accounting_when_grading_crashes(self):
        trial = self.trial()
        with self.stub(['success']), mock.patch.object(bench_tasks, 'grade', side_effect=RuntimeError('grader bug')):
            with self.assertRaises(RuntimeError):
                trial.step()
        saved = json.loads((self.repo_case.root/'trial'/'trial.json').read_text())
        self.assertEqual((saved['phase'], saved['accounting']['requests']), ('grading', 1))

    def test_a_changed_client_stops_the_trial_before_it_runs(self):
        trial = self.trial(client_version='2.1.281 (Claude Code)')
        with self.stub(['success']), mock.patch.object(bench, 'client_version', return_value='2.1.282 (Claude Code)'):
            with self.assertRaises(bench.ClientChanged):
                trial.step()
        self.assertEqual(self.calls, [])


class ClientTests(unittest.TestCase):
    def test_the_client_is_pinned_to_its_real_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp)/'versions'/'9.9'
            binary.parent.mkdir()
            binary.write_text('#!/bin/sh\necho "9.9 (Claude Code)"\n')
            binary.chmod(0o755)
            link = Path(tmp)/'claude'
            link.symlink_to(binary)
            self.assertEqual(bench.resolve_client(link), (binary.resolve(), '9.9 (Claude Code)'))

    def test_leftover_processes_in_a_trial_directory_are_found_and_killed(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)/'workspace'
            work.mkdir()
            # A tool subprocess in its own process group, as Claude Code's Bash tool leaves them.
            child = subprocess.Popen(['sleep', '60'], cwd=work, start_new_session=True)
            try:
                self.assertIn(child.pid, bench.leftover_processes(tmp))
                self.assertNotIn(os.getpid(), bench.leftover_processes(tmp))
                self.assertEqual(bench.reap(tmp), [child.pid])
                self.assertIsNotNone(child.wait(timeout=5))
            finally:
                if child.poll() is None:
                    child.kill()


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.repo_case = synthetic.BenchTaskTests('test_valid_task_fails_on_base_and_passes_on_reference')
        self.repo_case.setUp()
        self.task = dict(self.repo_case.task, repo_path=str(self.repo_case.repo))

    def tearDown(self):
        self.repo_case.tearDown()

    def test_reference_passes_are_recorded(self):
        expected = bench.preflight([self.task], sys.executable, self.repo_case.root/'preflight')
        self.assertEqual(expected['synthetic']['hidden_passed'], 2)

    def test_a_failing_reference_stops_the_run_before_any_request(self):
        broken = dict(self.task, hidden_command=['{python}', '-m', 'unittest', '-q', 'tests.test_missing'])
        with self.assertRaises(bench.PreflightError) as caught:
            bench.preflight([broken], sys.executable, self.repo_case.root/'preflight')
        self.assertIn('synthetic', str(caught.exception))


def normalized(value):
    """Request JSON as the API renders it: cache_control is a marker, a string content is one text block."""
    if isinstance(value, dict):
        out = {k: normalized(v) for k, v in value.items() if k != 'cache_control'}
        if isinstance(out.get('content'), str) and 'role' in out:
            out['content'] = [{'type': 'text', 'text': out['content']}]
        return out
    if isinstance(value, list):
        return [normalized(v) for v in value]
    return value


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
        self.cli, self.version = bench.resolve_client(shutil.which('claude'))

    def tearDown(self):
        self.upstream.shutdown()
        self.upstream.server_close()
        self.thread.join()
        self.repo_case.tearDown()

    def trial(self, name, script, **options):
        work = self.out/name/'workspace'
        self.upstream.script = [dict(step, input={k: (str(work/v) if k == 'file_path' else v) for k, v in step['input'].items()})
                                if 'tool' in step else step for step in script]
        options = dict(dict(python=sys.executable, max_turns=8, client_version=self.version), **options)
        return bench.run_trial(self.task, 'sonnet-5', self.out/name, self.cli, 'sk-ant-offline-not-a-key',
                               f'http://127.0.0.1:{self.upstream.server_port}', RATES, **options)

    FIX = [{'tool': 'Read', 'input': {'file_path': 'pkg/__init__.py'}},
           {'tool': 'Write', 'input': {'file_path': 'pkg/__init__.py', 'content': synthetic.FIXED}},
           {'tool': 'Bash', 'input': {'command': 'python3 -m unittest -q', 'description': 'tests'}},
           {'text': 'Done.'}]

    def test_fixing_agent_passes_and_idle_agent_fails_with_exact_accounting(self):
        fixed = self.trial('fixes', self.FIX)
        self.assertTrue(fixed['passed'], fixed['grade'])
        self.assertEqual(fixed['client']['subtype'], 'success')
        self.assertEqual(fixed['sessions'][0]['stop'], 'success')
        accounting = fixed['accounting']
        self.assertEqual(accounting['requests'], 4)
        self.assertTrue(accounting['tokens_match'], accounting)
        self.assertTrue(accounting['client_cost_matches'], accounting)
        self.assertAlmostEqual(accounting['cost_usd'], 4 * (100*2 + 4*10) / 1e6)
        self.assertEqual(fixed['cache']['cache_start']['warm'], False)
        self.assertAlmostEqual(fixed['cache']['cold_equivalent_cost_usd'], accounting['cost_usd'])
        self.assertEqual(fixed['client_version'], self.version)
        self.assertIn(b'items[-1]', (self.out/'fixes'/'agent.diff').read_bytes())
        self.assertFalse((self.out/'fixes'/'workspace').exists())  # only records are kept
        idle = self.trial('idle', [{'text': 'I could not find the problem.'}])
        self.assertEqual((idle['passed'], idle['grade']['reason']), (False, 'hidden_tests_failed'))
        self.assertEqual(idle['grade']['failing_tests'], ['test_many (tests.test_pkg.T)'])
        saved = json.loads((self.out/'idle'/'trial.json').read_text())
        self.assertEqual(saved['spec_sha256'], idle['spec_sha256'])

    def test_follow_up_resumes_the_session_with_an_unchanged_prefix(self):
        self.upstream.keep_bodies = True
        script = self.FIX + [{'tool': 'Bash', 'input': {'command': 'python3 -m unittest discover -q -s tests -t .',
                                                        'description': 'suite'}},
                             {'text': 'All tests pass.'}]
        record = self.trial('followup', script, shape='followup', gap=0)
        self.assertTrue(record['passed'], record['grade'])
        self.assertEqual([s['stop'] for s in record['sessions']], ['success', 'success'])
        self.assertEqual([s['requests'] for s in record['sessions']], [4, 2])
        accounting = record['accounting']
        # The resumed session's client totals are cumulative, so they match the whole trial's wire totals.
        self.assertTrue(accounting['tokens_match'], accounting)
        self.assertTrue(accounting['client_cost_matches'], accounting)
        self.assertEqual(accounting['requests'], 6)
        bodies = [json.loads(b) for b in self.upstream.bodies]
        tools = [b for b in bodies if b.get('tools')]
        resumed = next(i for i, b in enumerate(tools) if bench.FOLLOW_UP in json.dumps(b['messages']))
        before, after = normalized(tools[resumed - 1]), normalized(tools[resumed])
        # A warm follow-up can only hit the cache if the resumed request starts with the earlier one.
        self.assertEqual(before['system'], after['system'])
        self.assertEqual(before['tools'], after['tools'])
        self.assertEqual(after['messages'][:len(before['messages'])], before['messages'])
        self.assertFalse(record['sessions'][1]['physically_warm'])

    def test_turn_and_budget_limits_are_reported_as_such(self):
        loop = [{'tool': 'Read', 'input': {'file_path': 'pkg/__init__.py'}}] * 4 + [{'text': 'Done.'}]
        turns = self.trial('turns', loop, max_turns=1)
        self.assertEqual(turns['sessions'][0]['stop'], 'turn_limit')
        budget = self.trial('budget', loop, budget_usd=.000001)
        self.assertEqual(budget['sessions'][0]['stop'], 'budget_stop')

    def test_a_timeout_keeps_partial_output_and_leaves_no_processes(self):
        # Generous against a slow start on a busy machine; teardown does not wait for the sleeping reply.
        self.upstream.delay, self.upstream.block_on_close = 15, False
        record = self.trial('slow', [{'text': 'Done.'}], timeout=6, grace=2)
        self.assertEqual((record['status'], record['sessions'][0]['stop']), ('timeout', 'timeout'), record['sessions'])
        output = (self.out/'slow'/'client.stdout.jsonl').read_text()
        self.assertIn('"init"', output, output[-500:])
        self.assertEqual(bench.leftover_processes(self.out/'slow'), [])

    def test_background_tool_processes_do_not_outlive_the_trial(self):
        pidfile = self.repo_case.root/'bg.pid'
        started = self.trial('background', [{'tool': 'Bash', 'input': {
            'command': f'sleep 300 >/dev/null 2>&1 & echo $! > {pidfile}', 'description': 'background'}}, {'text': 'Done.'}])
        self.assertEqual(started['sessions'][0]['stop'], 'success')
        pid = int(pidfile.read_text())
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(.1)
        else:
            os.kill(pid, 9)
            self.fail('background sleep survived the trial')


if __name__ == '__main__': unittest.main()
