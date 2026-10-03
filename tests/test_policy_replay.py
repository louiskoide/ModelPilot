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


if __name__ == '__main__':
    unittest.main()
