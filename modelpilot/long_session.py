"""Long sessions (plan item 6): several tasks of one repository as the turns of one Claude Code session.

The cache-aware design is about what happens between turns: idle gaps that let the cache expire, compaction that
replaces the conversation, and the cache lifetime the client asks for. Single-prompt tasks have none of these. Here a
trial is a sequence of tuning tasks from one repository (bench/sequences.json), each in its own checkout under the
trial's workspace (task1/, task2/, ...), given as consecutive user turns of one session (--session-id, then --resume),
with --gap seconds of idle time between task turns. Other trials run during a gap (bench.interleave). Compaction
(--compact) adds a '/compact' turn after each task but the last: 'warm' right away, before the gap, while the
conversation's cache entry is still warm; 'cold' after the gap, just before the next task. --autocompact passes the
client's own auto-compaction window (100k tokens or more) to every session.

Each subtask is graded at the end, as bench grades a single task (hidden grader and edge suite), from its own
checkout; its diff is also taken right after its turn, and a later change to it is flagged (changed_after_turn). The
trial passes when every subtask does. turns() splits a trial's proxy rows by session: what each turn cost, how much of
the conversation its first request read back, and what compaction cost. report() pairs arms per subtask
(python3 -m modelpilot.long_session report runs/bench-...).

Tuning tasks only: the final split is reserved for the frozen evaluation. Without --live nothing is sent.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from . import bench, bench_report, bench_tasks

ROOT = Path(__file__).resolve().parents[1]
SEQUENCES = ROOT/'bench'/'sequences.json'
SEQUENCE_RULE = ("Every tuning repository's tasks in base-commit order, as sequences of 3 to 5 tasks; a repository "
                 'with more than 5 is split as evenly as possible (earlier tasks first). A repository with fewer than '
                 '3 tuning tasks has no sequence. Every other tuning task is in exactly one sequence, so each one can '
                 'be set against its single-prompt results.')
PREAMBLE = ('You are working in Python repositories under the current directory. The task below is in the directory '
            '{dir}/, a repository of its own: work only there. Run the relevant tests with python3 from that directory '
            'before you finish.\n\nTask: ')
COMPACT_PROMPT = '/compact'
COMPACT_MODES = ('none', 'warm', 'cold')
MIN_TASKS, MAX_TASKS = 3, 5
# Delegation briefs read one checkout's diff against its base commit; a sequence has one per subtask.
UNSUPPORTED_ARMS = ('modelpilot-delegate',)


def base_time(task):
    """Commit time of a task's base, the order a developer would meet its repository's tasks in."""
    repo = bench.task_repo(task)
    return int(subprocess.run(['git', '-C', str(repo), 'show', '-s', '--format=%ct', task['base']],
                              capture_output=True, text=True, check=True).stdout.strip())


def make_sequences(tasks, splits=None, when=base_time):
    """{sequence id: [task ids]} by SEQUENCE_RULE, from task specs (tuning tasks only)."""
    splits = splits or json.loads(bench_tasks.SPLITS.read_text())
    # Plan item 8's labelling batch (October 6) came later; the sequences keep the tuning tasks they were built from.
    tuning = [t for t in tasks if t['id'] in splits['tuning'] and not t.get('batch')]
    by_repo = {}
    for task in tuning:
        by_repo.setdefault(task['repo'], []).append(task)
    out = {}
    for repo, group in sorted(by_repo.items()):
        if len(group) < MIN_TASKS:
            continue
        group = sorted(group, key=lambda t: (when(t), t['id']))
        parts = -(-len(group) // MAX_TASKS)
        size, extra = divmod(len(group), parts)
        start = 0
        for n in range(parts):
            end = start + size + (n < extra)
            name = repo if parts == 1 else f'{repo}-{chr(ord("a") + n)}'
            out[name] = [t['id'] for t in group[start:end]]
            start = end
    return out


def load(ids=None, path=SEQUENCES):
    """[{'id', 'tasks': [task spec, ...]}] for the named sequences (all when None), checked against the split."""
    data = json.loads(Path(path).read_text())
    known = data['sequences']
    ids = list(known) if ids is None else ids
    missing = [i for i in ids if i not in known]
    if missing:
        raise ValueError(f'Unknown sequences: {missing}. Known: {", ".join(known)}')
    out = []
    for name in ids:
        tasks = [bench_tasks.load(t)[0] for t in known[name]]
        final = [t['id'] for t in tasks if bench_tasks.split_of(t['id']) != 'tuning']
        if final or len({t['repo'] for t in tasks}) != 1 or len(tasks) < MIN_TASKS:
            raise ValueError(f'{name}: a sequence is {MIN_TASKS}+ tuning tasks of one repository ({final or "mixed"})')
        out.append({'id': name, 'tasks': tasks})
    return out


def plan(count, gap, compact='none'):
    """The session's turns in order: [{'kind': 'task'|'compact', 'index': subtask, 'gap_before': seconds}]."""
    if compact not in COMPACT_MODES:
        raise ValueError(f'compact must be one of {COMPACT_MODES}')
    steps = [{'kind': 'task', 'index': 0, 'gap_before': 0}]
    for i in range(1, count):
        if compact == 'warm':  # compact while the conversation's entry is warm, then idle
            steps.append({'kind': 'compact', 'index': i - 1, 'gap_before': 0})
        if compact == 'cold':  # idle first, then compact on return
            steps += [{'kind': 'compact', 'index': i - 1, 'gap_before': gap}, {'kind': 'task', 'index': i, 'gap_before': 0}]
        else:
            steps.append({'kind': 'task', 'index': i, 'gap_before': gap})
    return steps


def directory(index):
    return f'task{index + 1}'


class SequenceTrial(bench.Trial):
    """One sequence × arm × trial: its task turns (and compaction turns) in one session, then every subtask graded."""
    def __init__(self, sequence, arm_id, trial_dir, cli, key, upstream, price_table, *, gap=0, compact='none',
                 autocompact=None, expected=None, **options):
        if arm_id in UNSUPPORTED_ARMS:
            raise ValueError(f'{arm_id}: its delegation briefs read one checkout; not supported in sequences')
        if autocompact is not None and not 100_000 <= int(autocompact) <= 1_000_000:
            raise ValueError('autocompact is a window of 100k to 1M tokens (the client refuses others)')
        tasks = sequence['tasks']
        super().__init__(tasks[0], arm_id, trial_dir, cli, key, upstream, price_table, gap=gap,
                         expected_hidden_passed=None, **options)
        self.sequence, self.tasks = sequence, tasks
        self.steps = plan(len(tasks), gap, compact)
        self.prompts = [bench_prompt(tasks[s['index']], s['index']) if s['kind'] == 'task' else COMPACT_PROMPT
                        for s in self.steps]
        self.expected_by_task = expected or {}
        self.turn_diffs = {}
        if autocompact is not None:
            self.client_extra = ['--autocompact', str(int(autocompact))]
        self.record.update(task=sequence['id'], shape='sequence', gap_requested_seconds=gap,
                           sequence={'id': sequence['id'], 'tasks': [t['id'] for t in tasks],
                                     'dirs': [directory(i) for i in range(len(tasks))]},
                           spec_sha256={t['id']: bench_tasks.spec_hash(t) for t in tasks},
                           compact=compact, autocompact=autocompact, steps=self.steps)

    def next_gap(self):
        return self.steps[len(self.sessions)]['gap_before'] if len(self.sessions) < len(self.steps) else 0

    def make_workspace(self):
        root = self.dir/'workspace'
        root.mkdir()
        self.repos, self.base_commits = [], []
        for i, task in enumerate(self.tasks):
            repo = bench.task_repo(task)
            work = bench_tasks.workspace(task, repo, root/directory(i))
            self.repos.append(repo)
            self.base_commits.append(subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=work, capture_output=True,
                                                    text=True, check=True).stdout.strip())
        self.repo, self.work, self.base_commit = self.repos[0], root, None
        return []  # PYTHONPATH is set per task turn

    def run_session(self, index):
        step = self.steps[index]
        if step['kind'] == 'task':  # the grader's import paths for this turn's checkout; a compaction keeps the last
            paths = bench_tasks.test_env_paths(self.tasks[step['index']], self.work/directory(step['index']))
            self.env = dict(self.env)
            if paths:
                self.env['PYTHONPATH'] = os.pathsep.join(paths)
            else:
                self.env.pop('PYTHONPATH', None)
        before = self.sessions[-1]['ended_unix'] if self.sessions else None
        super().run_session(index)
        session = self.sessions[-1]
        session.update(prompt=step['kind'], subtask=step['index'], gap_requested_seconds=step['gap_before'],
                       gap_seconds=round(session['started_unix'] - before, 3) if before is not None else None)
        if step['kind'] == 'task':
            work = self.work/directory(step['index'])
            self.turn_diffs[step['index']] = hashlib.sha256(
                bench.checkout_diff(work, self.base_commits[step['index']])).hexdigest()

    def finish(self, stopped=None):
        """Grade every subtask from its own checkout as the session left it."""
        if stopped:
            self.record['stopped'] = stopped
        self.close()
        self.save('grading')
        reached = {s['subtask'] for s in self.sessions if s.get('prompt') == 'task'}
        subtasks = []
        for i, task in enumerate(self.tasks):
            entry = {'task': task['id'], 'dir': directory(i)}
            if i not in reached:
                entry.update(passed=False, reason='not_reached')
                subtasks.append(entry)
                continue
            out = self.dir/'subtasks'/directory(i)
            out.mkdir(parents=True)
            expected = (self.expected_by_task.get(task['id']) or {}).get('hidden_passed')
            entry.update(bench.grade_checkout(task, self.work/directory(i), self.base_commits[i], self.repos[i],
                                              self.python, out, expected))
            entry['changed_after_turn'] = (hashlib.sha256((out/'agent.diff').read_bytes()).hexdigest()
                                           != self.turn_diffs.get(i))
            for name in ('grade', 'edge-env'):
                shutil.rmtree(out/name, ignore_errors=True)
            subtasks.append(entry)
        bench.reap(self.dir)
        for name in ('workspace', 'home', 'tmp'):
            shutil.rmtree(self.dir/name, ignore_errors=True)
        self.record.update(subtasks=subtasks, subtasks_passed=sum(bool(s['passed']) for s in subtasks),
                           passed=all(s['passed'] for s in subtasks),
                           pass_rule='every subtask: hidden grader and edge suite where it has one')
        self.record['complete'] = stopped is None and not self.account_error and not self.rate_limited
        if self.account_error:
            self.record['excluded_reason'] = 'anthropic_account_error'
        elif self.rate_limited:
            self.record['excluded_reason'] = 'rate_limited'
        self.finished = True
        self.save('graded')
        return self.record


def bench_prompt(task, index):
    return PREAMBLE.format(dir=directory(index)) + task['instruction']


def first_request(rows):
    """The first answered main-loop request of a session's rows, or None."""
    return next((r for r in sorted(rows, key=lambda r: r.get('started_unix', 0))
                 if r.get('kind') == 'messages' and r.get('tool_count') and r.get('http_status') == 200), None)


def prompt_tokens(row):
    usage = (row or {}).get('usage') or {}
    return sum(usage.get(k) or 0 for k in ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'))


def turns(record, rows, rates):
    """Per session of a trial: its kind, gap, requests, measured and cold-equivalent dollars (as the trial is
    priced; cold-equivalent reprices reads of entries other trials warmed, bench_report.cache_attribution), how much
    of the conversation the first request read back (return_read_fraction: its cache read over the previous
    request's prompt) and what compaction requests cost."""
    rows = bench_report.priced_rows(record, rows, rates)
    out, previous, cold_before = [], None, 0.0
    for session in record.get('sessions') or []:
        start, end = session['rows']
        mine = [r for r in rows[start:end] if r.get('kind') in ('messages', 'side_call')]
        costs = [r.get('cost_usd') for r in mine if r.get('status') != 'refused']
        cold_through = bench_report.cache_attribution(rows[:end], rates)['cold_equivalent_cost_usd']
        first = first_request(mine)
        usage = (first or {}).get('usage') or {}
        compaction = [r for r in mine if r.get('compaction')]
        out.append({'prompt': session.get('prompt'), 'subtask': session.get('subtask'),
                    'gap_seconds': session.get('gap_seconds'), 'stop': session.get('stop'),
                    'requests': session.get('requests'),
                    'cost_usd': sum(costs) if costs and None not in costs else None,
                    'cold_equivalent_usd': (None if cold_through is None or cold_before is None
                                            else cold_through - cold_before),
                    'first_read_tokens': usage.get('cache_read_input_tokens'),
                    'first_write_tokens': usage.get('cache_creation_input_tokens'),
                    'first_write_ttl': (first or {}).get('cache_ttl'),
                    'previous_prompt_tokens': prompt_tokens(previous) if previous else None,
                    'return_read_fraction': (round(usage.get('cache_read_input_tokens', 0) / prompt_tokens(previous), 3)
                                             if previous and first and prompt_tokens(previous) else None),
                    'compaction_requests': len(compaction),
                    'compaction_cost_usd': (sum(r['cost_usd'] for r in compaction)
                                            if compaction and all(r.get('cost_usd') is not None for r in compaction)
                                            else None if compaction else 0.0),
                    'compaction_output_tokens': sum(((r.get('usage') or {}).get('output_tokens') or 0)
                                                    for r in compaction)})
        cold_before = cold_through
        answered = [r for r in mine if r.get('kind') == 'messages' and r.get('tool_count') and r.get('http_status') == 200]
        previous = max(answered, key=lambda r: r.get('started_unix', 0)) if answered else previous
    return out


def subtask_costs(record, turn_list):
    """{subtask id: cold-equivalent dollars of its task turn plus the compaction turn that prepared it}, None when
    any of it is unknown. A compaction turn after task i is charged to task i + 1, whose context it shrinks."""
    out = {}
    for turn in turn_list:
        index = turn['subtask'] + (1 if turn['prompt'] == 'compact' else 0)
        task = record['sequence']['tasks'][index]
        value = turn['cold_equivalent_usd']
        out[task] = None if value is None or out.get(task, 0.0) is None else out.get(task, 0.0) + value
    return out


def report(trials, rng_seed=0, resamples=10000):
    """Per arm, over [(record, turns)] of complete sequence trials: subtasks passed, mean cold-equivalent dollars per
    sequence and per subtask, how returns found the conversation, and compaction; per pair of arms, paired
    differences per subtask (each subtask's turn, with its preparing compaction, is one pair), bootstrap 95%."""
    import random
    by_arm = {}
    for record, turn_list in trials:
        if record.get('complete') and record.get('shape') == 'sequence':
            by_arm.setdefault(record['arm'], []).append((record, turn_list))
    arms = {}
    per_subtask = {}
    for arm, items in sorted(by_arm.items()):
        costs, passes, returns, compaction = {}, {}, [], []
        sequence_costs = [r.get('cache', {}).get('cold_equivalent_cost_usd') for r, _ in items]
        for record, turn_list in items:
            for task, value in subtask_costs(record, turn_list).items():
                costs.setdefault(task, []).append(value)
            for sub in record.get('subtasks') or []:
                passes.setdefault(sub['task'], []).append(bool(sub['passed']))
            returns += [t['return_read_fraction'] for t in turn_list if t['prompt'] == 'task' and t['subtask']]
            compaction += [t for t in turn_list if t['prompt'] == 'compact']
        per_subtask[arm] = ({t: sum(v) / len(v) for t, v in costs.items() if None not in v},
                            {t: sum(v) / len(v) for t, v in passes.items()})
        known = [c for c in sequence_costs if c is not None]
        arms[arm] = {'sequences': len(items), 'subtasks': sum(len(v) for v in passes.values()),
                     'subtasks_passed': sum(sum(v) for v in passes.values()),
                     'mean_sequence_usd': sum(known) / len(known) if known else None,
                     'unknown_cost_sequences': len(sequence_costs) - len(known),
                     'returns': len(returns),
                     'returns_reading_conversation': sum(f is not None and f >= .5 for f in returns),
                     'compaction_turns': len(compaction),
                     'compaction_usd': sum(t['compaction_cost_usd'] or 0 for t in compaction),
                     'compaction_output_tokens': sum(t['compaction_output_tokens'] for t in compaction)}
    pairs = []
    names = sorted(per_subtask)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            shared = sorted(set(per_subtask[a][0]) & set(per_subtask[b][0]))
            rng = random.Random(rng_seed)
            def mean_diff(sample, index=0):
                return {'value': sum(per_subtask[a][index][t] - per_subtask[b][index][t] for t in sample) / len(sample)}
            entry = {'arms': [a, b], 'subtasks': len(shared)}
            if shared:
                entry['cost_per_subtask_usd'] = bench_report.difference(
                    mean_diff(shared)['value'], bench_report.bootstrap(shared, mean_diff, rng, resamples)['value'],
                    len(shared))
                passed = sorted(set(per_subtask[a][1]) & set(per_subtask[b][1]))
                entry['pass_rate'] = bench_report.difference(
                    mean_diff(passed, 1)['value'],
                    bench_report.bootstrap(passed, lambda s: mean_diff(s, 1), rng, resamples)['value'], len(passed))
            pairs.append(entry)
    return {'arms': arms, 'pairs': pairs,
            'pairing': 'per subtask: its task turn plus the compaction turn that prepared it, cold-equivalent dollars'}


def load_run(run_dir, rates):
    """[(record, turns)] for every trial of a sequence run, with the run's latest records and proxy rows."""
    out = []
    for path in sorted(Path(run_dir).glob('*/*/*/trial.json')):
        record = json.loads(path.read_text())
        log = path.parent/'observations.jsonl'
        rows = [json.loads(l) for l in log.read_text().splitlines() if l.strip()] if log.exists() else []
        out.append((record, turns(record, rows, rates)))
    return out


def write_sequences(path=SEQUENCES):
    tasks = bench_tasks.load()
    data = {'rule': SEQUENCE_RULE, 'sequences': make_sequences(tasks)}
    Path(path).write_text(json.dumps(data, indent=1) + '\n')
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('sequences', help='Rebuild bench/sequences.json from the tuning tasks ($0)')
    show = sub.add_parser('report', help="A sequence run's per-turn breakdown and paired summary ($0)")
    show.add_argument('runs', type=Path, nargs='+')
    show.add_argument('--out', type=Path)
    args = parser.parse_args()
    if args.command == 'sequences':
        print(json.dumps(write_sequences(), indent=1))
        return
    rates = bench.rates()
    trials = [t for run in args.runs for t in load_run(run, rates)]
    result = dict(report(trials), runs=[str(r) for r in args.runs],
                  trials=[{'trial': f"{r['task']}/{r['arm']}/{r.get('trial', 0)}", 'passed': r.get('passed'),
                           'subtasks_passed': r.get('subtasks_passed'), 'turns': t} for r, t in trials])
    text = json.dumps(result, indent=1)
    if args.out:
        args.out.write_text(text + '\n')
    print(json.dumps({k: result[k] for k in ('arms', 'pairs')}, indent=1))


if __name__ == '__main__':
    main()
