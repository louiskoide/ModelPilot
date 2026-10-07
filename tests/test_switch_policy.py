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


def jev_only(cfg=None):
    """The shipped config with calibration off: Jev's probabilities as given, to test the mechanics around them."""
    cfg = copy.deepcopy(cfg or sp.load())
    cfg['calibration']['enabled'] = False
    return cfg


class SwitchPolicyTests(unittest.TestCase):
    def setUp(self):
        self.cfg, self.rates = jev_only(), bench.rates()

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
        # Not at a wasted fraction as low as 0.25: a failure is redone at the turn's effort first (Opus 5.5 medium,
        # which Jev gives about 15% here) before a new turn can raise it, and when a failure wastes little, that
        # cheaper path comes within a few percent of jumping now (since October 3).
        for horizon, spread, wasted in itertools.product([5, 15, 40], [.5, 1, 2], [.5, .75]):
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
        self.assertEqual(self.decide(adv, (O, 'high'), kb=300, warm=True)['action'], 'stay')
        shorter = self.decide(adv, (O, 'high'), kb=100, warm=True)
        self.assertTrue(shorter['downgrade'])
        self.assertGreater(shorter['required_usd'], self.cfg['hysteresis_usd'])  # plus a multiple of the rewrite
        self.assertEqual(self.decide(adv, (O, 'high'), kb=20, warm=False)['action'], 'jump')

    def test_a_low_confidence_downgrade_is_refused(self):
        adv = advice(S, 'medium', model_p=.25, models={H: .6, S: .25, O: .15})
        decision = self.decide(adv, (O, 'high'))
        self.assertEqual((decision['action'], decision['reason']), ('stay', 'low_confidence_no_downgrade'))

    def test_hysteresis_protects_a_plausible_setting_not_a_hopeless_one(self):
        plausible = advice(S, 'high', model_p=.9, effort_p=.6)  # Sonnet medium has about a 20% chance of being enough
        self.assertEqual(self.decide(plausible, (S, 'medium'))['action'], 'jump')  # with a small margin
        cfg = dict(self.cfg, hysteresis_usd=5.0)
        decision = self.decide(plausible, (S, 'medium'), cfg=cfg)
        self.assertEqual((decision['action'], decision['reason']), ('stay', 'not_worth_switching'))
        hopeless = self.decide(advice(O, 'xhigh'), (S, 'medium'), cfg=cfg)  # almost surely not enough: little protection
        self.assertEqual(hopeless['action'], 'jump')
        self.assertLess(hopeless['required_usd'], .1)

    def off(self):
        """Top-level effort changes, as before per-message effort (September 30)."""
        cfg = copy.deepcopy(self.cfg)
        cfg['per_message_effort']['enabled'] = False
        return cfg

    def test_switch_costs_by_kind(self):
        prof = sp.profile(self.cfg, dict(request(40), tools=[{'name': 'x', 'description': 'y' * 20000}]), warm=True)
        self.assertEqual(sp.switch_cost(self.cfg, self.rates, (S, 'medium'), (S, 'high'), prof), 0)  # per-message effort
        off = self.off()
        model_change = sp.switch_cost(off, self.rates, (S, 'medium'), (O, 'medium'), prof)
        opus_effort = sp.switch_cost(off, self.rates, (O, 'medium'), (O, 'high'), prof)
        sonnet_effort = sp.switch_cost(off, self.rates, (S, 'medium'), (S, 'high'), prof)
        self.assertAlmostEqual(model_change, prof['prefix_tokens'] * (5 - .2) / 1e6)
        self.assertAlmostEqual(opus_effort, prof['messages_tokens'] * (5 - .2) / 1e6)  # Opus 5.5 keeps tools and system
        cfg = copy.deepcopy(off)
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
        # Inside the turn the effort can't change: only a stronger model at the turn's effort is offered.
        self.assertEqual(set(rows), {f'{S}/medium', f'{O}/medium'})
        self.assertEqual(decision['target'], [O, 'medium'])
        # On the top model nothing stronger is left in the turn (raising the effort would not take effect).
        self.assertEqual(self.decide(advice(O, 'max'), (O, 'xhigh'), trigger='stuck_evidence')['action'], 'stop')

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

    def test_effort_moves_only_at_a_turn_start(self):
        # Inside a turn's tool loop an effort change does not take effect (per-message-effort probe), so a mid-task
        # step offers Jev's model at the turn's effort; a turn start offers Jev's whole setting.
        turn = self.decide(advice(O, 'high'), (S, 'medium'), kb=60, warm=True)
        step = self.decide(advice(O, 'high'), (S, 'medium'), kb=60, warm=True, trigger='step')
        self.assertEqual({c['setting'] for c in turn['candidates']}, {f'{S}/medium', f'{O}/high'})
        self.assertEqual({c['setting'] for c in step['candidates']}, {f'{S}/medium', f'{O}/medium'})
        down = self.decide(advice(S, 'low'), (O, 'xhigh'), kb=60, warm=True, trigger='step')
        self.assertEqual({c['setting'] for c in down['candidates']}, {f'{O}/xhigh', f'{S}/xhigh'})
        # A configuration that allows effort moves at steps gets the effort-only candidate back.
        cfg = self.off()
        cfg['effort_changes_at'] = ['turn_start', 'step']
        step = self.decide(advice(O, 'high'), (S, 'medium'), kb=60, warm=True, trigger='step', cfg=cfg)
        self.assertEqual({c['setting'] for c in step['candidates']}, {f'{S}/medium', f'{O}/high', f'{S}/high'})

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
        on, off = self.off(), self.off()  # effort rewrites, to see what a warm entry saves
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
        decision = sp.decide(on, self.rates, advice(S, 'medium'), (O, 'xhigh'), prof, 'turn_start')
        target = next(c for c in decision['candidates'] if c['setting'] == f'{S}/medium')
        self.assertAlmostEqual(target['switch_usd'], 1000 * extra)

    def test_per_message_effort_makes_effort_changes_free_but_not_model_changes(self):
        request_ = dict(request(40), tools=[{'name': 'x', 'description': 'y' * 20000}])
        prof = sp.profile(self.cfg, request_, warm=True)
        on = self.cfg  # on by default since the probe
        self.assertEqual(sp.switch_cost(on, self.rates, (S, 'medium'), (S, 'xhigh'), prof), 0)
        self.assertGreater(sp.switch_cost(on, self.rates, (S, 'medium'), (O, 'medium'), prof), 0)
        self.assertGreater(sp.switch_cost(self.off(), self.rates, (S, 'medium'), (S, 'xhigh'), prof), 0)
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
                            (('per_message_effort', 'placement'), 'middle'), (('per_message_effort', 'beta'), 'a,b'),
                            (('effort_changes_at',), ['turn_start', 'hourly']),
                            (('effort_request_factor',), {'medium': 1.0}), (('effort_output_factor',), dict.fromkeys(EFFORTS, 0)),
                            (('calibration', 'enabled'), 'yes'), (('calibration', 'jev_weight'), 1.5),
                            (('calibration', 'applies_at'), ['stuck_evidence']),
                            (('calibration', 'outcomes'), {f'{S}/medium': {'passed': 5, 'trials': 4}}),
                            (('calibration', 'outcomes'), {f'{S}/extreme': {'passed': 1, 'trials': 1}})):
            cfg = copy.deepcopy(self.cfg)
            (cfg[path[0]] if len(path) == 2 else cfg).__setitem__(path[-1], value)
            with tempfile.NamedTemporaryFile('w', suffix='.json') as f:
                json.dump(cfg, f)
                f.flush()
                with self.assertRaises(ValueError, msg=path):
                    sp.load(f.name)
        cfg = copy.deepcopy(self.cfg)
        cfg['models'][O]['request_factor'] = 0
        with tempfile.NamedTemporaryFile('w', suffix='.json') as f:
            json.dump(cfg, f)
            f.flush()
            with self.assertRaises(ValueError):
                sp.load(f.name)

    def test_a_failure_is_redone_at_the_turns_effort_and_then_in_a_new_turn(self):
        # Jev's Opus 5.5 xhigh: a failure of Sonnet 5.5 medium shows inside the turn, where only the model can move, so
        # it is redone on Opus 5.5 medium; if that fails too, a new turn can raise the effort to Jev's setting.
        prof = sp.profile(self.cfg, request(11), False)
        adv = advice(O, 'xhigh', model_p=.85, effort_p=.7)
        decision = sp.decide(self.cfg, self.rates, adv, (S, 'medium'), prof, 'turn_start')
        stay = decision['candidates'][0]
        model_ok, effort_ok = sp.sufficiency(self.cfg, adv)
        run = lambda s: sp.run_cost(self.cfg, self.rates, s, prof)
        switch = lambda a, b: sp.switch_cost(self.cfg, self.rates, a, b, prof, warm=True)
        w = self.cfg['recovery']['wasted_fraction']
        p_om, p_ox = model_ok(O) * effort_ok('medium'), model_ok(O) * effort_ok('xhigh')
        top = run((O, 'max'))  # the strongest of all: where a failure of Jev's setting goes
        jev = p_ox * run((O, 'xhigh')) + (1 - p_ox) * (w * run((O, 'xhigh')) + switch((O, 'xhigh'), (O, 'max')) + top)
        opus_medium = p_om * run((O, 'medium')) + (1 - p_om) * (w * run((O, 'medium')) + jev)
        self.assertAlmostEqual(stay['recover_usd'], w * run((S, 'medium')) + switch((S, 'medium'), (O, 'medium')) + opus_medium)
        # A configuration whose stuck evidence could change the effort redoes it on Jev's setting straight away.
        cfg = copy.deepcopy(self.cfg)
        cfg['effort_changes_at'] = ['turn_start', 'stuck_evidence']
        anywhere = sp.decide(cfg, self.rates, adv, (S, 'medium'), prof, 'turn_start')['candidates'][0]
        self.assertAlmostEqual(anywhere['recover_usd'], w * run((S, 'medium')) + switch((S, 'medium'), (O, 'xhigh')) + jev)


class CostModelTests(unittest.TestCase):
    """run_cost against the measured shape of a task (configs/modelpilot-policy.json, defaults_evidence)."""

    def setUp(self):
        self.cfg, self.rates = sp.load(), bench.rates()
        self.prof = dict(self.cfg['defaults'], prefix_tokens=7100, messages_tokens=1500, warm=False, warm_entries={})

    def expected(self, model, requests, reply, warm=False):
        rate, d = self.rates[model], self.cfg['defaults']
        growth = d['new_input_tokens'] + reply
        prefix = 7100 + (requests - 1) / 2 * growth
        cold = 0 if warm else 7100 * (rate['write_5m'] - rate['read'])
        return (requests * (prefix * rate['read'] + growth * rate['write_5m'] + reply * rate['output']) + cold) / 1e6

    def test_each_request_reads_the_prefix_writes_what_it_adds_and_a_cold_start_writes_the_prefix(self):
        d = self.cfg['defaults']
        self.assertAlmostEqual(sp.run_cost(self.cfg, self.rates, (S, 'medium'), self.prof),
                               self.expected(S, d['horizon_requests'], d['output_tokens']))
        warm = dict(self.prof, warm=True)
        self.assertAlmostEqual(sp.run_cost(self.cfg, self.rates, (S, 'medium'), warm),
                               self.expected(S, d['horizon_requests'], d['output_tokens'], warm=True))
        # The measured shape forecasts what a Sonnet 5.5 and an Opus 5.5 medium task cost on the tuning split.
        self.assertAlmostEqual(sp.run_cost(self.cfg, self.rates, (S, 'medium'), self.prof), .085, delta=.01)
        self.assertAlmostEqual(sp.run_cost(self.cfg, self.rates, (O, 'medium'), self.prof), .219, delta=.02)

    def test_effort_and_model_scale_the_requests_and_the_output_per_request(self):
        d, cfg = self.cfg['defaults'], self.cfg
        for model, effort in ((S, 'high'), (O, 'medium'), (O, 'xhigh')):
            spec = cfg['models'][model]
            requests = d['horizon_requests'] * cfg['effort_request_factor'][effort] * spec['request_factor']
            reply = d['output_tokens'] * cfg['effort_output_factor'][effort] * spec['output_factor']
            self.assertAlmostEqual(sp.run_cost(cfg, self.rates, (model, effort), self.prof),
                                   self.expected(model, requests, reply), msg=(model, effort))


class CalibrationTests(unittest.TestCase):
    """P_ok blends Jev with the pass rates measured on the tuning split (configs/modelpilot-policy.json, calibration)."""

    # Jev's recorded answer for cachetools-tlru-cache (bench-20261003-095823): Sonnet 5.5, effort high at 0.34.
    TLRU = {'model': {'choice': S, 'confidence': .81, 'probabilities': {O: .12, S: .87, H: .01}},
            'effort': {'choice': 'high', 'confidence': .34,
                       'probabilities': {'low': 0, 'medium': .11, 'high': .48, 'xhigh': .4, 'max': .01}}}

    def setUp(self):
        self.cfg, self.rates = sp.load(), bench.rates()
        self.prof = sp.profile(self.cfg, request(16), False)

    def decide(self, adv, current=(S, 'medium'), trigger='turn_start', cfg=None):
        return sp.decide(cfg or self.cfg, self.rates, adv, current, self.prof, trigger)

    def test_the_shipped_config_is_calibrated_on_the_fixed_arms_strict_tuning_results(self):
        cal = self.cfg['calibration']
        self.assertTrue(cal['enabled'])
        # Strict passes (hidden grader and edge suite, October 4) over full sets of the 29 tuning tasks (October 6);
        # Sonnet 5.5 low is low concise, the ModelPilot arms' start.
        self.assertEqual(cal['outcomes'], {f'{S}/low': {'passed': 27, 'trials': 29},
                                           f'{S}/medium': {'passed': 48, 'trials': 52},
                                           f'{O}/medium': {'passed': 29, 'trials': 29}})
        self.assertAlmostEqual(sp.measured_ok(self.cfg, (S, 'low')), 28 / 31)
        self.assertAlmostEqual(sp.measured_ok(self.cfg, (S, 'medium')), 49 / 54)
        self.assertAlmostEqual(sp.measured_ok(self.cfg, (S, 'high')), 49 / 54)  # nothing stronger measured on Sonnet
        self.assertAlmostEqual(sp.measured_ok(self.cfg, (O, 'xhigh')), 30 / 31)  # at least as strong as a measured one
        self.assertAlmostEqual(sp.measured_ok(self.cfg, (O, 'low')), 28 / 31)  # only Sonnet 5.5 low is as weak

    def test_p_ok_blends_the_measured_rate_with_jevs_estimate(self):
        decision = self.decide(self.TLRU)
        w = self.cfg['calibration']['jev_weight']
        self.assertEqual(decision['calibration'], {'jev_weight': w})
        for row in decision['candidates']:
            self.assertAlmostEqual(row['p_ok'], (1 - w) * row['p_measured'] + w * row['p_jev'])
        stay = decision['candidates'][0]
        self.assertLess(stay['p_jev'], .15)  # Jev's answer read as given: medium is probably not enough
        self.assertGreater(stay['p_ok'], .75)

    def test_the_recorded_harder_task_advice_now_stays_where_it_jumped(self):
        self.assertEqual(self.decide(self.TLRU)['action'], 'stay')
        before = self.decide(self.TLRU, cfg=jev_only(self.cfg))
        self.assertEqual((before['action'], before['target']), ('jump', [S, 'high']))

    def test_a_setting_with_nothing_measured_as_weak_keeps_jevs_estimate(self):
        cfg = copy.deepcopy(self.cfg)
        del cfg['calibration']['outcomes'][f'{S}/low']  # as before October 6
        decision = self.decide(advice(S, 'low', effort_p=.9), current=(S, 'medium'), cfg=cfg)
        low = next(c for c in decision['candidates'] if c['setting'] == f'{S}/low')
        self.assertIsNone(low['p_measured'])
        self.assertAlmostEqual(low['p_ok'], low['p_jev'])

    # Jev's recorded first answer for cachetools-cache-key (bench-20261006-093401), its usual shape on the tuning
    # tasks: Sonnet 5.5, effort medium, nothing on low.
    USUAL = {'model': {'choice': S, 'confidence': .91, 'probabilities': {S: .95, H: .02, O: .03}},
             'effort': {'choice': 'medium', 'confidence': .55,
                        'probabilities': {'low': 0, 'medium': .64, 'high': .33, 'xhigh': .03, 'max': 0}}}

    def test_the_low_concise_start_holds_against_jevs_usual_medium_answer(self):
        decision = self.decide(self.USUAL, current=(S, 'low'))
        self.assertEqual((decision['action'], decision['target']), ('stay', [S, 'low']))
        stay = decision['candidates'][0]
        self.assertAlmostEqual(stay['p_measured'], 28 / 31)
        self.assertLess(stay['p_jev'], .02)
        # At the weight fitted on medium's outcomes alone, Jev's effort answer alone moved the start to medium.
        cfg = copy.deepcopy(self.cfg)
        cfg['calibration']['jev_weight'] = .09
        moved = self.decide(self.USUAL, current=(S, 'low'), cfg=cfg)
        self.assertEqual((moved['action'], moved['target']), ('jump', [S, 'medium']))

    def test_stuck_evidence_is_never_calibrated(self):
        adv = advice(S, 'medium')
        calibrated = self.decide(adv, trigger='stuck_evidence')
        self.assertNotIn('calibration', calibrated)
        self.assertEqual(calibrated, self.decide(adv, trigger='stuck_evidence', cfg=jev_only(self.cfg)))
        self.assertEqual(calibrated['target'], [O, 'medium'])  # the safety net: a stronger model at the turn's effort


class JevGateTests(unittest.TestCase):
    """Plan item 1 (October 6): Jev is asked only where some answer it could give changes the decision."""

    def setUp(self):
        self.cfg, self.rates = sp.load(), bench.rates()

    def gate(self, cfg=None, current=(S, 'low'), kb=11, warm=False, trigger='turn_start'):
        cfg = cfg or self.cfg
        return sp.jev_gate(cfg, self.rates, current, sp.profile(cfg, request(kb), warm), trigger)

    def test_at_the_shipped_weight_no_answer_moves_a_turn_start_or_a_step(self):
        for trigger in ('turn_start', 'step'):
            gate = self.gate(trigger=trigger)
            self.assertEqual((gate['can_change'], gate['reason']), (False, 'every_answer_stays'), trigger)
            self.assertLess(gate['closest_usd'], 0)
            self.assertEqual(gate['shapes'], 3 * 2 * (1 + 5 * 2))

    def test_where_jevs_answer_is_the_whole_estimate_it_is_always_asked(self):
        self.assertEqual(self.gate(cfg=jev_only())['reason'], 'uncalibrated')
        self.assertEqual(self.gate(trigger='stuck_evidence')['reason'], 'uncalibrated')
        delegate = sp.with_overrides(self.cfg, bench.ARMS['modelpilot-delegate']['policy_overrides'])
        self.assertEqual(self.gate(cfg=delegate, trigger='step')['reason'], 'consult_possible')  # its target is Jev's
        self.assertFalse(self.gate(cfg=delegate)['can_change'])  # no consult at a turn start
        partial = copy.deepcopy(self.cfg)
        partial['recovery']['wasted_fraction'] = .5
        self.assertEqual(self.gate(cfg=partial)['reason'], 'not_monotone')
        self.assertEqual(sp.gate({}), {'enabled': False})

    def test_a_skipped_point_stays_on_any_answer(self):
        """Soundness against arbitrary answers, at weights where moves happen; the sweep must also find points where
        the gate asks and an answer does move, so it isn't vacuous."""
        rng = __import__('random').Random(6)

        def answer():
            def dist(labels):
                x = [rng.random() ** 4 for _ in labels]
                return {k: v / sum(x) for k, v in zip(labels, x)}
            return {'model': {'choice': rng.choice(MODELS), 'confidence': rng.random(), 'probabilities': dist(MODELS)},
                    'effort': {'choice': rng.choice(EFFORTS), 'confidence': rng.random(), 'probabilities': dist(EFFORTS)}}
        skipped = moved = 0
        for w, kb, warm, trigger, current in itertools.product(
                (.03, .06, .2, .5), (2, 11, 60), (False, True), ('turn_start', 'step'), ((S, 'low'), (S, 'medium'), (O, 'medium'))):
            cfg = copy.deepcopy(self.cfg)
            cfg['calibration']['jev_weight'] = w
            prof = sp.profile(cfg, request(kb), warm)
            gate = sp.jev_gate(cfg, self.rates, current, prof, trigger)
            actions = {sp.decide(cfg, self.rates, answer(), current, prof, trigger)['action'] for _ in range(40)}
            if not gate['can_change']:
                skipped += 1
                self.assertEqual(actions, {'stay'}, (w, kb, warm, trigger, current))
            moved += actions != {'stay'}
        self.assertGreater(skipped, 0)
        self.assertGreater(moved, 0)


class BreakEvenTests(unittest.TestCase):
    """Plan item 7: what an unnoticed miss would have to cost for a stronger setting to pay. Shown, never priced."""
    def setUp(self):
        self.cfg, self.rates = sp.load(), bench.rates()

    def named(self, setting):
        return {'model': {'choice': setting[0], 'confidence': 1.0, 'probabilities': {setting[0]: 1.0}},
                'effort': {'choice': setting[1], 'confidence': 1.0, 'probabilities': {setting[1]: 1.0}}}

    def test_each_break_even_is_where_the_decision_turns(self):
        """Priced into the gate's own decision, a damage just under each figure stays and just over it moves there."""
        measured = copy.deepcopy(self.cfg)
        measured['calibration']['jev_weight'] = 0.0
        checked = 0
        for kb, warm, trigger, current in itertools.product(
                (2, 11, 60), (False, True), ('turn_start', 'step'), ((S, 'low'), (S, 'medium'), (O, 'low'))):
            prof = sp.profile(self.cfg, request(kb), warm)
            shown = sp.break_even(self.cfg, self.rates, current, prof, trigger)
            for move in shown['moves']:
                damage = move['damage_usd']
                if not damage:
                    continue
                target = tuple(move['setting'].split('/'))
                below, above = (sp._decide(measured, self.rates, self.named(target), current, prof, trigger,
                                           damage_usd=damage * f) for f in (.99, 1.01))
                self.assertEqual(below['action'], 'stay', (kb, warm, trigger, current, move))
                self.assertEqual((above['action'], above['target']), ('jump', list(target)), (kb, warm, trigger, move))
                checked += 1
        self.assertGreater(checked, 20)

    def test_from_the_low_start_only_opus_at_medium_or_above_has_fewer_measured_misses(self):
        prof = sp.profile(self.cfg, request(20), False)
        start = sp.break_even(self.cfg, self.rates, (S, 'low'), prof, 'turn_start')
        self.assertEqual(start['basis'], 'measured_pass_rates')
        self.assertEqual(start['cheapest']['setting'], f'{O}/medium')
        self.assertTrue(1 < start['cheapest']['damage_usd'] < 10, start['cheapest'])
        by = {m['setting']: m for m in start['moves']}
        self.assertIsNone(by[f'{O}/low']['damage_usd'])  # Opus low has only Sonnet low's measured rate
        self.assertGreater(by[f'{S}/medium']['damage_usd'], start['cheapest']['damage_usd'])  # 0.4 points fewer misses
        # Inside a turn the effort holds, and Opus at low has no fewer measured misses: no move can pay.
        step = sp.break_even(self.cfg, self.rates, (S, 'low'), prof, 'step')
        self.assertEqual([m['setting'] for m in step['moves']], [f'{O}/low'])
        self.assertIsNone(step['cheapest'])

    def test_it_is_shown_only_where_the_measured_rates_apply_and_changes_nothing(self):
        prof = sp.profile(self.cfg, request(11), False)
        self.assertIsNone(sp.break_even(self.cfg, self.rates, (S, 'low'), prof, 'stuck_evidence'))
        self.assertIsNone(sp.break_even(jev_only(), self.rates, (S, 'low'), prof, 'turn_start'))
        self.assertIsNone(sp.break_even(self.cfg, self.rates, (O, 'max'), prof, 'turn_start')['cheapest'])  # strongest
        adv = advice(O, 'xhigh')
        decision = sp.decide(self.cfg, self.rates, adv, (S, 'low'), prof, 'turn_start')
        self.assertEqual(decision['break_even'], sp.break_even(self.cfg, self.rates, (S, 'low'), prof, 'turn_start'))
        bare = sp._decide(self.cfg, self.rates, adv, (S, 'low'), prof, 'turn_start')
        self.assertEqual({k: decision[k] for k in bare}, bare)


if __name__ == '__main__':
    unittest.main()


class ExplorationPlanTests(unittest.TestCase):
    def cfg(self, **spec):
        return sp.with_overrides(sp.load(), {'exploration': dict({'enabled': True}, **spec)})

    def test_draws_are_reproducible_balanced_and_in_range(self):
        cfg = self.cfg()
        self.assertEqual(sp.exploration_plan(cfg, 'a/x/0'), sp.exploration_plan(cfg, 'a/x/0'))
        draws = [sp.exploration_plan(cfg, f'task{i}/modelpilot-explore/0') for i in range(4000)]
        self.assertAlmostEqual(sum(d['switch'] for d in draws) / len(draws), 0.5, delta=0.03)
        self.assertEqual({d['at_request'] for d in draws}, {2, 3, 4, 5})
        never = self.cfg(probability=0.0)
        self.assertFalse(any(sp.exploration_plan(never, f'k{i}')['switch'] for i in range(200)))

    def test_bad_settings_are_refused(self):
        for spec in ({'probability': 1.5}, {'first_request': 1}, {'first_request': 4, 'last_request': 3},
                     {'target_model': 'claude-haiku-4-5-20251001'}):
            with self.assertRaises(ValueError, msg=spec):
                self.cfg(**spec)
        self.assertFalse(sp.exploration(sp.load())['enabled'])  # off unless an arm turns it on
