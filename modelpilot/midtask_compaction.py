"""Mid-task compaction (user request, October 10): does a compaction summary keep the work, and does it matter who
writes it?

Compaction replaces the conversation with a summary. Between unrelated tasks (the sequence shape) the next task barely
needs the conversation, so every summarizer would look alike. Here it falls inside a task. The 'split' shape gives
each task as two turns of one session: a plan turn (investigate and plan, no edits), then a fix turn (make the change,
run the tests). Arms with a 'compaction' entry (bench.ARMS) add a '/compact' turn between them, so the fix turn starts
from the summary alone. The summary has to carry what the plan turn learned, the task itself included. The same arm
without the entry is the control: the same two turns with the whole conversation.

Who writes the summary is the arm's: 'session' leaves it to the session's own setting (what the client does); a model
id has the proxy send the compaction request there (proxy.compaction_model), at the client's effort. The checkout is
graded once, after the fix turn, as a single trial is (hidden grader and edge suite), so bench_report and regrade read
these runs as they read single ones. A plan turn that edits the checkout is recorded (plan_turn_edits), not stopped.
A trial whose compaction went to the wrong model, or a control that compacted, is excluded (compaction_check).

report() breaks each trial down by turn: what the plan, the compaction and the fix turn cost, the summary's output
tokens and what the fix turn wrote back to the cache. pooled() pairs pass rates and costs per task (bench_report) over
a run and its reruns, after regrade has re-graded each.

Tuning tasks only; fixed arms only. Without --live nothing is sent.
"""
import argparse
import hashlib
import json
from pathlib import Path
from . import bench, bench_report, long_session

PLAN_PREAMBLE = ('You are working in a Python repository in the current directory. This task takes two turns. In this '
                 'one, investigate only: find what has to change and plan the change (which files and functions, the '
                 'edge cases, and the tests to run). Do not edit, create or delete any file yet; you will make the change '
                 'in the next turn. End with your plan.\n\nTask: ')
FIX_PROMPT = 'Now make the change you planned. Run the relevant tests with python3 before you finish.'
COMPACT_PROMPT = long_session.COMPACT_PROMPT


def steps(arm):
    """The session's turns in order: 'plan', then 'compact' for an arm with a compaction entry, then 'fix'."""
    return ['plan'] + (['compact'] if 'compaction' in arm else []) + ['fix']


class SplitTrial(bench.Trial):
    """One task × arm × trial in the split shape: plan turn, optional compaction turn, fix turn; then graded."""
    def __init__(self, task, arm_id, trial_dir, cli, key, upstream, price_table, **options):
        if bench.ARMS[arm_id]['kind'] != 'fixed' or options.get('adapter') is not None:
            raise ValueError(f'{arm_id}: the split shape runs fixed arms only')
        if options.pop('gap', 0):
            raise ValueError('the split shape runs its turns back to back (no gap)')
        super().__init__(task, arm_id, trial_dir, cli, key, upstream, price_table, **options)
        self.steps = steps(self.arm)
        prompts = {'plan': PLAN_PREAMBLE + task['instruction'], 'compact': COMPACT_PROMPT, 'fix': FIX_PROMPT}
        self.prompts = [prompts[s] for s in self.steps]
        self.check_compaction = True  # the control must not compact either
        self.record.update(shape='split', steps=self.steps,
                           split_prompts={'plan_preamble': PLAN_PREAMBLE, 'fix': FIX_PROMPT})

    def run_session(self, index):
        super().run_session(index)
        self.sessions[-1]['prompt'] = self.steps[index]
        if self.steps[index] == 'plan':
            diff = bench.checkout_diff(self.work, self.base_commit)
            self.record['plan_turn_edits'] = {'edited': bool(diff), 'diff_bytes': len(diff),
                                              'sha256': hashlib.sha256(diff).hexdigest() if diff else None}


def breakdown(record, rows, rates):
    """One trial by turn: cost of each turn (cold-equivalent), the summary's cost and output tokens, and what the fix
    turn's first request wrote and read (after a compaction, the summary is written fresh)."""
    by_turn = {t['prompt']: t for t in long_session.turns(record, rows, rates)}
    fix = by_turn.get('fix') or {}
    compact = by_turn.get('compact')
    return {'trial': f"{record['task']}/{record['arm']}/{record.get('trial', 0)}", 'passed': record.get('passed'),
            'complete': record.get('complete'), 'ineligible': bench_report.ineligible_reason(record),
            'plan_turn_edited': (record.get('plan_turn_edits') or {}).get('edited'),
            'turn_cost_usd': {k: v['cold_equivalent_usd'] for k, v in by_turn.items()},
            'compaction_usd': compact['compaction_cost_usd'] if compact else None,
            'compaction_output_tokens': compact['compaction_output_tokens'] if compact else None,
            'compaction_model': (record.get('compaction_check') or {}).get('served'),
            'fix_first_write_tokens': fix.get('first_write_tokens'),
            'fix_first_read_tokens': fix.get('first_read_tokens'), 'fix_requests': fix.get('requests')}


def report(items):
    """Per arm over [(record, breakdown)] of split trials: complete and eligible trials, strict passes, plan turns
    that edited, and mean turn costs, summary cost and output tokens."""
    def mean(values):
        values = [v for v in values if v is not None]
        return sum(values) / len(values) if values else None
    arms = {}
    for record, item in items:
        arms.setdefault(record['arm'], []).append(item)
    out = {}
    for arm, trials in sorted(arms.items()):
        kept = [t for t in trials if t['complete'] and not t['ineligible']]
        out[arm] = {'trials': len(trials), 'eligible': len(kept), 'passed': sum(bool(t['passed']) for t in kept),
                    'plan_turn_edited': sum(bool(t['plan_turn_edited']) for t in kept),
                    'excluded': sorted(t['ineligible'] for t in trials if t['ineligible']),
                    'mean_turn_usd': {k: mean(t['turn_cost_usd'].get(k) for t in kept) for k in ('plan', 'compact', 'fix')},
                    'mean_compaction_usd': mean(t['compaction_usd'] for t in kept),
                    'mean_compaction_output_tokens': mean(t['compaction_output_tokens'] for t in kept),
                    'mean_fix_first_write_tokens': mean(t['fix_first_write_tokens'] for t in kept)}
    return out


def load_run(run_dir, rates):
    """[(record, breakdown)] for every split trial of a run, with the run's latest records and proxy rows."""
    out = []
    for path in sorted(Path(run_dir).glob('*/*/*/trial.json')):
        record = json.loads(path.read_text())
        if record.get('shape') != 'split':
            continue
        log = path.parent/'observations.jsonl'
        rows = [json.loads(l) for l in log.read_text().splitlines() if l.strip()] if log.exists() else []
        record.setdefault('transport_check', bench_report.transport_check(rows))  # as bench_report.load_run
        out.append((record, breakdown(record, rows, rates)))
    return out


def pooled(run_dirs, rates, resamples=10000):
    """The pre-registered comparison over several split runs (a run and the reruns that topped it up): each arm's
    trials pooled across the runs, not labeled per run as bench_report.load_runs does, then paired per task with
    bench_report.summarize, strict passes after the latest re-grade. The seed is the first run's. Secondary: the same
    without trials whose plan turn edited."""
    seed, arms, records = None, [], []
    for run in run_dirs:
        manifest, run_records = bench_report.load_run(run, rates)
        if manifest.get('shape') != 'split':
            raise ValueError(f'{run}: not a split-shape run')
        seed = manifest.get('seed', 0) if seed is None else seed
        arms += [a for a in manifest['arms'] if a not in arms]
        records += run_records
    no_edits = [r for r in records if not (r.get('plan_turn_edits') or {}).get('edited')]
    return {'seed': seed, 'trials': len(records),
            'primary': bench_report.summarize(records, arms, seed=seed, resamples=resamples),
            'without_plan_turn_edits': bench_report.summarize(no_edits, arms, seed=seed, resamples=resamples)}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('runs', type=Path, nargs='+', help='runs/bench-... directories of the split shape')
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    rates = bench.rates()
    items = [i for run in args.runs for i in load_run(run, rates)]
    result = {'runs': [str(r) for r in args.runs], 'arms': report(items), 'trials': [b for _, b in items],
              'paired': pooled(args.runs, rates)}
    if args.out:
        args.out.write_text(json.dumps(result, indent=1) + '\n')
    primary = result['paired']['primary']
    print(json.dumps({'arms': result['arms'],
                      'pass_rate_differences': [{'arms': p['arms'], **p['differences']['pass_rate']}
                                                for p in primary['paired']],
                      'excluded': [(t['task'], t['arm'], t['reason']) for t in primary['excluded_ineligible_trials']]},
                     indent=1))


if __name__ == '__main__':
    main()
