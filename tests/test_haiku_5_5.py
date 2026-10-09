"""Haiku 5.5 (October 8): its prices by prompt length, where they apply, its arms and probes, and its request shape."""
import json
from pathlib import Path
import unittest
from modelpilot import bench, bench_report, cache_probe, cache_replication, proxy, switch_policy
from modelpilot.fixture_dispatch import haiku_5_5_shape
from modelpilot.policy_actions import MODELS, transform_request

ROOT = Path(__file__).resolve().parents[1]
H, S, O = 'claude-haiku-5-5', 'claude-sonnet-5-5', 'claude-opus-5-5'
CONFIG = json.loads((ROOT/'configs/haiku-5-5-rates.json').read_text())
RATE = CONFIG['rates'][H]
LOW = dict(input=.1, output=.5, write_5m=.125, write_1h=.2, read=.01)  # prompts up to 100,000 tokens
HIGH = dict(input=.5, output=2.5, write_5m=.625, write_1h=1, read=.05)  # prompts over 100,000 tokens
CLIENT_SHAPE = json.loads((ROOT/'tests/fixtures/claude-2.1.284-haiku-5-5-shape.json').read_text())


def usage(read, write=0, uncached=10, output=100):
    return {'input_tokens': uncached, 'output_tokens': output, 'cache_read_input_tokens': read,
            'cache_creation_input_tokens': write,
            'cache_creation': {'ephemeral_5m_input_tokens': write, 'ephemeral_1h_input_tokens': 0}}


def priced(rate, u):
    return (u['input_tokens'] * rate['input'] + u['output_tokens'] * rate['output'] +
            u['cache_read_input_tokens'] * rate['read'] + u['cache_creation_input_tokens'] * rate['write_5m']) / 1e6


class RateTests(unittest.TestCase):
    def test_the_rate_file_states_both_tiers_and_its_source(self):
        self.assertEqual(RATE, {'tiers': [dict(LOW, max_prompt_tokens=100000), dict(HIGH, max_prompt_tokens=None)]})
        self.assertIn('platform.claude.com/docs/en/about-claude/pricing', CONFIG['source'])
        self.assertIn('Unconfirmed', CONFIG['source'])  # which tokens count toward the 100,000
        self.assertEqual(CONFIG['retrieved'], '2026-10-08')
        self.assertEqual(bench.rates()[H], RATE)
        self.assertEqual(cache_replication.RATES[H], RATE)

    def test_the_tier_follows_the_whole_prompt_cached_tokens_included(self):
        self.assertEqual(cache_probe.prompt_tokens(usage(99_000, 990)), 100_000)
        at, over = usage(99_000, 990), usage(99_000, 991)
        self.assertAlmostEqual(cache_probe.cost(at, RATE, '5m'), priced(LOW, at))
        self.assertAlmostEqual(cache_probe.cost(over, RATE, '5m'), priced(HIGH, over))
        # Mostly cache reads, as a Claude Code session is: still over 100,000, so the dearer tier.
        self.assertAlmostEqual(cache_probe.cost(usage(150_000), RATE, '5m'), priced(HIGH, usage(150_000)))

    def test_an_unknown_length_pays_the_dearest_tier_and_flat_rates_pass_through(self):
        self.assertEqual({k: cache_probe.tier(RATE)[k] for k in HIGH}, HIGH)
        flat = bench.rates()[O]
        self.assertIs(cache_probe.tier(flat, 10**6), flat)
        self.assertIsNone(cache_probe.tier(None, 5))
        with self.assertRaises(KeyError):  # code that doesn't pick a tier fails rather than underprices
            RATE['read']

    def test_the_proxy_prices_measured_usage_and_reserves_at_the_dearest_tier(self):
        u = dict(usage(20_000, 500), service_tier='standard', inference_geo='global')
        self.assertAlmostEqual(cache_probe.priced_usage({'model': H}, H, u, bench.rates()), priced(LOW, u))
        raw = b'x' * 3000  # about 1,000 tokens
        request = {'model': H, 'max_tokens': 2000}
        self.assertAlmostEqual(proxy.reservation_estimate(raw, request, {H: RATE}), (1000 * 1 + 2000 * 2.5) / 1e6)
        # An unknown model reserves at the dearest of every rate, tiered ones included.
        self.assertGreater(proxy.reservation_estimate(raw, {'model': 'x', 'max_tokens': 1}, {H: RATE}), 0)

    def test_reports_price_each_row_at_its_own_tier(self):
        rows = [{'kind': 'messages', 'model': H, 'cost_usd': 0.0, 'http_status': 200, 'usage': usage(50_000, 1000)},
                {'kind': 'messages', 'model': H, 'cost_usd': 0.0, 'http_status': 200, 'usage': usage(120_000, 1000)}]
        parts = bench_report.cost_components(rows, bench.rates())
        self.assertAlmostEqual(parts['cache_read'], (50_000 * LOW['read'] + 120_000 * HIGH['read']) / 1e6)
        self.assertAlmostEqual(parts['cache_write'], 1000 * (LOW['write_5m'] + HIGH['write_5m']) / 1e6)
        self.assertAlmostEqual(sum(parts.values()), priced(LOW, rows[0]['usage']) + priced(HIGH, rows[1]['usage']))
        hour = dict(rows[1], usage=dict(rows[1]['usage'], cache_creation={'ephemeral_5m_input_tokens': 0,
                                                                          'ephemeral_1h_input_tokens': 1000}))
        repriced = bench_report.api_key_equivalent([hour], bench.rates())[0]
        self.assertAlmostEqual(repriced['cost_usd'], priced(HIGH, rows[1]['usage']))

    def test_forecasts_refuse_a_price_by_prompt_length(self):
        cfg = switch_policy.load()
        with self.assertRaisesRegex(ValueError, 'prompt length'):
            switch_policy._rate(bench.rates(), H)
        self.assertNotIn(H, {m for m, _ in switch_policy.settings(cfg)})  # not a candidate, so never priced there


class TierTests(unittest.TestCase):
    def test_haiku_5_5_replaces_haiku_4_5_at_the_bottom_and_is_not_a_candidate(self):
        cfg = switch_policy.load()
        self.assertEqual(MODELS, (H, S, O))
        spec = cfg['models'][H]
        self.assertEqual((spec['rank'], spec['efforts'], spec['mid_conversation_system'], spec['candidate']),
                         (0, cfg['effort_order'], True, False))
        self.assertEqual(spec['effort_switch_rewrite'], 'full')  # the pessimistic choice until measured
        self.assertNotIn('claude-haiku-4-5-20251001', cfg['models'])

    def test_arms(self):
        self.assertEqual(bench.ARMS['haiku-5.5'], {'kind': 'fixed', 'model': H})
        self.assertEqual(bench.ARMS['haiku-5.5-low-concise'],
                         {'kind': 'fixed', 'model': H, 'effort': 'low', 'append_system_prompt': 'bench/prompts/concise.md'})
        self.assertEqual(bench.ARMS['haiku-4.5']['model'], 'claude-haiku-4-5-20251001')  # kept for its earlier runs
        for arm in ('jev-compat-o55', 'modelpilot', 'modelpilot-for-sonnet'):  # Jev now discovers Haiku 5.5
            self.assertEqual(tuple(bench.ARMS[arm]['models']), (H, S, O), arm)
        self.assertNotIn(H, bench.ARMS['modelpilot']['served_models'])


class ProbeTests(unittest.TestCase):
    def test_cache_suite_returns_to_haiku_5_5_and_keeps_its_effort(self):
        groups = cache_replication.plan('test', 'haiku-5-5')
        self.assertEqual(sum(len(g['steps']) for g in groups), 111)
        for g in groups:
            models = [p['model'] for _, _, p in g['steps']]
            self.assertEqual(models[0], H)
            if g['name'].startswith('model/'):
                self.assertEqual(models[-1], H)
            for _, _, p in g['steps']:
                self.assertNotIn('thinking', p)
                self.assertIn('effort', p['output_config'])  # Haiku 4.5's was dropped
        self.assertEqual({g['name'].split('/')[3] for g in groups if g['name'].startswith('model/')}, {S, O})
        self.assertEqual(len({g['steps'][0][2].get('system', g['steps'][0][2]['messages'][0]['content'])[0]['text']
                              for g in groups}), len(groups))
        # The opus-5-5 suite is unchanged by the shared plan.
        self.assertEqual(sum(len(g['steps']) for g in cache_replication.plan('test', 'opus-5-5')), 141)


class ShapeTests(unittest.TestCase):
    def request(self, **extra):
        return dict({'model': H, 'max_tokens': 32, 'thinking': {'type': 'adaptive'}, 'output_config': {'effort': 'low'},
                     'messages': [{'role': 'user', 'content': 'x'}, {'role': 'system', 'content': 'note'}]}, **extra)

    def test_the_fixture_rejects_what_haiku_5_5_rejects(self):
        haiku_5_5_shape(self.request())
        haiku_5_5_shape(self.request(temperature=1))
        haiku_5_5_shape(self.request(thinking={'type': 'disabled'}, output_config={'effort': 'high'}))
        for bad in (dict(temperature=.5), dict(top_p=1), dict(top_k=5), dict(temperature=1, top_p=.99),
                    dict(thinking={'type': 'enabled', 'budget_tokens': 2048}),
                    dict(thinking={'type': 'disabled'}, output_config={'effort': 'xhigh'})):
            with self.assertRaises(ValueError, msg=bad):
                haiku_5_5_shape(self.request(**bad))
        prefill = self.request()
        prefill['messages'].append({'role': 'assistant', 'content': 'Sure'})
        with self.assertRaises(ValueError):
            haiku_5_5_shape(prefill)

    def test_the_pinned_client_sends_haiku_5_5_a_shape_it_accepts(self):
        # Captured at $0 against the owned fixture: 2.1.284 has no entry for Haiku 5.5 and sends it what it sends Sonnet
        # 5.5 (adaptive thinking, top-level effort, mid-history system messages), with no sampling parameters, but without
        # its own effort message on the note after the prompt.
        spec = CLIENT_SHAPE['requests'][H]
        self.assertEqual(CLIENT_SHAPE['client_version'], '2.1.284 (Claude Code)')
        self.assertEqual((spec['thinking'], spec['effort']), ({'type': 'adaptive'}, 'medium'))
        self.assertFalse({'temperature', 'top_p', 'top_k'} & set(spec['top_level_keys']))
        self.assertTrue(spec['system_after_prompt'] and spec['system_after_tool_result'])
        self.assertNotIn('output_config', CLIENT_SHAPE['requests'][H]['messages'][1])
        self.assertNotIn('per-turn-control-2026-07-01', spec['anthropic_beta'])

    def test_the_transform_forwards_a_sonnet_request_as_haiku_5_5_accepts_it(self):
        p = self.request(model=S, context_management={'edits': [{'type': 'clear_thinking_20251015', 'keep': 'all'}]})
        haiku_5_5_shape(transform_request(p, H, 'low'))


if __name__ == '__main__':
    unittest.main()
