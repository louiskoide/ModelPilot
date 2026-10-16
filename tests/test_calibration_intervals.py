"""policy_replay --intervals (October 15, overnight cloud run): 95% intervals on the measured pass rates calibration
uses, and what within them would change a replayed decision. A report: decisions and the config stay as they are. $0."""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from modelpilot import bench, policy_replay as pr, switch_policy as sp
from tests import test_switch_policy
from tests.test_policy_replay import PROFILE, journal, record
from tests.test_switch_policy import S

TLRU = test_switch_policy.CalibrationTests.TLRU
LOW = f'{S}/low'


class IntervalMathTests(unittest.TestCase):
    def test_the_beta_distribution_against_closed_forms(self):
        self.assertAlmostEqual(pr.beta_cdf(.3, 2, 2), 3 * .3 ** 2 - 2 * .3 ** 3, places=12)  # Beta(2, 2)
        self.assertAlmostEqual(pr.beta_cdf(.7, 1, 1), .7, places=12)
        for a in (1, 4, 30):
            for q in (.025, .5, .975):
                self.assertAlmostEqual(pr.beta_quantile(q, a, 1), q ** (1 / a), places=10)  # Beta(a, 1): x^a
                self.assertAlmostEqual(pr.beta_quantile(q, 1, a), 1 - (1 - q) ** (1 / a), places=10)
        self.assertAlmostEqual(pr.beta_quantile(.5, 7, 7), .5, places=10)
        self.assertEqual((pr.beta_cdf(0, 3, 4), pr.beta_cdf(1, 3, 4)), (0.0, 1.0))

    def test_each_measured_rate_sits_in_its_interval_as_the_policy_reads_it(self):
        cfg = sp.load()
        intervals = pr.calibration_intervals(cfg)
        self.assertEqual(set(intervals), set(cfg['calibration']['outcomes']))
        for key, i in intervals.items():
            with self.subTest(key=key):
                own = cfg['calibration']['outcomes'][key]
                self.assertEqual(i['rate'], (own['passed'] + 1) / (own['trials'] + 2))
                # measured_ok takes the best rate among the settings a candidate bounds, its own included.
                self.assertGreaterEqual(sp.measured_ok(cfg, key.split('/')), i['rate'])
                self.assertLess(i['low'], i['rate'])
                self.assertLess(i['rate'], i['high'])
                self.assertLessEqual(i['high'], 1)
        opus = intervals['claude-opus-5-5/medium']  # 29/29: Beta(30, 1)
        self.assertAlmostEqual(opus['low'], .025 ** (1 / 30), places=10)
        self.assertAlmostEqual(opus['high'], .975 ** (1 / 30), places=10)

    def test_a_variant_reads_the_rate_it_was_given_and_leaves_the_config_alone(self):
        cfg = sp.load()
        before = json.dumps(cfg, sort_keys=True)
        variant = pr.with_rates(cfg, {LOW: .8})
        self.assertAlmostEqual((variant['calibration']['outcomes'][LOW]['passed'] + 1) /
                               (variant['calibration']['outcomes'][LOW]['trials'] + 2), .8, places=12)
        self.assertEqual(json.dumps(cfg, sort_keys=True), before)
        variants = pr.interval_variants(cfg, pr.calibration_intervals(cfg))
        self.assertEqual(len(variants), len(cfg['calibration']['outcomes']) * pr.INTERVAL_GRID + 2 + pr.JOINT_SAMPLES)
        self.assertEqual([label for label, _ in variants], [label for label, _ in pr.interval_variants(
            cfg, pr.calibration_intervals(cfg))])  # seeded: the same draws every time
        self.assertEqual(json.dumps(cfg, sort_keys=True), before)


class FlipTests(unittest.TestCase):
    def setUp(self):
        self.cfg, self.rates = sp.load(), bench.rates()
        self.payload = {'point': '1/turn/1', 'trigger': 'turn_start', 'current': [S, 'low'], 'action': 'stay',
                        'target': [S, 'low'], 'candidates': [{'p_ok': .9}], 'profile': PROFILE, 'advice': TLRU}

    def test_the_point_values_change_nothing_and_a_far_lower_rate_does(self):
        decision = pr.replay(self.payload, self.cfg, self.rates)
        self.assertEqual((decision['action'], pr._setting(decision)), ('stay', LOW))
        point = {k: i['rate'] for k, i in pr.calibration_intervals(self.cfg).items()}
        same = [({'kind': 'one', 'outcome': LOW, 'rate': point[LOW]}, pr.with_rates(self.cfg, point))]
        flips = pr.interval_flips(self.payload, same, self.rates, 4, [S, 'low'], decision)
        self.assertEqual(flips, {'one_at_a_time': [], 'all_low': None, 'all_high': None, 'joint_draws': 0, 'any': False})
        far = [({'kind': 'one', 'outcome': LOW, 'rate': .3}, pr.with_rates(self.cfg, {LOW: .3})),
               ({'kind': 'all_low'}, pr.with_rates(self.cfg, {LOW: .3})), ({'kind': 'joint'}, pr.with_rates(self.cfg, {LOW: .3}))]
        flips = pr.interval_flips(self.payload, far, self.rates, 4, [S, 'low'], decision)
        self.assertTrue(flips['any'])
        self.assertEqual([(f['outcome'], f['rate'], f['action']) for f in flips['one_at_a_time']], [(LOW, .3, 'jump')])
        self.assertEqual((flips['all_low']['action'], flips['all_high'], flips['joint_draws']), ('jump', None, 1))

    def test_a_run_replayed_with_intervals_decides_as_without_and_leaves_the_config_file_alone(self):
        step = dict(self.payload, point='1/step/tests/3', trigger='step')
        opus = dict(self.payload, current=['claude-opus-5-5', 'medium'], action='stay', target=['claude-opus-5-5', 'medium'])
        config_sha = hashlib.sha256(sp.CONFIG.read_bytes()).hexdigest()
        loaded = json.dumps(self.cfg, sort_keys=True)
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)/'bench-x'
            run.mkdir()
            (run/'manifest.json').write_text(json.dumps({'seed': 0, 'arms': {'modelpilot': {}}}))
            for task, payloads in (('t-a', [self.payload, step]), ('t-b', [opus]), ('t-c', [dict(step, profile=None)])):
                journal(run/task/'modelpilot'/'0', payloads)
                (run/task/'modelpilot'/'0'/'trial.json').write_text(json.dumps(record(task)))
            plain = pr.replay_run(run, self.cfg, self.rates)
            variants = pr.interval_variants(self.cfg, pr.calibration_intervals(self.cfg))
            checked = pr.replay_run(run, self.cfg, self.rates, variants=variants)
            out = Path(tmp)/'replay.json'
            with mock.patch('sys.argv', ['policy_replay', str(run), '--intervals', '--out', str(out)]), \
                    contextlib.redirect_stdout(io.StringIO()):
                pr.main()
            written = json.loads(out.read_text())
            with mock.patch('sys.argv', ['policy_replay', str(run), '--out', str(Path(tmp)/'plain.json')]), \
                    contextlib.redirect_stdout(io.StringIO()):
                pr.main()
            without = json.loads((Path(tmp)/'plain.json').read_text())
        strip = lambda rows: [{k: v for k, v in r.items() if k != 'interval_flips'} for r in rows]
        self.assertEqual(strip(checked), plain)  # the same decisions, rows and statuses
        self.assertEqual(strip(written['rows']), without['rows'])
        self.assertEqual({k: v for k, v in written['summary'].items() if k != 'interval_sensitivity'}, without['summary'])
        self.assertNotIn('intervals', without)
        self.assertEqual(hashlib.sha256(sp.CONFIG.read_bytes()).hexdigest(), config_sha)
        self.assertEqual(written['config_sha256'], config_sha)
        self.assertEqual(json.dumps(self.cfg, sort_keys=True), loaded)
        self.assertEqual(set(written['intervals']['rates']), set(self.cfg['calibration']['outcomes']))
        flips = [r.get('interval_flips') for r in checked]
        self.assertEqual([f is not None for f in flips], [True, True, True, False])  # not replayable: not checked
        sensitivity = written['summary']['interval_sensitivity']
        self.assertEqual(sensitivity['decisions'], 3)
        self.assertEqual(sensitivity['flippable'], sum(f['any'] for f in flips if f))


if __name__ == '__main__':
    unittest.main()
