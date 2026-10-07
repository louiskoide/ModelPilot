"""Plan item 7, $0: how exposed a change is, should it turn out wrong. A miss's damage has no measured price (the
benchmark has no users), so this measures what a host can compute from the change itself:

- public API touched: the definitions the change's source lines fall in, each with the shortest import path that
  reaches it (the nearest ancestor package that re-exports its top-level name, found by importing the tree), and
  whether that path and the rest of its name are public: no _private part (dunders count as public), and listed in
  __all__ wherever the importing module has one.
- call sites: references to each changed definition by name across the repository's own code, source and tests
  apart, outside the definition itself (Name and Attribute loads; a constructor is referenced through its class, and
  any other dunder, which runs implicitly, counts its class's references too and is marked implicit). Matching by
  name over-counts common method names, so it is an upper bound within the repository; outside users are unknown.
- existing test coverage: the task's own suite, at the base's tests (the change's test edits are dropped), run once
  under modelpilot/line_trace.py: which changed executable lines run inside a test, which only outside one (imports,
  collection), which never; and how many tests run each changed definition at all.

Changes are the reference fix of each task and the saved fixes on record (spec_tests.saved_fixes: complete,
eligible, strictly graded trials). Tuning tasks only. Applying a diff, importing the tree and running its tests
execute agent-written code, as grading does: not an OS sandbox, trusted task repositories only.

    python3 -m modelpilot.exposure --out runs/exposure-<ts> [--tasks a,b] [--fixes none|failed|miss-tasks|all] [--jobs 4]

--fixes: none (reference fixes only), failed (also every strict failure on record), miss-tasks (the default: also
every saved fix of a task with any strict failure), all (every saved fix of the tasks).
"""
import argparse
import ast
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from . import bench_tasks, regrade

ROOT = Path(__file__).resolve().parents[1]
TRACER = Path(__file__).resolve().with_name('line_trace.py')
TIMEOUT = 600  # the grader's 120 s, with room for tracing
HUNK = re.compile(r'^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')
CONSTRUCTORS = ('__init__', '__new__')
FIXES = ('none', 'failed', 'miss-tasks', 'all')


def is_private(name):
    return name.startswith('_') and not (name.startswith('__') and name.endswith('__'))


def changed_lines(tree):
    """parse_diff of a workspace's changes against its base commit, new files included."""
    subprocess.run(['git', 'add', '-A', '-N'], cwd=tree, capture_output=True, check=True)
    return parse_diff(subprocess.run(['git', 'diff', '-U0', '--no-color', '--no-ext-diff', '--no-renames', 'HEAD'],
                                     cwd=tree, capture_output=True, text=True, check=True).stdout)


def parse_diff(diff):
    """{path: {'added': [new-file lines], 'deleted_at': [new-file lines a deletion follows], 'deleted': count}} from
    a unified diff without renames; a deleted file has 'deleted_file'."""
    out, key, old, left = {}, None, None, 0
    for line in diff.splitlines():
        if line.startswith('\\'):  # "\ No newline at end of file" isn't one of a hunk's lines
            continue
        if left > 0:  # inside a hunk: its removed and added lines, whatever they start with
            left -= 1
            if line.startswith('-') and key:
                out[key]['deleted'] += 1
        elif line.startswith('diff --git '):
            key = old = None
        elif line.startswith('--- '):
            old = line[6:] if line.startswith('--- a/') else None
        elif line.startswith('+++ '):
            key = line[6:] if line.startswith('+++ b/') else old
            out[key] = {'added': [], 'deleted_at': [], 'deleted': 0}
            if not line.startswith('+++ b/'):
                out[key]['deleted_file'] = True
        elif key and line.startswith('@@'):
            match = HUNK.match(line)
            removed, start, count = int(match.group(1) or 1), int(match.group(2)), int(match.group(3) or 1)
            left = removed + count
            if not out[key].get('deleted_file'):
                out[key]['added'].extend(range(start, start + count))
                if count == 0:
                    out[key]['deleted_at'].append(start)
    return out


def definitions(text):
    """Every class and function reachable by name, methods and nested classes included: {'name': qualified name,
    'kind', 'start' (first decorator), 'end'}; None when the file doesn't parse here. Whatever a function defines
    inside itself is part of that function, not a definition of its own: nothing outside can import it."""
    try:
        module = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    out = []

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + child.name
                out.append({'name': name, 'kind': 'class' if isinstance(child, ast.ClassDef) else 'function',
                            'start': min([child.lineno] + [d.lineno for d in child.decorator_list]),
                            'end': child.end_lineno})
                if isinstance(child, ast.ClassDef):
                    walk(child, name + '.')
            else:
                walk(child, prefix)
    walk(module, '')
    return out


def enclosing(defs, line):
    """The innermost definition holding a line, or None at module level."""
    holding = [d for d in defs if d['start'] <= line <= d['end']]
    return max(holding, key=lambda d: d['start']) if holding else None


def module_name(path, task):
    """The dotted module a tree path imports as, under the task's PYTHONPATH root; None outside it."""
    try:
        parts = list(Path(path).relative_to(task.get('pythonpath') or '').with_suffix('').parts)
    except ValueError:
        return None
    if parts and parts[-1] == '__init__':
        parts.pop()
    return '.'.join(parts) if parts and all(p.isidentifier() for p in parts) else None


_INDEX = {}  # (path, sha256) -> {identifier: [lines]}: Name and Attribute loads of one file


def name_index(path, data):
    key = (str(path), hashlib.sha256(data).hexdigest())
    if key not in _INDEX:
        index = {}
        try:
            for node in ast.walk(ast.parse(data)):
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                    index.setdefault(node.id, []).append(node.lineno)
                elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                    index.setdefault(node.attr, []).append(node.lineno)
        except (SyntaxError, ValueError):
            index = None
        _INDEX[key] = index
    return _INDEX[key]


def references(tree, task, changed):
    """For each changed definition: references by name in the repository's source and test files, outside the
    definition itself. changed: [{'file', 'name', 'start', 'end'}]; adds 'references' and 'implicit' to each."""
    files = []
    for path in sorted(Path(tree).rglob('*.py')):
        relative = path.relative_to(tree).as_posix()
        if relative.startswith(('.git/', regrade.EDGE_DIR + '/')):
            continue
        files.append((relative, regrade.is_test_path(relative, task['test_dir']), name_index(relative, path.read_bytes())))
    for d in changed:
        parts = d['name'].split('.')
        last = parts[-1]
        dunder = last.startswith('__') and last.endswith('__')
        d['implicit'] = dunder and last not in CONSTRUCTORS
        ident = parts[-2] if dunder and len(parts) > 1 else last  # a constructor or dunder is reached through its class
        counts = {'source': 0, 'source_files': 0, 'tests': 0, 'test_files': 0, 'unparsed_files': 0, 'identifier': ident}
        for relative, is_test, index in files:
            if index is None:
                counts['unparsed_files'] += 1
                continue
            lines = [n for n in index.get(ident, ()) if not (relative == d['file'] and d['start'] <= n <= d['end'])]
            if lines:
                kind = 'tests' if is_test else 'source'
                counts[kind] += len(lines)
                counts['test_files' if is_test else 'source_files'] += 1
        d['references'] = counts
    return changed


API_SCRIPT = r'''
import importlib, json, sys
out = {}
for module, name in json.loads(sys.argv[1]):
    key = module + ':' + name
    try:
        target = importlib.import_module(module)
        obj = getattr(target, name)
    except Exception as error:
        out[key] = {'error': type(error).__name__}
        continue
    parts = module.split('.')
    for i in range(1, len(parts) + 1):
        package = '.'.join(parts[:i])
        try:
            holder = importlib.import_module(package)
        except Exception:
            continue
        if getattr(holder, name, None) is obj:
            listed = getattr(holder, '__all__', None)
            out[key] = {'import_path': package + '.' + name,
                        'listed_in_all': None if listed is None else name in listed}
            break
print(json.dumps(out))
'''


def api(tree, task, python, scratch, names):
    """{'module:name': {'import_path', 'listed_in_all'} or {'error'}} for top-level names, by importing the tree."""
    if not names:
        return {}
    home, tmp = bench_tasks.isolated_dirs(Path(scratch)/'api-env')
    env = bench_tasks.clean_env(python, home, tmp)
    if task.get('pythonpath'):
        env['PYTHONPATH'] = os.path.abspath(Path(tree)/task['pythonpath'])
    try:
        result = subprocess.run([python, '-c', API_SCRIPT, json.dumps(sorted(names))], cwd=tree, env=env,
                                capture_output=True, text=True, timeout=TIMEOUT)
        return json.loads(result.stdout.strip().splitlines()[-1]) if result.returncode == 0 else {'error': 'import_failed'}
    except (subprocess.TimeoutExpired, ValueError, IndexError):
        return {'error': 'import_failed'}


def trace(tree, task, python, scratch, watch):
    """The task's suite under line_trace.py, watching {tree path: lines}: its output, or {'error'}."""
    command = task['suite_command']
    if command[:2] != ['{python}', '-m']:
        return {'error': 'suite_not_a_module'}
    scratch = Path(scratch)
    spec, out = scratch/'trace-spec.json', scratch/'trace-out.json'
    spec.write_text(json.dumps({'files': {str((Path(tree)/p).resolve()): sorted(lines) for p, lines in watch.items()}}))
    home, tmp = bench_tasks.isolated_dirs(scratch/'trace-env')
    env = bench_tasks.clean_env(python, home, tmp)
    if task.get('pythonpath'):
        env['PYTHONPATH'] = os.path.abspath(Path(tree)/task['pythonpath'])
    argv = [python, str(TRACER), str(spec.resolve()), str(out.resolve()), *command[1:]]
    started = time.monotonic()
    try:
        run = subprocess.run(argv, cwd=tree, env=env, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return {'error': 'timeout'}
    if not out.exists():
        return {'error': 'no_trace', 'exit_code': run.returncode, 'output_tail': (run.stdout + run.stderr)[-1000:]}
    result = json.loads(out.read_text())
    tests_run, failed, _ = bench_tasks.parse_results(command, run.returncode, run.stdout + run.stderr)
    result['suite'] = {'exit_code': run.returncode, 'tests_run': tests_run, 'tests_failed': failed,
                       'seconds': round(time.monotonic() - started, 1)}
    return result


def keep_source_only(tree, task):
    """Undo a change's test edits so the suite is the base's: test paths back to the base commit (new ones removed).
    Returns the paths undone."""
    subprocess.run(['git', 'add', '-A', '-N'], cwd=tree, capture_output=True, check=True)
    changed = subprocess.run(['git', 'diff', '--name-only', '--no-renames', 'HEAD'], cwd=tree, capture_output=True,
                             text=True, check=True).stdout.split()
    tracked = set(subprocess.run(['git', 'ls-tree', '-r', '--name-only', 'HEAD'], cwd=tree, capture_output=True,
                                 text=True, check=True).stdout.split())
    undone = [p for p in changed if regrade.is_test_path(p, task['test_dir'])]
    for path in undone:
        if path in tracked:
            subprocess.run(['git', 'checkout', 'HEAD', '--', path], cwd=tree, capture_output=True, check=True)
        else:
            subprocess.run(['git', 'rm', '-q', '--cached', '--', path], cwd=tree, capture_output=True)
            (Path(tree)/path).unlink(missing_ok=True)
    return undone


def change_tree(task, scratch, diff=None, repo=None):
    """The base workspace with a change applied: a saved fix's diff, or (diff None) the reference fix. Returns
    (tree, error)."""
    repo = repo or bench_tasks.REPOS/task['repo']
    tree = bench_tasks.workspace(task, repo, Path(scratch)/'work')
    if diff is None:
        patch = Path(scratch)/'reference.diff'
        patch.write_bytes(bench_tasks.git(repo, 'diff', '--binary', task['base'], task['reference'], text=False))
        diff = patch
    if Path(diff).exists() and Path(diff).stat().st_size:
        applied = subprocess.run(['git', 'apply', '--binary', '--whitespace=nowarn', str(Path(diff).resolve())],
                                 cwd=tree, capture_output=True, text=True)
        if applied.returncode:
            return tree, 'diff_did_not_apply'
    return tree, None


def coverage_of(lines, executable, hits):
    """Changed lines read against one file's trace: executable ones run inside a test, only outside one, or never."""
    executable = set(executable or ())
    run = {int(k): v for k, v in hits.items()}
    mine = sorted(set(lines) & executable)
    inside = [n for n in mine if any(t for t in run.get(n, ()))]
    outside = [n for n in mine if n in run and n not in inside]
    return {'executable': len(mine), 'run_in_tests': len(inside), 'run_outside_tests_only': len(outside),
            'not_run': len(mine) - len(inside) - len(outside)}


def exposure(task, python, scratch, diff=None, repo=None):
    """One change's exposure (module docstring). diff: a saved fix's agent.diff; None for the reference fix.
    repo: the task's repository (default: its clone under work/)."""
    scratch = Path(scratch)
    tree, error = change_tree(task, scratch, diff, repo)
    if error:
        return {'error': error}
    undone = keep_source_only(tree, task)
    lines = changed_lines(tree)
    source = {p: v for p, v in lines.items() if p.endswith('.py') and not regrade.is_test_path(p, task['test_dir'])}
    out = {'files': {'source': sorted(source), 'other': sorted(p for p in lines if p not in source),
                     'test_edits_dropped': sorted(undone)},
           'lines': {'added': sum(len(v['added']) for v in source.values()),
                     'deleted': sum(v['deleted'] for v in source.values())},
           'definitions': []}
    changed, module_level, watch = {}, {}, {}
    for path, change in source.items():
        if change.get('deleted_file'):
            out['definitions'].append({'file': path, 'name': None, 'kind': 'deleted_file', 'deleted': change['deleted']})
            continue
        defs = definitions((tree/path).read_text(errors='replace'))
        if defs is None:
            out.setdefault('unparsed', []).append(path)
            continue
        watch[path] = set(change['added'])
        for line, kind in [(n, 'added') for n in change['added']] + [(max(1, n), 'deleted_at')
                                                                       for n in change['deleted_at']]:
            d = enclosing(defs, line)
            if d is None:
                level = module_level.setdefault(path, {'added': set(), 'deletions_at': 0})
                if kind == 'added':
                    level['added'].add(line)
                else:
                    level['deletions_at'] += 1
                continue
            entry = changed.setdefault((path, d['name']), dict(d, file=path, module=module_name(path, task),
                                                              top=d['name'].split('.')[0], lines=[], deletions_at=0))
            if kind == 'added':
                entry['lines'].append(line)
            else:
                entry['deletions_at'] += 1
            watch[path].update(range(d['start'], d['end'] + 1))
    defs = references(tree, task, list(changed.values()))
    found = api(tree, task, python, scratch, {(d['module'], d['top']) for d in defs if d['module']})
    if 'error' in found:
        out['api_error'], found = found['error'], {}
    traced = trace(tree, task, python, scratch, watch) if watch else {'error': 'nothing_to_watch'}
    files = traced.get('files') or {}

    def file_trace(path):
        return files.get(str((tree/path).resolve())) or {}
    for d in defs:
        where = found.get(f"{d['module']}:{d['top']}") if d['module'] else None
        rest = d['name'].split('.')[1:]
        if where and 'import_path' in where:
            path = '.'.join([where['import_path'], *rest])
            public = (not any(is_private(p) for p in path.split('.'))) and where.get('listed_in_all') is not False
        else:
            path, public = None, None
        ft = file_trace(d['file'])
        span = range(d['start'], d['end'] + 1)
        tests = {t for n in span for t in ft.get('hits', {}).get(str(n), ()) if t}
        out['definitions'].append({
            'file': d['file'], 'name': d['name'], 'kind': d['kind'], 'module': d['module'],
            'import_path': path, 'public': public, 'listed_in_all': (where or {}).get('listed_in_all'),
            'import_error': (where or {}).get('error'), 'implicit': d['implicit'], 'added': len(d['lines']),
            'deletions_at': d['deletions_at'], 'references': d['references'],
            'coverage': dict(coverage_of(d['lines'], ft.get('executable'), ft.get('hits', {})),
                             tests=len(tests)) if ft else None})
    for path, level in sorted(module_level.items()):
        ft = file_trace(path)
        out['definitions'].append({'file': path, 'name': None, 'kind': 'module_level', 'module': module_name(path, task),
                                   'added': len(level['added']), 'deletions_at': level['deletions_at'],
                                   'coverage': coverage_of(level['added'], ft.get('executable'), ft.get('hits', {}))
                                   if ft else None})
    totals = dict.fromkeys(('executable', 'run_in_tests', 'run_outside_tests_only', 'not_run'), 0)
    every_test = set()
    for path, change in source.items():
        ft = file_trace(path)
        if ft:
            for k, v in coverage_of(change['added'], ft.get('executable'), ft.get('hits', {})).items():
                totals[k] += v
            every_test |= {t for n in watch.get(path, ()) for t in ft.get('hits', {}).get(str(n), ()) if t}
    named = [d for d in out['definitions'] if d.get('name')]
    out['coverage'] = None if traced.get('error') else dict(totals, tests=len(every_test), suite=traced['suite'])
    out['trace_error'] = traced.get('error')
    out['public_api'] = sorted({d['import_path'] for d in named if d['public']})
    out['summary'] = {
        'definitions': len(named), 'public': sum(bool(d['public']) for d in named),
        'source_references': max((d['references']['source'] for d in named), default=0),
        'test_references': max((d['references']['tests'] for d in named), default=0),
        'changed_lines_run_in_tests': (None if out['coverage'] is None or not totals['executable'] else
                                       round(totals['run_in_tests'] / totals['executable'], 3)),
        'tests_running_changed_definitions': None if out['coverage'] is None else len(every_test)}
    return out


def changes(tasks, fixes, rates):
    """(task, label, diff or None) for each change to measure: the reference fixes, then saved fixes per --fixes."""
    from .spec_tests import saved_fixes
    out = [(t, {'change': 'reference'}, None) for t in tasks]
    if fixes == 'none':
        return out
    by_id = {t['id']: t for t in tasks}
    saved = saved_fixes(set(by_id), rates)
    missed = {trial.parts[-3] for trial, strict in saved if not strict}
    for trial, strict in saved:
        task_id = trial.parts[-3]
        if fixes == 'failed' and strict or fixes == 'miss-tasks' and task_id not in missed:
            continue
        label = {'change': 'fix', 'run': trial.parts[-4], 'arm': trial.parts[-2], 'trial': int(trial.parts[-1]),
                 'strict': strict}
        out.append((by_id[task_id], label, trial/'agent.diff'))
    return out


def summarize(rows):
    """Per task: the reference fix's summary, and the saved fixes' summaries split by strict verdict."""
    out = {}
    for row in rows:
        task = out.setdefault(row['task'], {'reference': None, 'strict_failures': [], 'strict_passes': []})
        brief = (dict(row['exposure']['summary'], public_api=row['exposure']['public_api'])
                 if 'summary' in row['exposure'] else {'error': row['exposure'].get('error')})
        if row['change'] == 'reference':
            task['reference'] = brief
        else:
            task['strict_passes' if row['strict'] else 'strict_failures'].append(brief)
    return out


def main():
    from .bench import rates
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--tasks', help='comma-separated task ids (default: every tuning task)')
    parser.add_argument('--fixes', choices=FIXES, default='miss-tasks')
    parser.add_argument('--jobs', type=int, default=4)
    args = parser.parse_args()
    tasks = bench_tasks.load(args.tasks) if args.tasks else [t for t in bench_tasks.load()
                                                             if bench_tasks.split_of(t['id']) == 'tuning']
    if args.tasks and set(args.tasks.split(',')) - {t['id'] for t in tasks}:
        parser.error('No task spec for ' + ', '.join(sorted(set(args.tasks.split(',')) - {t['id'] for t in tasks})))
    final = [t['id'] for t in tasks if bench_tasks.split_of(t['id']) != 'tuning']
    if final:
        parser.error(f'Tuning tasks only: {", ".join(final)}')
    args.out.mkdir(parents=True, exist_ok=False)  # a new directory per run: evidence is never overwritten
    python = bench_tasks.interpreter()
    work = changes(tasks, args.fixes, rates())
    scratch = args.out/'scratch'

    def one(item):
        index, (task, label, diff) = item
        try:
            return exposure(task, python, scratch/str(index), diff)
        except (subprocess.CalledProcessError, OSError) as error:
            return {'error': type(error).__name__}
        finally:
            shutil.rmtree(scratch/str(index), ignore_errors=True)
    started = time.monotonic()
    with ThreadPoolExecutor(max(1, args.jobs)) as pool:
        results = list(pool.map(one, enumerate(work)))
    shutil.rmtree(scratch, ignore_errors=True)
    rows = [dict(label, task=task['id'], exposure=result) for (task, label, _), result in zip(work, results)]
    report = {'about': __doc__.split('\n\n')[0], 'python': python, 'fixes': args.fixes,
              'seconds': round(time.monotonic() - started, 1), 'tasks': summarize(rows), 'changes': rows}
    with (args.out/'exposure.json').open('x') as f:
        json.dump(report, f, indent=1)
        f.write('\n')
    print(json.dumps({t: v['reference'] for t, v in report['tasks'].items()}, indent=1))
    print(args.out/'exposure.json')


if __name__ == '__main__':
    main()
