import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from modelpilot.jev_route_check import (arrangement, child_env, command, git_diff, parse_events, reconcile,
                                        validate, verify_checkout)

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

    def test_serving_and_billing_must_match_selection(self):
        stderr = STDERR.replace('served by claude-sonnet-5', 'served by claude-opus-5')
        self.assertFalse(validate(events(), stderr, [DECISION], TOKEN)['checks']['selected_model_served'])
        billed = validate(events({'claude-opus-5': {}}), STDERR, [DECISION], TOKEN)
        self.assertFalse(billed['checks']['selected_model_billed_by_client'])

    def test_wrong_answer_or_extra_decisions_fail(self):
        self.assertFalse(validate(events(answer='guess'), STDERR, [DECISION], TOKEN)['passed'])
        self.assertFalse(validate(events(), STDERR, [DECISION, DECISION], TOKEN)['checks']['one_jev_decision'])

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



def row(model='claude-sonnet-5', cost=.01, status=200, tools=1, kind='messages'):
    return {'kind': kind, 'model': model, 'cost_usd': cost, 'http_status': status, 'tool_count': tools, 'applied': False}


class WireAccountingTests(unittest.TestCase):
    def setUp(self):
        self.rows = [row('claude-models-catalog', None, kind='models', tools=0), row(), row(),
                     row('claude-haiku-4-5-20251001', .001, tools=0)]
        self.result = {'total_cost_usd': .021}
        self.decisions = [dict(DECISION, jev={'request': {'state': {}}, 'response': {'usage': {'input_tokens': 893, 'output_tokens': 100}}})]

    def test_matching_totals_and_models_reconcile(self):
        r = reconcile(self.rows, self.result, 'claude-sonnet-5', self.decisions)
        self.assertTrue(r['accounting_matches'], r)
        self.assertEqual((r['proxy_requests'], r['catalog_requests'], r['unpriced_requests']), (3, 1, 0))
        self.assertAlmostEqual(r['proxy_known_cost_usd'], .021)
        self.assertEqual(r['helper_models'], ['claude-haiku-4-5-20251001'])
        self.assertEqual(r['routed_model_mismatch'], [])

    def test_total_mismatch_fails(self):
        self.assertFalse(reconcile(self.rows, {'total_cost_usd': .0211}, 'claude-sonnet-5', self.decisions)['accounting_matches'])
        self.assertFalse(reconcile(self.rows, {}, 'claude-sonnet-5', self.decisions)['accounting_matches'])

    def test_unpriced_or_failed_messages_fail(self):
        unpriced = reconcile(self.rows + [row(cost=None)], self.result, 'claude-sonnet-5', self.decisions)
        self.assertEqual(unpriced['unpriced_requests'], 1)
        self.assertFalse(unpriced['accounting_matches'])
        failed = reconcile(self.rows + [row(cost=0, status=429)], self.result, 'claude-sonnet-5', self.decisions)
        self.assertEqual(failed['non_200_requests'], 1)
        self.assertFalse(failed['accounting_matches'])

    def test_routed_model_change_fails_but_helper_call_does_not(self):
        rows = self.rows[:2] + [row('claude-opus-5', .01)] + self.rows[3:]
        r = reconcile(rows, self.result, 'claude-sonnet-5', self.decisions)
        self.assertEqual(r['routed_model_mismatch'], ['claude-opus-5'])
        self.assertFalse(r['accounting_matches'])

    def test_empty_log_never_passes(self):
        self.assertFalse(reconcile([], {'total_cost_usd': 0}, 'claude-sonnet-5', self.decisions)['accounting_matches'])

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


if __name__ == '__main__': unittest.main()
