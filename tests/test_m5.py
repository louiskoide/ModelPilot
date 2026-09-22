import copy
from concurrent.futures import ThreadPoolExecutor
import unittest
from modelpilot.m5 import Budget, RebaseQueue, demo, learn


class RebaseTests(unittest.TestCase):
    def test_coalesce_latest_and_preserve_pending(self):
        q = RebaseQueue()
        q.queue('model', 'first', 1)
        q.queue('model', 'second', 1)
        q.queue('effort', 'high', 1)
        p = q.plan(1, explicit_boundary=True)
        self.assertEqual(p['rebuilds_planned'], 1)
        self.assertEqual(len(p['changes']), 2)
        self.assertEqual(p['changes'][0]['value'], 'second')
        self.assertFalse(p['applied'])
        self.assertEqual(len(q.pending), 2)
        p['changes'][0]['value'] = 'mutated'
        self.assertEqual(q.pending['model']['value'], 'second')

    def test_stale_and_inflight_are_not_applied(self):
        q = RebaseQueue()
        q.queue('prune', ['old'], 1)
        self.assertEqual(q.plan(2, explicit_boundary=True)['changes'], [])
        self.assertEqual(q.plan(1, explicit_boundary=True, in_flight=1)['action'], 'defer')
        self.assertEqual(q.plan(1)['action'], 'defer')

    def test_each_boundary(self):
        q = RebaseQueue()
        q.queue('compact', True, 1)
        for trigger in ({'idle_seconds': 300}, {'cache_expired': True},
                        {'switch_justified': True}, {'explicit_boundary': True}):
            self.assertEqual(q.plan(1, **trigger)['action'], 'would_rebase')


class BudgetTests(unittest.TestCase):
    def test_concurrent_reservations_cannot_oversubscribe(self):
        b = Budget(1)
        with ThreadPoolExecutor(max_workers=8) as pool:
            accepted = list(pool.map(lambda i: b.reserve(str(i), .5), range(20)))
        self.assertEqual(sum(accepted), 2)
        self.assertEqual(b.policy()['mode'], 'halt')

    def test_actual_overrun_halts_and_settlement_is_idempotent(self):
        b = Budget(1)
        self.assertTrue(b.reserve('a', .1))
        b.settle('a', 1.2)
        b.settle('a', 1.2)
        self.assertEqual(b.spent, 1.2)
        self.assertFalse(b.reserve('b', 0))
        with self.assertRaises(ValueError): b.settle('a', 1.3)

    def test_unknown_cost_halts_and_releases_reservation(self):
        b = Budget(1)
        b.reserve('a', .1)
        b.settle('a', None)
        self.assertFalse(b.reserve('b', .1))
        self.assertFalse(b.policy()['cost_complete'])
        self.assertEqual(b.policy()['reserved_usd'], 0)

    def test_conserve_preserves_verification_and_unused_reservation_released(self):
        b = Budget(1)
        b.reserve('a', .9)
        self.assertTrue(b.policy()['acceptance_floor_unchanged'])
        self.assertFalse(b.policy()['optional_escalation_allowed'])
        b.settle('a', .1)
        self.assertEqual(b.policy()['mode'], 'normal')
        with self.assertRaises(ValueError): b.reserve('a', .1)

    def test_invalid_costs(self):
        for cost in (-1, float('nan'), float('inf'), True):
            with self.assertRaises(ValueError): Budget(cost)
            with self.assertRaises(ValueError): Budget(1).reserve('a', cost)


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.rows = [{'context': 'repo/read', 'task_id': f'{split}-{i}', 'split': split,
                      'score_source': 'host_verifier', 'score': .6 if i % 2 else .95,
                      'draft_pass': i % 2 == 0, 'fallback_pass': True,
                      'draft_cost': .001, 'fallback_cost': .01}
                     for split in ('train', 'validation') for i in range(20)]

    def test_selects_safe_training_candidate_then_validates(self):
        r = learn(self.rows, 'repo/read')
        self.assertEqual(r['status'], 'proposal')
        self.assertGreater(r['threshold'], .6)
        self.assertAlmostEqual(r['validation']['mean_cost'], .006)
        self.assertFalse(r['applied'])

    def test_holdout_failure_cannot_retune_on_validation(self):
        original = learn(self.rows, 'repo/read')
        for row in self.rows:
            if row['split'] == 'validation': row['draft_pass'] = False
        r = learn(self.rows, 'repo/read')
        self.assertEqual(r['threshold'], original['threshold'])
        self.assertEqual(r['status'], 'validation_failed')

    def test_context_isolation_and_insufficient_data(self):
        self.assertEqual(learn(self.rows, 'another/read')['status'], 'insufficient_evidence')
        self.assertEqual(learn(self.rows[:21], 'repo/read')['status'], 'insufficient_evidence')

    def test_leakage_and_self_confidence_rejected(self):
        for mutation in ('duplicate', 'confidence', 'missing_counterfactual'):
            rows = copy.deepcopy(self.rows)
            if mutation == 'duplicate': rows[-1]['task_id'] = rows[0]['task_id']
            elif mutation == 'confidence': rows[0]['score_source'] = 'model'
            else: del rows[0]['fallback_pass']
            with self.assertRaises((ValueError, KeyError)): learn(rows, 'repo/read')

    def test_no_quality_eligible_candidate(self):
        for row in self.rows:
            row['draft_pass'] = row['fallback_pass'] = False
        self.assertEqual(learn(self.rows, 'repo/read')['status'], 'no_eligible_threshold')

    def test_demo(self):
        self.assertEqual(demo()['status'], 'passed')


if __name__ == '__main__': unittest.main()
