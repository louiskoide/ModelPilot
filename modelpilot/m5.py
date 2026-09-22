"""Offline M5 controls. No provider calls or live policy activation."""
import argparse
import copy
import json
from pathlib import Path
from threading import RLock
from .m4 import nonnegative


class RebaseQueue:
    """Session-local queue; plans never drain it or mutate a conversation."""
    def __init__(self):
        self.pending = {}

    def queue(self, kind, value, revision):
        if kind not in ('model', 'effort', 'prune', 'compact'):
            raise ValueError('Unknown cache-breaking change')
        if type(revision) is not int or revision < 0:
            raise ValueError('Invalid task revision')
        self.pending[kind] = {'kind': kind, 'value': copy.deepcopy(value), 'revision': revision}

    def plan(self, revision, *, idle_seconds=0, idle_threshold=300,
             cache_expired=False, switch_justified=False, explicit_boundary=False,
             in_flight=0):
        nonnegative(idle_seconds, 'idle_seconds')
        nonnegative(idle_threshold, 'idle_threshold')
        if idle_threshold == 0 or type(in_flight) is not int or in_flight < 0:
            raise ValueError('Invalid rebase boundary')
        eligible = [copy.deepcopy(v) for v in self.pending.values() if v['revision'] == revision]
        stale = [k for k, v in self.pending.items() if v['revision'] != revision]
        trigger = ('explicit_boundary' if explicit_boundary else 'cache_expired' if cache_expired
                   else 'justified_switch' if switch_justified else 'idle' if idle_seconds >= idle_threshold else None)
        ready = bool(eligible and trigger and not in_flight)
        return {'applied': False, 'action': 'would_rebase' if ready else 'defer',
                'trigger': trigger, 'changes': eligible, 'stale_changes': stale,
                'rebuilds_planned': int(ready), 'in_flight': in_flight}


class Budget:
    """Thread-safe session accounting with reservations, not a billing ceiling.

    Callers must gate dispatch on reserve(). Unknown actual cost halts new work.
    Reservations are estimates; actual cost may exceed them.
    """
    def __init__(self, limit):
        nonnegative(limit, 'limit')
        self.limit = limit
        self.spent = 0
        self.pending = {}
        self.settled = {}
        self.unknown = False
        self.lock = RLock()

    def reserve(self, request_id, estimate):
        nonnegative(estimate, 'estimate')
        if not isinstance(request_id, str) or not request_id:
            raise ValueError('Request ID required')
        with self.lock:
            if request_id in self.pending or request_id in self.settled:
                raise ValueError('Duplicate request ID')
            available = self.limit - self.spent - sum(self.pending.values())
            if self.unknown or available <= 0 or estimate > available:
                return False
            self.pending[request_id] = estimate
            return True

    def settle(self, request_id, actual):
        if actual is not None:
            nonnegative(actual, 'actual')
        with self.lock:
            if request_id in self.settled:
                if self.settled[request_id] != actual:
                    raise ValueError('Conflicting settlement')
                return
            if request_id not in self.pending:
                raise ValueError('No reservation')
            del self.pending[request_id]
            self.settled[request_id] = actual
            if actual is None:
                self.unknown = True
            else:
                self.spent += actual

    def policy(self):
        with self.lock:
            remaining = max(0, self.limit - self.spent - sum(self.pending.values()))
            fraction = remaining / self.limit if self.limit else 0
            mode = 'halt' if self.unknown or remaining == 0 else 'conserve' if fraction <= .2 else 'normal'
            return {'mode': mode, 'spent_usd': self.spent, 'reserved_usd': sum(self.pending.values()),
                    'available_usd': remaining, 'cost_complete': not self.unknown,
                    'optional_escalation_allowed': mode == 'normal',
                    'prefer_output_digest': mode != 'normal',
                    'acceptance_floor_unchanged': True, 'applied': False}


def learn(rows, context, thresholds=(.5, .75, .9), min_samples=20, max_failure_rate=.05):
    """Full-information offline replay; requires both candidate and fallback outcomes.

    Only training data selects a threshold. A separate validation partition gates
    the proposal. Scores must come from a host verifier, not model self-confidence.
    This is not a contextual bandit or an unbiased evaluation of logged one-arm data.
    """
    if type(min_samples) is not int or min_samples < 1:
        raise ValueError('Invalid minimum sample count')
    nonnegative(max_failure_rate, 'max_failure_rate')
    if max_failure_rate > 1 or not thresholds:
        raise ValueError('Invalid learning configuration')
    for t in thresholds:
        nonnegative(t, 'threshold')
        if t > 1:
            raise ValueError('Threshold above one')
    selected = [r for r in rows if r['context'] == context]
    seen = set()
    for r in selected:
        if not isinstance(r['task_id'], str) or not r['task_id'] or r['task_id'] in seen:
            raise ValueError('Duplicate/missing task identity; possible partition leakage')
        seen.add(r['task_id'])
        if r['split'] not in ('train', 'validation') or r['score_source'] != 'host_verifier':
            raise ValueError('Invalid evidence provenance')
        for k in ('score', 'draft_cost', 'fallback_cost'):
            nonnegative(r[k], k)
        if r['score'] > 1 or any(type(r[k]) is not bool for k in ('draft_pass', 'fallback_pass')):
            raise ValueError('Invalid outcome')
    train = [r for r in selected if r['split'] == 'train']
    validation = [r for r in selected if r['split'] == 'validation']
    if min(len(train), len(validation)) < min_samples:
        return {'status': 'insufficient_evidence', 'applied': False}

    def evaluate(partition, threshold):
        failures = 0
        total = 0
        for r in partition:
            accept = r['score'] >= threshold
            failures += not (r['draft_pass'] if accept else r['fallback_pass'])
            total += r['draft_cost'] + (0 if accept else r['fallback_cost'])
        return {'failure_rate': failures / len(partition), 'mean_cost': total / len(partition), 'samples': len(partition)}

    candidates = [(t, evaluate(train, t)) for t in thresholds]
    safe = [(t, m) for t, m in candidates if m['failure_rate'] <= max_failure_rate]
    if not safe:
        return {'status': 'no_eligible_threshold', 'applied': False}
    threshold, training = min(safe, key=lambda pair: (pair[1]['mean_cost'], -pair[0]))
    held_out = evaluate(validation, threshold)
    return {'status': 'proposal' if held_out['failure_rate'] <= max_failure_rate else 'validation_failed',
            'context': context, 'threshold': threshold, 'training': training, 'validation': held_out,
            'applied': False, 'statistical_guarantee': False}


def demo():
    queue = RebaseQueue()
    queue.queue('model', 'target', 1)
    queue.queue('effort', 'high', 1)
    rebase = queue.plan(1, explicit_boundary=True)
    budget = Budget(1)
    assert budget.reserve('a', .85)
    conserving = budget.policy()
    budget.settle('a', .9)
    assert not budget.reserve('b', .2)
    rows = [{'context': 'fixture/read', 'task_id': f'{split}-{i}', 'split': split,
             'score_source': 'host_verifier', 'score': .6 if i % 2 else .95,
             'draft_pass': i % 2 == 0, 'fallback_pass': True, 'draft_cost': .001,
             'fallback_cost': .01} for split in ('train', 'validation') for i in range(20)]
    proposal = learn(rows, 'fixture/read')
    assert rebase['rebuilds_planned'] == 1 and conserving['mode'] == 'conserve'
    assert proposal['status'] == 'proposal' and proposal['validation']['failure_rate'] == 0
    return {'status': 'passed', 'synthetic': True, 'api_calls': 0, 'actual_api_cost_usd': 0,
            'rebase': rebase, 'budget': conserving, 'learning': proposal, 'applied': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    result = demo()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x') as f:
        json.dump(result, f, indent=2)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
