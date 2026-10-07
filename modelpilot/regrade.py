"""Re-grade a finished benchmark run's saved fixes, $0: no model request is made.

Each trial saved its fix as agent.diff. For every trial this rebuilds the task's base checkout (the same
history-free workspace the agent had), applies the diff, and then:
- re-runs the hidden grader (bench_tasks.grade) against the trial's own bar. It has to reproduce the
  recorded verdict; a mismatch (flaky tests, a different environment) is reported, never hidden.
- runs the task's frozen edge-case tests, when bench/edge_tests/<task>/ holds any: extra tests the agent
  never saw, which look past pass/fail. A task's edge suite is used only if its reference fix passes all
  of it, so a suite can't check behaviour the real upstream fix never had.
- measures the fix's scope from the diff: files and lines changed, split into source and tests.

Results go to a new directory under runs/; the run itself is never modified. Applying a diff and running
tests executes agent-written code, as the original grading did: not an OS sandbox, trusted task repos only.

    python3 -m modelpilot.regrade runs/bench-20260929-000001 [--tasks a,b] [--arms x,y] [--jobs 4]
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil
import statistics
import subprocess
import time
from . import bench_tasks

ROOT = Path(__file__).resolve().parents[1]
EDGE = ROOT/'bench'/'edge_tests'
EDGE_DIR = '_edge'  # where a task's edge tests are copied inside the graded tree
EDGE_COMMAND = ['{python}', '-m', 'unittest', 'discover', '-v', '-s', EDGE_DIR, '-t', EDGE_DIR]


def edge_files(task_id, root=EDGE):
    return sorted((root/task_id).glob('test_*.py'))


def run_edge(task, tree, python, scratch, root=EDGE, keep_output=False):
    """The task's edge tests against a tree, with the grader's isolation (no credentials, own HOME/TMPDIR).
    keep_output keeps the run's whole output."""
    target = Path(tree)/EDGE_DIR
    if target.exists():
        shutil.rmtree(target)
    target.mkdir()
    for path in edge_files(task['id'], root):
        shutil.copy2(path, target/path.name)
    home, tmp = bench_tasks.isolated_dirs(scratch)
    result = bench_tasks.run_tests(EDGE_COMMAND, tree, python, task.get('pythonpath'), home=home, tmp=tmp,
                                   keep_output=keep_output)
    return {k: result[k] for k in ('exit_code', 'tests_run', 'tests_passed', 'tests_skipped', 'failing_tests',
                                   'output_tail', 'output') if k in result}


def edge_all_passed(result):
    return bool(result['exit_code'] == 0 and result['tests_run'] and result['tests_passed'] == result['tests_run'])


def check_edge(task, repo, python, scratch, root=EDGE):
    """Run a task's edge suite on its reference fix and its base. usable: the reference passes all of it."""
    out = {}
    for name in ('reference', 'base'):
        tree = bench_tasks.task_tree(task, repo, task[name], Path(scratch)/name)
        out[name] = run_edge(task, tree, python, Path(scratch)/f'{name}-env', root)
    out['files'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in edge_files(task['id'], root)}
    out['usable'] = edge_all_passed(out['reference'])
    return out


def is_test_path(path, test_dir):
    parts = Path(path).parts
    return path.startswith(test_dir.rstrip('/') + '/') or 'tests' in parts or Path(path).name.startswith('test_')


def scope(tree, task):
    """Files and lines the fix changed, source apart from tests (binary files count as files only)."""
    subprocess.run(['git', 'add', '-A', '-N'], cwd=tree, capture_output=True, check=True)
    stat = subprocess.run(['git', 'diff', '--numstat', 'HEAD'], cwd=tree, capture_output=True, text=True,
                          check=True).stdout
    out = dict.fromkeys(('source_files', 'test_files', 'source_added', 'source_deleted', 'test_added', 'test_deleted'), 0)
    for line in stat.splitlines():
        added, deleted, path = line.split('\t', 2)
        kind = 'test' if is_test_path(path, task['test_dir']) else 'source'
        out[f'{kind}_files'] += 1
        out[f'{kind}_added'] += int(added) if added.isdigit() else 0
        out[f'{kind}_deleted'] += int(deleted) if deleted.isdigit() else 0
    out['source_lines'] = out['source_added'] + out['source_deleted']
    return out


def regrade_trial(trial_dir, task, repo, python, scratch, edge=False, root=EDGE):
    trial_dir, scratch = Path(trial_dir), Path(scratch)
    record = json.loads((trial_dir/'trial.json').read_text())
    out = {'task': record['task'], 'arm': record['arm'], 'trial': record['trial'],
           'recorded_passed': record.get('passed'), 'recorded_reason': (record.get('grade') or {}).get('reason')}
    diff = trial_dir/'agent.diff'
    if not (diff.exists() and diff.stat().st_size) and record.get('passed'):
        # Before September 29 the diff was taken against HEAD, so an agent that committed its fix left it empty.
        return dict(out, error='fix_not_saved', detail='agent.diff is empty but the trial passed')
    work = bench_tasks.workspace(task, repo, scratch/'work')
    if diff.exists() and diff.stat().st_size:
        applied = subprocess.run(['git', 'apply', '--binary', '--whitespace=nowarn', str(diff.resolve())], cwd=work,
                                 capture_output=True, text=True)
        if applied.returncode:
            return dict(out, error='diff_did_not_apply', detail=applied.stderr[-500:])
    out['scope'] = scope(work, task)
    graded = bench_tasks.grade(task, work, repo, python, scratch/'grade',
                               expected_hidden_passed=record.get('expected_hidden_passed'))
    out.update(regraded_passed=graded['passed'], regraded_reason=graded['reason'],
               matches=graded['passed'] == record.get('passed'))
    if edge:
        out['edge'] = run_edge(task, scratch/'grade'/'graded', python, scratch/'edge-env', root)
    return out


def summarize(results, edge_suites, pair):
    arms = {}
    for r in results:
        a = arms.setdefault(r['arm'], {'trials': 0, 'recorded_passes': 0, 'regraded_passes': 0, 'mismatches': [],
                                       'errors': [], 'edge_trials': 0, 'edge_all_passed': 0, 'edge_failed_tests': 0,
                                       'strict_passes': 0,
                                       '_lines': [], 'trials_adding_tests': 0})
        a['trials'] += 1
        a['recorded_passes'] += bool(r['recorded_passed'])
        if 'error' in r:
            a['errors'].append(r['task'])
            continue
        a['regraded_passes'] += bool(r['regraded_passed'])
        if not r['matches']:
            a['mismatches'].append(r['task'])
        a['_lines'].append(r['scope']['source_lines'])
        a['trials_adding_tests'] += r['scope']['test_added'] > 0
        if r.get('edge'):
            e = r['edge']
            a['edge_trials'] += 1
            a['edge_all_passed'] += edge_all_passed(e)
            a['strict_passes'] += bool(r['regraded_passed']) and edge_all_passed(e)  # the pass rule since October 4
            a['edge_failed_tests'] += e['tests_run'] - e['tests_passed']
    for a in arms.values():
        lines = a.pop('_lines')
        a['median_source_lines'] = statistics.median(lines) if lines else None
    by = {(r['task'], r['arm']): r for r in results if 'error' not in r}
    paired = []
    for task in sorted({r['task'] for r in results}):
        rows = [by.get((task, arm)) for arm in pair]
        if all(rows):
            paired.append({'task': task, **{arm: {'passed': r['regraded_passed'], 'source_lines': r['scope']['source_lines'],
                                                  'test_added': r['scope']['test_added'],
                                                  'edge': None if not r.get('edge') else
                                                  f"{r['edge']['tests_passed']}/{r['edge']['tests_run']}"}
                                           for arm, r in zip(pair, rows)}})
    return {'arms': arms, 'paired': {'arms': list(pair), 'tasks': paired},
            'edge_suites': {t: {'usable': s['usable'], 'reference': f"{s['reference']['tests_passed']}/{s['reference']['tests_run']}",
                                'base': f"{s['base']['tests_passed']}/{s['base']['tests_run']}"}
                            for t, s in edge_suites.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('run', type=Path)
    parser.add_argument('--tasks', help='comma-separated task IDs (default: every task in the run)')
    parser.add_argument('--arms', help='comma-separated arms (default: every arm in the run)')
    parser.add_argument('--pair', default='sonnet-5.5,opus-5.5', help='two arms compared task by task')
    parser.add_argument('--jobs', type=int, default=4)
    parser.add_argument('--python', default=bench_tasks.interpreter())
    parser.add_argument('--edge-root', type=Path, default=EDGE)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    specs = {t['id']: t for t in bench_tasks.load()}
    wanted_tasks = set(args.tasks.split(',')) if args.tasks else None
    wanted_arms = set(args.arms.split(',')) if args.arms else None
    trials = [p.parent for p in sorted(args.run.glob('*/*/*/trial.json'))
              if (wanted_tasks is None or p.parts[-4] in wanted_tasks) and (wanted_arms is None or p.parts[-3] in wanted_arms)]
    missing = sorted({t.parts[-3] for t in trials} - set(specs))
    if missing:
        parser.error(f'No task spec for {", ".join(missing)}')
    out = args.out or ROOT/'runs'/f"regrade-{args.run.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    out.mkdir(parents=True)
    scratch = out/'scratch'
    repo = lambda task: Path(task['repo_path']) if 'repo_path' in task else bench_tasks.REPOS/task['repo']
    edge_suites = {}
    for task_id in sorted({t.parts[-3] for t in trials}):
        if edge_files(task_id, args.edge_root):
            task = specs[task_id]
            edge_suites[task_id] = check_edge(task, repo(task), args.python, scratch/f'edge-{task_id}', args.edge_root)

    def one(item):
        index, trial = item
        task = specs[trial.parts[-3]]
        work = scratch/str(index)
        try:
            return regrade_trial(trial, task, repo(task), args.python, work,
                                 edge=(edge_suites.get(task['id']) or {}).get('usable', False), root=args.edge_root)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    with ThreadPoolExecutor(max(1, args.jobs)) as pool:
        results = list(pool.map(one, enumerate(trials)))
    shutil.rmtree(scratch, ignore_errors=True)
    summary = summarize(results, edge_suites, args.pair.split(','))
    (out/'results.json').write_text(json.dumps({'run': str(args.run), 'python': args.python, 'summary': summary,
                                                'edge_suites': edge_suites, 'trials': results}, indent=2))
    for arm, a in summary['arms'].items():
        print(f"{arm}: regraded {a['regraded_passes']}/{a['trials']} (recorded {a['recorded_passes']}), "
              f"mismatches {a['mismatches'] or 'none'}, errors {a['errors'] or 'none'}, "
              f"median source lines {a['median_source_lines']}, trials adding tests {a['trials_adding_tests']}"
              + (f", edge all-passed {a['edge_all_passed']}/{a['edge_trials']}, strict passes {a['strict_passes']}" if a['edge_trials'] else ''))
    print(out/'results.json')


if __name__ == '__main__':
    main()
