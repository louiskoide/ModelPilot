import multiprocessing
import os
from pathlib import Path
import tempfile
import time
import unittest
import json
from modelpilot.governor import BudgetRefused, Governor, demo, execute_fallback, governed_transport, reconcile_log
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

    def test_unenforced_admission_always_reserves_and_journals_would_refuse(self):
        result = self.gov.admit('big', 5, enforce=False)
        self.assertEqual((result['admitted'], result['reason'], result['reserved'], result['enforced']),
                         (False, 'insufficient_budget', True, False))
        self.assertEqual(self.gov.policy()['reserved_usd'], 5)
        self.gov.settle('big', .2)
        self.assertAlmostEqual(self.gov.policy()['spent_usd'], .2)
        with self.assertRaises(ValueError): self.gov.admit('big', .1, enforce=False)
        stale = self.gov.admit('stale', .1, self.task, self.rev+1, enforce=False)
        self.assertEqual((stale['reason'], stale['reserved']), ('stale_task', True))
        enforced = self.gov.admit('refused', 5)
        self.assertEqual((enforced['reserved'], enforced['enforced']), (False, True))
        self.assertTrue(all(e['payload']['applied'] is False for e in self.gov.journal('admit')))

    def test_reconcile_log_settles_orphans_and_records_untracked_rows(self):
        self.gov.admit('seen', .1, ttl=30, enforce=False)
        self.gov.admit('lost', .1, ttl=30, enforce=False)
        self.gov.admit('done', .1, enforce=False)
        self.gov.settle('done', .03)
        self.now[0] += 31
        self.assertEqual(self.gov.policy()['mode'], 'halt')
        rows = [{'kind': 'models', 'cost_usd': None},
                {'governor_request_id': 'seen', 'governor_status': 'settle_failed', 'cost_usd': .05},
                {'governor_request_id': 'done', 'governor_status': 'settled', 'cost_usd': .03},
                {'governor_request_id': 'untracked-1', 'governor_status': 'untracked', 'cost_usd': .01},
                {'governor_request_id': 'untracked-2', 'governor_status': 'untracked', 'cost_usd': None}]
        log = Path(self.tmp.name)/'log.jsonl'
        log.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        result = reconcile_log(self.gov, log)
        self.assertEqual(result, {'rows': 4, 'settled': 1, 'untracked_recorded': 2, 'still_unknown': 2,
                                  'orphans_without_rows': 1, 'policy': result['policy']})
        policy = self.gov.policy()
        self.assertAlmostEqual(policy['spent_usd'], .09)
        self.assertFalse(policy['cost_complete'])  # 'lost' has no row; untracked-2 is unknown
        again = reconcile_log(self.gov, log)
        self.assertEqual((again['settled'], again['untracked_recorded']), (0, 0))

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
                         lambda: reply(usage=dict(USAGE, cache_creation_input_tokens=5)),
                         lambda: reply(usage=dict(USAGE, service_tier='priority'))):
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


class FallbackTests(unittest.TestCase):
    """A would_escalate review runs one stronger-model request through enforced admission, then re-verifies it."""
    TEXT = 'The verification token is ALPHA.'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.gov = Governor(Path(self.tmp.name)/'state.db', 's', .05)
        self.task = self.gov.state.create('t', 'find token')['id']
        self.rev = self.gov.state.claim(self.task, 1, 'coordinator')['revision']
        start = self.TEXT.index('ALPHA')
        self.good = {'start': start, 'end': start+5, 'answer': 'ALPHA', 'source_sha256': sha(self.TEXT)}
        self.evidence = {'revision': self.rev, 'text': self.TEXT, 'current_source_sha256': sha(self.TEXT)}
        self.calls = []

    def tearDown(self):
        self.gov.close()
        self.tmp.cleanup()

    def escalated(self, fallback_estimate=.01, operation='read'):
        decision = self.gov.review(self.task, operation, dict(self.good, answer='BETA'), self.evidence, self.rev, fallback_estimate)
        self.assertEqual(decision['action'], 'would_escalate')
        return decision

    def run_fallback(self, decision, candidate=None, fail=False, during=None, allow=True):
        def transport(request, config):
            self.calls.append(request)
            if during:
                during()
            if fail:
                raise OSError('connection reset')
            return reply(model='test-model', content=[{'type': 'text', 'text': json.dumps(candidate or self.good)}])
        parse = lambda response: json.loads(response['content'][0]['text'])
        return execute_fallback(self.gov, decision, self.evidence, lambda d: {'model': 'test-model', 'max_tokens': 64},
                                parse, transport, RATES, allow_calls=allow)

    def test_verified_fallback_is_accepted_and_settled(self):
        result = self.run_fallback(self.escalated())
        self.assertEqual((result['action'], result['fallback']['executed']), ('would_accept', True))
        self.assertEqual(len(self.calls), 1)
        self.assertAlmostEqual(self.gov.policy()['spent_usd'], .0045)
        entry = self.gov.journal('fallback')[-1]['payload']
        self.assertEqual((entry['executed'], entry['applied'], entry['final_action']), (True, False, 'would_accept'))

    def test_unverified_fallback_defers_and_never_retries(self):
        result = self.run_fallback(self.escalated(), candidate=dict(self.good, answer='GAMMA'))
        self.assertEqual((result['action'], result['reason']), ('would_defer', 'fallback_not_verified'))
        self.assertEqual(len(self.calls), 1)

    def test_refused_admission_sends_nothing(self):
        decision = self.escalated()
        self.gov.admit('other', .045)  # leaves less than the fallback estimate
        result = self.run_fallback(decision)
        self.assertEqual((result['action'], result['reason']), ('would_defer', 'fallback_refused:insufficient_budget'))
        self.assertEqual(self.calls, [])

    def test_transport_error_settles_unknown_and_halts(self):
        with self.assertRaises(OSError): self.run_fallback(self.escalated(), fail=True)
        policy = self.gov.policy()
        self.assertEqual((policy['mode'], policy['cost_complete']), ('halt', False))
        self.assertTrue(self.gov.journal('fallback')[-1]['payload']['executed'])

    def test_correction_before_admission_refuses_stale_work(self):
        decision = self.escalated()
        self.gov.state.correct(self.task, self.rev, 'find other token')
        result = self.run_fallback(decision)
        self.assertEqual(result['reason'], 'fallback_refused:stale_task')
        self.assertEqual(self.calls, [])

    def test_correction_during_the_call_rejects_the_late_result(self):
        result = self.run_fallback(self.escalated(), during=lambda: self.gov.state.correct(self.task, self.rev, 'changed'))
        self.assertEqual(result['action'], 'reject_stale')
        self.assertAlmostEqual(self.gov.policy()['spent_usd'], .0045)  # the call still cost money

    def test_nothing_is_called_unless_allowed_affordable_and_verifiable(self):
        decision = self.escalated()
        self.assertEqual(self.run_fallback(decision, allow=False), decision)
        unaffordable = self.gov.review(self.task, 'read', dict(self.good, answer='BETA'), self.evidence, self.rev, 1)
        self.assertEqual(self.run_fallback(unaffordable)['action'], 'would_defer')
        edit = self.gov.review(self.task, 'edit', {}, {}, self.rev, .01)
        result = self.run_fallback(edit)
        self.assertEqual((result['action'], result['reason']), ('would_defer', 'fallback_unverifiable_operation'))
        self.assertEqual(self.calls, [])


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
