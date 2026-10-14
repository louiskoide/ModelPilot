"""Miss detector, third signal (built October 14, $0; not yet run): tests that *other* agents wrote while solving the
same task, run against each counted dev trial's fix.

The misses on record are implied requirements that were never implemented, so the agent's own tests can't catch them
(miss_detector, October 13), and Opus's spec-written tests caught parse 8/8 but none of tomli or ISMAGS (spec_tests,
`spec-tests-20261006-183227`). The question here is whether a peer that did implement the requirement left a test that
fails on the trial that didn't. Dev split only (miss_detector.split_runs('dev')); the holdout is the user's call.

For each counted dev trial A, its peers are the other counted dev trials of A's task:
- a peer's tests are the test functions its saved diff added or changed (by `ast`, against the base's same file), kept
  only if they pass on the peer's own tree, which a host can check. The reference fix and the hidden tests are never
  used to filter; a reading kept apart (`reference_bound`) also drops peer tests the reference fix fails, as an upper
  bound a host can't reach.
- A's tree gets each sampled peer's test files copied in beside the originals under new names, and the peer's kept
  tests run by node id under pytest with the grader's isolation, one run per trial for all its peers.
- a failure is API-shape noise when it is an ImportError, AttributeError, NameError, missing fixture or unexpected
  keyword naming something the base's source doesn't have (the peer's own API, not A's miss); any other failure
  flags A (`assertion` or `other_error`).

    python3 -m modelpilot.miss_crosscheck estimate --out runs/miss-crosscheck-plan-<ts>.json       # $0: work and time
    python3 -m modelpilot.miss_crosscheck run --out runs/miss-crosscheck-<ts> [--max-peers 16] [--seed 0] [--jobs 6]
    python3 -m modelpilot.miss_crosscheck score runs/miss-crosscheck-<ts>                        # $0: the readings

Readings: peers from the same arm (a second cheap run of the deployed setting), from Opus 5.5 (a stronger model's
tests) or from any arm; one seeded random peer (what a deployment gets: a peer that wrote no test flags nothing) or
every sampled peer (an upper bound). For each, misses and passes flagged overall, per task and leaving each task out
(37 of the 40 dev misses are on 3 tasks). A deployed rule is priced from recorded costs: the mean cold-equivalent cost
of a peer trial, against the $0.25 redo and the forced Opus review's +$0.069 a task (which rescued none).

$0: recorded files and host-run tests only, no model request. Applying diffs and running tests executes agent-written
code, as grading does: not an OS sandbox, trusted task repositories only.
"""
import argparse
import ast
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from . import bench_tasks, exposure, regrade

ROOT = Path(__file__).resolve().parents[1]
TIMEOUT = 900
OPUS = 'claude-opus-5-5'
SOURCES = ('same_arm', 'opus', 'any')
REDO_USD, REVIEW_USD = 0.25, 0.069  # runs/redo-cost-20261004.json; bench-20261006-093401 (+$0.069 a task)
NOISE = (re.compile(r"cannot import name '([A-Za-z_]\w*)'"), re.compile(r"No module named '([\w.]+)'"),
         re.compile(r"has no attribute '([A-Za-z_]\w*)'"), re.compile(r"name '([A-Za-z_]\w*)' is not defined"),
         re.compile(r"unexpected keyword argument '([A-Za-z_]\w*)'"), re.compile(r"fixture '([A-Za-z_]\w*)' not found"))
IDENTIFIER = re.compile(r'[A-Za-z_]\w*')


def test_functions(text):
    """{qualified name: normalized source} of a module's test functions: top-level `test*` functions and `test*`
    methods of its classes ('Class::method'), as pytest names them. None when the file doesn't parse here."""
    try:
        module = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    out = {}
    for node in module.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test'):
            out[node.name] = ast.dump(node)
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith('test'):
                    out[f'{node.name}::{item.name}'] = ast.dump(item)
    return out


def changed_tests(tree, task):
    """The test functions a change added or changed, by file: {path: {'names': [...], 'text': post-image}}. A test file
    the change deleted, or one that doesn't parse, gives none."""
    subprocess.run(['git', 'add', '-A', '-N'], cwd=tree, capture_output=True, check=True)
    paths = subprocess.run(['git', 'diff', '--name-only', '--no-renames', 'HEAD'], cwd=tree, capture_output=True,
                           text=True, check=True).stdout.split()
    out = {}
    for path in paths:
        if not (path.endswith('.py') and regrade.is_test_path(path, task['test_dir'])) or not (Path(tree)/path).exists():
            continue
        text = (Path(tree)/path).read_text(errors='replace')
        base = subprocess.run(['git', 'show', f'HEAD:{path}'], cwd=tree, capture_output=True, text=True)
        before = (test_functions(base.stdout) or {}) if base.returncode == 0 else {}
        after = test_functions(text)
        names = sorted(n for n, dump in (after or {}).items() if before.get(n) != dump)
        if names:
            out[path] = {'names': names, 'text': text}
    return out


def copied_path(path, tag):
    """Where a peer's test file goes in another tree: beside the original, under a name no other file has."""
    path = Path(path)
    return (path.parent/f'test_xc_{tag}_{path.stem}.py').as_posix()


def node_ids(path, names):
    return [f'{path}::{name}' for name in names]


def junit_outcomes(xml_path):
    """{(classname, base test name): {'outcome', 'text'}} from pytest's JUnit XML, parametrized cases folded into their
    test (failed if any case failed, skipped only if all were); collection errors as ('', module) entries."""
    out = {}
    root = ET.parse(xml_path).getroot()
    for case in root.iter('testcase'):
        name = case.get('name', '').split('[', 1)[0]
        key = (case.get('classname', ''), name)
        child = next((c for c in case if c.tag in ('failure', 'error', 'skipped')), None)
        outcome = 'passed' if child is None else {'failure': 'failed', 'error': 'error', 'skipped': 'skipped'}[child.tag]
        text = '' if child is None else ((child.get('message') or '') + '\n' + (child.text or ''))[-4000:]
        seen = out.get(key)
        if seen is None or (seen['outcome'] in ('passed', 'skipped') and outcome in ('failed', 'error')) or \
                (seen['outcome'] == 'skipped' and outcome == 'passed'):
            out[key] = {'outcome': outcome, 'text': text}
    return out


def outcome_of(results, path, name):
    """One selected test's outcome from junit_outcomes: its own case, else its file's collection error, else
    'not_run' (pytest found no such test)."""
    module = path[:-3].replace('/', '.')
    cls, _, func = name.rpartition('::')
    found = results.get((f'{module}.{cls}' if cls else module, func))
    if found:
        return found
    collection = results.get(('', module))
    return collection or {'outcome': 'not_run', 'text': ''}


def run_selected(tree, task, python, scratch, ids):
    """The given pytest node ids in a tree, with the grader's isolation (no credentials, own HOME and TMPDIR), the
    repository's own addopts dropped. Their files are passed, not the ids, with -k on the selected names: a node id in a
    file that fails to import aborts the whole run ("found no collectors"), and a peer's file importing its own new API
    is common. Tests -k also matches are run but never read. Returns junit_outcomes or {'error'}."""
    scratch = Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    xml = scratch/'junit.xml'
    xml.unlink(missing_ok=True)
    home, tmp = bench_tasks.isolated_dirs(scratch/'env')
    env = bench_tasks.clean_env(python, home, tmp)
    if task.get('pythonpath'):
        env['PYTHONPATH'] = os.path.abspath(Path(tree)/task['pythonpath'])
    files = sorted({i.split('::', 1)[0] for i in ids})
    names = sorted({i.rsplit('::', 1)[1] for i in ids})
    argv = [python, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '-o', 'addopts=', '-o', 'junit_family=xunit1',
            '--continue-on-collection-errors', '-k', ' or '.join(names), f'--junitxml={xml.resolve()}', *files]
    try:
        run = subprocess.run(argv, cwd=tree, env=env, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return {'error': 'timeout'}
    if not xml.exists():
        return {'error': 'no_junit', 'exit_code': run.returncode, 'output_tail': (run.stdout + run.stderr)[-1500:]}
    try:
        return {'results': junit_outcomes(xml), 'exit_code': run.returncode}
    except ET.ParseError:
        return {'error': 'bad_junit', 'exit_code': run.returncode}


def base_names(task, repo, scratch):
    """Every identifier and module name in the base's source (tests apart): what a failure must name to be A's
    problem rather than the peer's own API."""
    tree = bench_tasks.task_tree(task, repo, task['base'], Path(scratch)/'base')
    names = set()
    for path in tree.rglob('*.py'):
        relative = path.relative_to(tree).as_posix()
        if regrade.is_test_path(relative, task['test_dir']):
            continue
        names.update(IDENTIFIER.findall(path.read_text(errors='replace')))
        names.update(p for p in Path(relative).with_suffix('').parts if p.isidentifier())
    shutil.rmtree(tree, ignore_errors=True)
    return names


def classify(result, known):
    """'passed', 'skipped', 'not_run', 'api_noise' (a failure naming something the base's source lacks), 'assertion'
    or 'other_error'."""
    outcome = result['outcome']
    if outcome in ('passed', 'skipped', 'not_run'):
        return outcome
    text = result['text']
    for pattern in NOISE:
        for match in pattern.finditer(text):
            name = match.group(1).split('.')[-1]
            if name not in known or pattern.pattern.startswith('fixture'):
                return 'api_noise'
    return 'assertion' if re.search(r'AssertionError|^E\s+assert ', text, re.M) or text.startswith('assert ') \
        else 'other_error'


def peer_tests(trial, task, repo, python, scratch, reference=True):
    """Phase 1, one trial as a peer: the test functions its diff added or changed, and which pass on its own tree (and,
    for the upper bound, on the reference fix). {'files': {path: {'names', 'text', 'sha256'}}, 'kept': {path: [names]},
    'reference_kept': ...} or {'error'}."""
    scratch = Path(scratch)
    tree, error = exposure.change_tree(task, scratch/'own', trial['diff'], repo)
    if error:
        return {'error': error}
    files = changed_tests(tree, task)
    out = {'files': {p: {'names': v['names'], 'text': v['text'],
                         'sha256': hashlib.sha256(v['text'].encode()).hexdigest()} for p, v in files.items()},
           'kept': {}, 'reference_kept': None}
    if not files:
        return out
    own = run_selected(tree, task, python, scratch/'own-run', [i for p, v in files.items() for i in node_ids(p, v['names'])])
    if 'error' in own:
        return dict(out, error='own_' + own['error'])
    out['kept'] = {p: [n for n in v['names'] if outcome_of(own['results'], p, n)['outcome'] == 'passed']
                   for p, v in files.items()}
    out['kept'] = {p: names for p, names in out['kept'].items() if names}
    shutil.rmtree(scratch/'own', ignore_errors=True)
    if reference and out['kept']:
        ref, error = exposure.change_tree(task, scratch/'reference', None, repo)
        if error:
            out['reference_error'] = error
            return out
        ids = []
        for path, names in out['kept'].items():
            target = copied_path(path, 'ref')
            (ref/target).parent.mkdir(parents=True, exist_ok=True)
            (ref/target).write_text(files[path]['text'])
            ids += node_ids(target, names)
        ran = run_selected(ref, task, python, scratch/'reference-run', ids)
        if 'error' in ran:
            out['reference_error'] = ran['error']
        else:
            out['reference_kept'] = {p: [n for n in names
                                         if outcome_of(ran['results'], copied_path(p, 'ref'), n)['outcome'] == 'passed']
                                     for p, names in out['kept'].items()}
        shutil.rmtree(scratch/'reference', ignore_errors=True)
    return out


def sample_peers(trial, peers, max_peers, seed):
    """A seeded sample of at most max_peers of a trial's peers (every peer when there are no more)."""
    rng = random.Random(f"{seed}:{trial['key']}")
    return sorted(peers, key=lambda p: p['key']) if len(peers) <= max_peers else \
        sorted(rng.sample(sorted(peers, key=lambda p: p['key']), max_peers), key=lambda p: p['key'])


def against_peers(trial, task, repo, python, scratch, peers, tests, known):
    """Phase 2, one trial A: every sampled peer's kept tests on A's tree, one pytest run. Identical peer files are
    copied once. Returns {peer key: {'tests': n, 'classes': {class: count}, 'failing': [[path, name, class]]}} (path as
    the peer wrote it) or {'error'}."""
    scratch = Path(scratch)
    tree, error = exposure.change_tree(task, scratch/'tree', trial['diff'], repo)
    if error:
        return {'error': error}
    placed, ids, plan = {}, [], {}
    for peer in peers:
        found = tests.get(peer['key']) or {}
        for path, names in (found.get('kept') or {}).items():
            digest = found['files'][path]['sha256']
            target = placed.get((path, digest))
            if target is None:
                target = placed[(path, digest)] = copied_path(path, digest[:12])
                (tree/target).parent.mkdir(parents=True, exist_ok=True)
                (tree/target).write_text(found['files'][path]['text'])
            fresh = [i for i in node_ids(target, names) if i not in ids]
            ids += fresh
            plan.setdefault(peer['key'], []).extend((path, target, n) for n in names)
    if not ids:
        return {}
    ran = run_selected(tree, task, python, scratch/'run', ids)
    if 'error' in ran:
        return {'error': ran['error']}
    out = {}
    for key, items in plan.items():
        classes, failing = {}, []
        for path, target, name in items:
            label = classify(outcome_of(ran['results'], target, name), known)
            classes[label] = classes.get(label, 0) + 1
            if label in ('assertion', 'other_error', 'api_noise'):
                failing.append([path, name, label])
        out[key] = {'tests': len(items), 'classes': classes, 'failing': failing}
    return out


def flags(result, allowed=None):
    """A peer's tests flag A: any assertion failure or other error that isn't API-shape noise; with allowed
    ({path: names}), only among those tests."""
    return bool(result) and any(label in ('assertion', 'other_error') and (allowed is None or name in allowed.get(path, ()))
                                for path, name, label in result['failing'])


def in_source(trial, peer, source):
    return source == 'any' or (source == 'same_arm' and peer['arm'] == trial['arm']) or \
        (source == 'opus' and peer.get('model') == OPUS)


def readings_for(trial, peers, cross, tests, seed, bound=False):
    """{source: {'one': flagged?, 'all': flagged?}} for one trial; None where no peer of that source exists. With bound,
    only peer tests the reference fix passes count (the upper bound)."""
    def allowed(peer_key):
        return ((tests.get(peer_key) or {}).get('reference_kept') or {}) if bound else None
    out = {}
    for source in SOURCES:
        chosen = [p for p in peers if in_source(trial, p, source)]
        if not chosen:
            out[source] = None
            continue
        flagged = {p['key']: flags(cross.get(p['key']), allowed(p['key'])) for p in chosen}
        one = random.Random(f"{seed}:one:{source}:{trial['key']}").choice(sorted(flagged))
        out[source] = {'one': flagged[one], 'all': any(flagged.values()), 'peers': len(chosen),
                       'peers_with_tests': sum(bool((tests.get(k) or {}).get('kept')) for k in flagged)}
    return out


def share(rows, source, count):
    usable = [r for r in rows if r['readings'].get(source)]
    misses = [r for r in usable if not r['strict_pass']]
    passes = [r for r in usable if r['strict_pass']]
    flagged = lambda group: sum(bool(r['readings'][source][count]) for r in group)
    return {'misses_flagged': flagged(misses), 'misses': len(misses), 'passes_flagged': flagged(passes),
            'passes': len(passes)}


def summarize(rows, key='readings'):
    """Each reading overall, per task and leaving each task out."""
    rows = [dict(r, readings=r[key]) for r in rows if r.get(key)]
    tasks = sorted({r['task'] for r in rows})
    out = {}
    for source in SOURCES:
        for count in ('one', 'all'):
            out[f'{source}/{count}'] = {
                'overall': share(rows, source, count),
                'per_task': {t: share([r for r in rows if r['task'] == t], source, count) for t in tasks},
                'leave_one_task_out': {t: share([r for r in rows if r['task'] != t], source, count) for t in tasks}}
    return out


def price(rows):
    """What a deployed second run costs, from recorded costs: per source, the mean cold-equivalent cost of the peer
    trials a deployment would run (same arm, Opus 5.5 or any), and that cost over every trial per miss the one-peer
    reading flags, against the redo and the forced review. The all-peers reading is an upper bound, not priced: a
    deployment would pay for every peer."""
    out = {}
    for source in SOURCES:
        usable = [r for r in rows if (r.get('readings') or {}).get(source) and r['peer_cost'].get(source) is not None]
        costs = [r['peer_cost'][source] for r in usable]
        caught = sum(bool(r['readings'][source]['one']) for r in usable if not r['strict_pass'])
        out[source] = {'mean_second_run_usd': round(sum(costs) / len(costs), 4) if costs else None, 'trials': len(costs),
                       'usd_per_miss_flagged_one_peer': round(sum(costs) / caught, 4) if caught else None}
    return dict(out, redo_usd=REDO_USD, forced_review_usd=REVIEW_USD)


def peer_cost(trial, peers, source):
    costs = [p['cost'] for p in peers if in_source(trial, p, source) and p.get('cost') is not None]
    return sum(costs) / len(costs) if costs else None


def crosscheck(trials, tasks, out, python, repo_of, max_peers=16, seed=0, jobs=4, reference=True):
    """Both phases over counted trials (dicts with key, task, arm, model, strict_pass, diff, cost), into out/. Returns the
    report written to out/crosscheck.json."""
    out = Path(out)
    out.mkdir(parents=True)  # a new directory: never overwrite evidence
    scratch = out/'scratch'
    started = time.monotonic()

    def phase1(item):
        index, trial = item
        try:
            return peer_tests(trial, tasks[trial['task']], repo_of(tasks[trial['task']]), python, scratch/f'p{index}',
                              reference)
        except (subprocess.CalledProcessError, OSError) as error:
            return {'error': type(error).__name__}
        finally:
            shutil.rmtree(scratch/f'p{index}', ignore_errors=True)
    with ThreadPoolExecutor(max(1, jobs)) as pool:
        found = list(pool.map(phase1, enumerate(trials)))
    tests = {t['key']: f for t, f in zip(trials, found)}
    (out/'peer_tests.json').write_text(json.dumps(
        {k: {key: v for key, v in f.items() if key != 'files'} | {'files': {p: {'names': x['names'], 'sha256': x['sha256']}
                                                                           for p, x in (f.get('files') or {}).items()}}
         for k, f in tests.items()}, indent=1) + '\n')
    known = {tid: base_names(tasks[tid], repo_of(tasks[tid]), scratch/f'names-{tid}') for tid in {t['task'] for t in trials}}
    by_task = {}
    for t in trials:
        by_task.setdefault(t['task'], []).append(t)
    sampled = {t['key']: sample_peers(t, [p for p in by_task[t['task']] if p['key'] != t['key']], max_peers, seed)
               for t in trials}

    def phase2(item):
        index, trial = item
        try:
            return against_peers(trial, tasks[trial['task']], repo_of(tasks[trial['task']]), python, scratch/f'a{index}',
                                 sampled[trial['key']], tests, known[trial['task']])
        except (subprocess.CalledProcessError, OSError) as error:
            return {'error': type(error).__name__}
        finally:
            shutil.rmtree(scratch/f'a{index}', ignore_errors=True)
    with ThreadPoolExecutor(max(1, jobs)) as pool:
        crossed = list(pool.map(phase2, enumerate(trials)))
    shutil.rmtree(scratch, ignore_errors=True)
    rows = []
    for trial, cross in zip(trials, crossed):
        peers = sampled[trial['key']]
        row = {k: trial[k] for k in ('key', 'run', 'task', 'arm', 'model', 'trial', 'strict_pass') if k in trial}
        row.update(peers=[p['key'] for p in peers], own_tests=sum(len(v) for v in (tests[trial['key']].get('kept') or {}).values()),
                   cross={k: v for k, v in cross.items() if k != 'error'}, error=cross.get('error'),
                   peer_cost={s: peer_cost(trial, peers, s) for s in SOURCES})
        if not row['error']:
            row['readings'] = readings_for(trial, peers, cross, tests, seed)
            row['reference_bound'] = readings_for(trial, peers, cross, tests, seed, bound=True)
        rows.append(row)
    report = {'max_peers': max_peers, 'seed': seed, 'python': python, 'seconds': round(time.monotonic() - started, 1),
              'trials': len(rows), 'misses': sum(not r['strict_pass'] for r in rows),
              'errors': sum(bool(r['error']) for r in rows), 'rows': rows}
    report['summary'] = score_rows(rows)
    with (out/'crosscheck.json').open('x') as f:
        json.dump(report, f, indent=1)
        f.write('\n')
    return report


def score_rows(rows):
    classes = {}
    for r in rows:
        for result in (r.get('cross') or {}).values():
            for label, n in result['classes'].items():
                classes[label] = classes.get(label, 0) + n
    return {'readings': summarize(rows), 'reference_bound (an upper bound a host can\'t reach)': summarize(rows, 'reference_bound'),
            'price': price(rows), 'peer_test_outcomes': classes,
            'caveat': '37 of the 40 dev misses are on 3 tasks; see leave_one_task_out'}


def dev_trials(rates):
    """Every counted dev trial (miss_detector's rule) as crosscheck's input, with its recorded cold-equivalent cost."""
    from . import miss_detector
    from .bench_report import cold_cost, load_run
    rows = []
    for run in miss_detector.split_runs('dev'):
        for record in load_run(run, rates)[1]:
            if miss_detector.counted(record):
                rows.append({'key': f"{run.name}__{record['task']}__{record['arm']}__{record['trial']}", 'run': run.name,
                             'task': record['task'], 'arm': record['arm'], 'model': record.get('model'),
                             'trial': record['trial'], 'strict_pass': bool(record['passed']), 'cost': cold_cost(record),
                             'diff': run/record['task']/record['arm']/str(record['trial'])/'agent.diff'})
    return rows


def estimate(trials, tasks, python, repo_of, scratch, max_peers=16, jobs=6):
    """The run's size before running it: trials, peers, and pytest runs (one per trial as a peer, one on the reference
    for the upper bound, one per trial against its peers), timed by one peer phase per task."""
    by_task = {}
    for t in trials:
        by_task.setdefault(t['task'], []).append(t)
    timing = {}
    for task_id, group in sorted(by_task.items()):
        started = time.monotonic()
        result = peer_tests(group[0], tasks[task_id], repo_of(tasks[task_id]), python, Path(scratch)/task_id)
        shutil.rmtree(Path(scratch)/task_id, ignore_errors=True)
        timing[task_id] = {'seconds': round(time.monotonic() - started, 1), 'trials': len(group),
                           'sample_tests_kept': sum(len(v) for v in (result.get('kept') or {}).values()),
                           'error': result.get('error')}
    # A peer phase builds two trees and runs pytest twice; the cross phase builds one and runs once: about 1.5 phases.
    serial = sum(v['seconds'] * v['trials'] * 1.5 for v in timing.values())
    return {'trials': len(trials), 'misses': sum(not t['strict_pass'] for t in trials), 'tasks': len(by_task),
            'max_peers': max_peers, 'peers_per_trial': {t: min(max_peers, len(g) - 1) for t, g in sorted(by_task.items())},
            'pytest_runs': 3 * len(trials), 'timing_per_task': timing, 'jobs': jobs,
            'estimated_hours': round(serial / max(1, jobs) / 3600, 2)}


def main():
    from .bench import rates
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='step', required=True)
    e = sub.add_parser('estimate', help='$0: the run\'s size and time, from one timed peer phase per task')
    e.add_argument('--out', type=Path, required=True)
    e.add_argument('--max-peers', type=int, default=16)
    e.add_argument('--jobs', type=int, default=6)
    r = sub.add_parser('run', help='$0: both phases over the dev split (runs agent code)')
    r.add_argument('--out', type=Path, required=True, help='a new directory')
    r.add_argument('--max-peers', type=int, default=16)
    r.add_argument('--seed', type=int, default=0)
    r.add_argument('--jobs', type=int, default=6)
    r.add_argument('--no-reference-bound', action='store_true', help='skip the reference-filtered upper bound')
    s = sub.add_parser('score', help='$0: print a run\'s readings')
    s.add_argument('out', type=Path)
    args = parser.parse_args()
    if args.step == 'score':
        report = json.loads((args.out/'crosscheck.json').read_text())
        print(json.dumps({k: v['overall'] for k, v in report['summary']['readings'].items()}, indent=1))
        print(json.dumps(report['summary']['price'], indent=1))
        return
    if args.out.exists():
        parser.error(f'{args.out} exists: use a new path')
    trials = dev_trials(rates())
    if not trials:
        raise SystemExit('No counted dev trials under runs/: the dev split\'s bench runs are needed.')
    tasks = {t['id']: t for t in bench_tasks.load()}
    repo_of = lambda task: bench_tasks.REPOS/task['repo']
    python = bench_tasks.interpreter()
    if args.step == 'estimate':
        args.out.parent.mkdir(parents=True, exist_ok=True)
        scratch = args.out.with_name(args.out.name + '.scratch')
        try:
            report = estimate(trials, tasks, python, repo_of, scratch, args.max_peers, args.jobs)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        with args.out.open('x') as f:
            json.dump(report, f, indent=1)
            f.write('\n')
        print(json.dumps({k: report[k] for k in ('trials', 'misses', 'pytest_runs', 'estimated_hours')}, indent=1))
        return
    report = crosscheck(trials, tasks, args.out, python, repo_of, args.max_peers, args.seed, args.jobs,
                        reference=not args.no_reference_bound)
    print(f"dev: {report['trials']} trials, {report['misses']} misses, {report['errors']} errors, "
          f"{report['seconds']} s -> {args.out/'crosscheck.json'}")
    print(json.dumps({k: v['overall'] for k, v in report['summary']['readings'].items()}, indent=1))


if __name__ == '__main__':
    main()
