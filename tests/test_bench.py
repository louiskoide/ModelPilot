import http.client
import hashlib
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
from modelpilot.policy_actions import MODELS as POLICY_TIERS
from tests import test_bench_tasks as synthetic
from tests import test_regrade as regrade_tests

# Sonnet 5 serves the harness tests' fixed arm; Sonnet 5.5 is the ModelPilot arm's start tier. Same rates.
RATES = {m: dict(input=2, output=10, read=.2, write_5m=2.5, write_1h=4) for m in ('claude-sonnet-5', 'claude-sonnet-5-5')}


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

    def test_the_modelpilot_arm_needs_its_adapter(self):
        with self.assertRaises(NotImplementedError):
            bench.run_trial({'id': 't'}, 'modelpilot', '/nonexistent', 'claude', 'k', 'http://127.0.0.1:1', RATES)

    def test_the_modelpilot_arm_gets_the_active_policy_jev_as_advisor_and_the_same_limit_per_session(self):
        self.assertIsNone(bench.arm_adapter('sonnet-5', 1.5, 1))
        adapter = bench.arm_adapter('modelpilot', 1.5, 2, jev_key='ts-key')
        self.assertEqual((adapter.policy.mode, adapter.tools, adapter.channel, adapter.limit), ('active', False, False, 3.0))
        delegate = bench.arm_adapter('modelpilot-delegate', 1.5, 2, jev_key='ts-key')  # tools and channel with delegation
        self.assertEqual((delegate.tools, delegate.channel), (True, True))
        self.assertTrue(bench.arm_adapter('modelpilot', 1, 1, tools=True).tools)  # offline tests' host test runs
        self.assertEqual((adapter.arm_id, adapter.model, adapter.effort), ('modelpilot', 'claude-sonnet-5-5', 'low'))
        self.assertIs(adapter.policy.advisor, adapter.advisor)
        self.assertEqual((adapter.advisor.live, adapter.advisor.key, adapter.advisor.checkout),
                         (True, 'ts-key', bench.ROOT/'work/jev-router-compat'))
        self.assertIsNone(bench.arm_adapter('modelpilot', 1, 1).advisor)  # no key: no advice, never eligible
        self.assertFalse(bench.arm_adapter('modelpilot', 1, 1, advisor_stub={}).advisor.live)
        arm = bench.ARMS['modelpilot']
        self.assertEqual((arm['served_models'], arm['models']), (['claude-opus-5-5', 'claude-sonnet-5-5'], POLICY_TIERS))
        self.assertNotIn('modelpilot-o55', bench.ARMS)  # the start is Jev's prediction now, not a tuned variant
        self.assertTrue(all(m in bench.rates() for m in arm['served_models']))
        with self.assertRaises(ValueError):  # Haiku targets are not implemented
            from modelpilot.modelpilot_adapter import ModelPilotAdapter
            ModelPilotAdapter(mode='active', tools=True, model='claude-haiku-4-5-20251001', effort=None)
        manifest = bench.modelpilot_manifest(['sonnet-5', 'modelpilot', 'modelpilot-delegate'], 1.0, 1)
        self.assertEqual([(manifest[a]['r5_tools'], manifest[a]['channel_declared'])
                          for a in ('modelpilot', 'modelpilot-delegate')], [(False, False), (True, True)])
        parameters = manifest['modelpilot']['parameters']
        self.assertEqual(parameters['decision_points'], ['turn_start', 'stuck_evidence', 'step'])
        self.assertEqual((parameters['step']['enabled'], parameters['return_reuse']),
                         (True, {'enabled': True, 'max_positions': 25}))
        self.assertIn('claude-opus-5-5/xhigh', parameters['settings'])
        self.assertNotIn('ladder', parameters)
        self.assertIsNone(bench.modelpilot_manifest(['sonnet-5'], 1.0, 1))

    def test_refused_requests_are_counted_apart_from_sent_ones(self):
        rows = [{'kind': 'messages', 'http_status': 200, 'cost_usd': .01, 'usage': {'input_tokens': 5}},
                {'kind': 'refused', 'refusal': 'insufficient_budget', 'http_status': 400, 'cost_usd': 0.0}]
        result = bench.accounting(rows, {})
        self.assertEqual((result['requests'], result['rejected_requests'], result['refused_requests'], result['cost_usd']),
                         (1, 0, 1, .01))
        self.assertEqual(result['refusal_reasons'], {'insufficient_budget': 1})

    def test_every_fixed_arm_model_has_rates(self):
        table = bench.rates()
        self.assertTrue(all(arm['model'] in table for arm in bench.ARMS.values() if arm['kind'] == 'fixed'))

    def test_fixed_arms_cover_the_policy_tiers(self):
        from modelpilot.policy_actions import MODELS
        self.assertEqual(bench.ARMS['opus-5.5'], {'kind': 'fixed', 'model': 'claude-opus-5-5'})
        self.assertEqual(bench.ARMS['sonnet-5.5'], {'kind': 'fixed', 'model': 'claude-sonnet-5-5'})
        self.assertEqual(bench.ARMS['sonnet-5.5-low'], {'kind': 'fixed', 'model': 'claude-sonnet-5-5', 'effort': 'low'})
        self.assertEqual(bench.ARMS['sonnet-5.5-concise'], {'kind': 'fixed', 'model': 'claude-sonnet-5-5',
                                                            'append_system_prompt': 'bench/prompts/concise.md'})
        self.assertEqual(bench.ARMS['sonnet-5.5-low-concise'], {'kind': 'fixed', 'model': 'claude-sonnet-5-5', 'effort': 'low',
                                                                'append_system_prompt': 'bench/prompts/concise.md'})
        fixed = {arm['model'] for arm in bench.ARMS.values() if arm['kind'] == 'fixed'}
        self.assertTrue(set(MODELS) <= fixed)

    def test_policy_tiers_have_rates_with_provenance(self):
        from modelpilot.cache_replication import RATES
        for model, name, rate in (('claude-opus-5-5', 'opus-5-5', dict(input=4, output=20, write_5m=5, write_1h=8, read=.2)),
                                  ('claude-sonnet-5-5', 'sonnet-5-5', dict(input=2, output=10, write_5m=2.5, write_1h=4, read=.2))):
            self.assertEqual(bench.rates()[model], rate)
            self.assertEqual(RATES[model], rate)  # the probe's table
            config = json.loads((bench.ROOT/f'configs/{name}-rates.json').read_text())
            self.assertTrue(config['source'] and config['retrieved'])

    def test_session_end_reasons(self):
        ok = {'subtype': 'success', 'is_error': False}
        cases = [(('completed', ok, []), 'success'),
                 (('timeout', {}, []), 'timeout'),
                 (('completed', {'subtype': 'error_max_turns', 'is_error': True}, []), 'turn_limit'),
                 (('completed', {'subtype': 'error_max_budget_usd', 'is_error': True}, []), 'budget_stop'),
                 (('completed', {}, [{'kind': 'messages', 'status': 'transport_error'}]), 'transport_error'),
                 (('completed', {'subtype': 'error_during_execution', 'is_error': True, 'api_error_status': 400}, []), 'api_error'),
                 # runs/bench-20260928-150510: every trial ended like this when the account ran out of credit
                 (('completed', {'subtype': 'success', 'is_error': True, 'api_error_status': 400,
                                 'result': 'Credit balance is too low'}, []), 'account_error'),
                 (('completed', {'subtype': 'success', 'is_error': True, 'api_error_status': 401,
                                 'result': 'Invalid API key · Fix external API key'}, []), 'account_error'),
                 # A subscription token the API refuses; the message names no API key.
                 (('completed', {'subtype': 'success', 'is_error': True, 'api_error_status': 401,
                                 'result': 'OAuth token has expired'}, []), 'account_error'),
                 # A usage or rate limit: from the client's result, or from the proxy's row when the client says nothing.
                 (('completed', {'subtype': 'success', 'is_error': True, 'api_error_status': 429,
                                 'result': 'Claude usage limit reached'}, []), 'rate_limited'),
                 (('completed', {'subtype': 'error_during_execution', 'is_error': True},
                   [{'kind': 'messages', 'http_status': 429}]), 'rate_limited'),
                 (('completed', {'subtype': 'error_during_execution', 'is_error': True}, []), 'client_error'),
                 (('completed', {}, []), 'client_error')]
        error = {'subtype': 'success', 'is_error': True}  # how the client ends after a proxy refusal
        for reason, expected in (('insufficient_budget', 'budget_stop'), ('cost_unknown', 'cost_unknown_halt'),
                                 ('policy_stop:re_diagnose', 'policy_stop'), ('stale_task', 'policy_refused')):
            cases.append((('completed', error, [{'kind': 'refused', 'refusal': reason}]), expected))
        for args, expected in cases:
            self.assertEqual(bench.stop_reason(*args), expected, args)

    def test_only_the_pinned_client_may_run(self):
        pinned = json.loads((bench.ROOT/'bench/environment.json').read_text())['claude_code']
        self.assertIsNone(bench.client_problem(f'{pinned} (Claude Code)'))
        for other in ('2.1.278 (Claude Code)', '2.1.2840 (Claude Code)', '', None):  # 4a ran 2.1.278 on another computer
            problem = bench.client_problem(other)
            self.assertIn(f'npm install --save-exact @anthropic-ai/claude-code@{pinned}', problem)

    def test_the_manifest_records_the_code_it_ran(self):
        revision = bench.code_revision()
        if revision is not None:  # a Git checkout
            self.assertRegex(revision['commit'], '^[0-9a-f]{40}$')
            self.assertIsInstance(revision['uncommitted_changes'], bool)

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

    def test_the_policys_side_calls_are_billed_but_not_matched_against_the_client(self):
        rows = [{'kind': 'messages', 'http_status': 200, 'cost_usd': .01, 'usage': {'input_tokens': 5}},
                {'kind': 'side_call', 'purpose': 'consult', 'http_status': 200, 'status': 'ok', 'cost_usd': .03,
                 'usage': {'input_tokens': 900}},
                {'kind': 'side_call', 'purpose': 'consult', 'status': 'refused', 'cost_usd': 0.0}]
        final = {'total_cost_usd': .01, 'modelUsage': {'claude-sonnet-5-5': {'inputTokens': 5}}}
        result = bench.accounting(rows, final)
        self.assertEqual((result['requests'], result['side_calls'], result['side_refused']), (1, 1, 1))
        self.assertAlmostEqual(result['cost_usd'], .04)
        self.assertAlmostEqual(result['side_cost_usd'], .03)
        self.assertEqual(result['side_purposes'], {'consult': 1})
        self.assertTrue(result['tokens_match'])  # the client never saw the consult
        self.assertTrue(result['client_cost_matches'])
        unpriced = bench.accounting(rows + [{'kind': 'side_call', 'purpose': 'handoff_note', 'status': 'transport_error',
                                             'cost_usd': None}], final)
        self.assertIsNone(unpriced['cost_usd'])  # unknown stays unknown
        self.assertIsNone(unpriced['side_cost_usd'])
        self.assertNotIn('side_calls', bench.accounting(rows[:1], final))  # other arms' records are unchanged

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

    def test_a_fixed_arm_passes_its_effort_and_the_others_leave_it_to_the_client(self):
        command = bench.client_command('/c', 'do it', 'claude-sonnet-5-5', 30, 1.0, ['--session-id', 'x'], effort='low')
        self.assertEqual(command[command.index('--effort') + 1], 'low')
        self.assertNotIn('--effort', bench.client_command('/c', 'do it', 'claude-sonnet-5-5', 30, 1.0, ['--session-id', 'x']))

    def test_an_arm_appends_its_prompt_file_and_the_others_do_not(self):
        appended = bench.appended_prompt(bench.ARMS['sonnet-5.5-concise'])
        data = (bench.ROOT/'bench/prompts/concise.md').read_bytes()
        self.assertEqual((appended['file'], appended['sha256']), (bench.ROOT/'bench/prompts/concise.md', hashlib.sha256(data).hexdigest()))
        self.assertEqual(appended['text'], data.decode().strip())
        self.assertTrue(all(len(line) < 400 for line in appended['text'].splitlines()))
        self.assertLess(len(data), 1500)  # written once per session and re-read every step: keep it short
        for arm in ('sonnet-5.5', 'sonnet-5.5-low', 'jev-compat-o55'):
            self.assertIsNone(bench.appended_prompt(bench.ARMS[arm]), arm)
        # The ModelPilot arms start where low concise runs (October 6): low effort, the same prompt.
        for arm in ('modelpilot', 'modelpilot-delegate'):
            self.assertEqual(bench.appended_prompt(bench.ARMS[arm]), appended, arm)
            self.assertEqual(bench.ARMS[arm]['effort'], bench.ARMS['sonnet-5.5-low-concise']['effort'])
        command = bench.client_command('/c', 'do it', 'claude-sonnet-5-5', 30, 1.0, ['--session-id', 'x'],
                                       append_prompt=appended['file'])
        self.assertEqual(command[command.index('--append-system-prompt-file') + 1], str(appended['file']))
        self.assertNotIn('--append-system-prompt-file',
                         bench.client_command('/c', 'do it', 'claude-sonnet-5-5', 30, 1.0, ['--session-id', 'x']))

    def test_the_prompt_check_reads_main_loop_requests_only(self):
        rows = [{'kind': 'messages', 'tool_count': 6, 'system_marker': True},
                {'kind': 'messages', 'tool_count': 0, 'system_marker': False},  # a side call has its own system prompt
                {'kind': 'count_tokens', 'tool_count': 6}]
        self.assertEqual(bench.prompt_check(rows, 'abc'), {'requested_sha256': 'abc', 'present': 1, 'missing': 0, 'applied': True})
        missing = rows + [{'kind': 'messages', 'tool_count': 6, 'system_marker': False}, {'kind': 'messages', 'tool_count': 6}]
        self.assertEqual(bench.prompt_check(missing, 'abc'), {'requested_sha256': 'abc', 'present': 1, 'missing': 2, 'applied': False})
        self.assertIsNone(bench.prompt_check([], 'abc')['applied'])

    def test_the_effort_check_reads_main_loop_requests_only(self):
        rows = [{'kind': 'messages', 'tool_count': 6, 'effort': 'low'},
                {'kind': 'messages', 'tool_count': 0, 'effort': None},  # a side call
                {'kind': 'count_tokens', 'tool_count': 6}]
        self.assertEqual(bench.effort_check(rows, 'low'), {'requested': 'low', 'sent': {'low': 1}, 'applied': True})
        ignored = rows + [{'kind': 'messages', 'tool_count': 6, 'effort': 'medium'}]
        self.assertEqual(bench.effort_check(ignored, 'low'),
                         {'requested': 'low', 'sent': {'low': 1, 'medium': 1}, 'applied': False})
        # A per-message effort would be what the model ran at.
        self.assertFalse(bench.effort_check([{'kind': 'messages', 'tool_count': 6, 'effort': 'low',
                                              'effective_effort': 'high'}], 'low')['applied'])
        # Nothing sent is unknown, not a mismatch: the trial keeps its own stop reason and isn't excluded for effort.
        self.assertIsNone(bench.effort_check([], 'low')['applied'])

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
    def __init__(self, name, clock, sessions=2, seconds=100, cost=.4, fail_on=None, unavailable_after=None, account_error=False,
                 rate_limited=False, auth='api_key'):
        self.key, self.clock, self.sessions, self.seconds, self.cost, self.fail_on = (name, 'sonnet-5', 0), clock, sessions, seconds, cost, fail_on
        self.ran, self.started, self.finished, self.record = 0, False, False, None
        self.unavailable_after, self.router_unavailable, self.closed = unavailable_after, False, False
        self.account_error_after, self.account_error = account_error, False
        self.rate_limited_after, self.rate_limited, self.auth = rate_limited, False, auth

    def step(self):
        if self.fail_on == self.ran + 1:
            raise RuntimeError('harness bug')
        self.started = True
        self.clock.now += self.seconds
        self.clock.log.append((self.key[0], self.ran + 1))
        self.ran += 1
        self.router_unavailable = self.ran == self.unavailable_after
        self.account_error = bool(self.account_error_after)
        self.rate_limited = bool(self.rate_limited_after)
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


class SubscriptionAccountingTests(unittest.TestCase):
    def rows(self):
        usage = {'input_tokens': 2, 'output_tokens': 100, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 8000,
                 'cache_creation': {'ephemeral_5m_input_tokens': 0, 'ephemeral_1h_input_tokens': 8000}}
        return [{'kind': 'messages', 'http_status': 200, 'model': 'claude-sonnet-5-5', 'usage': usage,
                 'cost_usd': (2*2 + 100*10 + 8000*4) / 1e6}]

    def test_dollars_are_api_key_equivalent_and_as_sent_is_kept(self):
        final = {'total_cost_usd': (2*2 + 100*10 + 8000*4) / 1e6,
                 'modelUsage': {'claude-sonnet-5-5': {'inputTokens': 2, 'outputTokens': 100, 'cacheReadInputTokens': 0,
                                                     'cacheCreationInputTokens': 8000}}}
        out = bench.subscription_accounting(self.rows(), final, RATES)
        self.assertAlmostEqual(out['cost_usd'], (2*2 + 100*10 + 8000*2.5) / 1e6)
        self.assertAlmostEqual(out['as_sent']['cost_usd'], (2*2 + 100*10 + 8000*4) / 1e6)
        self.assertEqual((out['cost_scope'], out['billing']), ('api_key_equivalent', 'subscription'))
        self.assertTrue(out['client_cost_matches'])  # the client priced what it actually sent

    def test_only_fixed_arms_run_on_a_subscription_with_a_token(self):
        task = {'id': 't', 'instruction': 'x'}
        for arm, token in (('sonnet-5.5', None), ('jev-compat-o55', 'sk-ant-oat01-offline')):
            with self.subTest(arm=arm), self.assertRaises(ValueError):
                bench.Trial(task, arm, '/tmp/unused', '/fake/claude', None, 'http://127.0.0.1:1', RATES,
                            auth='subscription', oauth_token=token)
        with self.assertRaises(ValueError):
            bench.Trial(task, 'sonnet-5.5', '/tmp/unused', '/fake/claude', 'k', 'http://127.0.0.1:1', RATES, auth='oauth')


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
        self.assertEqual(manifest['code'], bench.code_revision())  # which ModelPilot design ran

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

    def test_a_rate_limit_stops_the_run(self):
        first = bench.schedule([{'id': n} for n in 'ABC'], ['sonnet-5'], 1, 0)[0][0]
        summary = self.run_fake('ABC', fake={first: {'rate_limited': True, 'sessions': 1}}, shape='single')
        self.assertEqual((summary['stopped'], summary['complete'], len(self.made)), ('rate_limited', False, 1))

    def test_subscription_trials_do_not_count_toward_the_run_budget(self):
        fake = {n: {'auth': 'subscription', 'sessions': 1} for n in 'ABC'}
        summary = self.run_fake('ABC', fake=fake, shape='single', run_budget=0.5, subscription_arms=['sonnet-5'],
                                oauth_token='sk-ant-oat01-offline')
        self.assertEqual((summary['stopped'], summary['known_spend_usd']), (None, 0))
        self.assertAlmostEqual(summary['subscription_as_sent_usd'], 1.2)
        manifest = json.loads((self.out/'manifest.json').read_text())
        self.assertEqual(manifest['auth'], {'sonnet-5': 'subscription'})
        self.assertIn('1h cache writes', manifest['subscription_note'])

    def test_subscription_arms_must_be_fixed_arms_of_the_run_with_a_token(self):
        for arms, token in ((['opus-5.5'], 'sk-ant-oat01-offline'), (['sonnet-5'], None)):
            with self.subTest(arms=arms), self.assertRaises(ValueError):
                self.run_fake('A', subscription_arms=arms, oauth_token=token)

    def test_an_anthropic_account_error_stops_the_run(self):
        first = bench.schedule([{'id': n} for n in 'ABC'], ['sonnet-5'], 1, 0)[0][0]
        summary = self.run_fake('ABC', fake={first: {'account_error': True, 'sessions': 1}}, shape='single')
        self.assertEqual((summary['stopped'], summary['complete'], len(self.made)), ('anthropic_account_error', False, 1))


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

    def stub(self, subtypes, fix=None):
        results = iter(subtypes)

        def run_client(command, env, cwd, timeout, **_):
            self.calls.append(command)
            if fix is not None:  # what the agent leaves in its checkout
                (Path(cwd)/'pkg'/'__init__.py').write_text(fix)
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

    def test_a_rejected_advisor_key_makes_the_router_unavailable(self):
        class RejectedKey:  # the ModelPilot arm's adapter after TypeSafe refused its key
            key, arm_id, model = '', 'sonnet-5', 'claude-sonnet-5'
            def verify(self): pass
            def proxy_options(self): return {}
            def command(self, command, proxy_url): return command
            def environment(self, env): return env
            def accounting(self, rows, final): return bench.accounting(rows, final)
            def evidence(self, directory):
                return {'kind': 'modelpilot_policy', 'advisor': {'live': True, 'calls': 1, 'failures': 1, 'auth_failures': 1}}
        trial = self.trial(adapter=RejectedKey())
        with self.stub(['success']):
            trial.step()
        self.assertTrue(trial.router_unavailable)  # run_bench then stops with jev_router_unavailable
        trial.close()

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

    def test_a_fix_passes_only_with_its_tasks_edge_suite(self):
        edge = self.repo_case.root/'edge'
        (edge/'synthetic').mkdir(parents=True)
        (edge/'synthetic'/'test_edge.py').write_text(regrade_tests.EDGE)
        records = {}
        for name, fix in (('partial', regrade_tests.LISTS_ONLY), ('fixed', synthetic.FIXED)):
            self.calls = []
            trial = bench.Trial(self.task, 'sonnet-5', self.repo_case.root/name, '/fake/claude', 'k',
                                f'http://127.0.0.1:{self.upstream.server_port}', RATES, python=sys.executable)
            with self.stub(['success'], fix=fix), mock.patch.object(bench, 'EDGE_ROOT', edge):
                self.assertFalse(trial.step())
            records[name] = trial.record
        partial, fixed = records['partial'], records['fixed']
        self.assertTrue(partial['grade']['passed'])  # the hidden grader can't tell them apart
        self.assertEqual((partial['edge']['tests_passed'], partial['edge']['tests_run']), (1, 2))
        self.assertEqual(partial['grade']['edge_passed'], False)
        self.assertFalse(partial['passed'])
        self.assertEqual((fixed['grade']['edge_passed'], fixed['passed']), (True, True))
        self.assertEqual({partial['pass_rule'], fixed['pass_rule']}, {'hidden_and_edge'})

    def test_a_task_without_an_edge_suite_passes_on_its_hidden_tests(self):
        trial = self.trial()
        with self.stub(['success'], fix=synthetic.FIXED), mock.patch.object(bench, 'EDGE_ROOT', self.repo_case.root/'none'):
            trial.step()
        self.assertTrue(trial.record['passed'])
        self.assertIsNone(trial.record['grade']['edge_passed'])
        self.assertEqual(trial.record['pass_rule'], 'hidden')
        self.assertNotIn('edge', trial.record)

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

    def test_an_edge_suite_must_pass_on_the_reference_before_any_request(self):
        edge = self.repo_case.root/'edge'
        (edge/'synthetic').mkdir(parents=True)
        (edge/'synthetic'/'test_edge.py').write_text(regrade_tests.EDGE)
        with mock.patch.object(bench, 'EDGE_ROOT', edge):
            expected = bench.preflight([self.task], sys.executable, self.repo_case.root/'preflight')
            self.assertEqual(expected['synthetic']['edge_tests'], 2)
            (edge/'synthetic'/'test_beyond.py').write_text(regrade_tests.BEYOND_REFERENCE)
            with self.assertRaises(bench.PreflightError) as caught:
                bench.preflight([self.task], sys.executable, self.repo_case.root/'preflight2')
        self.assertIn('edge suite', str(caught.exception))
        with mock.patch.object(bench, 'EDGE_ROOT', edge):
            self.assertEqual(set(bench.edge_manifest([self.task])['synthetic']), {'test_edge.py', 'test_beyond.py'})
        self.assertIsNone(bench.edge_manifest([dict(self.task, id='no-suite')]))

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

    def trial(self, name, script, arm='sonnet-5', **options):
        work = self.out/name/'workspace'
        self.upstream.script = [dict(step, input={k: (str(work/v) if k == 'file_path' else v) for k, v in step['input'].items()})
                                if 'tool' in step else step for step in script]
        options = dict(dict(python=sys.executable, max_turns=8, client_version=self.version), **options)
        return bench.run_trial(self.task, arm, self.out/name, self.cli, 'sk-ant-offline-not-a-key',
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
        # An agent that commits its fix (Haiku did in 4a): the diff and the test-config check still see it.
        commit = 'git add -A && git -c user.name=a -c user.email=a@localhost commit -qm fix'
        committed = self.trial('commits', self.FIX[:2] + [
            {'tool': 'Write', 'input': {'file_path': 'conftest.py', 'content': ''}},
            {'tool': 'Bash', 'input': {'command': commit, 'description': 'commit'}}, {'text': 'Done.'}])
        self.assertTrue(committed['passed'], committed['grade'])
        self.assertTrue(committed['agent_moved_head'])
        self.assertEqual(committed['test_config_changed'], ['conftest.py'])
        self.assertIn(b'items[-1]', (self.out/'commits'/'agent.diff').read_bytes())
        self.assertFalse(fixed['agent_moved_head'])
        idle = self.trial('idle', [{'text': 'I could not find the problem.'}])
        self.assertEqual((idle['passed'], idle['grade']['reason']), (False, 'hidden_tests_failed'))
        self.assertEqual(idle['grade']['failing_tests'], ['test_many (tests.test_pkg.T)'])
        saved = json.loads((self.out/'idle'/'trial.json').read_text())
        self.assertEqual(saved['spec_sha256'], idle['spec_sha256'])

    def test_a_subscription_trial_logs_in_with_its_token_and_never_records_it(self):
        token = 'sk-ant-oat01-offline-fixture-not-a-token-1234'
        record = self.trial('subscription', self.FIX, auth='subscription', oauth_token=token)
        self.assertTrue(record['passed'], record['grade'])
        self.assertEqual((record['auth'], record['sessions'][0]['stop']), ('subscription', 'success'))
        sent = [r for r in self.upstream.received if 'sha256' in r]
        self.assertTrue(sent)
        self.assertTrue(all(r['bearer'] and r['key'] is None for r in sent))  # no API key reached the upstream
        accounting = record['accounting']
        self.assertEqual((accounting['cost_scope'], accounting['billing']), ('api_key_equivalent', 'subscription'))
        self.assertTrue(accounting['tokens_match'], accounting)
        self.assertIn('as_sent', record)
        for path in (self.out/'subscription').rglob('*'):
            if path.is_file():
                self.assertNotIn(token.encode(), path.read_bytes(), path)
        # The client's own budget stop still applies when it is logged in with a subscription.
        stopped = self.trial('subscription-budget', self.FIX, auth='subscription', oauth_token=token, budget_usd=0.0001)
        self.assertEqual(stopped['sessions'][0]['stop'], 'budget_stop')

    def test_a_low_effort_arm_sends_low_effort_with_an_api_key_and_on_a_subscription(self):
        token = 'sk-ant-oat01-offline-fixture-not-a-token-1234'
        for name, options in (('low', {}), ('low-subscription', dict(auth='subscription', oauth_token=token))):
            record = self.trial(name, self.FIX, arm='sonnet-5.5-low', **options)
            self.assertTrue(record['passed'], record['grade'])
            self.assertEqual((record['model'], record['effort']), ('claude-sonnet-5-5', 'low'))
            self.assertEqual(record['effort_check'], {'requested': 'low', 'sent': {'low': 4}, 'applied': True}, name)
            self.assertEqual([s['setting'] for s in record['path']['steps']], [['claude-sonnet-5-5', 'low']])
            self.assertTrue(record['accounting']['tokens_match'], record['accounting'])
        # The arm without an effort leaves it to the client, which asks for medium.
        default = self.trial('default', self.FIX, arm='sonnet-5.5')
        self.assertNotIn('effort_check', default)
        self.assertEqual([s['setting'] for s in default['path']['steps']], [['claude-sonnet-5-5', 'medium']])

    def test_a_concise_arm_sends_its_prompt_with_an_api_key_and_on_a_subscription(self):
        token = 'sk-ant-oat01-offline-fixture-not-a-token-1234'
        text = (bench.ROOT/'bench/prompts/concise.md').read_text().strip()
        self.upstream.keep_bodies = True
        for name, options in (('concise', {}), ('concise-subscription', dict(auth='subscription', oauth_token=token))):
            before = len(self.upstream.bodies)
            record = self.trial(name, self.FIX, arm='sonnet-5.5-concise', **options)
            self.assertTrue(record['passed'], record['grade'])
            self.assertEqual((record['model'], record['effort']), ('claude-sonnet-5-5', None))
            self.assertEqual(record['append_system_prompt']['path'], 'bench/prompts/concise.md')
            self.assertEqual(record['prompt_check'], {'requested_sha256': record['append_system_prompt']['sha256'],
                                                      'present': 4, 'missing': 0, 'applied': True}, name)
            self.assertEqual([s['setting'] for s in record['path']['steps']], [['claude-sonnet-5-5', 'medium']])
            sent = [json.loads(b) for b in self.upstream.bodies[before:]]
            main = [b for b in sent if b.get('tools')]
            self.assertTrue(main and all(b['system'][-1]['text'].rstrip().endswith(text) for b in main), name)
            self.assertNotIn(text[:40], (self.out/name/'observations.jsonl').read_text())  # the log keeps only the flag
            self.assertEqual(sum(record['write_sources']['tokens'].values()),
                             sum(json.loads(l).get('usage', {}).get('cache_creation_input_tokens', 0)
                                 for l in (self.out/name/'observations.jsonl').read_text().splitlines()
                                 if json.loads(l).get('kind') == 'messages'))
        default = self.trial('default-prompt', self.FIX, arm='sonnet-5.5')
        self.assertNotIn('prompt_check', default)
        self.assertIsNone(default['append_system_prompt'])

    def test_a_low_concise_arm_sends_low_effort_and_its_prompt_and_checks_both(self):
        token = 'sk-ant-oat01-offline-fixture-not-a-token-1234'
        text = (bench.ROOT/'bench/prompts/concise.md').read_text().strip()
        self.upstream.keep_bodies = True
        for name, options in (('low-concise', {}), ('low-concise-subscription', dict(auth='subscription', oauth_token=token))):
            before = len(self.upstream.bodies)
            record = self.trial(name, self.FIX, arm='sonnet-5.5-low-concise', **options)
            self.assertTrue(record['passed'], record['grade'])
            self.assertEqual((record['model'], record['effort']), ('claude-sonnet-5-5', 'low'))
            self.assertEqual(record['effort_check'], {'requested': 'low', 'sent': {'low': 4}, 'applied': True}, name)
            self.assertTrue(record['prompt_check']['applied'], name)
            self.assertEqual(record['prompt_check']['missing'], 0)
            self.assertEqual([s['setting'] for s in record['path']['steps']], [['claude-sonnet-5-5', 'low']])
            main = [b for b in (json.loads(x) for x in self.upstream.bodies[before:]) if b.get('tools')]
            self.assertTrue(main and all(b['output_config']['effort'] == 'low' and
                                         b['system'][-1]['text'].rstrip().endswith(text) for b in main), name)

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
