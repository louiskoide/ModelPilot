"""The risk-routed arm for an Opus user (October 15, user request), built offline after the turn-start risk predictor
passed its pre-registered test (docs/m6-benchmark-plan.md, "Turn-start risk predictor: result"). That pass commits to
this arm and to no claim until it passes on new tasks.

Before a trial's first session, Haiku 5.5 rates the task exactly as risk_predictor did: the same prompt, source,
parser and SAMPLES requests, each sent once, scored by the median risk. A task rated THRESHOLD or more runs as
modelpilot-for-opus (the user's own model, Opus 5.5 medium); the rest run as modelpilot-for-sonnet (low concise, under
Sonnet 5.5 medium's quality floor). That puts the unflagged tasks below an Opus user's floor on the predictor's word
alone; on the pre-registered tasks 2 of the 6 tasks only Opus solved went unflagged.

- THRESHOLD is the pre-registered run's top-third cut, frozen. A router sees one task at a time, so it can't flag a
  third of a batch.
- A task with no usable answer runs on the user's own model. The test left such a task unflagged; an unknown risk is
  no evidence that the cheaper setting is safe.
- Only the predictor that passed: refuses to rate if risk_predictor.py no longer has its pre-registered SHA-256.
- Only new tasks: refuses the final split and every batch that existed at the pre-registration (all 98 specs, among
  them the 66 rated tasks and the 7 stage-2 tasks screened before the prompt was written).
- The rating's dollars join the trial's (with_rating_cost), its own figures beside them. An unknown rating cost makes
  the trial's cost unknown.

    python3 -m modelpilot.risk_route replay runs/risk-predictor-20261015-174922    # $0: its route for each rated task
"""
import argparse
import hashlib
import json
from pathlib import Path
import statistics as st

from . import bench_tasks, miss_checklist, risk_predictor

THRESHOLD = 55  # runs/risk-predictor-20261015-174922: the top-third cut of 66 tasks (27 at or above it, ties included)
FLAGGED, UNFLAGGED = 'modelpilot-for-opus', 'modelpilot-for-sonnet'
KNOWN_BATCHES = (None, 'item-8-labels', 'sqlglot-stage-1', 'sqlglot-stage-2')  # every batch at the pre-registration
PREDICTOR_SHA256 = '35efe96e0e75ab089976ff36ad6afabe57e5728bce98982a4aadf201bef2bf80'  # pre-registered, and what ran


def predictor_changed():
    """Whether risk_predictor.py differs from the module that was pre-registered and passed."""
    return hashlib.sha256(Path(risk_predictor.__file__).read_bytes()).hexdigest() != PREDICTOR_SHA256


def route(risk):
    """(arm, reason) for a task's median risk; None (no usable answer) stays on the user's own model."""
    if risk is None:
        return FLAGGED, 'unrated'
    return (FLAGGED, 'flagged') if risk >= THRESHOLD else (UNFLAGGED, 'unflagged')


def refused(tasks, final):
    """{task id: reason} for tasks this arm may not run: the final split, and every batch known at the
    pre-registration, whose outcomes could have shaped the prompt or the threshold."""
    out = {}
    for task in tasks:
        if task['id'] in final:
            out[task['id']] = 'final split (locked)'
        elif task.get('batch') in KNOWN_BATCHES:
            out[task['id']] = f"batch {task.get('batch') or 'dev'} existed at the pre-registration"
    return out


def payload(task, repo=None):
    """The predictor's request for one task, with the record of the source it got."""
    source, record = risk_predictor.context(task, repo)
    return risk_predictor.request(task, source), record


def max_cost(task, rates, repo=None):
    """The most a task's rating can cost: SAMPLES requests at their maximum."""
    return risk_predictor.SAMPLES * miss_checklist.max_cost(payload(task, repo)[0], rates)


def rate(task, out, rates, transport, repo=None):
    """SAMPLES requests for one task into out/ (a new directory), each sent once; the median of the usable risks."""
    out = Path(out)
    out.mkdir(mode=0o700, parents=True)  # a new directory: never overwrite evidence
    body, record = payload(task, repo)
    (out/'prompt.json').write_text(json.dumps(body, indent=2) + '\n')
    limit = risk_predictor.SAMPLES * miss_checklist.max_cost(body, rates) + 1e-9  # every sample, never more

    def parse(key, response):
        answer = risk_predictor.parse_risk(miss_checklist.reply_text(response))
        return None if answer is None else dict(answer, status='rated')
    items = [(f"{task['id']}#{i}", body) for i in range(1, risk_predictor.SAMPLES + 1)]
    results, totals = miss_checklist.send_all(items, out, rates, limit, transport, parse)
    risks = [r['risk'] for r in results.values() if r.get('status') == 'rated']
    arm, reason = route(st.median(risks) if risks else None)
    rating = {'model': risk_predictor.MODEL, 'effort': risk_predictor.EFFORT, 'samples': risk_predictor.SAMPLES,
              'threshold': THRESHOLD, 'spec_sha256': bench_tasks.spec_hash(task), 'source': record,
              'answers': risks, 'risk': st.median(risks) if risks else None, 'routed_to': arm, 'reason': reason,
              'results': results, **totals,
              'cost_usd': totals['known_usd'] if totals['cost_complete'] else None}
    (out/'rating.json').write_text(json.dumps(rating, indent=2) + '\n')
    return rating


def with_rating_cost(record, rating):
    """The trial's record with the rating's dollars added to every dollar figure the report reads, the sessions' own
    figures kept beside them. An unknown rating cost makes the trial's cost unknown."""
    add = lambda value: None if value is None or rating['cost_usd'] is None else value + rating['cost_usd']
    accounting, cache = dict(record.get('accounting') or {}), dict(record.get('cache') or {})
    accounting.update(session_cost_usd=accounting.get('cost_usd'), cost_usd=add(accounting.get('cost_usd')),
                      session_known_cost_usd=accounting.get('known_cost_usd', 0.0),
                      known_cost_usd=accounting.get('known_cost_usd', 0.0) + rating['known_usd'],
                      cost_if_rejected_free_usd=add(accounting.get('cost_if_rejected_free_usd')),
                      rating_cost_usd=rating['cost_usd'])
    if rating['cost_usd'] is None:
        accounting['cost_complete'] = False
    for key in ('cold_equivalent_cost_usd', 'cold_equivalent_if_rejected_free_usd'):
        if key in cache:
            cache.update({'session_' + key: cache[key], key: add(cache[key])})
    return dict(record, accounting=accounting, cache=cache)


class RoutedTrial:
    """One trial of the risk-routed arm: the rating, then the routed arm's own trial (make_trial(arm)) in trial_dir,
    recorded under this arm. The rating's files go beside it, in <trial_dir>-risk-rating."""
    def __init__(self, task, arm_id, trial_dir, rates, make_trial, *, final=(), transport=None, repo=None):
        problem = refused([task], set(final)).get(task['id'])
        if predictor_changed():
            raise ValueError(f'{arm_id}: risk_predictor.py is not the pre-registered module; not rating')
        if problem:
            raise ValueError(f"{arm_id} runs new tasks only: {task['id']} ({problem})")
        self.task, self.arm_id, self.rates, self.make_trial, self.repo = task, arm_id, rates, make_trial, repo
        self.dir = Path(trial_dir).absolute()
        self.rating_dir = self.dir.with_name(self.dir.name + '-risk-rating')
        self.transport = transport or miss_checklist.default_transport()
        self.inner = self.rating = None
        self.added = False

    def __getattr__(self, name):  # started, finished, auth, router_unavailable, account_error, rate_limited, ...
        inner = self.__dict__.get('inner')
        if inner is None:
            if name in ('started', 'finished', 'router_unavailable', 'account_error', 'rate_limited'):
                return False
            raise AttributeError(name)
        return getattr(inner, name)

    @property
    def record(self):
        return self.inner.record if self.inner else None

    def known_cost(self):
        rating = self.rating['known_usd'] if self.rating and not self.added else 0.0
        return (self.inner.known_cost() if self.inner else 0.0) + rating

    def step(self):
        if self.inner is None:
            self.rating = rate(self.task, self.rating_dir, self.rates, self.transport, self.repo)
            self.inner = self.make_trial(self.rating['routed_to'])
            self.inner.record.update(arm=self.arm_id,
                                     risk_route={k: v for k, v in self.rating.items() if k != 'results'})
        more = self.inner.step()
        if not more:
            self.add_cost()
        return more

    def next_gap(self):
        return self.inner.next_gap()

    def finish(self, stopped=None):
        record = self.inner.finish(stopped=stopped)
        self.add_cost()
        return record

    def close(self):
        if self.inner:
            self.inner.close()

    def add_cost(self):
        """Once the routed trial is graded: its record, and trial.json, with the rating's dollars in."""
        if self.added or not self.inner.finished:
            return
        self.inner.record = with_rating_cost(self.inner.record, self.rating)
        self.added = True
        (self.inner.dir/'trial.json').write_text(json.dumps(self.inner.record, indent=2) + '\n')


def replay(run, out=None):
    """$0: the arm's route for each task of a recorded rating run (risk_predictor's plan.json), against that run's
    pre-registered flags (score.json)."""
    run = Path(run)
    plan, score = json.loads((run/'plan.json').read_text()), json.loads((run/'score.json').read_text())
    answers = {t: [] for t in plan['tasks']}
    for key, row in plan['results'].items():
        if row.get('status') == 'rated':
            answers[key.rsplit('#', 1)[0]].append(row['risk'])
    rows = {t: dict(zip(('arm', 'reason'), route(st.median(a) if a else None)), risk=st.median(a) if a else None,
                    flagged=t in score['flagged'], M=score['per_task'][t]['M'], Y_O=score['per_task'][t]['Y_O'])
            for t, a in answers.items()}
    report = {'run': run.name, 'threshold': THRESHOLD, 'tasks': len(rows),
              'to': {arm: sum(r['arm'] == arm for r in rows.values()) for arm in (FLAGGED, UNFLAGGED)},
              'agrees_with_flags': sum((r['arm'] == FLAGGED) == r['flagged'] for r in rows.values()),
              'Y_O_unflagged': sorted(t for t, r in rows.items() if r['Y_O'] and r['arm'] == UNFLAGGED),
              'M_unflagged': sorted(t for t, r in rows.items() if r['M'] and r['arm'] == UNFLAGGED),
              'refused_by_the_arm': len(refused([bench_tasks.load(t)[0] for t in rows], set())),
              'per_task': rows}
    if out:
        with Path(out).open('x') as f:  # never overwrite evidence
            json.dump(report, f, indent=1)
            f.write('\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='step', required=True)
    r = sub.add_parser('replay')
    r.add_argument('run', type=Path)
    r.add_argument('--out', type=Path)
    args = parser.parse_args()
    report = replay(args.run, args.out)
    print(json.dumps({k: v for k, v in report.items() if k != 'per_task'}, indent=1))


if __name__ == '__main__':
    main()
