"""The risk-routed arm (modelpilot.risk_route): its frozen route, the new-tasks-only rule, the rating as the predictor
sends it, the rating's dollars in the trial's record, and the bench wiring. $0: nothing is sent."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from modelpilot import bench, bench_report, bench_tasks, risk_predictor as rp, risk_route
from modelpilot import long_session, midtask_compaction  # noqa: F401 (they subclass bench.Trial, patched below)
from tests.test_risk_predictor import ToyRepo, rated, reply

ARM = 'modelpilot-for-opus-risk'
REQUEST_USD = (9000 * .1 + 1500 * .5) / 1e6  # one scripted Haiku 5.5 reply (test_risk_predictor.USAGE)


def scripted(*answers):
    """A transport giving each answer in turn (an exception is raised); .sent keeps what it was sent."""
    sent = []

    def transport(payload):
        sent.append(payload)
        answer = answers[len(sent) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer, f'req_{len(sent)}'
    transport.sent = sent
    return transport


class FakeRouted:
    """Stands in for the routed arm's bench.Trial: one session, then a graded record and its trial.json."""
    def __init__(self, task, arm, trial_dir, *args, cost=.3, **options):
        self.dir, self.cost, self.arm = Path(trial_dir), cost, arm
        self.started = self.finished = False
        self.record = {'task': task['id'], 'arm': arm, 'trial': options.get('trial', 0), 'model': bench.ARMS[arm]['model']}

    def step(self):
        self.started = True
        self.finish()
        return False

    def known_cost(self):  # as bench.Trial: a finished trial's from its record
        return self.record['accounting']['known_cost_usd'] if self.finished else self.cost * self.started

    def finish(self, stopped=None):
        self.finished = True
        self.record.update(passed=True, complete=stopped is None, stopped=stopped, wall_seconds=1.0,
                           sessions=[{'stop': 'success'}],
                           accounting={'cost_usd': self.cost, 'known_cost_usd': self.cost,
                                       'cost_if_rejected_free_usd': self.cost},
                           cache={'cold_equivalent_cost_usd': self.cost + .1})
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir/'trial.json').write_text(json.dumps(self.record))
        return self.record

    def close(self):
        pass


class RiskRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.toy = ToyRepo(self.root)
        self.task = dict(self.toy.task, batch='fresh-1')  # a batch made after the pre-registration
        self.rates = bench.rates()

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_route_is_the_frozen_cut_and_an_unrated_task_stays_on_opus(self):
        self.assertEqual(risk_route.route(55), ('modelpilot-for-opus', 'flagged'))
        self.assertEqual(risk_route.route(85), ('modelpilot-for-opus', 'flagged'))
        self.assertEqual(risk_route.route(54.5), ('modelpilot-for-sonnet', 'unflagged'))
        self.assertEqual(risk_route.route(0), ('modelpilot-for-sonnet', 'unflagged'))
        self.assertEqual(risk_route.route(None), ('modelpilot-for-opus', 'unrated'))

    def test_the_arm_is_the_two_quality_floor_arms_behind_the_predictors_rater(self):
        arm = bench.ARMS[ARM]
        self.assertEqual((arm['kind'], arm['model'], arm['rater'], arm['threshold']),
                         ('risk_route', 'claude-opus-5-5', rp.MODEL, 55))
        self.assertEqual(arm['routes'], {'flagged': 'modelpilot-for-opus', 'unflagged': 'modelpilot-for-sonnet'})
        floors = {r: bench.ARMS[r]['policy_overrides']['quality_floor']['baseline'] for r in arm['routes'].values()}
        self.assertEqual(floors, {'modelpilot-for-opus': 'claude-opus-5-5/medium',
                                  'modelpilot-for-sonnet': 'claude-sonnet-5-5/medium'})
        for route in arm['routes'].values():
            self.assertEqual(bench.appended_prompt(bench.ARMS[route]), bench.appended_prompt(arm), route)
            self.assertLessEqual(set(bench.ARMS[route]['served_models']), set(arm['served_models']), route)
        self.assertIn(rp.MODEL, arm['served_models'])
        self.assertIsNone(bench.arm_adapter(ARM, 1, 1))  # no session of its own: each trial runs a route's

    def test_it_rates_with_the_predictor_that_passed_and_only_that(self):
        self.assertEqual(hashlib.sha256(Path(rp.__file__).read_bytes()).hexdigest(), risk_route.PREDICTOR_SHA256)
        self.assertFalse(risk_route.predictor_changed())
        with mock.patch.object(risk_route, 'PREDICTOR_SHA256', '0' * 64):
            with self.assertRaisesRegex(ValueError, 'not the pre-registered module'):
                risk_route.RoutedTrial(self.task, ARM, self.root/'t'/'0', self.rates, self.fail)

    def test_only_new_tasks_never_a_rated_screened_or_final_one(self):
        specs = bench_tasks.load()
        final = bench.final_ids(specs)
        refused = risk_route.refused(specs, final)
        self.assertLessEqual({s['id'] for s, _ in rp.tasks()}, set(refused))  # the 66 rated tasks
        stage2 = {s['id'] for s in specs if s.get('batch') == 'sqlglot-stage-2'}
        self.assertEqual(len(stage2), 7)  # screened October 13, before the prompt was written
        self.assertLessEqual(stage2 | set(rp.EXCLUDED) | final, set(refused))
        self.assertEqual({refused[t] for t in final}, {'final split (locked)'})
        self.assertEqual(risk_route.refused([self.task], set()), {})
        self.assertEqual(risk_route.refused([self.task], {self.task['id']}), {self.task['id']: 'final split (locked)'})
        with self.assertRaisesRegex(ValueError, 'new tasks only'):
            risk_route.RoutedTrial(self.toy.task, ARM, self.root/'t'/'0', self.rates, self.fail)  # no batch: dev

    def test_a_rating_is_the_predictors_request_sent_once_each_and_routed_on_the_median(self):
        transport = scripted(rated(70), rated(40), rated(60))
        rating = risk_route.rate(self.task, self.root/'high', self.rates, transport, self.toy.path)
        source, _ = rp.context(self.task, self.toy.path)
        self.assertEqual(transport.sent, [rp.request(self.task, source)] * rp.SAMPLES)
        self.assertEqual((rating['answers'], rating['risk'], rating['routed_to'], rating['reason']),
                         ([70, 40, 60], 60, 'modelpilot-for-opus', 'flagged'))
        self.assertTrue(rating['cost_complete'])
        self.assertAlmostEqual(rating['cost_usd'], 3 * REQUEST_USD)
        for name in ('prompt.json', 'rating.json', 'responses/toy-task#1.json'):
            self.assertTrue((self.root/'high'/name).exists(), name)
        low = risk_route.rate(self.task, self.root/'low', self.rates,
                              scripted(rated(50), reply('no json'), rated(54)), self.toy.path)
        self.assertEqual((low['answers'], low['risk'], low['routed_to']), ([50, 54], 52, 'modelpilot-for-sonnet'))
        transport = scripted(TimeoutError('slow'), reply('no json'), reply('nothing'))
        none = risk_route.rate(self.task, self.root/'none', self.rates, transport, self.toy.path)
        self.assertEqual(len(transport.sent), 3)  # the timeout is recorded, never retried
        self.assertEqual((none['risk'], none['routed_to'], none['reason']), (None, 'modelpilot-for-opus', 'unrated'))
        self.assertEqual((none['cost_complete'], none['cost_usd']), (False, None))  # the timeout may have been billed
        with self.assertRaises(FileExistsError):  # never over earlier evidence
            risk_route.rate(self.task, self.root/'high', self.rates, scripted(), self.toy.path)

    def test_the_ratings_dollars_join_the_trials_and_unknown_stays_unknown(self):
        record = {'accounting': {'cost_usd': .3, 'known_cost_usd': .3, 'cost_if_rejected_free_usd': .3},
                  'cache': {'cold_equivalent_cost_usd': .4}}
        out = risk_route.with_rating_cost(record, {'cost_usd': .01, 'known_usd': .01})
        accounting = out['accounting']
        for key, value in (('cost_usd', .31), ('known_cost_usd', .31), ('cost_if_rejected_free_usd', .31),
                           ('session_cost_usd', .3), ('session_known_cost_usd', .3), ('rating_cost_usd', .01)):
            self.assertAlmostEqual(accounting[key], value, msg=key)
        self.assertAlmostEqual(bench_report.cold_cost(out), .41)
        self.assertAlmostEqual(out['cache']['session_cold_equivalent_cost_usd'], .4)
        self.assertEqual(record['accounting']['cost_usd'], .3)  # the record it was given is unchanged
        unknown = risk_route.with_rating_cost(record, {'cost_usd': None, 'known_usd': .005})
        self.assertEqual((unknown['accounting']['cost_usd'], unknown['accounting']['cost_complete']), (None, False))
        self.assertAlmostEqual(unknown['accounting']['known_cost_usd'], .305)  # a lower bound
        self.assertIsNone(bench_report.cold_cost(unknown))

    def test_a_routed_trial_rates_first_then_runs_the_route_under_its_own_arm(self):
        trial_dir = self.root/'run'/'toy-task'/ARM/'0'
        made = []

        def make(target):
            made.append(FakeRouted(self.task, target, trial_dir))
            return made[-1]
        trial = risk_route.RoutedTrial(self.task, ARM, trial_dir, self.rates, make, repo=self.toy.path,
                                       transport=scripted(rated(30), rated(35), rated(20)))
        self.assertEqual((trial.started, trial.finished, trial.record, trial.known_cost()), (False, False, None, 0.0))
        self.assertFalse(trial.step())
        self.assertEqual([m.arm for m in made], ['modelpilot-for-sonnet'])
        record = trial.record
        self.assertEqual((record['arm'], record['model']), (ARM, 'claude-sonnet-5-5'))
        self.assertEqual((record['risk_route']['risk'], record['risk_route']['routed_to']), (30, 'modelpilot-for-sonnet'))
        self.assertNotIn('results', record['risk_route'])  # those stay in the rating's own files
        self.assertAlmostEqual(record['accounting']['cost_usd'], .3 + 3 * REQUEST_USD)
        self.assertAlmostEqual(trial.known_cost(), .3 + 3 * REQUEST_USD)  # the rating counted once
        trial.add_cost()
        self.assertAlmostEqual(trial.record['accounting']['cost_usd'], .3 + 3 * REQUEST_USD)
        self.assertEqual(json.loads((trial_dir/'trial.json').read_text())['accounting'], record['accounting'])
        rating = json.loads((trial_dir.parent/'0-risk-rating'/'rating.json').read_text())
        self.assertEqual(set(rating['results']), {'toy-task#1', 'toy-task#2', 'toy-task#3'})
        self.assertEqual(sorted(p.parent.name for p in (self.root/'run').glob('*/*/*/trial.json')), ['0'])

    def test_the_bench_refuses_old_tasks_and_sequences_before_making_its_directory(self):
        out = self.root/'refused'
        common = ('/fake/claude', 'k', 'http://127.0.0.1:1', self.rates)
        with self.assertRaisesRegex(ValueError, 'new tasks only'):
            bench.run_bench([self.toy.task], [ARM], 1, 0, out, *common, client_version='9.9 (fake)')
        with self.assertRaisesRegex(ValueError, 'sequences'):
            bench.run_bench([{'id': 's', 'tasks': [self.task]}], [ARM], 1, 0, out, *common,
                            client_version='9.9 (fake)', shape='sequence')
        self.assertFalse(out.exists())

    def test_the_bench_rates_each_trial_runs_its_route_and_reports_it_under_the_arm(self):
        out = self.root/'run'
        made = []

        def fake_trial(*args, **options):
            made.append(FakeRouted(*args, **options))
            return made[-1]
        with mock.patch.object(bench, 'Trial', fake_trial), mock.patch.object(bench, 'arm_adapter', lambda *a, **k: None), \
                mock.patch.object(bench_tasks, 'REPOS', self.root), \
                mock.patch.object(risk_route.miss_checklist, 'default_transport',
                                  lambda: scripted(rated(80), rated(60), rated(90))):
            summary = bench.run_bench([self.task], [ARM], 1, 0, out, '/fake/claude', 'k', 'http://127.0.0.1:1',
                                      self.rates, client_version='9.9 (fake)', python=sys.executable)
        self.assertEqual([m.arm for m in made], ['modelpilot-for-opus'])
        self.assertEqual((summary['trials'], summary['complete'], summary['arms'][0]['arm']), (1, True, ARM))
        self.assertAlmostEqual(summary['known_spend_usd'], .3 + 3 * REQUEST_USD)
        saved = json.loads((out/'toy-task'/ARM/'0'/'trial.json').read_text())
        self.assertEqual((saved['arm'], saved['risk_route']['risk'], saved['risk_route']['routed_to']),
                         (ARM, 80, 'modelpilot-for-opus'))
        manifest = json.loads((out/'manifest.json').read_text())['risk_route'][ARM]
        self.assertEqual((manifest['threshold'], manifest['routes'], manifest['rater']['predictor_sha256']),
                         (55, bench.ARMS[ARM]['routes'], risk_route.PREDICTOR_SHA256))
        self.assertEqual(set(manifest['routed']), {'modelpilot-for-opus', 'modelpilot-for-sonnet'})

    def test_the_plan_names_the_rating_its_cost_and_both_routes(self):
        text = '\n'.join(bench.risk_route_plan(ARM, .19, 1.0))
        for words in ('3 claude-haiku-5-5 requests', '$0.1900', 'median risk of 55 or more, or no usable answer',
                      'below an Opus user\'s floor', 'New tasks only', 'ModelPilot arm modelpilot-for-opus:',
                      'ModelPilot arm modelpilot-for-sonnet:', 'ANTHROPIC_API_KEY'):
            self.assertIn(words, text)


if __name__ == '__main__':
    unittest.main()
