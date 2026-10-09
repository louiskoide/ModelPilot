"""Haiku 5.5 (October 8): its prices by prompt length, where they apply, its arms and probes, and its request shape."""
import copy
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
# Strict outcomes of the Haiku 5.5 runs (the policy evidence is in the ignored runs/haiku-policy-evidence-20261008.json):
# medium, two trials a task (bench-20261008-195122, -214840), failed tomli both times; low concise, one trial
# (bench-20261008-195122), missed four tasks. setting: (trials a task, failed trials by task)
TASKS = sorted(json.loads((ROOT/'configs/modelpilot-policy.json').read_text())['quality_floor']['outcomes'][f'{S}/medium'])
HAIKU_OUTCOMES = {f'{H}/medium': (2, {'tomli-decode-error-attrs': 2}),
                  f'{H}/low': (1, {'nx-classes-weak-views': 1, 'nx-ismags-monomorphism': 1, 'parse-decimal-grouping': 1,
                                   'tomli-decode-error-attrs': 1})}
# Jev's usual answer on the tuning tasks (bench-20261006-093401): Sonnet 5.5, effort medium.
USUAL = {'model': {'choice': S, 'confidence': .91, 'probabilities': {S: .95, H: .02, O: .03}},
         'effort': {'choice': 'medium', 'confidence': .55,
                    'probabilities': {'low': 0, 'medium': .64, 'high': .33, 'xhigh': .03, 'max': 0}}}


def with_haiku_outcomes(cfg):
    """A config copy with the first Haiku run's outcomes in the calibration and the floor's evidence (left out of the
    shipped config until the user decides how they may bound other models' settings)."""
    cfg = copy.deepcopy(cfg)
    for setting, (n, failed) in HAIKU_OUTCOMES.items():
        cfg['calibration']['outcomes'][setting] = {'passed': n * len(TASKS) - sum(failed.values()), 'trials': n * len(TASKS)}
        cfg['quality_floor']['outcomes'][setting] = {t: [n - failed.get(t, 0), n] for t in TASKS}
    return cfg


def haiku_on(baseline=None, measured=True):
    """The config once the probes pass: Haiku 5.5 a candidate with per-message effort, its outcomes in, the floor on
    for baseline (None: off), as the arm's overrides would set it."""
    cfg = with_haiku_outcomes(switch_policy.load())
    cfg['models'][H].update(candidate=True, per_message_effort=True)
    cfg['measured_candidates']['enabled'] = measured
    if baseline:
        cfg['quality_floor'].update(enabled=True, baseline=baseline)
    return cfg


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

    def test_forecasts_price_each_request_at_the_tier_its_prompt_reaches(self):
        low, high = dict(LOW, max_prompt_tokens=100000), dict(HIGH, max_prompt_tokens=None)
        self.assertEqual(switch_policy._segments(RATE, 90_000, 2_000, 10), [(5.0, low), (5.0, high)])
        self.assertEqual(switch_policy._segments(RATE, 10_000, 2_000, 10), [(10, low)])
        self.assertEqual(switch_policy._segments(RATE, 120_000, 2_000, 4), [(4, high)])
        self.assertEqual(switch_policy._segments(RATE, 50_000, 0, 3), [(3, low)])
        flat = bench.rates()[O]
        self.assertEqual(switch_policy._segments(flat, 90_000, 2_000, 10), [(10, flat)])
        cfg = switch_policy.load()
        prof = dict(cfg['defaults'], prefix_tokens=90_000, messages_tokens=1500, warm=True, warm_entries={})
        requests, output = switch_policy._scale(cfg, (H, 'medium'), prof)
        horizon = prof['horizon_requests'] * requests
        reply = prof['output_tokens'] * output
        growth = prof['new_input_tokens'] + reply
        n1 = (100_000 - 90_000) / growth
        expected = (n1 * ((90_000 + (n1 - 1) / 2 * growth) * LOW['read'] + growth * LOW['write_5m'] + reply * LOW['output']) +
                    (horizon - n1) * ((90_000 + n1 * growth + (horizon - n1 - 1) / 2 * growth) * HIGH['read'] +
                                      growth * HIGH['write_5m'] + reply * HIGH['output'])) / 1e6
        self.assertAlmostEqual(switch_policy.run_cost(cfg, bench.rates(), (H, 'medium'), prof), expected)
        # On the measured task shape, Haiku 5.5 medium forecasts below Sonnet 5.5 low concise, as measured.
        cold = dict(prof, prefix_tokens=7100, warm=False)
        self.assertLess(switch_policy.run_cost(cfg, bench.rates(), (H, 'medium'), cold),
                        switch_policy.run_cost(cfg, bench.rates(), (S, 'low'), cold))
        self.assertEqual(switch_policy._rate(bench.rates(), H, 150_000)['read'], .05)


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


class CandidacyTests(unittest.TestCase):
    """What turning Haiku 5.5 on will do (user decisions, October 8: every arm, reached by proxy moves, measured
    settings weighed beside Jev's), checked now with the flag that the probes will turn on."""
    def setUp(self):
        self.rates = bench.rates()
        self.prof = switch_policy.profile(switch_policy.load(), {'model': S, 'messages': [{'role': 'user', 'content': 'x' * 19800}]},
                                          False)

    def decide(self, cfg, current, adv=USUAL, trigger='turn_start'):
        return switch_policy.decide(cfg, self.rates, adv, current, self.prof, trigger)

    def test_shipped_haiku_is_not_a_candidate_and_needs_per_message_effort_to_be_one(self):
        cfg = switch_policy.load()
        self.assertNotIn(H, {m for m, _ in switch_policy.settings(cfg)})
        spec = cfg['models'][H]
        self.assertEqual((spec['candidate'], spec['per_message_effort'], spec['candidate_efforts']), (False, False, ['medium']))
        self.assertEqual((spec['request_factor'], spec['output_factor']), (2.11, 2.32))  # runs/haiku-policy-evidence-20261008
        with self.assertRaisesRegex(ValueError, 'per_message_effort'):  # the client's own effort message would hold
            switch_policy.with_overrides(cfg, {'models': {H: {'candidate': True}}})
        on = switch_policy.with_overrides(cfg, {'models': {H: {'candidate': True, 'per_message_effort': True}}})
        self.assertEqual([c for c in switch_policy.settings(on) if c[0] == H], [(H, 'medium')])  # the measured effort
        with self.assertRaisesRegex(ValueError, 'candidate_efforts'):
            switch_policy.with_overrides(cfg, {'models': {H: {'candidate_efforts': ['turbo']}}})

    def test_a_sonnet_user_starting_on_low_concise_moves_to_haiku_medium(self):
        for baseline in (f'{S}/medium', None):  # modelpilot-for-sonnet, and modelpilot without the floor
            decision = self.decide(haiku_on(baseline), (S, 'low'))
            self.assertEqual((decision['action'], decision['target'], decision['reason']),
                             ('jump', [H, 'medium'], 'expected_cost_lower'), baseline)
            haiku = next(c for c in decision['candidates'] if c['setting'] == f'{H}/medium')
            self.assertTrue(haiku['measured_only'])  # Jev named Sonnet: weighed for its measured outcomes
            self.assertAlmostEqual(haiku['p_measured'], 57 / 60)  # 56/58 with the uniform prior

    def test_an_opus_user_never_weighs_haiku(self):
        decision = self.decide(haiku_on(f'{O}/medium'), (O, 'medium'))
        self.assertNotIn(f'{H}/medium', {c['setting'] for c in decision['candidates']})
        self.assertEqual(decision['action'], 'stay')

    def test_without_measured_candidates_haiku_waits_for_jev_to_name_it(self):
        cfg = haiku_on(f'{S}/medium', measured=False)
        self.assertNotIn(f'{H}/medium', {c['setting'] for c in self.decide(cfg, (S, 'low'))['candidates']})
        named = {'model': {'choice': H, 'confidence': .9, 'probabilities': {H: .9, S: .08, O: .02}},
                 'effort': USUAL['effort']}
        self.assertEqual(self.decide(cfg, (S, 'low'), named)['target'], [H, 'medium'])

    def test_measured_candidates_join_only_at_turn_starts_and_need_an_answer(self):
        cfg = haiku_on(f'{S}/medium')
        warm = switch_policy.profile(cfg, {'model': S, 'messages': [{'role': 'user', 'content': 'x' * 19800}]}, True)
        step = switch_policy.decide(cfg, self.rates, USUAL, (S, 'low'), warm, 'step')
        self.assertFalse(any(c.get('measured_only') for c in step['candidates']))
        self.assertEqual(self.decide(cfg, (S, 'low'), None)['reason'], 'advice_unavailable')
        gate = switch_policy.jev_gate(cfg, self.rates, (S, 'low'), self.prof, 'turn_start')
        self.assertEqual((gate['can_change'], gate['reason']), (True, 'a_move_can_pay'))

    def test_haikus_outcomes_would_lift_stronger_settings_under_the_floors_premise(self):
        # Why the shipped config leaves them out (a question for the user, October 8): under "a stronger setting never
        # misses more", Haiku 5.5 medium's two trials a task raise Sonnet 5.5 medium's measured rate and clear its parse
        # miss against an Opus user. No setting's floor verdict changes.
        base, cfg = switch_policy.load(), with_haiku_outcomes(switch_policy.load())
        self.assertAlmostEqual(switch_policy.measured_ok(base, (S, 'medium')), 49 / 54)
        self.assertAlmostEqual(switch_policy.measured_ok(cfg, (S, 'medium')), 57 / 60)
        opus = copy.deepcopy(cfg)
        opus['quality_floor'].update(enabled=True, baseline=f'{O}/medium')
        self.assertEqual(switch_policy.floor_allows(opus, (S, 'medium'))['extra_misses'], ['tomli-decode-error-attrs'])
        everything = [(m, e) for m in (H, S, O) for e in base['effort_order']]
        for baseline in (f'{S}/medium', f'{O}/medium'):
            before, after = copy.deepcopy(base), copy.deepcopy(cfg)
            for c in (before, after):
                c['quality_floor'].update(enabled=True, baseline=baseline)
            self.assertEqual([switch_policy.floor_allows(before, c)['allowed'] for c in everything if c[0] != H],
                             [switch_policy.floor_allows(after, c)['allowed'] for c in everything if c[0] != H], baseline)
        sonnet = copy.deepcopy(cfg)
        sonnet['quality_floor'].update(enabled=True, baseline=f'{S}/medium')
        self.assertTrue(switch_policy.floor_allows(sonnet, (H, 'medium'))['allowed'])
        self.assertEqual(switch_policy.floor_allows(sonnet, (H, 'low'))['extra_misses'], ['nx-classes-weak-views'])


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
