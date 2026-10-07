"""Plan item 7, the measured part: what a miss costs to fix once someone notices it late.

Each strict failure on record (spec_tests.saved_fixes: complete, eligible, strictly graded trials of tuning tasks, by
the policy's models) ships: the task's base workspace with the agent's diff applied and committed, the task's
instruction as the commit message. A fresh Claude Code session on a fixed arm then gets a bug report the host writes
from one failing test, and nothing else; no model writes the report. Its tree is graded as a benchmark trial is
(hidden grader and edge suite), so a repair that clears the report but leaves another failure, or breaks something,
is still a miss. The session's cost is that miss's late fix.

A lower bound on a late fix: the report gives the failing call, what came back and the test around it, and the culprit
is the latest commit. Real symptoms surface further downstream, with less to go on and other changes landed on top.
The damage meanwhile isn't measured here (docs/m6-benchmark-plan.md, "Exposure and break-even damage").

    python3 -m modelpilot.late_fix prepare --out runs/late-fix-<ts> [--tasks a,b] [--models m,n] [--jobs 4]
    python3 -m modelpilot.late_fix run runs/late-fix-<ts> [--arm sonnet-5.5-low-concise] [--auth subscription]
        [--seed 0] [--live --limit-usd 5]

prepare ($0, no model request): grades each task's reference fix first (bench.preflight), then each shipped miss with
the test output kept. A miss that no longer fails strictly is left out (not_reproduced). The reported test is the
first failing one: hidden tests, then the edge suite, then the repository's suite. reports/<item>.md holds the
reporter's test with the module-level definitions it uses, transitively (from the test file that ran, which isn't in
the agent's checkout), and the test runner's own account of the failure, paths made relative. plan.json lists every
miss, ready or why not. Read the reports before a live run.

run: without --live, prints the plan: sessions, limits, the stop threshold and an estimate from the arm's own costs on
these tasks. With --live, one session per ready miss in a seeded order, with a benchmark trial's limits and isolation
(bench.Trial) and no retries. It stops on a rate limit (HTTP 429) or an account error, and starts no session once
known spend as sent reaches --limit-usd (a stopping threshold, not a billing cap). Results go to repairs/ and
summary.json in the prepared directory, once: another run needs a new preparation. Subscription sessions log in with
a claude setup-token token (hidden prompt) and their dollars are API-key equivalent, as in the benchmark.

Applying a diff and running tests executes agent-written code, as grading does: not an OS sandbox, trusted task
repositories only.
"""
import argparse
import ast
from concurrent.futures import ThreadPoolExecutor
import getpass
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import statistics
import subprocess
import tempfile
import time
from . import bench, bench_report, bench_tasks, regrade, spec_tests, switch_policy

ROOT = Path(__file__).resolve().parents[1]
ARM = 'sonnet-5.5-low-concise'  # the cheapest setting at equal strict passes, where the policy starts
PREAMBLE = ('You are working in a Python repository in the current directory. Its latest commit is a change that has '
            'shipped, and a bug report has come in against it. Fix the bug. Run the relevant tests with python3 before '
            'you finish.\n\nBug report:\n\n')
SOURCES = ('hidden', 'edge', 'suite')  # the order a reported test is picked in
FIXTURES = ('setUp', 'tearDown', 'setUpClass', 'tearDownClass', 'asyncSetUp', 'asyncTearDown', 'setup_method',
            'teardown_method', 'setup_class', 'teardown_class')
SHIPPED_DATE = '2000-01-02T00:00:00Z'  # a day after the base commit


def policy_models():
    return sorted({m for m, _ in switch_policy.settings(switch_policy.load())})


def misses(task_ids, models, rates):
    """Every strict failure on record of these tasks by these models: (trial directory, record)."""
    return [(trial, record) for trial, strict, record in spec_tests.saved_fixes(task_ids, rates, with_records=True)
            if not strict and record.get('model') in models]


def item_id(trial_dir):
    """run/task/arm/trial: where the miss is on record."""
    return '/'.join(Path(trial_dir).parts[-4:])


# The report: the runner's account of one failure, and the reporter's test.

def failure_block(output, test_id, command):
    """The test runner's own account of one failing test: unittest's FAIL/ERROR blocks for it (one per failing
    subtest), or pytest's entry under FAILURES (or ERRORS). None when the output has none."""
    lines = bench_tasks.uncolored(output).splitlines()
    if bench_tasks.is_pytest(command):
        name = '.'.join(test_id.split('::')[1:])  # pytest heads an entry with Class.test[params]
        heads = [i for i, line in enumerate(lines) if pytest_title(line)]
        bounds = [i for i, line in enumerate(lines) if pytest_title(line) or re.fullmatch(r'=+( .* )?=+', line)]
        for i in heads:
            if pytest_title(lines[i]) in (name, f'ERROR at setup of {name}', f'ERROR at teardown of {name}'):
                end = next((b for b in bounds if b > i), len(lines))
                return '\n'.join(lines[i:end]).strip()
        return None
    blocks = []
    for i, line in enumerate(lines):
        found = re.match(r'(?:FAIL|ERROR): (\S+ \([^)]*\))', line)
        if found and bench_tasks.unittest_id(found.group(1)) == test_id:
            end = next((j for j in range(i + 1, len(lines)) if lines[j] == '=' * 70 or
                        (lines[j] == '-' * 70 and j + 1 < len(lines) and lines[j + 1].startswith('Ran '))), len(lines))
            blocks.append('\n'.join(lines[i:end]).strip())
    return '\n\n'.join(blocks) or None


def pytest_title(line):
    """The title of a pytest entry's header line (___ test_x[a] ___), or None; the _ _ _ line between an entry's
    frames has none."""
    found = re.fullmatch(r'_+ (.+?) _+', line)
    return found.group(1) if found and set(found.group(1)) - {'_', ' '} else None


def test_location(test_id, block, command):
    """(file relative to the tree, [Class, ]function) of a failing test; None if the block doesn't say."""
    if bench_tasks.is_pytest(command):
        path, *names = test_id.split('::')
        return (path, [re.sub(r'\[.*\]$', '', n) for n in names]) if names else None
    found = re.fullmatch(r'(\w+) \((\S+)\)', test_id)
    if not found:
        return None
    method, owner = found.groups()
    frame = re.search(r'^\s*File "([^"]+)", line \d+, in ' + re.escape(method) + '$', block or '', re.M)
    return (frame.group(1), [owner.split('.')[-1], method]) if frame else None


def _roots(target):
    """The names an assignment target binds or changes: x, x.attr and x[i] all count as x."""
    while isinstance(target, (ast.Attribute, ast.Subscript, ast.Starred)):
        target = target.value
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [n for e in target.elts for n in _roots(e)]
    return []


def _defines(node):
    """Module-level names a statement defines or changes (a method call on a name, x.add(...), changes x)."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [(a.asname or a.name).split('.')[0] for a in node.names]
    if isinstance(node, ast.Assign):
        return [n for t in node.targets for n in _roots(t)]
    if isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        return _roots(node.target)
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute):
        return _roots(node.value.func.value)
    return []


def _names(node):
    names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):  # pytest fixtures arrive by parameter name
        names |= {a.arg for a in node.args.args + node.args.kwonlyargs}
    return names


def _start(node):
    return min([node.lineno] + [d.lineno for d in getattr(node, 'decorator_list', [])])


def reproduction(tree, location):
    """The failing test's source with every module-level definition it uses, transitively, in file order: what a
    reporter would paste so the failure can be run. A unittest method comes in its class with the class's own
    statements, fixtures (setUp and the like) and the helper methods it calls on self. None when the file or the
    test can't be found."""
    if not location:
        return None
    path, names = location
    file = Path(tree)/path
    if not file.is_file() or '..' in Path(path).parts or Path(path).is_absolute():
        return None
    text = file.read_text()
    module, lines = ast.parse(text), text.splitlines()
    by_name = {n.name: (i, n) for i, n in enumerate(module.body)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    if names[0] not in by_name:
        return None
    index, target = by_name[names[0]]
    members = None
    if len(names) > 1:  # a method: keep it, the class's statements and fixtures, and the methods it calls on self
        methods = {m.name: m for m in target.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}
        if names[-1] not in methods:
            return None
        keep = {names[-1]} | (set(FIXTURES) & set(methods))
        queue = list(keep)
        while queue:
            calls = {a.attr for a in ast.walk(methods[queue.pop()]) if isinstance(a, ast.Attribute)
                     and isinstance(a.value, ast.Name) and a.value.id in ('self', 'cls')}
            queue += [c for c in calls & set(methods) if c not in keep]
            keep |= calls & set(methods)
        members = [m for m in target.body if not isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) or m.name in keep]
        used = set().union(*(_names(m) for m in members), *(_names(b) for b in target.bases + target.decorator_list))
    else:
        used = _names(target)
    defined = {}
    for i, node in enumerate(module.body):
        for name in _defines(node):
            defined.setdefault(name, []).append(i)
    chosen = {i for i, n in enumerate(module.body) if isinstance(n, ast.ImportFrom) and n.module == '__future__'}
    queue = list(used)
    while queue:
        for i in defined.get(queue.pop(), []):
            if i != index and i not in chosen:
                chosen.add(i)
                queue += list(_names(module.body[i]))
    segment = lambda node: '\n'.join(lines[_start(node) - 1:node.end_lineno])
    text, last = '', None
    for i in sorted(chosen | {index}):
        node = module.body[i]
        imports = isinstance(node, (ast.Import, ast.ImportFrom))
        text += '' if last is None else '\n' if imports and last else '\n\n'
        if i != index or members is None:
            text += segment(node) + '\n'
        else:
            head = '\n'.join(lines[_start(target) - 1:_start(target.body[0]) - 1])
            text += head + '\n' + '\n\n'.join(segment(m) for m in members) + '\n'
        last = imports
    return text


def report(test_id, block, reproducer):
    """The bug report the host writes: no model takes part."""
    name = test_id.split('::')[-1] if '::' in test_id else test_id.split(' ')[0]
    text = f'A test of the reporter\'s, `{name}`, fails against the latest commit.\n\n'
    if reproducer:
        text += ('The test, with what it uses from their test module (their tests are not part of this checkout):\n\n'
                 f'```python\n{reproducer}```\n\n')
    return text + f'What they got:\n\n```\n{block}\n```\n'


def relative(text, tree):
    """Paths as the agent's checkout has them: the graded tree's prefix dropped, the host's own replaced."""
    return text.replace(str(Path(tree).resolve()) + '/', '').replace(str(tree) + '/', '').replace(str(ROOT), '<host>')


# Prepare ($0).

def apply_diff(trial_dir, work):
    """The miss's saved diff applied to a base workspace; the error, or None."""
    diff = Path(trial_dir)/'agent.diff'
    if diff.exists() and diff.stat().st_size:
        applied = subprocess.run(['git', 'apply', '--binary', '--whitespace=nowarn', str(diff.resolve())], cwd=work,
                                 capture_output=True, text=True)
        if applied.returncode:
            return applied.stderr[-500:]
    return None


def examine(trial_dir, task, python, scratch, expected_hidden_passed=None):
    """One shipped miss re-graded strictly with the test output kept, and its report."""
    edge_root = bench.EDGE_ROOT  # the benchmark's own, so the strict verdict is the one trials get
    scratch = Path(scratch)
    repo = bench.task_repo(task)
    work = bench_tasks.workspace(task, repo, scratch/'work')
    error = apply_diff(trial_dir, work)
    if error:
        return {'status': 'diff_did_not_apply', 'detail': error}
    graded = bench_tasks.grade(task, work, repo, python, scratch/'grade', expected_hidden_passed, keep_output=True)
    tree = scratch/'grade'/'graded'
    runs = {'hidden': (task['hidden_command'], graded['hidden']), 'suite': (task['suite_command'], graded['suite'])}
    if regrade.edge_files(task['id'], edge_root):
        runs['edge'] = (regrade.EDGE_COMMAND, regrade.run_edge(task, tree, python, scratch/'edge-env', edge_root,
                                                               keep_output=True))
    strict = graded['passed'] and ('edge' not in runs or regrade.edge_all_passed(runs['edge'][1]))
    out = {'grade_reason': graded['reason'], 'strict_now': strict,
           'failing_tests': {s: runs[s][1]['failing_tests'] for s in SOURCES if s in runs}}
    if strict:
        return dict(out, status='not_reproduced')
    picked = next(((s, t) for s in SOURCES if s in runs for t in sorted(runs[s][1]['failing_tests'])), None)
    if not picked:
        return dict(out, status='no_failing_test')
    source, test_id = picked
    command, result = runs[source]
    block = failure_block(result['output'], test_id, command)
    if not block:
        return dict(out, status='no_failure_block', reported_test=test_id, reported_source=source)
    block = relative(block, tree)
    reproducer = reproduction(tree, test_location(test_id, block, command))
    return dict(out, status='ready', reported_test=test_id, reported_source=source, reproducer=bool(reproducer),
                report=report(test_id, block, reproducer))


def prepare(out, found, tasks, python=None, jobs=4):
    """found: the misses, (trial directory, record); tasks: specs by ID. Writes plan.json and the reports."""
    out = Path(out)
    out.mkdir(parents=True)  # a new directory: never overwrite evidence
    python = python or bench_tasks.interpreter()
    involved = [tasks[t] for t in sorted({record['task'] for _, record in found})]
    with tempfile.TemporaryDirectory(prefix='late-fix-preflight-') as scratch:
        expected = bench.preflight(involved, python, scratch)  # stops at a reference that fails here
    scratch = out/'scratch'

    def one(item):
        index, (trial, record) = item
        try:
            return examine(trial, tasks[record['task']], python, scratch/str(index),
                           expected[record['task']]['hidden_passed'])
        finally:
            shutil.rmtree(scratch/str(index), ignore_errors=True)
    with ThreadPoolExecutor(max(1, jobs)) as pool:
        results = list(pool.map(one, enumerate(found)))
    shutil.rmtree(scratch, ignore_errors=True)
    plan = {'python': bench.python_version(python), 'code': bench.code_revision(), 'preamble': PREAMBLE,
            'shipped_message': 'the task instruction', 'reference_preflight': expected, 'items': []}
    for (trial, record), result in zip(found, results):
        diff = Path(trial)/'agent.diff'
        item = {'id': item_id(trial), 'trial_dir': str(trial), 'task': record['task'], 'arm': record['arm'],
                'trial': record['trial'], 'model': record.get('model'), 'effort': record.get('effort'),
                'spec_sha256': bench_tasks.spec_hash(tasks[record['task']]),
                'miss_cost_usd': bench_report.cold_cost(record),
                'shipped_diff_sha256': hashlib.sha256(diff.read_bytes() if diff.exists() else b'').hexdigest()}
        text = result.pop('report', None)
        item.update(result)
        if text:
            path = out/'reports'/f"{item['id']}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            item.update(report=str(path.relative_to(out)), report_sha256=hashlib.sha256(text.encode()).hexdigest(),
                        report_bytes=len(text.encode()))
        plan['items'].append(item)
    plan['counts'] = {status: sum(i['status'] == status for i in plan['items'])
                      for status in sorted({i['status'] for i in plan['items']})}
    with (out/'plan.json').open('x') as f:
        json.dump(plan, f, indent=2)
        f.write('\n')
    return plan


# Run (live, behind --live).

def ship(work, trial_dir, message):
    """Commit the miss on top of the base, what the late fix starts from; its commit. A diff that no longer applies
    raises: a harness fault, not a result."""
    error = apply_diff(trial_dir, work)
    if error:
        raise RuntimeError(f'shipped diff did not apply: {error}')
    env = dict(bench_tasks.clean_env(), **dict(bench_tasks.GIT_IDENTITY, GIT_AUTHOR_DATE=SHIPPED_DATE,
                                               GIT_COMMITTER_DATE=SHIPPED_DATE))
    for args in (['add', '-A'], ['commit', '-q', '--allow-empty', '-m', message]):
        subprocess.run(['git', *args], cwd=work, env=env, check=True, capture_output=True)
    return subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=work, capture_output=True, text=True,
                          check=True).stdout.strip()


class LateFixTrial(bench.Trial):
    """A benchmark trial whose workspace starts at the shipped miss and whose prompt is the bug report. Its diff
    (agent.diff) is the repair alone, taken against the shipped commit."""
    def __init__(self, item, report_text, task, arm_id, trial_dir, cli, key, upstream, price_table, **options):
        super().__init__(task, arm_id, trial_dir, cli, key, upstream, price_table, **options)
        self.item = item
        self.prompts = [PREAMBLE + report_text]
        self.record['late_fix'] = {k: item.get(k) for k in ('id', 'trial_dir', 'arm', 'trial', 'model', 'effort',
                                                            'reported_test', 'reported_source', 'report_sha256',
                                                            'shipped_diff_sha256', 'miss_cost_usd')}

    def setup(self):
        super().setup()
        self.base_commit = ship(self.work, self.item['trial_dir'], self.task['instruction'])
        self.record['late_fix']['shipped_commit'] = self.base_commit

    def finish(self, stopped=None):
        record = super().finish(stopped)
        record['late_fix'].update(reported_test_fixed=reported_fixed(record, self.item),
                                  repair_scope=diff_scope(self.dir/'agent.diff', self.task['test_dir']))
        (self.dir/'trial.json').write_text(json.dumps(record, indent=2) + '\n')
        return record


def reported_fixed(record, item):
    """Whether the reported test passes after the repair; None when its run can't say (timeout, tests not run)."""
    test, source = item['reported_test'], item['reported_source']
    grade = record.get('grade') or {}
    if grade.get('reason') in (None, 'grader_timeout', 'hidden_tests_not_run'):
        return None
    if source == 'edge':
        edge = record.get('edge') or {}
        return test not in edge['failing_tests'] if edge.get('tests_run') else None
    failing = grade.get('failing_tests') or []
    if test.split('::')[0] in failing:  # its module didn't import: a collection error
        return False
    return test not in failing


def diff_scope(diff, test_dir):
    """Lines and files the repair changed, source apart from tests, read from its diff alone."""
    out = dict.fromkeys(('source_files', 'test_files', 'source_lines', 'test_lines'), 0)
    if not (diff.exists() and diff.stat().st_size):
        return out
    stat = subprocess.run(['git', 'apply', '--numstat', str(diff)], capture_output=True, text=True, check=True).stdout
    for line in stat.splitlines():
        added, deleted, path = line.split('\t', 2)
        kind = 'test' if regrade.is_test_path(path, test_dir) else 'source'
        out[f'{kind}_files'] += 1
        out[f'{kind}_lines'] += sum(int(n) for n in (added, deleted) if n.isdigit())
    return out


def row(record):
    late = record.get('late_fix') or {}
    return {'id': late.get('id'), 'task': record['task'], 'reported_test': late.get('reported_test'),
            'complete': record.get('complete'), 'excluded_reason': record.get('excluded_reason'),
            'ineligible_reason': bench_report.ineligible_reason(record),
            'stop': (record.get('client') or {}).get('stop'), 'requests': (record.get('accounting') or {}).get('requests'),
            'wall_seconds': record.get('wall_seconds'), 'cost_usd': bench_report.cold_cost(record),
            'miss_cost_usd': late.get('miss_cost_usd'), 'repaired': bool(record.get('passed')),
            'grade_reason': (record.get('grade') or {}).get('reason'), 'edge_passed': (record.get('grade') or {}).get('edge_passed'),
            'reported_test_fixed': late.get('reported_test_fixed'), 'repair_scope': late.get('repair_scope')}


def aggregate(rows):
    done = [r for r in rows if r['complete'] and not r['ineligible_reason']]  # effort and prompt reached the wire
    priced = [r['cost_usd'] for r in done if r['cost_usd'] is not None]
    walls = [r['wall_seconds'] for r in done if r['wall_seconds'] is not None]
    return {'sessions': len(rows), 'complete': len(done), 'repaired': sum(r['repaired'] for r in done),
            'reported_test_fixed': sum(r['reported_test_fixed'] is True for r in done),
            'priced': len(priced), 'cost_usd': None if not priced else {
                'mean': round(statistics.mean(priced), 6), 'median': round(statistics.median(priced), 6),
                'min': round(min(priced), 6), 'max': round(max(priced), 6), 'total': round(sum(priced), 6)},
            'mean_wall_seconds': round(statistics.mean(walls), 1) if walls else None,
            'unknown_cost': [r['id'] for r in done if r['cost_usd'] is None]}


def summarize(records):
    rows = [row(r) for r in records]
    return {'cost_basis': 'cold-equivalent; API-key equivalent for subscription sessions', 'rows': rows,
            'all': aggregate(rows), 'tasks': {t: aggregate([r for r in rows if r['task'] == t])
                                              for t in sorted({r['task'] for r in rows})}}


def estimate(items, arm, rates):
    """A session per miss at the arm's own mean cost on its task (cold-equivalent; complete, strictly graded trials
    on record): an estimate, None for a task the arm has no priced trial of."""
    tasks = {i['task'] for i in items}
    costs = {}
    for _, _, record in spec_tests.saved_fixes(tasks, rates, with_records=True):
        if record['arm'] == arm and bench_report.cold_cost(record) is not None:
            costs.setdefault(record['task'], []).append(bench_report.cold_cost(record))
    per_task = {t: round(statistics.mean(costs[t]), 4) if t in costs else None for t in sorted(tasks)}
    known = [per_task[i['task']] for i in items if per_task[i['task']] is not None]
    return {'per_task': per_task, 'total_usd': round(sum(known), 2), 'sessions_priced': len(known)}


def run(out, cli, key, upstream, price_table, *, arm=ARM, auth='subscription', oauth_token=None, seed=0,
        limit_usd=None, client_version=None, tasks=None, trial_factory=None, **limits):
    """One session per ready miss, in a seeded order; results into out/repairs and out/summary.json, once."""
    out = Path(out)
    plan = json.loads((out/'plan.json').read_text())
    tasks = tasks or {t['id']: t for t in bench_tasks.load()}
    ready = [i for i in plan['items'] if i['status'] == 'ready']
    changed = [i['id'] for i in ready if bench_tasks.spec_hash(tasks[i['task']]) != i['spec_sha256']]
    if changed:
        raise ValueError(f'Task specs changed since preparing: {changed}')
    order = sorted(ready, key=lambda i: i['id'])
    random.Random(seed).shuffle(order)
    (out/'repairs').mkdir()  # once per preparation: never into earlier results
    manifest = {'arm': arm, 'arm_spec': bench.ARMS[arm], 'auth': auth, 'seed': seed, 'order': [i['id'] for i in order],
                'limit_usd': limit_usd, 'limits': limits, 'client': str(cli), 'client_version': client_version,
                'code': bench.code_revision(), 'preamble': PREAMBLE, 'pass_rule': bench.PASS_RULE,
                'note': 'Stop thresholds are not billing caps. No retries.'}
    with (out/'repairs'/'manifest.json').open('x') as f:
        json.dump(manifest, f, indent=2)

    def default_factory(item):
        report_text = (out/item['report']).read_text()
        if hashlib.sha256(report_text.encode()).hexdigest() != item['report_sha256']:
            raise ValueError(f"{item['report']} changed since preparing")
        subscription = auth == 'subscription'
        return LateFixTrial(item, report_text, tasks[item['task']], arm, out/'repairs'/item['id'], cli, key, upstream,
                            price_table, client_version=client_version, auth=auth,
                            oauth_token=oauth_token if subscription else None,
                            expected_hidden_passed=plan['reference_preflight'][item['task']]['hidden_passed'], **limits)
    factory = trial_factory or default_factory
    trials, reason, error = [], None, None
    spend = lambda: sum(t.known_cost() for t in trials)
    try:
        for item in order:
            if any(t.account_error for t in trials):
                reason = 'anthropic_account_error'
            elif any(t.rate_limited for t in trials):
                reason = 'rate_limited'
            elif limit_usd is not None and spend() >= limit_usd:
                reason = 'limit'
            if reason:
                break
            trial = factory(item)
            trials.append(trial)
            try:
                trial.step()  # one session, then grading
            finally:
                trial.close()
            record = trial.record
            print(item['id'], 'REPAIRED' if record.get('passed') else 'NOT REPAIRED',
                  bench_report.cold_cost(record), flush=True)
    except BaseException as e:
        error = type(e).__name__
        raise
    finally:
        records = [t.record for t in trials if t.finished]
        summary = dict(summarize(records), stopped=reason, error=error, known_spend_as_sent_usd=round(spend(), 6),
                       not_run=[i['id'] for i in order[len(trials):]],
                       complete=reason is None and error is None and len(records) == len(order))
        with (out/'summary.json').open('x') as f:
            json.dump(summary, f, indent=2)
            f.write('\n')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='step', required=True)
    p = sub.add_parser('prepare')
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--tasks', help='comma-separated tuning tasks (default: every tuning task)')
    p.add_argument('--models', default=','.join(policy_models()), help='models whose misses ship (default: the policy\'s)')
    p.add_argument('--jobs', type=int, default=4)
    r = sub.add_parser('run')
    r.add_argument('out', type=Path)
    r.add_argument('--arm', default=ARM, help='a fixed benchmark arm (default: %(default)s)')
    r.add_argument('--auth', choices=bench.AUTHS, default='subscription')
    r.add_argument('--seed', type=int, default=0)
    r.add_argument('--max-turns', type=int, default=30)
    r.add_argument('--budget', type=float, default=1.0, help='per-session client stop threshold, not a billing cap')
    r.add_argument('--limit-usd', type=float, help='start no session once known spend as sent reaches this (needed with --live)')
    r.add_argument('--claude', type=Path)
    r.add_argument('--live', action='store_true', help='required to send requests')
    args = parser.parse_args()
    table = bench.rates()
    specs = {t['id']: t for t in bench_tasks.load()}
    if args.step == 'prepare':
        wanted = args.tasks.split(',') if args.tasks else [t for t in specs if bench_tasks.split_of(t) == 'tuning']
        unknown = [t for t in wanted if t not in specs]
        final = [t for t in wanted if t in specs and bench_tasks.split_of(t) != 'tuning']
        if unknown or final:
            parser.error(f'Tuning tasks only: {", ".join(unknown + final)}')
        found = misses(set(wanted), set(args.models.split(',')), table)
        try:
            plan = prepare(args.out, found, specs, jobs=args.jobs)
        except bench.PreflightError as e:
            raise SystemExit(str(e))
        print(json.dumps(plan['counts']))
        for item in plan['items']:
            print(item['id'], item['status'], item.get('reported_test') or '')
        print(args.out/'plan.json')
        return
    if bench.ARMS.get(args.arm, {}).get('kind') != 'fixed':
        parser.error(f'--arm takes a fixed arm: {", ".join(a for a, v in bench.ARMS.items() if v["kind"] == "fixed")}')
    plan = json.loads((args.out/'plan.json').read_text())
    ready = [i for i in plan['items'] if i['status'] == 'ready']
    if not args.live:
        guess = estimate(ready, args.arm, table)
        stop = (f'stop threshold ${args.limit_usd:.2f} of known spend as sent' if args.limit_usd is not None
                else 'no stop threshold yet (--live needs --limit-usd)')
        print(f'Prepared {len(ready)} late-fix sessions of {len(plan["items"])} misses on {args.arm}, {args.auth}; at most '
              f'{args.max_turns} turns and ${args.budget:.2f} per session, {stop}. Estimate ${guess["total_usd"]:.2f} '
              f'({guess["sessions_priced"]} sessions priced at the arm\'s mean cost on their task: {guess["per_task"]}).'
              ' Thresholds are not billing caps. No retries. Read the reports, then add --live.')
        if args.auth == 'subscription':
            print('Subscription: the client logs in with a claude setup-token token (hidden prompt with --live) and asks '
                  'for 1h cache writes; dollars are API-key equivalent. A usage or rate limit (HTTP 429) stops the run.')
        return
    if args.limit_usd is None:
        raise SystemExit('--live needs --limit-usd: known spend at which no further session starts.')
    if (args.out/'repairs').exists():
        raise SystemExit(f'{args.out} has been run already; prepare a new directory.')
    cli, version = bench.resolve_client(args.claude or shutil.which('claude') or 'claude')
    if bench.client_problem(version):
        raise SystemExit(bench.client_problem(version) + ' No requests sent.')
    python = bench_tasks.interpreter()
    if bench.python_version(python) != plan['python']:
        raise SystemExit(f'The bench interpreter is not the Python {plan["python"]} the misses were prepared with.')
    with tempfile.TemporaryDirectory(prefix='late-fix-preflight-') as scratch:
        try:
            bench.preflight(sorted((specs[t] for t in {i['task'] for i in ready}), key=lambda t: t['id']), python, scratch)
        except bench.PreflightError as e:
            raise SystemExit(str(e))
    try:
        bench_tasks.lock_benchmark_environment(python)
    except (OSError, ValueError) as e:
        raise SystemExit(f'Cannot lock benchmark dependencies: {e}')
    key = oauth_token = None
    if args.auth == 'api_key':
        key = os.environ.get('ANTHROPIC_API_KEY') or getpass.getpass('Anthropic API key (hidden): ').strip()
        problem = bench.check_anthropic_key(key)
        if problem:
            raise SystemExit(problem + ' No requests sent.')
    else:
        oauth_token = (os.environ.get('CLAUDE_CODE_OAUTH_TOKEN') or
                       getpass.getpass('Claude subscription token from `claude setup-token` (hidden): ').strip())
        if not oauth_token or any(c.isspace() for c in oauth_token):
            raise SystemExit('Missing or malformed subscription token. No requests sent.')
    print(f'Running {len(ready)} late-fix sessions with {cli} ({version}); stop at ${args.limit_usd:.2f}; results: '
          f'{args.out/"repairs"}', flush=True)
    summary = run(args.out, cli, key, 'https://api.anthropic.com', table, arm=args.arm, auth=args.auth,
                  oauth_token=oauth_token, seed=args.seed, limit_usd=args.limit_usd, client_version=version, tasks=specs,
                  max_turns=args.max_turns, budget_usd=args.budget, python=python)
    print(json.dumps({k: summary[k] for k in ('all', 'tasks', 'stopped', 'known_spend_as_sent_usd')}, indent=1))


if __name__ == '__main__':
    main()
