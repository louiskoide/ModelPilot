"""ModelPilot's switch policy: whether moving to Jev's recommended setting pays. Pure, $0."""
import copy
import itertools
import json
import tempfile
import unittest
from modelpilot import bench, switch_policy as sp

H, S, O = 'claude-haiku-4-5-20251001', 'claude-sonnet-5-5', 'claude-opus-5-5'
MODELS, EFFORTS = [H, S, O], ['low', 'medium', 'high', 'xhigh', 'max']


def advice(model, effort, model_p=.9, effort_p=.85, models=None):
    def spread(labels, top, p):
        return {x: p if x == top else (1 - p) / (len(labels) - 1) for x in labels}
    return {'model': {'choice': model, 'confidence': model_p, 'probabilities': models or spread(MODELS, model, model_p)},
            'effort': {'choice': effort, 'confidence': effort_p, 'probabilities': spread(EFFORTS, effort, effort_p)}}


def request(kb):
    return {'model': S, 'messages': [{'role': 'user', 'content': 'x' * (kb * 1000 - 200)}]}


class SwitchPolicyTests(unittest.TestCase):
    def setUp(self):
        self.cfg, self.rates = sp.load(), bench.rates()

    def decide(self, adv, current, kb=11, warm=False, trigger='turn_start', cfg=None):
        cfg = cfg or self.cfg
        return sp.decide(cfg, self.rates, adv, current, sp.profile(cfg, request(kb), warm), trigger)

    def test_the_settings_come_from_the_config(self):
        settings = sp.settings(self.cfg)
        self.assertNotIn(H, {m for m, _ in settings})  # not a candidate: the config says why
        self.assertIn((O, 'xhigh'), settings)
        self.assertEqual(len(settings), 10)
        cfg = copy.deepcopy(self.cfg)  # a new model is a config entry and a rate, not code
        cfg['models']['claude-next'] = dict(cfg['models'][O], rank=3)
        rates = dict(self.rates, **{'claude-next': self.rates[O]})
        adv = advice('claude-next', 'high', models={H: .01, S: .02, O: .02, 'claude-next': .95})
        decision = sp.decide(cfg, rates, adv, (S, 'medium'), sp.profile(cfg, request(11), False), 'turn_start')
        self.assertEqual((decision['action'], decision['target']), ('jump', ['claude-next', 'high']))

    def test_a_confident_hard_prediction_jumps_straight_to_it(self):
        decision = self.decide(advice(O, 'xhigh'), (S, 'medium'))
        self.assertEqual((decision['action'], decision['target']), ('jump', [O, 'xhigh']))
        # Jev is sure of both: only staying and its setting are weighed, nothing cheaper is tried first.
        self.assertEqual({c['setting'] for c in decision['candidates']}, {f'{S}/medium', f'{O}/xhigh'})

    def test_a_partial_move_is_weighed_only_where_jev_is_unsure(self):
        unsure_effort = self.decide(advice(O, 'xhigh', effort_p=.4), (S, 'medium'))
        self.assertEqual({c['setting'] for c in unsure_effort['candidates']}, {f'{S}/medium', f'{O}/xhigh', f'{O}/medium'})
        unsure_model = self.decide(advice(O, 'xhigh', model_p=.5), (S, 'medium'))
        self.assertEqual({c['setting'] for c in unsure_model['candidates']}, {f'{S}/medium', f'{O}/xhigh', f'{S}/xhigh'})

    def test_a_clear_prediction_jumps_whatever_the_guessed_parameters(self):
        # A redo can fail too: staying on a setting that will likely fail carries the same downstream risk as moving
        # now, so the guesses (horizon, effort output factors, wasted fraction) can't turn a clear case into a stay.
        for horizon, spread, wasted in itertools.product([5, 15, 40], [.5, 1, 2], [.25, .5, .75]):
            cfg = copy.deepcopy(self.cfg)
            cfg['defaults']['horizon_requests'] = horizon
            cfg['effort_output_factor'] = {e: 1 + (f - 1) * spread for e, f in self.cfg['effort_output_factor'].items()}
            cfg['recovery']['wasted_fraction'] = wasted
            for kb, warm in ((11, False), (200, True)):
                decision = self.decide(advice(O, 'xhigh', model_p=.85, effort_p=.7), (S, 'medium'), kb, warm, cfg=cfg)
                self.assertEqual((decision['action'], decision['target']), ('jump', [O, 'xhigh']),
                                 (horizon, spread, wasted, kb))

    def test_it_still_jumps_when_a_warm_rewrite_is_worth_it(self):
        decision = self.decide(advice(O, 'xhigh'), (S, 'medium'), kb=200, warm=True)
        self.assertEqual(decision['action'], 'jump')
        target = next(c for c in decision['candidates'] if c['setting'] == f'{O}/xhigh')
        self.assertGreater(target['switch_usd'], 0)

    def test_uncertain_between_the_current_and_a_dearer_setting_stays(self):
        adv = advice(S, 'medium', model_p=.55, models={H: .05, S: .55, O: .40})
        self.assertEqual(self.decide(adv, (S, 'medium'), kb=100, warm=True)['action'], 'stay')

    def test_a_downgrade_pays_when_cold_but_not_in_a_warm_long_session(self):
        adv = advice(S, 'medium', model_p=.85, models={H: .1, S: .85, O: .05})
        warm = self.decide(adv, (O, 'high'), kb=300, warm=True)
        self.assertEqual((warm['action'], warm['reason'], warm['downgrade']), ('stay', 'not_worth_switching', True))
        self.assertGreater(warm['required_usd'], self.cfg['hysteresis_usd'])  # plus a multiple of the rewrite
        self.assertEqual(self.decide(adv, (O, 'high'), kb=20, warm=False)['action'], 'jump')

    def test_a_low_confidence_downgrade_is_refused(self):
        adv = advice(S, 'medium', model_p=.25, models={H: .6, S: .25, O: .15})
        decision = self.decide(adv, (O, 'high'))
        self.assertEqual((decision['action'], decision['reason']), ('stay', 'low_confidence_no_downgrade'))

    def test_hysteresis_protects_a_plausible_setting_not_a_hopeless_one(self):
        plausible = advice(S, 'high', model_p=.9, effort_p=.6)  # Sonnet medium has about a 20% chance of being enough
        self.assertEqual(self.decide(plausible, (S, 'medium'))['action'], 'jump')  # a small gain, with a small margin
        cfg = dict(self.cfg, hysteresis_usd=1.0)
        decision = self.decide(plausible, (S, 'medium'), cfg=cfg)
        self.assertEqual((decision['action'], decision['reason']), ('stay', 'not_worth_switching'))
        hopeless = self.decide(advice(O, 'xhigh'), (S, 'medium'), cfg=cfg)  # almost surely not enough: little protection
        self.assertEqual(hopeless['action'], 'jump')
        self.assertLess(hopeless['required_usd'], .05)

    def test_switch_costs_by_kind(self):
        prof = sp.profile(self.cfg, dict(request(40), tools=[{'name': 'x', 'description': 'y' * 20000}]), warm=True)
        model_change = sp.switch_cost(self.cfg, self.rates, (S, 'medium'), (O, 'medium'), prof)
        opus_effort = sp.switch_cost(self.cfg, self.rates, (O, 'medium'), (O, 'high'), prof)
        sonnet_effort = sp.switch_cost(self.cfg, self.rates, (S, 'medium'), (S, 'high'), prof)
        self.assertAlmostEqual(model_change, prof['prefix_tokens'] * (5 - .2) / 1e6)
        self.assertAlmostEqual(opus_effort, prof['messages_tokens'] * (5 - .2) / 1e6)  # Opus 5.5 keeps tools and system
        cfg = copy.deepcopy(self.cfg)
        cfg['models'][S]['effort_switch_rewrite'] = 'full'  # as Sonnet 5 did
        self.assertAlmostEqual(sp.switch_cost(cfg, self.rates, (S, 'medium'), (S, 'high'), prof),
                               prof['prefix_tokens'] * (2.5 - .2) / 1e6)
        self.assertAlmostEqual(sonnet_effort, prof['messages_tokens'] * (2.5 - .2) / 1e6)  # so does Sonnet 5.5 (probe)
        self.assertLess(opus_effort, model_change)
        self.assertEqual(sp.switch_cost(self.cfg, self.rates, (S, 'medium'), (O, 'medium'), dict(prof, warm=False)), 0)

    def test_stuck_evidence_rules_out_the_current_setting_and_weakers(self):
        decision = self.decide(advice(S, 'medium'), (S, 'medium'), kb=60, warm=True, trigger='stuck_evidence')
        self.assertEqual(decision['action'], 'jump')
        rows = {c['setting']: c for c in decision['candidates']}
        self.assertEqual(rows[f'{S}/medium']['p_ok'], 0)
        self.assertTrue(all(sp.at_least(self.cfg, tuple(c.split('/')), (S, 'medium')) for c in rows))
        self.assertNotIn(f'{O}/low', rows)  # weaker effort: not ruled in by the evidence

    def test_stuck_with_nothing_stronger_stops(self):
        decision = self.decide(advice(O, 'max'), (O, 'max'), trigger='stuck_evidence')
        self.assertEqual((decision['action'], decision['reason']), ('stop', 'no_stronger_setting'))

    def test_without_advice_it_stays(self):
        self.assertEqual(self.decide(None, (S, 'medium'))['reason'], 'advice_unavailable')
        self.assertEqual(self.decide({'model': {'probabilities': {}}}, (S, 'medium'))['reason'], 'advice_unavailable')

    def test_without_an_effort_answer_effort_does_not_enter_the_estimate(self):
        adv = advice(O, 'xhigh')
        del adv['effort']
        decision = self.decide(adv, (S, 'medium'))
        self.assertEqual({c['setting'] for c in decision['candidates']}, {f'{S}/medium', f'{O}/medium'})

    def test_a_step_always_weighs_jevs_effort_on_the_current_model(self):
        # Jev is sure of both answers: a turn start weighs only its setting, a mid-task step also the cheaper
        # effort-only move, whose rewrite is smaller than a model change's.
        turn = self.decide(advice(O, 'high'), (S, 'medium'), kb=60, warm=True)
        step = self.decide(advice(O, 'high'), (S, 'medium'), kb=60, warm=True, trigger='step')
        self.assertEqual({c['setting'] for c in turn['candidates']}, {f'{S}/medium', f'{O}/high'})
        self.assertEqual({c['setting'] for c in step['candidates']}, {f'{S}/medium', f'{O}/high', f'{S}/high'})
        rows = {c['setting']: c for c in step['candidates']}
        self.assertLess(rows[f'{S}/high']['switch_usd'], rows[f'{O}/high']['switch_usd'])

    def test_every_decision_carries_the_forecast_it_leaves_the_task_on(self):
        prof = sp.profile(self.cfg, request(11), False)
        for adv, current in ((advice(O, 'xhigh'), (S, 'medium')), (None, (S, 'medium')), (advice(O, 'max'), (O, 'max'))):
            trigger = 'stuck_evidence' if current == (O, 'max') else 'turn_start'
            decision = sp.decide(self.cfg, self.rates, adv, current, prof, trigger)
            self.assertAlmostEqual(decision['forecast_usd'],
                                   sp.run_cost(self.cfg, self.rates, tuple(decision['target']), prof))

    def test_a_warm_return_is_priced_by_what_its_entry_does_not_cover(self):
        request_ = dict(request(40), tools=[{'name': 'x', 'description': 'y' * 20000}])
        prof = sp.profile(self.cfg, request_, warm=True)
        covered = prof['prefix_tokens'] - 1000  # Sonnet medium ran until 1,000 tokens ago
        prof = sp.profile(self.cfg, request_, warm=True, entries={f'{S}/medium': covered})
        extra = (2.5 - .2) / 1e6
        on, off = copy.deepcopy(self.cfg), copy.deepcopy(self.cfg)
        on['return_reuse']['enabled'], off['return_reuse']['enabled'] = True, False
        self.assertTrue(self.cfg['return_reuse']['enabled'])  # on since the returns probe (September 30)
        # Off: a return is priced like a first switch.
        self.assertAlmostEqual(sp.switch_cost(off, self.rates, (S, 'high'), (S, 'medium'), prof, reuse=True),
                               prof['messages_tokens'] * extra)
        self.assertAlmostEqual(sp.switch_cost(on, self.rates, (S, 'high'), (S, 'medium'), prof, reuse=True), 1000 * extra)
        self.assertAlmostEqual(sp.switch_cost(on, self.rates, (O, 'xhigh'), (S, 'medium'), prof, reuse=True), 1000 * extra)
        # A later, hypothetical move (a failure's redo) never counts on an entry that is warm now.
        self.assertAlmostEqual(sp.switch_cost(on, self.rates, (O, 'xhigh'), (S, 'medium'), prof),
                               prof['prefix_tokens'] * extra)
        self.assertAlmostEqual(sp.switch_cost(on, self.rates, (S, 'high'), (S, 'low'), prof, reuse=True),
                               prof['messages_tokens'] * extra)  # no entry of its own: a first switch
        decision = sp.decide(on, self.rates, advice(S, 'medium'), (O, 'xhigh'), prof, 'step')
        target = next(c for c in decision['candidates'] if c['setting'] == f'{S}/medium')
        self.assertAlmostEqual(target['switch_usd'], 1000 * extra)

    def test_per_message_effort_makes_effort_changes_free_but_not_model_changes(self):
        request_ = dict(request(40), tools=[{'name': 'x', 'description': 'y' * 20000}])
        prof = sp.profile(self.cfg, request_, warm=True)
        on = copy.deepcopy(self.cfg)
        on['per_message_effort']['enabled'] = True
        self.assertEqual(sp.switch_cost(on, self.rates, (S, 'medium'), (S, 'xhigh'), prof), 0)
        self.assertGreater(sp.switch_cost(on, self.rates, (S, 'medium'), (O, 'medium'), prof), 0)
        self.assertGreater(sp.switch_cost(self.cfg, self.rates, (S, 'medium'), (S, 'xhigh'), prof), 0)  # off by default
        # A return to a warm model counts its newest entry, whatever effort it ran at.
        prof = sp.profile(on, request_, warm=True, entries={f'{S}/*': prof['prefix_tokens'] - 1000})
        self.assertAlmostEqual(sp.switch_cost(on, self.rates, (O, 'xhigh'), (S, 'low'), prof, reuse=True),
                               1000 * (2.5 - .2) / 1e6)

    def test_a_bad_config_is_refused(self):
        for change in ({'effort_switch_rewrite': 'partial'}, {'efforts': ['medium', 'extreme']}):
            cfg = copy.deepcopy(self.cfg)
            cfg['models'][S].update(change)
            with tempfile.NamedTemporaryFile('w', suffix='.json') as f:
                json.dump(cfg, f)
                f.flush()
                with self.assertRaises(ValueError):
                    sp.load(f.name)
        for path, value in ((('decision_points',), ['turn_start', 'hourly']), (('step', 'min_requests_between'), 0),
                            (('step', 'max_per_revision'), 1.5), (('step', 'overrun_factor'), 0),
                            (('step', 'enabled'), 'yes'), (('return_reuse', 'enabled'), 1),
                            (('return_reuse', 'max_positions'), -1), (('return_reuse', 'max_positions'), 2.5),
                            (('per_message_effort', 'placement'), 'middle'), (('per_message_effort', 'beta'), 'a,b')):
            cfg = copy.deepcopy(self.cfg)
            (cfg[path[0]] if len(path) == 2 else cfg).__setitem__(path[-1], value)
            with tempfile.NamedTemporaryFile('w', suffix='.json') as f:
                json.dump(cfg, f)
                f.flush()
                with self.assertRaises(ValueError, msg=path):
                    sp.load(f.name)


if __name__ == '__main__':
    unittest.main()
