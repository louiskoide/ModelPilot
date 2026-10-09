"""Replaying journaled ModelPilot decisions through today's switch policy. Pure, $0."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from modelpilot import bench, policy_replay as pr, switch_policy as sp
from tests import test_switch_policy
from tests.test_switch_policy import S

TLRU = test_switch_policy.CalibrationTests.TLRU

PROFILE = {'prefix_tokens': 5584.25, 'messages_tokens': 1085.5, 'warm': False, 'warm_entries': {}}


def journal(trial, payloads):
    trial.mkdir(parents=True)
    db = sqlite3.connect(trial/'governor.sqlite3')
    db.execute('create table gov_decisions (seq integer primary key, session, kind, task, revision, payload, created)')
    db.execute("insert into gov_decisions (kind, payload) values ('admit', '{}')")  # other kinds are ignored
    for p in payloads:
        db.execute("insert into gov_decisions (kind, payload) values ('advisor_decision', ?)", (json.dumps(p),))
    db.commit()
    db.close()


def record(task, passed=True, cost=.27):
    return {'task': task, 'arm': 'modelpilot', 'trial': 0, 'passed': passed, 'complete': True,
            'routing': {'kind': 'modelpilot_policy'}, 'accounting': {'cost_usd': cost},
            'cache': {'cold_equivalent_cost_usd': cost}}


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.cfg, self.rates = sp.load(), bench.rates()
        self.jump = {'point': '1/turn/1', 'trigger': 'turn_start', 'current': [S, 'medium'], 'action': 'jump',
                     'target': [S, 'high'], 'candidates': [{'p_ok': .11}], 'profile': PROFILE,
                     'advice': TLRU}

    def test_token_counts_are_rescaled_to_todays_bytes_per_token(self):
        decision = pr.replay(self.jump, self.cfg, self.rates)
        scale = pr.RECORDED_BYTES_PER_TOKEN / self.cfg['bytes_per_token']
        prof = dict(self.cfg['defaults'], prefix_tokens=PROFILE['prefix_tokens'] * scale,
                    messages_tokens=PROFILE['messages_tokens'] * scale, warm=False, warm_entries={})
        self.assertEqual((decision['action'], decision['target']), ('stay', [S, 'medium']))
        self.assertAlmostEqual(decision['forecast_usd'], sp.run_cost(self.cfg, self.rates, (S, 'medium'), prof))
        self.assertIsNone(pr.replay(dict(self.jump, profile=None), self.cfg, self.rates))
        failed = pr.replay(dict(self.jump, advice=dict(TLRU, error='timeout')), self.cfg, self.rates)
        self.assertEqual(failed['reason'], 'advice_unavailable')  # as the live policy treats a failed advisor call

    def test_a_run_replays_every_trial_and_marks_decisions_after_a_change(self):
        step = {'point': '1/step/tests/3', 'trigger': 'step', 'current': [S, 'high'], 'action': 'stay',
                'target': [S, 'high'], 'candidates': [{'p_ok': .97}], 'profile': PROFILE, 'advice': TLRU}
        stay = dict(self.jump, action='stay', target=[S, 'medium'])
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)/'bench-x'
            run.mkdir()
            (run/'manifest.json').write_text(json.dumps({'seed': 0, 'arms': {'modelpilot': {}, 'sonnet-5.5': {}}}))
            for task, payloads in (('t-jumped', [self.jump, step]), ('t-stayed', [stay]), ('t-old', [dict(stay, profile=None)])):
                journal(run/task/'modelpilot'/'0', payloads)
                (run/task/'modelpilot'/'0'/'trial.json').write_text(json.dumps(record(task)))
            journal(run/'t-jumped'/'sonnet-5.5'/'0', [self.jump])  # not a ModelPilot trial: skipped
            (run/'t-jumped'/'sonnet-5.5'/'0'/'trial.json').write_text(json.dumps(dict(record('t-jumped'), arm='sonnet-5.5',
                                                                                     routing=None)))
            rows = pr.replay_run(run, self.cfg, self.rates)
        self.assertEqual([(r['task'], r['trigger'], r['status']) for r in rows],
                         [('t-jumped', 'turn_start', 'changed'), ('t-jumped', 'step', 'path_diverged'),
                          ('t-old', 'turn_start', 'not_replayable'), ('t-stayed', 'turn_start', 'same')])
        jumped = rows[0]
        self.assertEqual((jumped['recorded']['target'], jumped['replayed']['target']), (f'{S}/high', f'{S}/medium'))
        self.assertEqual((jumped['trial_passed'], jumped['trial_cost_usd']), (True, .27))
        summary = pr.summarize(rows)
        self.assertEqual(summary['status'], {'changed': 1, 'path_diverged': 1, 'not_replayable': 1, 'same': 1})
        self.assertEqual(summary['moves'], {f'jump {S}/high -> stay {S}/medium': 1})
        # The gate would have skipped Jev at all three replayable points, and none of them moves on its real answer.
        self.assertEqual(summary['gate']['skipped'], {'turn_start': 2, 'step': 1})
        self.assertEqual((summary['gate']['asked'], summary['gate']['skipped_but_moved']), ({}, 0))
        self.assertTrue(jumped['replayed']['gate']['skip'])
        self.assertIsNone(jumped['replayed']['quality_floor'])  # off in the shipped config

    def test_points_below_the_quality_floor_are_counted_apart_from_gate_errors(self):
        # modelpilot-for-opus from its old start: every point is below the floor, where the gate skips Jev and returns no
        # closest_usd; the floor's moves are by design, not a skip that moved (October 8: summarize raised KeyError).
        def row(action, reason, **gate):
            return {'trigger': 'turn_start', 'status': 'same', 'replayed': {
                'action': action, 'target': f'{S}/low', 'gate': dict(gate, can_change=False, reason=reason, skip=True)}}
        rows = [row('jump', 'below_quality_floor'), row('stay', 'below_quality_floor'),
                row('stay', 'every_answer_stays', closest_usd=-.01), row('jump', 'every_answer_stays', closest_usd=-.02)]
        gate = pr.summarize(rows)['gate']
        self.assertEqual(gate['below_floor'], {'decisions': 2, 'moved': 1})
        self.assertEqual((gate['skipped_but_moved'], gate['closest_skipped_usd']), (1, -.01))

    def test_an_arms_start_replaces_the_recorded_one_until_the_path_diverges(self):
        stay = dict(self.jump, action='stay', target=[S, 'medium'])
        step = {'point': '1/step/spend/3', 'trigger': 'step', 'current': [S, 'medium'], 'action': 'stay',
                'target': [S, 'medium'], 'candidates': [{'p_ok': .97}], 'profile': PROFILE, 'advice': TLRU}
        high = dict(step, current=[S, 'high'], target=[S, 'high'])
        # Since October 3 a trial records the bytes per token its profiles were estimated at.
        recorded = {'S0': [S, 'medium'], 'cost_model': {'bytes_per_token': 2.8}}
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)/'bench-x'
            run.mkdir()
            (run/'manifest.json').write_text(json.dumps({'seed': 0, 'arms': {'modelpilot': {}}}))
            for task, payloads in (('t-jumped', [self.jump, high]), ('t-stayed', [stay, step])):
                journal(run/task/'modelpilot'/'0', payloads)
                (run/task/'modelpilot'/'0'/'trial.json').write_text(json.dumps(
                    dict(record(task), routing={'kind': 'modelpilot_policy', 'policy': {'parameters': recorded}})))
            rows = pr.replay_run(run, self.cfg, self.rates, start=[S, 'low'])
            unchanged = pr.replay_run(run, self.cfg, self.rates, start=[S, 'medium'])
        self.assertEqual([(r['task'], r['trigger'], r['current'], r['status']) for r in rows],
                         [('t-jumped', 'turn_start', f'{S}/low', 'changed'), ('t-jumped', 'step', f'{S}/high', 'path_diverged'),
                          ('t-stayed', 'turn_start', f'{S}/low', 'same'), ('t-stayed', 'step', f'{S}/low', 'same')])
        # A recorded stay on the recorded start counts as a stay on the new start; a jump keeps its target.
        self.assertEqual([r['baseline'] for r in rows if 'baseline' in r],
                         [{'action': 'jump', 'target': f'{S}/high', 'p_ok_current': .11},
                          {'action': 'stay', 'target': f'{S}/low', 'p_ok_current': .11},
                          {'action': 'stay', 'target': f'{S}/low', 'p_ok_current': .97}])
        self.assertEqual({r['recorded_current'] for r in rows if 'baseline' in r}, {f'{S}/medium'})
        self.assertEqual(rows[2]['recorded'], {'action': 'stay', 'target': f'{S}/medium', 'p_ok_current': .11})
        summary = pr.summarize(rows)
        self.assertEqual((summary['rebased'], summary['moves']), (3, {f'jump {S}/high -> stay {S}/low': 1}))
        # The trial's own bytes per token, not the pre-October 3 default of 4.
        self.assertAlmostEqual(rows[2]['replayed']['forecast_usd'],
                               pr.replay(stay, self.cfg, self.rates, 2.8, current=[S, 'low'])['forecast_usd'])
        self.assertNotAlmostEqual(rows[2]['replayed']['forecast_usd'],
                                  pr.replay(stay, self.cfg, self.rates, 4, current=[S, 'low'])['forecast_usd'])
        self.assertFalse(any('baseline' in r for r in unchanged))  # the recorded start: nothing to rebase


if __name__ == '__main__':
    unittest.main()
