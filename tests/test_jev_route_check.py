import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from modelpilot.jev_route_check import (arrangement, check_anthropic_key, child_env, command, git_diff, parse_events,
                                        reconcile, validate, verify_checkout)

TOKEN = 'JEV_ROUTE_ABC'
STDERR = '\n'.join([
    '[jev] 1a2b3c4d5e6f 412ms p=0.91 opus -> sonnet (jev) ctx~5000 | Use the Read tool',
    '[jev] 1a2b3c4d5e6f rewrite jev-router -> claude-sonnet-5',
    '[jev] 200 served by claude-sonnet-5',
    '[jev] passthrough, user selected claude-haiku-4-5-20251001',
    '[jev] 200 served by claude-haiku-4-5-20251001',
    '[jev] 1a2b3c4d5e6f rewrite jev-router -> claude-sonnet-5',
    '[jev] 200 served by claude-sonnet-5',
])
DECISION = {'model': 'claude-sonnet-5', 'tier': 'sonnet', 'confidence': .91, 'reason': 'jev',
            'jev': {'request': {'state': {}}, 'response': {'model': 'jev-1.13.0'}}}


def events(model_usage=None, answer=TOKEN):
    return [{'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'Read'}]}},
            {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': answer, 'total_cost_usd': .03,
             'modelUsage': model_usage or {'claude-sonnet-5': {}, 'claude-haiku-4-5-20251001': {}}}]


class RouteCheckTests(unittest.TestCase):
    def test_genuine_routed_task_passes(self):
        result = validate(events(), STDERR, [DECISION], TOKEN)
        self.assertTrue(result['passed'], result['checks'])
        self.assertEqual((result['selected_model'], result['jev_ms']), ('claude-sonnet-5', 412))

    def test_fail_open_claude_is_not_routing(self):
        stderr = '[jev] routing failed, keeping claude-opus-5: 401\n' + STDERR.replace('412ms p=0.91', 'no-jev')
        result = validate(events(), stderr, [dict(DECISION, jev=None)], TOKEN)
        self.assertFalse(result['passed'])
        self.assertFalse(result['checks']['no_fallback_or_errors'])
        self.assertFalse(result['checks']['decision_has_exchange'])

    def test_continuation_on_another_model_fails(self):
        stderr = STDERR.replace('rewrite jev-router -> claude-sonnet-5\n[jev] 200 served by claude-sonnet-5\n[jev] passthrough',
                                'rewrite jev-router -> claude-opus-5\n[jev] 200 served by claude-sonnet-5\n[jev] passthrough', 1)
        self.assertFalse(validate(events(), stderr, [DECISION], TOKEN)['checks']['continuations_stay_on_selection'])

    def test_serving_must_match_selection_and_client_usage_must_exist(self):
        stderr = STDERR.replace('served by claude-sonnet-5', 'served by claude-opus-5')
        self.assertFalse(validate(events(), stderr, [DECISION], TOKEN)['checks']['selected_model_served'])
        # Claude Code only knows the sentinel, so its per-model usage cannot name the routed model.
        sentinel = validate(events({'jev-router': {'inputTokens': 1}}), STDERR, [DECISION], TOKEN)
        self.assertTrue(sentinel['passed'], sentinel['checks'])
        empty = validate([e if e['type'] != 'result' else dict(e, modelUsage={}) for e in events()], STDERR, [DECISION], TOKEN)
        self.assertFalse(empty['checks']['client_usage_reported'])

    def test_wrong_answer_fails(self):
        self.assertFalse(validate(events(answer='guess'), STDERR, [DECISION], TOKEN)['passed'])

    def test_repeated_same_model_decisions_are_reported_not_hidden(self):
        # Claude Code resends a reshaped opening request after a 400; Jev sees a new conversation.
        stderr = STDERR + '\n[jev] 9f9f9f9f9f9f 101ms p=0.95 opus -> sonnet (jev) ctx~5100 | Use the Read tool'
        result = validate(events(), stderr, [DECISION, DECISION], TOKEN)
        self.assertTrue(result['passed'], result['checks'])
        self.assertEqual((result['jev_decisions'], result['extra_decisions']), (2, 1))

    def test_decisions_on_different_models_fail(self):
        stderr = STDERR + '\n[jev] 9f9f9f9f9f9f 101ms p=0.95 opus -> opus (jev) ctx~5100 | Use the Read tool'
        other = dict(DECISION, model='claude-opus-5', tier='opus')
        result = validate(events(), stderr, [DECISION, other], TOKEN)
        self.assertFalse(result['checks']['jev_decisions_consistent'])
        self.assertIsNone(result['selected_model'])

    def test_decision_log_and_records_must_agree(self):
        self.assertFalse(validate(events(), STDERR, [DECISION, DECISION], TOKEN)['checks']['jev_decisions_consistent'])

    def test_unrouted_sentinel_rewrite_is_flagged(self):
        stderr = '[jev] 1a2b3c4d5e6f rewrite jev-router -> claude-opus-5\n' * 3
        result = validate(events({'claude-opus-5': {}}), stderr, [], TOKEN)
        self.assertFalse(result['passed'])
        self.assertIn('without any Jev decision', result['hint'])
        self.assertIsNone(validate(events(), STDERR, [DECISION], TOKEN)['hint'])

    def test_command_never_pins_a_model(self):
        cmd = command(__import__('pathlib').Path('/jev'), 'prompt', .5)
        self.assertNotIn('--model', cmd)
        self.assertEqual(cmd[1], '/jev/bin/jev-claude.mjs')

    def test_environment_is_explicit(self):
        base = {'PATH': '/usr/bin', 'ANTHROPIC_MODEL': 'claude-opus-5', 'ANTHROPIC_BASE_URL': 'http://elsewhere',
                'CLAUDE_CODE_OAUTH_TOKEN': 'secret', 'HOME': '/real', 'NODE_OPTIONS': '--inspect'}
        env = child_env(base, 'jev', 'sk-ant-x', '/iso/home', '/iso/tmp', '/iso/config', '/cli')
        for name in ('ANTHROPIC_MODEL', 'ANTHROPIC_BASE_URL', 'CLAUDE_CODE_OAUTH_TOKEN', 'NODE_OPTIONS'):
            self.assertNotIn(name, env)
        self.assertEqual((env['HOME'], env['CLAUDE_CONFIG_DIR'], env['JEV_DEBUG']), ('/iso/home', '/iso/config', '1'))
        self.assertTrue(env['PATH'].startswith('/cli'))
        # Client retries would re-trigger Jev routing and change cost/cache state.
        self.assertEqual(env['CLAUDE_CODE_MAX_RETRIES'], '0')

    def test_parse_events_skips_noise(self):
        self.assertEqual(parse_events('noise\n' + json.dumps({'type': 'result'}) + '\n[1]\n'), [{'type': 'result'}])



class CheckoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)/'jev'
        self.repo.mkdir()
        git = lambda *a: subprocess.run(['git', '-C', str(self.repo), *a], check=True, capture_output=True)
        git('init', '-q')
        (self.repo/'proxy.mjs').write_bytes(b'const last = messages.at(-1);\r\n')
        git('add', '.')
        git('-c', 'user.name=t', '-c', 'user.email=t@example.com', 'commit', '-qm', 'pin')
        self.commit = subprocess.run(['git', '-C', str(self.repo), 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()

    def tearDown(self):
        self.tmp.cleanup()

    def test_stock_requires_clean_pinned_checkout(self):
        self.assertTrue(verify_checkout(self.repo, self.commit))
        self.assertFalse(verify_checkout(self.repo, '0'*40))
        (self.repo/'proxy.mjs').write_bytes(b'patched\r\n')
        self.assertFalse(verify_checkout(self.repo, self.commit))

    def test_variant_requires_exactly_the_recorded_patch(self):
        (self.repo/'proxy.mjs').write_bytes(b'const last = messages.findLast(m => m.role !== "system");\r\n')
        patch = Path(self.tmp.name)/'fix.patch'
        patch.write_bytes(git_diff(self.repo))
        self.assertTrue(verify_checkout(self.repo, self.commit, patch))
        (self.repo/'proxy.mjs').write_bytes(b'something else\r\n')
        self.assertFalse(verify_checkout(self.repo, self.commit, patch))



def row(model='claude-sonnet-5', cost=.01, status=200, tools=1, kind='messages', usage=None):
    usage = {'input_tokens': 1000, 'output_tokens': 10, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0} \
        if usage is None and status == 200 and kind == 'messages' else (usage or {})
    return {'kind': kind, 'model': model, 'cost_usd': cost, 'http_status': status, 'tool_count': tools, 'applied': False,
            'usage': usage}


def client_usage(model, inp, out):
    return {model: {'inputTokens': inp, 'outputTokens': out, 'cacheReadInputTokens': 0, 'cacheCreationInputTokens': 0, 'costUSD': .05}}


class WireAccountingTests(unittest.TestCase):
    def setUp(self):
        self.rows = [row(None, None, kind='models', tools=0), row(), row(),
                     row('claude-haiku-4-5-20251001', .001, tools=0, usage={'input_tokens': 100, 'output_tokens': 5,
                         'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0})]
        self.result = {'total_cost_usd': .05, 'modelUsage': dict(client_usage('jev-router', 2000, 20),
                                                                  **client_usage('claude-haiku-4-5-20251001', 100, 5))}
        self.decisions = [dict(DECISION, jev={'request': {'state': {}}, 'response': {'usage': {'input_tokens': 893, 'output_tokens': 100}}})]

    def test_tokens_and_models_reconcile(self):
        r = reconcile(self.rows, self.result, 'claude-sonnet-5', self.decisions)
        self.assertTrue(r['accounting_matches'], r)
        self.assertTrue(r['cost_complete'])
        self.assertEqual((r['proxy_requests'], r['catalog_requests'], r['unpriced_requests']), (3, 1, 0))
        self.assertAlmostEqual(r['proxy_known_cost_usd'], .021)
        self.assertEqual(r['helper_models'], ['claude-haiku-4-5-20251001'])
        self.assertEqual(r['routed_model_mismatch'], [])
        # The client priced the sentinel with its own guess; its dollars are recorded, never trusted.
        self.assertEqual(r['client_cost_basis'], 'sentinel_model_unknown_price')

    def test_token_mismatch_or_missing_client_usage_fails(self):
        wrong = dict(self.result, modelUsage=client_usage('jev-router', 2001, 25))
        self.assertFalse(reconcile(self.rows, wrong, 'claude-sonnet-5', self.decisions)['accounting_matches'])
        self.assertFalse(reconcile(self.rows, {}, 'claude-sonnet-5', self.decisions)['accounting_matches'])

    def test_unpriced_success_fails(self):
        r = reconcile(self.rows + [row(cost=None, usage={'input_tokens': 0, 'output_tokens': 0})], self.result, 'claude-sonnet-5', self.decisions)
        self.assertEqual(r['unpriced_requests'], 1)
        self.assertFalse(r['accounting_matches'])

    def test_rejected_request_is_listed_and_cost_marked_incomplete(self):
        r = reconcile(self.rows + [row(cost=None, status=400)], self.result, 'claude-sonnet-5', self.decisions)
        self.assertTrue(r['accounting_matches'], r)  # tokens and models still reconcile
        self.assertFalse(r['cost_complete'])  # never assumed free
        self.assertEqual(r['rejected_requests'], [{'http_status': 400, 'model': 'claude-sonnet-5'}])

    def test_routed_model_change_fails_but_helper_call_does_not(self):
        rows = self.rows[:2] + [row('claude-opus-5', .01)] + self.rows[3:]
        r = reconcile(rows, self.result, 'claude-sonnet-5', self.decisions)
        self.assertEqual(r['routed_model_mismatch'], ['claude-opus-5'])
        self.assertFalse(r['accounting_matches'])

    def test_empty_log_never_passes(self):
        self.assertFalse(reconcile([], {'modelUsage': {}}, 'claude-sonnet-5', self.decisions)['accounting_matches'])

    def test_router_usage_recorded_and_left_unpriced(self):
        r = reconcile(self.rows, self.result, 'claude-sonnet-5', self.decisions)
        self.assertEqual(r['router_usage'], [{'input_tokens': 893, 'output_tokens': 100}])
        self.assertIsNone(r['router_cost_usd'])

    def test_arrangement_labels(self):
        wire = arrangement('compat', 'wire', {'path': 'p', 'sha256': 'x'})
        self.assertEqual((wire['accounting'], wire['launcher'], wire['variant']), ('wire', 'accounted-harness', 'compat-patched'))
        self.assertFalse(wire['baseline_eligible_as_stock_jev'])
        client = arrangement('stock', 'client', None)
        self.assertEqual((client['accounting'], client['launcher'], client['harness_patch']), ('client', 'stock', None))
        self.assertTrue(client['baseline_eligible_as_stock_jev'])



class KeyCheckTests(unittest.TestCase):
    def fake(self, status):
        calls = []
        def send(url, headers):
            calls.append((url, headers))
            return status
        return send, calls

    def test_valid_key_passes_with_one_free_catalog_call(self):
        send, calls = self.fake(200)
        self.assertIsNone(check_anthropic_key('sk-ant-api03-abc', send))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 'https://api.anthropic.com/v1/models?limit=1')
        self.assertEqual(calls[0][1]['x-api-key'], 'sk-ant-api03-abc')

    def test_rejected_key_stops_before_any_billable_work(self):
        send, _ = self.fake(401)
        self.assertIn('401', check_anthropic_key('sk-ant-api03-abc', send))

    def test_subscription_token_is_refused_without_network(self):
        send, calls = self.fake(200)
        self.assertIn('subscription', check_anthropic_key('sk-ant-oat01-abc', send))
        self.assertEqual(calls, [])

    def test_malformed_key_refused_without_network(self):
        send, calls = self.fake(200)
        for key in ('', 'sk-live-x', 'sk-ant-api03 abc'):
            self.assertIsNotNone(check_anthropic_key(key, send))
        self.assertEqual(calls, [])


if __name__ == '__main__': unittest.main()
