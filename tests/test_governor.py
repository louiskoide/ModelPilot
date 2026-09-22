import multiprocessing
import os
from pathlib import Path
import tempfile
import time
import unittest
from modelpilot.governor import BudgetRefused, Governor, demo, governed_transport
from modelpilot.m4 import sha
from modelpilot.workers import Worker


def contend(path, index, barrier):
    gov = Governor(path, 's', 1)
    try:
        barrier.wait()
        return gov.admit(f'p{index}', .3)['admitted']
    finally:
        gov.close()


def crash_after_reserving(path):
    gov = Governor(path, 's', 1)
    gov.admit('in-flight', .2, ttl=1)
    os._exit(1)  # no settle, no close: simulated coordinator crash mid-request


class GovernorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'state.db'
        self.now = [1000.]
        self.gov = Governor(self.path, 's', 1, clock=lambda: self.now[0])
        self.task = self.gov.state.create('t', 'find token')['id']
        self.rev = self.gov.state.claim(self.task, 1, 'coordinator')['revision']

    def tearDown(self):
        self.gov.close()
        self.tmp.cleanup()

    def reopen(self, **kw):
        self.gov.close()
        self.gov = Governor(self.path, 's', 1, clock=lambda: self.now[0], **kw)

    def test_active_mode_and_limit_changes_refused(self):
        with self.assertRaises(ValueError): Governor(self.path, 's', 1, mode='active')
        with self.assertRaises(ValueError): Governor(self.path, 's', 2)

    def test_settled_spend_survives_restart(self):
        self.assertTrue(self.gov.admit('a', .5)['admitted'])
        self.gov.settle('a', .7)
        self.reopen()
        self.assertAlmostEqual(self.gov.policy()['spent_usd'], .7)
        self.assertFalse(self.gov.admit('b', .5)['admitted'])
        with self.assertRaises(ValueError): self.gov.admit('a', .1)
        with self.assertRaises(ValueError): self.gov.settle('a', .8)
        self.gov.settle('a', .7)  # idempotent repeat

    def test_expired_reservation_after_restart_halts_until_reconciled(self):
        self.gov.admit('lost', .1, ttl=30)
        self.reopen()
        self.assertEqual(self.gov.policy()['mode'], 'normal')  # still in flight: reserved, not unknown
        self.now[0] += 31
        policy = self.gov.policy()
        self.assertEqual((policy['mode'], policy['cost_complete'], policy['reserved_usd']), ('halt', False, 0))
        self.assertEqual(self.gov.admit('next', .01)['reason'], 'cost_unknown')
        self.gov.settle('lost', None)  # still unknown: stays halted
        self.assertFalse(self.gov.policy()['cost_complete'])
        policy = self.gov.settle('lost', .12)  # measured evidence reconciles it
        self.assertTrue(policy['cost_complete'])
        self.assertAlmostEqual(policy['spent_usd'], .12)
        self.assertTrue(self.gov.journal('settle')[-1]['payload']['reconciled'])

    def test_unknown_settlement_halts(self):
        self.gov.admit('a', .1)
        self.gov.settle('a', None)
        self.assertEqual(self.gov.admit('b', 0)['reason'], 'cost_unknown')

    def test_stale_or_terminal_task_work_refused(self):
        self.assertEqual(self.gov.admit('a', .1, self.task, self.rev+1)['reason'], 'stale_task')
        with self.assertRaises(ValueError): self.gov.admit('b', .1, self.task)
        self.gov.state.correct(self.task, self.rev, 'find other token')
        self.assertEqual(self.gov.admit('c', .1, self.task, self.rev)['reason'], 'stale_task')
        self.assertEqual(self.gov.policy()['reserved_usd'], 0)

    def evidence(self, rev):
        text = 'The verification token is ALPHA.'
        start = text.index('ALPHA')
        good = {'start': start, 'end': start+5, 'answer': 'ALPHA', 'source_sha256': sha(text)}
        return good, {'revision': rev, 'text': text, 'current_source_sha256': sha(text)}

    def test_review_uses_ledger_revision_and_budget(self):
        good, evidence = self.evidence(self.rev)
        self.assertEqual(self.gov.review(self.task, 'read', good, evidence, self.rev, .01)['action'], 'would_accept')
        bad = dict(good, answer='BETA')
        escalate = self.gov.review(self.task, 'read', bad, evidence, self.rev, .01)
        self.assertEqual((escalate['action'], escalate['escalation_affordable']), ('would_escalate', True))
        self.assertEqual(self.gov.review(self.task, 'read', bad, evidence, self.rev, 2)['action'], 'would_defer')
        self.gov.state.correct(self.task, self.rev, 'changed')
        self.assertEqual(self.gov.review(self.task, 'read', good, evidence, self.rev, .01)['action'], 'reject_stale')

    def test_budget_pressure_never_accepts_unverified(self):
        good, evidence = self.evidence(self.rev)
        self.gov.admit('a', .1)
        self.gov.settle('a', None)
        decision = self.gov.review(self.task, 'read', dict(good, answer='BETA'), evidence, self.rev, 0)
        self.assertEqual((decision['action'], decision['budget_mode']), ('would_defer', 'halt'))
        self.assertEqual(self.gov.review(self.task, 'edit', {}, {}, self.rev, 0)['action'], 'would_defer')
        self.assertEqual(self.gov.review(self.task, 'destructive', {}, {}, self.rev, 0)['action'], 'blocked')

    def test_completed_task_rejects_late_draft(self):
        good, evidence = self.evidence(self.rev)
        self.gov.state.complete(self.task, self.rev, 'coordinator', 'done')
        self.assertEqual(self.gov.review(self.task, 'read', good, evidence, self.rev, .01)['reason'], 'task_terminal')

    def test_observe_journals_recommendation_without_applying(self):
        for _ in range(3):
            result = self.gov.observe(self.task, self.rev, 'coordinator', {'error': 'same failure'})
        self.assertEqual(result['recommendation'], 'increase_effort')
        self.assertEqual(self.gov.state.get(self.task)['level'], 0)
        self.assertFalse(self.gov.journal('stuck')[-1]['payload']['applied'])
        with self.assertRaises(ValueError): self.gov.observe(self.task, self.rev, 'intruder', {'progress': True})

    def test_rebase_queue_survives_restart_and_ack_keeps_newer_values(self):
        self.gov.queue_change('model', 'first', 1)
        self.gov.queue_change('model', 'second', 1)
        self.gov.queue_change('effort', 'high', 1)
        self.gov.queue_change('prune', ['old'], 0)
        self.reopen()
        self.assertEqual(self.gov.plan_rebase(1, in_flight=1, explicit_boundary=True)['action'], 'defer')
        plan = self.gov.plan_rebase(1, explicit_boundary=True)
        self.assertEqual([c['value'] for c in plan['changes']], ['high', 'second'])
        self.assertEqual(plan['stale_changes'], ['prune'])
        self.assertEqual(self.gov.plan_rebase(1, cache_expired=True)['plan_id'], plan['plan_id'])
        self.gov.queue_change('model', 'third', 1)
        self.reopen()
        pending = self.gov.acknowledge_rebase(plan['plan_id'])['pending']
        self.assertEqual({c['kind']: c['value'] for c in pending}, {'model': 'third', 'prune': ['old']})
        self.assertEqual(self.gov.acknowledge_rebase(plan['plan_id'])['pending'], pending)  # idempotent
        with self.assertRaises(ValueError): self.gov.acknowledge_rebase('missing')

    def test_acknowledging_one_plan_supersedes_others(self):
        self.gov.queue_change('effort', 'high', 1)
        first = self.gov.plan_rebase(1, explicit_boundary=True)
        self.gov.queue_change('model', 'target', 1)
        second = self.gov.plan_rebase(1, explicit_boundary=True)
        self.assertNotEqual(first['plan_id'], second['plan_id'])
        self.gov.acknowledge_rebase(second['plan_id'])
        with self.assertRaises(ValueError): self.gov.acknowledge_rebase(first['plan_id'])
        self.assertEqual(self.gov.pending_changes(), [])

    def test_journal_never_records_applied_actions(self):
        self.gov.admit('a', .1, self.task, self.rev)
        self.gov.settle('a', .1)
        self.gov.queue_change('compact', True, self.rev)
        self.gov.plan_rebase(self.rev, idle_seconds=300)
        entries = self.gov.journal()
        self.assertEqual([e['kind'] for e in entries], ['admit', 'settle', 'queue_change', 'plan_rebase'])
        self.assertTrue(all(e['payload'].get('applied') is False for e in entries))

    def test_invalid_inputs(self):
        for bad in (-1, float('nan'), True):
            with self.assertRaises(ValueError): self.gov.admit('x', bad)
        with self.assertRaises(ValueError): self.gov.admit('x', .1, ttl=0)
        with self.assertRaises(ValueError): self.gov.settle('missing', .1)
        with self.assertRaises(ValueError): self.gov.queue_change('temperature', 1, 1)
        with self.assertRaises(ValueError): self.gov.plan_rebase(1, idle_threshold=0)

    def test_demo(self):
        self.assertEqual(demo(Path(self.tmp.name)/'demo.db')['status'], 'passed')


RATES = {'test-model': {'input': 3, 'output': 15, 'read': .3, 'write_5m': 3.75, 'write_1h': 6}}
USAGE = {'input_tokens': 1000, 'output_tokens': 100, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0}


def reply(**changes):
    response = {'model': 'test-model', 'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'needle found'}],
                'usage': dict(USAGE)}
    response.update(changes)
    return response, 'req-1'


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.gov = Governor(self.root/'state.db', 's', .01)
        self.calls = []

    def tearDown(self):
        self.gov.close()
        self.tmp.cleanup()

    def wrap(self, response=reply, estimate=.001):
        def inner(request, config):
            self.calls.append(request)
            return response()
        return governed_transport(self.gov, inner, RATES, estimate)

    def test_measured_cost_is_settled(self):
        self.wrap()({'model': 'test-model'}, {})
        self.assertAlmostEqual(self.gov.policy()['spent_usd'], .0045)
        self.assertEqual(self.gov.policy()['reserved_usd'], 0)

    def test_refusal_sends_nothing(self):
        with self.assertRaises(BudgetRefused): self.wrap(estimate=.02)({'model': 'test-model'}, {})
        self.assertEqual(self.calls, [])

    def test_failures_and_ambiguous_usage_are_unknown(self):
        def boom():
            raise OSError('connection reset')
        with self.assertRaises(OSError): self.wrap(boom)({'model': 'test-model'}, {})
        self.assertEqual(self.gov.policy()['mode'], 'halt')
        for response in (lambda: reply(model='alias'),
                         lambda: reply(usage=dict(USAGE, cache_creation_input_tokens=5))):
            gov = Governor(self.root/f'{len(self.calls)}.db', 's', 1)
            try:
                governed_transport(gov, lambda r, c: response(), RATES, .001)({'model': 'test-model'}, {})
                self.assertFalse(gov.policy()['cost_complete'])
            finally:
                gov.close()
            self.calls.append(None)

    def test_worker_dispatch_is_admitted_and_budget_exhaustion_releases_task(self):
        (self.root/'file.txt').write_text('needle = value\n')
        worker = Worker('file_search', root=self.root, db=self.root/'state.db', model='test-model',
                        rates=RATES, transport=self.wrap(estimate=.006))
        first = self.gov.state.create('search', 'Find needle')['id']
        self.assertEqual(worker.dispatch(first, 1, ['file.txt'], query='needle')['cost_usd'], .0045)
        second = self.gov.state.create('search', 'Find needle again')['id']
        with self.assertRaises(BudgetRefused): worker.dispatch(second, 1, ['file.txt'], query='needle')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.gov.state.get(second)['status'], 'pending')  # released, not completed
        self.assertEqual([e['payload']['admitted'] for e in self.gov.journal('admit')], [True, False])


class ProcessTests(unittest.TestCase):
    """Separate OS processes share one database, as a proxy and worker dispatcher would."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name)/'state.db')
        Governor(self.path, 's', 1).close()
        self.ctx = multiprocessing.get_context('spawn')

    def tearDown(self):
        self.tmp.cleanup()

    def test_processes_cannot_oversubscribe(self):
        with self.ctx.Manager() as manager:
            barrier = manager.Barrier(8)
            with self.ctx.Pool(8) as pool:
                admitted = pool.starmap(contend, [(self.path, i, barrier) for i in range(8)])
        self.assertEqual(sum(admitted), 3)
        gov = Governor(self.path, 's', 1)
        try:
            self.assertAlmostEqual(gov.policy()['reserved_usd'], .9)
            self.assertEqual(len(gov.journal('admit')), 8)
        finally:
            gov.close()

    def test_crashed_process_reservation_becomes_unknown(self):
        process = self.ctx.Process(target=crash_after_reserving, args=(self.path,))
        process.start()
        process.join(30)
        self.assertEqual(process.exitcode, 1)
        gov = Governor(self.path, 's', 1, clock=lambda: time.time()+5)
        try:
            self.assertEqual(gov.recover()['orphaned'], 1)
            self.assertEqual(gov.admit('after-crash', .01)['reason'], 'cost_unknown')
            gov.settle('in-flight', .2)
            self.assertTrue(gov.admit('after-reconcile', .01)['admitted'])
        finally:
            gov.close()


if __name__ == '__main__': unittest.main()
