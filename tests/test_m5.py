import copy
from concurrent.futures import ThreadPoolExecutor
import unittest
from modelpilot.m5 import IDLE_BOUNDARY_SECONDS, Budget, RebaseQueue, compaction_check, demo, idle_boundary, learn

SONNET_5_5 = {'output': 10, 'write': 2.5, 'read': .1}  # from October 7, 5m writes
SONNET_5_5_1H = {'output': 10, 'write': 4, 'read': .1}  # a 1h session (a subscription's default)


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
        for trigger in ({'idle_seconds': IDLE_BOUNDARY_SECONDS}, {'cache_expired': True},
                        {'switch_justified': True}, {'explicit_boundary': True}):
            self.assertEqual(q.plan(1, **trigger)['action'], 'would_rebase')

    def test_idle_waits_past_a_5m_entry_seen_warm(self):
        # The live governed session read its whole 5m entry 305.6 s after last use; 330 s was always a rewrite.
        q = RebaseQueue()
        q.queue('compact', True, 1)
        self.assertEqual(q.plan(1, idle_seconds=305.6)['action'], 'defer')
        self.assertEqual(q.plan(1, idle_seconds=330)['action'], 'would_rebase')

    def test_the_idle_boundary_follows_the_cache_lifetime(self):
        self.assertEqual((idle_boundary('5m'), idle_boundary('1h')), (330, 3630))
        self.assertEqual(idle_boundary(None), 3630)  # unknown: wait for the longest



class CompactionCheckTests(unittest.TestCase):
    def test_compaction_pays_only_where_the_reads_it_saves_beat_its_summary(self):
        # Summary: 3,925 tokens output and written, plus one more read of the 8,000-token base, $0.0499. The compaction
        # request is one more request, so a 7-request turn saves reads on 6: at $0.10 it must remove 83,104.
        large = compaction_check(150_000, 8_000, SONNET_5_5, 7)
        self.assertEqual((large['verdict'], large['removed_tokens']), ('pays', 138_075))
        self.assertAlmostEqual(large['cost_usd'], .0498625, places=5)
        self.assertAlmostEqual(large['saving_usd'], .082845)
        self.assertEqual(compaction_check(95_100, 8_000, SONNET_5_5, 7)['verdict'], 'pays')
        self.assertEqual(compaction_check(95_000, 8_000, SONNET_5_5, 7)['verdict'], 'does_not_pay')
        self.assertEqual(compaction_check(500_000, 8_000, SONNET_5_5, 1)['verdict'], 'does_not_pay')  # no later request
        # The live governed session's context (October 12): its compaction removed nothing worth reading.
        small = compaction_check(4_158, 2_713, SONNET_5_5, 2)
        self.assertEqual((small['verdict'], small['removed_tokens'], small['saving_usd']), ('does_not_pay', 0, 0))

    def test_a_1h_session_writes_the_summary_at_the_hours_rate(self):
        # The compaction request reads the entry the triggering prompt left warm, so only the summary's write differs:
        # $0.0558 with the base's read, from 104,842 tokens over 7 requests.
        self.assertEqual(compaction_check(104_900, 8_000, SONNET_5_5_1H, 7)['verdict'], 'pays')
        self.assertEqual(compaction_check(104_800, 8_000, SONNET_5_5_1H, 7)['verdict'], 'does_not_pay')

    def test_unknown_evidence_is_never_a_guess(self):
        for args in ((None, 8_000, SONNET_5_5, 7), (90_000, None, SONNET_5_5, 7), (90_000, 8_000, None, 7),
                     (90_000, 8_000, SONNET_5_5, None), (90_000, 8_000, {'output': 10, 'write': 2.5}, 7)):
            self.assertEqual(compaction_check(*args), {'verdict': 'unknown', 'reason': 'no_context_evidence'})


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
