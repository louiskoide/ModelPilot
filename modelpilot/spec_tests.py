"""Plan item 5, first check: tests a stronger model writes from a task's specification alone, run by the host
against saved fixes. Do they catch the misses the edge suites found, and how often do they flag a correct fix?

Two steps, each into one new directory under runs/:

    python3 -m modelpilot.spec_tests write --tasks a,b,c --out runs/spec-tests-<ts> [--live] [--limit-usd 1]
    python3 -m modelpilot.spec_tests score runs/spec-tests-<ts> [--jobs 4]

write: one request per task to Opus 5.5 at medium effort, given only the task's instruction and its repository's
name: never the hidden tests, the reference fix, the edge suite or any agent's diff. Without --live it writes the
prompts and the plan (requests, token limits, the most each can cost) and sends nothing. With --live (needs
ANTHROPIC_API_KEY in the environment) each request is sent once, with no retry and no fallback model, and none is sent
once the next one's maximum cost would take the total past --limit-usd (a stopping threshold, not a billing cap). A
refusal, an error or a reply without a Python block is recorded as it is. Replies are saved whole; the Python block
becomes suites/<task>/test_spec.py.

score ($0, no model request): runs each suite on the task's reference fix and base, then on every saved fix of the
task in the bench runs on record (complete, eligible, strictly graded trials), and sets what it flags beside the strict
verdict. Two readings: every generated test, which is what a host without the reference fix would see, and only the
tests the reference fix passes, the most such a detector could do. Applying a diff and running tests executes
agent-written and model-written code, as grading does: not an OS sandbox, trusted task repositories only.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from . import bench_tasks, cache_probe, regrade

ROOT = Path(__file__).resolve().parents[1]
MODEL, EFFORT, MAX_TOKENS, TIMEOUT = 'claude-opus-5-5', 'medium', 12000, 600
SUITE = 'test_spec.py'
SYSTEM = ('You write tests for a change to a Python library, from its specification alone. Another developer is '
          'implementing the change; your tests will run against their work to find where it falls short of the '
          'specification. Write one file of plain unittest tests (standard library and the library itself only). '
          'Every assertion must follow from the specification or from the library\'s established public behaviour; '
          'don\'t rely on private names, internals or exact wording the specification doesn\'t give. Cover what the '
          'specification asks for, and especially what it implies without spelling out. The tests run from the '
          'repository root with the library importable. Reply with the file in one ```python block.')


def prompt(task):
    return (f"Repository: {task['repo']} ({task['url']}).\n\nSpecification of the change:\n\n{task['instruction']}\n\n"
            'Write the test file.')


def request(task):
    """The request for one task. Opus 5.5 always thinks (adaptively); effort is set, not left at its default."""
    return {'model': MODEL, 'max_tokens': MAX_TOKENS, 'output_config': {'effort': EFFORT}, 'system': SYSTEM,
            'messages': [{'role': 'user', 'content': prompt(task)}]}


def max_cost(payload, rates):
    """The most one request can cost: its bytes as tokens (at most one token a byte) and every output token."""
    rate = rates[payload['model']]
    return (len(json.dumps(payload).encode()) * rate['input'] + payload['max_tokens'] * rate['output']) / 1e6


def code_block(text):
    found = re.search(r'```python\n(.*?)```', text, re.S)
    return found.group(1) if found else None


def write(tasks, out, rates, live=False, limit_usd=1.0, transport=None):
    out = Path(out)
    out.mkdir(parents=True)  # a new directory: never overwrite evidence
    from .bench import code_revision
    plan = {'model': MODEL, 'effort': EFFORT, 'max_tokens': MAX_TOKENS, 'system': SYSTEM, 'live': live,
            'limit_usd': limit_usd, 'code': code_revision(), 'tasks': {}}
    for task in tasks:
        payload = request(task)
        (out/'prompts').mkdir(exist_ok=True)
        (out/'prompts'/f"{task['id']}.json").write_text(json.dumps(payload, indent=2) + '\n')
        plan['tasks'][task['id']] = {'spec_sha256': bench_tasks.spec_hash(task),
                                     'max_cost_usd': round(max_cost(payload, rates), 4)}
    plan['max_cost_usd'] = round(sum(t['max_cost_usd'] for t in plan['tasks'].values()), 4)
    results, spent = {}, 0.0
    if live:
        transport = transport or (lambda p: cache_probe.send(p, {}, timeout=TIMEOUT))
        (out/'responses').mkdir()
        for task in tasks:
            payload = request(task)
            if spent + max_cost(payload, rates) > limit_usd:
                results[task['id']] = {'status': 'not_sent', 'reason': 'limit'}
                continue
            try:
                response, request_id = transport(payload)
            except Exception as error:  # recorded, never retried: a retry changes what was billed
                results[task['id']] = dict(cache_probe.error_details(error), status='error',
                                           error_type=type(error).__name__)
                continue
            (out/'responses'/f"{task['id']}.json").write_text(json.dumps(response, indent=2) + '\n')
            cost = cache_probe.priced_usage(payload, response.get('model'), response.get('usage'), rates)
            spent += cost if cost is not None else max_cost(payload, rates)  # unknown cost counts at its most
            text = ''.join(b.get('text', '') for b in response.get('content') or [] if b.get('type') == 'text')
            code = code_block(text)
            row = {'status': 'written' if code else 'no_code_block', 'request_id': request_id,
                   'stop_reason': response.get('stop_reason'), 'usage': response.get('usage'), 'cost_usd': cost}
            if code:
                suite = out/'suites'/task['id']
                suite.mkdir(parents=True)
                (suite/SUITE).write_text(code)
                row.update(bytes=len(code.encode()), sha256=hashlib.sha256(code.encode()).hexdigest(),
                           tests=len(re.findall(r'^\s*def test', code, re.M)))
            results[task['id']] = row
        plan['spent_usd'] = round(spent, 6)
        plan['cost_complete'] = all(r.get('cost_usd') is not None for r in results.values() if r['status'] != 'not_sent')
    plan['results'] = results
    (out/'plan.json').write_text(json.dumps(plan, indent=2) + '\n')
    return plan


def saved_fixes(task_ids, rates, with_records=False):
    """Every complete, eligible, strictly graded trial of these tasks on record: (trial directory, strict pass), and
    with_records its record as bench_report loads it (cost repriced from its proxy log)."""
    from .bench_report import ineligible_reason, load_run
    out = []
    for run in sorted((ROOT/'runs').glob('bench-2026*')):
        if not (run/'manifest.json').exists():
            continue
        for r in load_run(run, rates)[1]:
            if (r['task'] in task_ids and r.get('pass_rule') == 'hidden_and_edge' and r.get('complete', True)
                    and not ineligible_reason(r)):
                out.append((run/r['task']/r['arm']/str(r['trial']), bool(r['passed'])) + ((r,) if with_records else ()))
    return out


def run_on_fix(trial_dir, task, python, scratch, suites):
    """The suite on one saved fix: the base workspace, the agent's diff applied, the suite run."""
    work = bench_tasks.workspace(task, bench_tasks.REPOS/task['repo'], Path(scratch)/'work')
    diff = Path(trial_dir)/'agent.diff'
    if diff.exists() and diff.stat().st_size:
        applied = subprocess.run(['git', 'apply', '--binary', '--whitespace=nowarn', str(diff.resolve())], cwd=work,
                                 capture_output=True, text=True)
        if applied.returncode:
            return {'error': 'diff_did_not_apply'}
    return regrade.run_edge(task, work, python, Path(scratch)/'env', suites)


def readings(result, invalid):
    """(flagged by any test, flagged by a test the reference fix passes); None where the suite didn't run."""
    if result.get('error') or not result.get('tests_run'):
        return None, None
    failing = set(result['failing_tests'])
    return bool(failing) or result['exit_code'] != 0, bool(failing - invalid)


def rates_of(rows, flag):
    def share(rows):
        hit = [r for r in rows if r[flag] is not None]
        return {'flagged': sum(bool(r[flag]) for r in hit), 'of': len(hit)}
    return {'strict_failures': share([r for r in rows if not r['strict']]),
            'strict_passes': share([r for r in rows if r['strict']])}


def score(out, rates, python=None, jobs=4):
    out = Path(out)
    suites = out/'suites'
    python = python or bench_tasks.interpreter()
    tasks = {t['id']: t for t in bench_tasks.load() if (suites/t['id']/SUITE).exists()}
    report = {'python': python, 'suites': {}, 'tasks': {}}
    scratch = out/'scratch'
    for task_id, task in sorted(tasks.items()):
        check = regrade.check_edge(task, bench_tasks.REPOS/task['repo'], python, scratch/f'check-{task_id}', suites)
        report['suites'][task_id] = check
    fixes = [(trial, strict, tasks[trial.parts[-3]]) for trial, strict in saved_fixes(set(tasks), rates)]

    def one(item):
        index, (trial, strict, task) = item
        try:
            return run_on_fix(trial, task, python, scratch/str(index), suites)
        finally:
            shutil.rmtree(scratch/str(index), ignore_errors=True)
    with ThreadPoolExecutor(max(1, jobs)) as pool:
        results = list(pool.map(one, enumerate(fixes)))
    shutil.rmtree(scratch, ignore_errors=True)
    for (trial, strict, task), result in zip(fixes, results):
        reference = report['suites'][task['id']]['reference']
        invalid = set(reference['failing_tests']) if reference.get('tests_run') else set()
        raw, valid = readings(result, invalid)
        report['tasks'].setdefault(task['id'], []).append(
            {'run': trial.parts[-4], 'arm': trial.parts[-2], 'trial': int(trial.parts[-1]), 'strict': strict,
             'flagged': raw, 'flagged_valid_tests': valid, 'failing_tests': result.get('failing_tests'),
             'error': result.get('error')})
    report['summary'] = {task_id: {'suite_tests': report['suites'][task_id]['reference'].get('tests_run'),
                                   'reference_failing': len(report['suites'][task_id]['reference']['failing_tests']),
                                   'base_failing': len(report['suites'][task_id]['base']['failing_tests']),
                                   'every_test': rates_of(rows, 'flagged'),
                                   'tests_the_reference_passes': rates_of(rows, 'flagged_valid_tests')}
                         for task_id, rows in sorted(report['tasks'].items())}
    with (out/'score.json').open('x') as f:  # never overwrite evidence
        json.dump(report, f, indent=2)
        f.write('\n')
    return report


def main():
    from .bench import rates
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='step', required=True)
    w = sub.add_parser('write')
    w.add_argument('--tasks', required=True)
    w.add_argument('--out', type=Path, required=True)
    w.add_argument('--live', action='store_true')
    w.add_argument('--limit-usd', type=float, default=1.0)
    s = sub.add_parser('score')
    s.add_argument('out', type=Path)
    s.add_argument('--jobs', type=int, default=4)
    args = parser.parse_args()
    if args.step == 'write':
        tasks = bench_tasks.load(args.tasks)
        missing = set(args.tasks.split(',')) - {t['id'] for t in tasks}
        if missing:
            parser.error(f'No task spec for {", ".join(sorted(missing))}')
        final = [t['id'] for t in tasks if bench_tasks.split_of(t['id']) != 'tuning']
        if final:  # final-split tasks get nothing added after the lock
            parser.error(f'Tuning tasks only: {", ".join(final)}')
        if args.live and not os.environ.get('ANTHROPIC_API_KEY'):
            raise SystemExit('Set ANTHROPIC_API_KEY in the environment; no requests sent.')
        plan = write(tasks, args.out, rates(), args.live, args.limit_usd)
        print(json.dumps({k: plan[k] for k in ('live', 'max_cost_usd', 'limit_usd')} |
                         {'spent_usd': plan.get('spent_usd'), 'results': {t: r.get('status') for t, r in plan['results'].items()}},
                         indent=1))
    else:
        report = score(args.out, rates(), jobs=args.jobs)
        print(json.dumps(report['summary'], indent=1))
        print(args.out/'score.json')


if __name__ == '__main__':
    main()
