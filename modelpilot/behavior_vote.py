"""Cross-model behavioural vote (CBV), offline validation ($0; the October 16 research report's first step).

The report ("Catching ModelPilot's confident wrong finishes cheaply", `docs/m6-benchmark-plan.md`, "Behavioural
vote") proposes: after the cheap agent finishes, run two or three independent Haiku 5.5 solutions, run every solution
on probe *inputs* for the API the task touches, and flag the agent's fix when its behaviour differs from the Haiku
majority on a probe every Haiku solution runs without crashing. Haiku's peer tests failed mostly on wrong expected
values (17.5/37 misses flagged, 3.7/37 once tests the reference fails were dropped); a vote asks Haiku for no expected
value, only for behaviour.

This checks the idea on recorded trials before anything is paid for, as the report says to:
- **Probes:** the peer tests of `miss_crosscheck`'s recorded run (`runs/miss-crosscheck-20261014-112627`; every counted
  dev trial's added or changed test functions that pass on its own tree), with every expected value dropped by
  `probe_source`: an assertion records the values it would have compared (literal operands dropped), a raises check
  records the exception type, a warns check the warning categories, all wrapped in `vote_plugin`'s recorder.
- **Voters:** the task's counted dev Haiku 5.5 trials, each diff applied to a fresh base tree as `regrade` does, the
  target's own trial left out. Each voter runs twice; a probe whose reading differs between a voter's two runs is
  unstable and dropped for the task.
- **Signal:** a probe is usable for a target when every voter runs it with status 'ok'; it votes when more than half
  the voters agree on its reading (status, observations, warnings). The target is flagged when its reading of any
  voting probe differs from the majority's. A target status of 'assertion' (an expected value in a helper) leaves
  that probe out for it.
- **Score** (`score`): the pre-registered verdict (`PREREG`) and the readings reported beside it.

    python3.12 -m modelpilot.behavior_vote run --out runs/behavior-vote-<ts> [--jobs 6]
    python3.12 -m modelpilot.behavior_vote score runs/behavior-vote-<ts>

$0: recorded files and host-run tests only, no model request. Applying diffs and running tests executes agent-written
code, as grading does: not an OS sandbox, trusted task repositories only.
"""
import argparse
import ast
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
import hashlib
import json
from math import comb
import os
from pathlib import Path
import random
import shutil
import subprocess
import time

from . import bench_tasks, exposure, miss_crosscheck, regrade

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = Path(__file__).with_name('vote_plugin.py')
CROSSCHECK = ROOT/'runs'/'miss-crosscheck-20261014-112627'
HAIKU = 'claude-haiku-5-5'
OPUS = 'claude-opus-5-5'
TIMEOUT = 900
MIN_VOTERS = 2
BOOTSTRAP, BOOTSTRAP_SEED = 10_000, 0
LATE_FIX_USD = 0.056  # plan item 7, runs/late-fix-analysis-20261007.json
PREREG = {'recall': 0.75, 'recall_lower_95': 0.60, 'false_flag_rate': 0.15, 'p_within_task': 0.01, 'tasks_caught': 3}
RUNTIME = ('_cbv_value', '_cbv_raised', '_cbv_raises', '_cbv_raises_call', '_cbv_warns', '_cbv_warns_call',
           '_cbv_reached')

# unittest assertion methods: how many leading arguments are values (the rest, and msg, are dropped). isinstance checks
# record their result; assertCountEqual its first argument's items unordered; regex checks only the text.
VALUES = {**dict.fromkeys(('assertEqual', 'assertEquals', 'failUnlessEqual', 'assertNotEqual', 'assertNotEquals',
                           'assertAlmostEqual', 'assertAlmostEquals', 'assertNotAlmostEqual', 'assertListEqual',
                           'assertTupleEqual', 'assertDictEqual', 'assertSetEqual', 'assertSequenceEqual',
                           'assertMultiLineEqual', 'assertGreater', 'assertGreaterEqual', 'assertLess',
                           'assertLessEqual', 'assertIs', 'assertIsNot', 'assertIn', 'assertNotIn',
                           'assertDictContainsSubset'), 2),
          **dict.fromkeys(('assertTrue', 'assert_', 'failUnless', 'assertFalse', 'failIf', 'assertIsNone',
                           'assertIsNotNone', 'assertRegex', 'assertNotRegex', 'assertRegexpMatches'), 1)}
INSTANCE = ('assertIsInstance', 'assertNotIsInstance')
RAISES = {'assertRaises': 1, 'failUnlessRaises': 1, 'assertRaisesRegex': 2, 'assertRaisesRegexp': 2}
WARNS = {'assertWarns': 1, 'assertWarnsRegex': 2}
FAILS = ('fail',)


def is_literal(node):
    """A value written into the test: a constant, or a container or negation of constants. An expected value."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return is_literal(node.operand)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(is_literal(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(k is not None and is_literal(k) for k in node.keys) and all(is_literal(v) for v in node.values)
    return False


def pytest_call(node, name):
    """`pytest.<name>(...)` or a bare `<name>(...)` imported from pytest."""
    func = node.func if isinstance(node, ast.Call) else None
    return (isinstance(func, ast.Attribute) and func.attr == name and isinstance(func.value, ast.Name)
            and func.value.id == 'pytest') or (isinstance(func, ast.Name) and func.id == name)


def method_call(node, names):
    """`<object>.<one of names>(...)`: unittest's assertion methods (any receiver: self, cls, a test case)."""
    func = node.func if isinstance(node, ast.Call) else None
    return func.attr if isinstance(func, ast.Attribute) and func.attr in names else None


class Probe(ast.NodeTransformer):
    """Drops every expected value from a test module (module docstring of vote_plugin). Sites are the original
    line:column, the same on every tree since every tree gets the same file."""

    def __init__(self):
        self.changed = 0

    @staticmethod
    def site(node):
        return f'{node.lineno}:{node.col_offset}'

    def record(self, node, values, sort=False):
        """try: v = values  except Exception as e: _cbv_raised(site, e)  else: _cbv_value(site, v). A statement, so
        names an assertion binds (walrus) stay in the test's scope."""
        self.changed += 1
        site = ast.Constant(self.site(node))
        values = [v for v in values if not is_literal(v)]
        value = values[0] if len(values) == 1 else ast.Tuple(values, ast.Load())
        keywords = [ast.keyword('sort', ast.Constant(True))] if sort else []
        statement = ast.Try(
            body=[ast.Assign([ast.Name('_cbv_v', ast.Store())], value)],
            handlers=[ast.ExceptHandler(ast.Name('Exception', ast.Load()), '_cbv_e', [ast.Expr(ast.Call(
                ast.Name('_cbv_raised', ast.Load()), [site, ast.Name('_cbv_e', ast.Load())], []))])],
            orelse=[ast.Expr(ast.Call(ast.Name('_cbv_value', ast.Load()), [site, ast.Name('_cbv_v', ast.Load())],
                                      keywords))],
            finalbody=[])
        return ast.copy_location(statement, node)

    def runtime(self, name, node, args, keywords=()):
        self.changed += 1
        return ast.copy_location(ast.Call(ast.Name(name, ast.Load()), [ast.Constant(self.site(node)), *args],
                                          list(keywords)), node)

    def visit_Assert(self, node):
        test = node.test
        if isinstance(test, ast.Compare):
            values = [test.left, *test.comparators]
        elif isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            values = [test.operand]
        else:
            values = [test]
        return self.record(node, [self.visit(v) for v in values])

    def visit_Expr(self, node):
        call = node.value
        name = method_call(call, VALUES) or method_call(call, INSTANCE)
        if name:
            args = [self.visit(a) for a in call.args]
            if name in INSTANCE:
                if len(args) < 2:
                    return self.generic_visit(node)
                check = ast.Call(ast.Name('isinstance', ast.Load()), args[:2], [])
                return self.record(node, [check])
            values = args[:VALUES[name]] + [self.visit(k.value) for k in call.keywords
                                            if k.arg in ('first', 'second', 'expr', 'obj', 'member', 'container',
                                                         'text')]
            return self.record(node, values, sort=False)
        if method_call(call, ('assertCountEqual', 'assertItemsEqual')) and call.args:
            args = [self.visit(a) for a in call.args[:2]]
            return self.record(node, [next((a for a in args if not is_literal(a)), args[0])], sort=True)
        return self.generic_visit(node)

    def visit_With(self, node):
        items = []
        for item in node.items:
            call = item.context_expr
            if pytest_call(call, 'raises') or method_call(call, RAISES):
                item = ast.withitem(self.runtime('_cbv_raises', call, []), item.optional_vars)
            elif pytest_call(call, 'warns') or pytest_call(call, 'deprecated_call') or method_call(call, WARNS):
                item = ast.withitem(self.runtime('_cbv_warns', call, []), item.optional_vars)
            else:
                item = self.visit(item)
            items.append(item)
        node.items = items
        node.body = [self.visit(s) for s in node.body]
        return node

    visit_AsyncWith = visit_With

    def visit_Call(self, node):
        node = self.generic_visit(node)
        name = method_call(node, RAISES)
        if pytest_call(node, 'raises') and len(node.args) >= 2:
            return self.runtime('_cbv_raises_call', node, node.args[1:],
                                [k for k in node.keywords if k.arg != 'match'])
        if name and len(node.args) > RAISES[name]:
            return self.runtime('_cbv_raises_call', node, node.args[RAISES[name]:], node.keywords)
        name = method_call(node, WARNS)
        if pytest_call(node, 'warns') and len(node.args) >= 2:
            return self.runtime('_cbv_warns_call', node, node.args[1:], [k for k in node.keywords if k.arg != 'match'])
        if name and len(node.args) > WARNS[name]:
            return self.runtime('_cbv_warns_call', node, node.args[WARNS[name]:], node.keywords)
        if pytest_call(node, 'deprecated_call') and node.args:
            return self.runtime('_cbv_warns_call', node, node.args, node.keywords)
        if pytest_call(node, 'fail') or method_call(node, FAILS):
            return self.runtime('_cbv_reached', node, [])
        return node


def probe_source(text):
    """A test module with every expected value dropped (Probe), importing vote_plugin's recorder. None when the module
    doesn't parse."""
    try:
        module = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    module = Probe().visit(module)
    at = 0
    body = module.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and \
            isinstance(body[0].value.value, str):
        at = 1
    while at < len(body) and isinstance(body[at], ast.ImportFrom) and body[at].module == '__future__':
        at += 1
    body.insert(at, ast.ImportFrom('vote_plugin', [ast.alias(n) for n in RUNTIME], 0))
    return ast.unparse(ast.fix_missing_locations(module)) + '\n'


# --- Probe pool: the recorded peer tests, as probes -----------------------------------------------------------------


def recorded_peer_tests(path=CROSSCHECK/'peer_tests.json'):
    return json.loads(Path(path).read_text())


def pool_of(trial, task, recorded, repo, scratch):
    """One trial's probes: its changed test files (post-image, rebuilt from its diff and checked against the cross-check's
    recorded SHA-256) with the test names the cross-check kept. {path: {'sha256', 'names', 'text'}} or {'error'}."""
    found = recorded.get(trial['key']) or {}
    kept = found.get('kept') or {}
    if not kept:
        return {}
    tree, error = exposure.change_tree(task, Path(scratch)/'own', trial['diff'], repo)
    if error:
        return {'error': error}
    files = miss_crosscheck.changed_tests(tree, task)
    out = {}
    for path, names in kept.items():
        text = (files.get(path) or {}).get('text')
        digest = hashlib.sha256(text.encode()).hexdigest() if text is not None else None
        if digest != found['files'][path]['sha256']:
            return {'error': f'changed_since_crosscheck:{path}'}
        out[path] = {'sha256': digest, 'names': names, 'text': text}
    return out


def task_probes(trials, pools):
    """The task's probe files, one per distinct (path, content): {(path, sha256): {'text', 'names': {name: [author
    keys]}}}."""
    out = {}
    for trial in trials:
        for path, found in (pools.get(trial['key']) or {}).items():
            if path == 'error':
                continue
            entry = out.setdefault((path, found['sha256']), {'text': found['text'], 'names': {}})
            for name in found['names']:
                entry['names'].setdefault(name, []).append(trial['key'])
    return out


def placed_path(path, digest):
    return miss_crosscheck.copied_path(path, 'cbv' + digest[:12])


# --- Readings: every probe on one tree ------------------------------------------------------------------------------


def plugin_dir(scratch):
    target = Path(scratch)/'plugin'
    target.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(PLUGIN, target/'vote_plugin.py')
    return target


def run_probes(tree, task, python, scratch, files, select, plugin, hypothesis=False):
    """Every selected probe on one tree, under pytest with the grader's isolation (miss_crosscheck.run_selected's
    rootdir and addopts rule), the plugin recording. files: {placed path: probe text}; select: [[placed path, name]].
    Returns the plugin's {'probes', 'collect'} or {'error'}."""
    scratch = Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    for path, text in files.items():
        (Path(tree)/path).parent.mkdir(parents=True, exist_ok=True)
        (Path(tree)/path).write_text(text)
    (scratch/'select.json').write_text(json.dumps(select))
    out = scratch/'readings.json'
    out.unlink(missing_ok=True)
    home, tmp = bench_tasks.isolated_dirs(scratch/'env')
    basetemp = scratch/'basetemp'
    env = bench_tasks.clean_env(python, home, tmp)
    env['PYTHONPATH'] = os.pathsep.join([*bench_tasks.test_env_paths(task, tree), str(Path(plugin).resolve())])
    env.update(VOTE_SELECT=str((scratch/'select.json').resolve()), VOTE_OUT=str(out.resolve()),
               VOTE_TREE=str(Path(tree).resolve()), VOTE_TMP=os.pathsep.join(str(p.resolve()) for p in (tmp, basetemp, home)))
    argv = [python, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '-p', 'vote_plugin', '-o', 'addopts=',
            f'--rootdir={Path(tree).resolve()}', f'--basetemp={basetemp.resolve()}', '--continue-on-collection-errors',
            *(['--hypothesis-seed=0'] if hypothesis else []), *sorted(files)]
    try:
        run = subprocess.run(argv, cwd=tree, env=env, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return {'error': 'timeout'}
    if not out.exists():
        return {'error': 'no_readings', 'exit_code': run.returncode, 'output_tail': (run.stdout + run.stderr)[-1500:]}
    return json.loads(out.read_text())


def signatures(readings, select):
    """{probe: reading} for one tree: each recorded node id's (status, observations, warnings), and each selected test
    the tree never ran (its file's collection error, else 'missing') under its unparametrized id."""
    out = {}
    seen = set()
    for nodeid, probe in (readings.get('probes') or {}).items():
        out[nodeid] = [probe['status'], [o[:3] for o in probe['obs']], probe['warnings']]
        seen.add(tuple(probe['key']))
    for path, name in select:
        if (path, name) not in seen:
            out[f'{path}::{name}'] = [(readings.get('collect') or {}).get(path, 'missing'), [], []]
    return out


def tree_readings(trial, task, repo, python, scratch, files, select, plugin, hypothesis=False):
    tree, error = exposure.change_tree(task, Path(scratch)/'tree', trial['diff'], repo)
    if error:
        return {'error': error}
    readings = run_probes(tree, task, python, Path(scratch)/'run', files, select, plugin, hypothesis)
    if 'error' in readings:
        return readings
    return {'signatures': signatures(readings, select),
            'statuses': dict(Counter(p['status'] for p in readings['probes'].values())),
            'details': {k: p['obs'] for k, p in readings['probes'].items()}}


# --- The vote --------------------------------------------------------------------------------------------------------


def key_of(reading):
    return json.dumps(reading, sort_keys=True)


def vote(target, voters, unstable=frozenset(), probes=None):
    """One target against its voters (module docstring). target, voters: {probe: reading}. probes limits the probes
    read (a reading's subset, e.g. by author). Returns usable, voting and divergent probe counts and the divergences."""
    names = set(target) | {p for v in voters for p in v}
    if probes is not None:
        names &= set(probes)
    usable = voting = 0
    divergent = []
    for probe in sorted(names - set(unstable)):
        readings = [v.get(probe) for v in voters]
        if any(r is None or r[0] != 'ok' for r in readings):
            continue
        mine = target.get(probe) or ['missing', [], []]
        if mine[0] == 'assertion':
            continue
        usable += 1
        top, count = Counter(key_of(r) for r in readings).most_common(1)[0]
        if count * 2 <= len(readings):
            continue
        voting += 1
        if key_of(mine) != top:
            divergent.append(probe)
    return {'usable': usable, 'voting': voting, 'divergent': divergent, 'flagged': bool(divergent)}


def unstable_probes(first, second):
    """Probes whose reading differs between a voter's two runs of the same tree."""
    out = set()
    for a, b in zip(first, second):
        for probe in set(a) | set(b):
            if key_of(a.get(probe)) != key_of(b.get(probe)):
                out.add(probe)
    return out


# --- Statistics ------------------------------------------------------------------------------------------------------


def hypergeom(n, flagged, misses):
    total = comb(n, misses)
    return [Fraction(comb(flagged, j) * comb(n - flagged, misses - j), total) for j in range(misses + 1)]


def within_task(rows):
    """rows: (task, is_miss, is_flagged). The within-task conditional permutation null, computed exactly: in each task
    the flags fall on its trials regardless of grade (hypergeometric), summed over tasks by convolution. Expected misses
    flagged and P(X >= observed)."""
    by_task = defaultdict(list)
    for task, miss, flag in rows:
        by_task[task].append((miss, flag))
    dist, expected = [Fraction(1)], Fraction(0)
    for trials in by_task.values():
        n, f, m = len(trials), sum(fl for _, fl in trials), sum(mi for mi, _ in trials)
        if m == 0:
            continue
        expected += Fraction(f * m, n)
        task = hypergeom(n, f, m)
        new = [Fraction(0)] * (len(dist) + len(task) - 1)
        for i, a in enumerate(dist):
            for j, b in enumerate(task):
                new[i + j] += a * b
        dist = new
    observed = sum(1 for _, miss, flag in rows if miss and flag)
    return {'misses_flagged': observed, 'expected': round(float(expected), 2), 'p': float(sum(dist[observed:]))}


def recall_lower(rows, resamples=BOOTSTRAP, seed=BOOTSTRAP_SEED):
    """The 2.5th percentile of recall over task-clustered bootstrap resamples: the tasks holding a miss drawn with
    replacement, recall pooled over the draw."""
    by_task = defaultdict(lambda: [0, 0])
    for task, miss, flag in rows:
        if miss:
            by_task[task][0] += flag
            by_task[task][1] += 1
    tasks = sorted(by_task)
    if not tasks:
        return None
    rng = random.Random(seed)
    values = []
    for _ in range(resamples):
        draw = [by_task[rng.choice(tasks)] for _ in tasks]
        values.append(sum(d[0] for d in draw) / sum(d[1] for d in draw))
    values.sort()
    return values[int(0.025 * resamples)]


def tally(rows):
    misses = [r for r in rows if r[1]]
    passes = [r for r in rows if not r[1]]
    return {'misses_flagged': sum(r[2] for r in misses), 'misses': len(misses),
            'passes_flagged': sum(r[2] for r in passes), 'passes': len(passes)}


def verdict(rows):
    """The pre-registered test (PREREG) on (task, is_miss, is_flagged) rows."""
    counts = tally(rows)
    recall = counts['misses_flagged'] / counts['misses'] if counts['misses'] else 0.0
    false = counts['passes_flagged'] / counts['passes'] if counts['passes'] else 0.0
    lower = recall_lower(rows)
    null = within_task(rows)
    tasks = sorted({t for t, miss, flag in rows if miss and flag})
    checks = {'recall': recall >= PREREG['recall'],
              'recall_lower_95': lower is not None and lower >= PREREG['recall_lower_95'],
              'false_flag_rate': false <= PREREG['false_flag_rate'],
              'p_within_task': null['p'] < PREREG['p_within_task'],
              'tasks_caught': len(tasks) >= PREREG['tasks_caught']}
    return {**counts, 'recall': round(recall, 3), 'recall_lower_95': None if lower is None else round(lower, 3),
            'false_flag_rate': round(false, 3), 'within_task': null, 'tasks_caught': tasks, 'checks': checks,
            'verdict': 'pass' if all(checks.values()) else 'fail'}


# --- The run ---------------------------------------------------------------------------------------------------------


def dev_trials(rates):
    """The cross-check's trials: every counted dev trial (miss_detector's rule)."""
    return miss_crosscheck.dev_trials(rates)


def has_hypothesis(python):
    return subprocess.run([python, '-c', 'import hypothesis'], capture_output=True).returncode == 0


def collect(trials, tasks, out, python, repo_of, recorded, jobs=4, rerun_voters=True, meta=None):
    """Phase 1: each trial's probes. Phase 2: every task's probes on every trial's tree (voters twice). Writes
    out/readings.json (meta, e.g. the module hashes, at its top) and returns it."""
    out = Path(out)
    scratch = out/'scratch'
    started = time.monotonic()
    plugin = plugin_dir(scratch)
    hypothesis = has_hypothesis(python)

    def phase1(item):
        index, trial = item
        try:
            return pool_of(trial, tasks[trial['task']], recorded, repo_of(tasks[trial['task']]), scratch/f'p{index}')
        except (subprocess.CalledProcessError, OSError) as error:
            return {'error': type(error).__name__}
        finally:
            shutil.rmtree(scratch/f'p{index}', ignore_errors=True)
    with ThreadPoolExecutor(max(1, jobs)) as pool:
        pools = dict(zip((t['key'] for t in trials), pool.map(phase1, enumerate(trials))))
    by_task = defaultdict(list)
    for trial in trials:
        by_task[trial['task']].append(trial)
    probe_files, selects, authors = {}, {}, {}
    for task_id, group in by_task.items():
        found = task_probes(group, pools)
        probe_files[task_id], selects[task_id], authors[task_id] = {}, [], {}
        for (path, digest), entry in sorted(found.items()):
            text = probe_source(entry['text'])
            if text is None:
                continue
            placed = placed_path(path, digest)
            probe_files[task_id][placed] = text
            for name, keys in sorted(entry['names'].items()):
                selects[task_id].append([placed, name])
                authors[task_id][f'{placed}::{name}'] = keys
    work = [(t, 0) for t in trials] + ([(t, 1) for t in trials if t.get('model') == HAIKU] if rerun_voters else [])

    def phase2(item):
        index, (trial, repeat) = item
        task_id = trial['task']
        if not selects[task_id]:
            return {'error': 'no_probes'}
        try:
            return tree_readings(trial, tasks[task_id], repo_of(tasks[task_id]), python, scratch/f'r{index}',
                                 probe_files[task_id], selects[task_id], plugin, hypothesis)
        except (subprocess.CalledProcessError, OSError) as error:
            return {'error': type(error).__name__}
        finally:
            shutil.rmtree(scratch/f'r{index}', ignore_errors=True)
    with ThreadPoolExecutor(max(1, jobs)) as pool:
        results = list(pool.map(phase2, enumerate(work)))
    shutil.rmtree(scratch, ignore_errors=True)
    readings = defaultdict(dict)
    for (trial, repeat), result in zip(work, results):
        readings[trial['key']][str(repeat)] = result
    report = {**(meta or {}), 'python': python, 'hypothesis_seed': 0 if hypothesis else None, 'crosscheck': str(CROSSCHECK),
              'seconds': round(time.monotonic() - started, 1),
              'trials': [{k: t[k] for k in ('key', 'run', 'task', 'arm', 'model', 'trial', 'strict_pass', 'cost')}
                         for t in trials],
              'pool_errors': {k: v['error'] for k, v in pools.items() if 'error' in v},
              'probe_files': {t: sorted(f) for t, f in probe_files.items()},
              'probes_selected': {t: len(s) for t, s in selects.items()},
              'authors': authors, 'readings': readings}
    with (out/'readings.json').open('x') as f:
        json.dump(report, f)
        f.write('\n')
    return report


# --- Scoring ---------------------------------------------------------------------------------------------------------


def author_probe_ids(signatures_, authors, keep):
    """The probe ids (node ids, parameters included) whose test some author accepted by keep(author key) wrote."""
    out = set()
    for probe in signatures_:
        base = probe.split('[', 1)[0]
        if any(keep(a) for a in authors.get(base, ())):
            out.add(probe)
    return out


def score_report(report):
    """Every target's vote, then the pre-registered verdict and the readings beside it."""
    trials = {t['key']: t for t in report['trials']}
    readings = report['readings']
    by_task = defaultdict(list)
    for t in report['trials']:
        by_task[t['task']].append(t)
    model_of = {k: t.get('model') for k, t in trials.items()}
    rows = []
    for task_id, group in sorted(by_task.items()):
        voters_all = [t for t in group if t.get('model') == HAIKU]
        sig = {t['key']: (readings.get(t['key']) or {}).get('0') or {} for t in group}
        repeats = [((readings.get(v['key']) or {}).get('1') or {}) for v in voters_all]
        pairs = [(sig[v['key']]['signatures'], again['signatures']) for v, again in zip(voters_all, repeats)
                 if 'signatures' in sig[v['key']] and 'signatures' in again]
        unstable = unstable_probes([a for a, _ in pairs], [b for _, b in pairs])
        authors = report['authors'].get(task_id, {})
        for trial in group:
            mine = sig[trial['key']]
            voters = [v for v in voters_all if v['key'] != trial['key']]
            ok_voters = [v for v in voters if 'signatures' in sig[v['key']]]
            row = {'key': trial['key'], 'task': task_id, 'arm': trial['arm'], 'model': trial.get('model'),
                   'strict_pass': trial['strict_pass'], 'cost': trial.get('cost'), 'voters': len(ok_voters),
                   'voter_errors': len(voters) - len(ok_voters), 'error': mine.get('error'),
                   'unstable_probes': len(unstable), 'voters_rerun': len(pairs)}
            if 'signatures' in mine and len(ok_voters) >= MIN_VOTERS:
                target = mine['signatures']
                voter_sigs = [sig[v['key']]['signatures'] for v in ok_voters]
                every = set(target) | {p for v in voter_sigs for p in v}
                subsets = {
                    'all': None,
                    'not_opus_authored': author_probe_ids(every, authors, lambda a: model_of.get(a) != OPUS),
                    'haiku_authored': author_probe_ids(every, authors, lambda a: model_of.get(a) == HAIKU),
                    'not_own': author_probe_ids(every, authors, lambda a: a != trial['key'])}
                row['votes'] = {}
                for name, probes in subsets.items():
                    result = vote(target, voter_sigs, unstable, probes)
                    row['votes'][name] = {k: v for k, v in result.items() if k != 'divergent'}
                    row['votes'][name]['divergent'] = result['divergent'][:20]
                    row['votes'][name]['divergent_count'] = len(result['divergent'])
            rows.append(row)
    return rows, summarize(rows, report)


def flag_rows(rows, subset='all', where=lambda r: True):
    """(task, is_miss, is_flagged) for the eligible rows (enough voters, a reading) that where accepts."""
    return [(r['task'], not r['strict_pass'], bool(r['votes'][subset]['flagged'])) for r in rows
            if r.get('votes') and where(r)]


def summarize(rows, report):
    eligible = [r for r in rows if r.get('votes')]
    primary = flag_rows(rows)
    out = {'eligible_trials': len(eligible), 'trials': len(rows),
           'not_eligible': dict(Counter('error' if r.get('error') else f"voters_{r['voters']}" for r in rows
                                        if not r.get('votes'))),
           'primary (pre-registered)': verdict(primary)}
    beside = {}
    for subset in ('all', 'not_opus_authored', 'haiku_authored', 'not_own'):
        for label, where in (('every target', lambda r: True),
                             ('non-Haiku targets', lambda r: r['model'] != HAIKU),
                             ('Sonnet low concise and ModelPilot arms', lambda r: r['model'] != HAIKU and (
                                 'low-concise' in r['arm'] or r['arm'].startswith('modelpilot')))):
            flagged = flag_rows(rows, subset, where)
            if flagged:
                beside[f'{subset} / {label}'] = dict(tally(flagged), within_task=within_task(flagged))
    out['readings'] = beside
    tasks = sorted({r['task'] for r in eligible})
    out['per_task'] = {t: tally([x for x in primary if x[0] == t]) for t in tasks}
    out['leave_one_task_out'] = {t: tally([x for x in primary if x[0] != t]) for t in tasks
                                 if any(x[1] for x in primary if x[0] == t)}
    out['tomli'] = out['per_task'].get('tomli-decode-error-attrs')
    probes = Counter()
    for r in eligible:
        v = r['votes']['all']
        probes['usable'] += v['usable']
        probes['voting'] += v['voting']
        probes['divergent'] += v['divergent_count']
    out['probes_per_eligible_trial'] = {k: round(n / len(eligible), 1) for k, n in probes.items()} if eligible else {}
    out['no_majority_share'] = round(1 - probes['voting'] / probes['usable'], 3) if probes['usable'] else None
    out['price'] = price(rows)
    return out


def price(rows):
    """Recorded costs only: the mean cold-equivalent cost of a Haiku 5.5 dev trial (a voter, as recorded) by arm, and
    the deployed check at that cost times the voters plus a late fix per flag (item 7), per trial and per miss caught.
    The probe generator isn't priced: these probes were free (recorded peer tests)."""
    costs = defaultdict(list)
    for r in rows:
        if r['model'] == HAIKU and r.get('cost') is not None:
            costs[r['arm']].append(r['cost'])
    flagged = flag_rows(rows)
    out = {'haiku_trial_usd_by_arm': {a: round(sum(c) / len(c), 4) for a, c in sorted(costs.items())}}
    every = [c for cs in costs.values() for c in cs]
    if every and flagged:
        voter = sum(every) / len(every)
        n = len(flagged)
        rate = sum(f for _, _, f in flagged) / n
        caught = sum(f for _, m, f in flagged if m)
        per_trial = 3 * voter + rate * LATE_FIX_USD
        out.update(voters=3, voter_usd=round(voter, 4), flag_rate=round(rate, 3), usd_per_trial=round(per_trial, 4),
                   usd_per_miss_caught=round(per_trial * n / caught, 3) if caught else None, late_fix_usd=LATE_FIX_USD)
    return out


def module_hashes():
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (Path(__file__), PLUGIN)}


def main():
    from .bench import code_revision, rates
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='step', required=True)
    r = sub.add_parser('run', help='$0: every dev task\'s probes on every counted dev trial\'s tree (runs agent code)')
    r.add_argument('--out', type=Path, required=True, help='a new directory')
    r.add_argument('--jobs', type=int, default=6)
    s = sub.add_parser('score', help='$0: the votes, the pre-registered verdict and the readings')
    s.add_argument('out', type=Path)
    s.add_argument('--out-file', type=Path, help='default: <out>/score.json (never overwritten)')
    args = parser.parse_args()
    if args.step == 'score':
        report = json.loads((args.out/'readings.json').read_text())
        rows, summary = score_report(report)
        summary = {'run_modules': report.get('modules'), 'score_modules': module_hashes(), 'code': report.get('code'),
                   **summary}
        target = args.out_file or args.out/'score.json'
        with Path(target).open('x') as f:
            json.dump({'summary': summary, 'rows': rows}, f, indent=1)
            f.write('\n')
        print(json.dumps({k: summary[k] for k in ('eligible_trials', 'primary (pre-registered)', 'tomli')}, indent=1))
        print(target)
        return
    if args.out.exists():
        parser.error(f'{args.out} exists: use a new path')
    if not (CROSSCHECK/'peer_tests.json').exists():
        raise SystemExit(f'{CROSSCHECK} is needed: the recorded peer tests are the probes.')
    trials = dev_trials(rates())
    if not trials:
        raise SystemExit('No counted dev trials under runs/: the dev split\'s bench runs are needed.')
    tasks = {t['id']: t for t in bench_tasks.load()}
    args.out.mkdir(parents=True)  # a new directory: never overwrite evidence
    (args.out/'plan.json').write_text(json.dumps({'modules': module_hashes(), 'code': code_revision(),
                                                  'trials': len(trials), 'jobs': args.jobs}, indent=1) + '\n')
    report = collect(trials, tasks, args.out, bench_tasks.interpreter(), lambda task: bench_tasks.REPOS/task['repo'],
                     recorded_peer_tests(), args.jobs, meta={'modules': module_hashes(), 'code': code_revision()})
    print(f"dev: {len(report['trials'])} trials, {len(report['pool_errors'])} probe-pool errors, "
          f"{report['seconds']} s -> {args.out/'readings.json'}")


if __name__ == '__main__':
    main()
