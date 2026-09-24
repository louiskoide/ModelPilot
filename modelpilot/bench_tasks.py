"""M6 benchmark tasks mined from real repositories (SWE-bench style), with a shared grader.

A task is a repository at the parent of a real fix commit plus an instruction. The hidden
grader is the fix commit's own test directory: the agent's tree is graded with every test
file restored to the reference commit, so edited or deleted tests cannot help it. A task is
valid only if the base tree fails that grader and the reference tree passes it, repeatedly.

Checkouts are exported without history, so the agent cannot read the fix from git. Third-
party source lives in ignored work/bench/; only task metadata (bench/tasks/) is committed.
Grading runs without provider credentials. Test execution is not an OS sandbox.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import time

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT/'bench'/'tasks'
REPOS = ROOT/'work'/'bench'/'repos'
REQUIRED = ('id', 'repo', 'url', 'license', 'base', 'reference', 'instruction', 'type',
            'test_dir', 'hidden_tests', 'hidden_command', 'suite_command', 'source')
TYPES = ('bug_fix', 'feature', 'question')
TIMEOUT = 120
VENV = ROOT/'work'/'bench'/'py312'


def interpreter():
    """Benchmark Python: the local 3.12 venv when present (agent and grader share it), else this one."""
    candidate = VENV/'bin'/'python'
    return str(candidate) if candidate.exists() else sys.executable


def git(repo, *args, text=True):
    return subprocess.run(['git', '-C', str(repo), *args], capture_output=True, check=True, text=text).stdout


def candidates(repo, source_prefix, test_prefix, max_source_lines=40, since='2019-01-01'):
    """Non-merge commits that change a little source and some tests, and nothing else of substance."""
    log = git(repo, 'log', '--no-merges', f'--since={since}', '--format=@%H%x09%P%x09%ad%x09%s', '--date=short', '--numstat')
    found, current = [], None

    def flush(commit):
        if not commit:
            return
        files = commit['files']
        # Tests can live inside the package (toolz/tests/): they are never counted as source.
        source = sum(a + d for p, a, d in files if p.startswith(source_prefix) and not p.startswith(test_prefix))
        tests = sum(a + d for p, a, d in files if p.startswith(test_prefix))
        other = [p for p, _, _ in files if not p.startswith((source_prefix, test_prefix))
                 and not p.endswith(('.md', '.rst', '.txt'))]
        if commit['parent'] and 0 < source <= max_source_lines and tests > 0 and not other:
            found.append({'commit': commit['sha'], 'date': commit['date'], 'subject': commit['subject'],
                          'source_lines': source, 'test_lines': tests,
                          'source_files': sorted(p for p, _, _ in files if p.startswith(source_prefix) and not p.startswith(test_prefix)),
                          'test_files': sorted(p for p, _, _ in files if p.startswith(test_prefix))})
    for line in log.splitlines():
        if line.startswith('@'):
            flush(current)
            sha, parent, date, subject = line[1:].split('\t', 3)
            current = {'sha': sha, 'parent': parent, 'date': date, 'subject': subject, 'files': []}  # root commits have no base
        elif line.strip():
            added, deleted, path = line.split('\t', 2)
            if added != '-':
                current['files'].append((path, int(added), int(deleted)))
    flush(current)
    return found


# Python 3.14 filters extracted archives by default; ask for the same filter wherever it exists.
SAFE_TAR = {'filter': 'data'} if hasattr(tarfile, 'data_filter') else {}


def export(repo, commit, destination):
    """Write the tree at commit into destination with no git history."""
    destination = Path(destination)
    destination.mkdir(parents=True)
    data = subprocess.run(['git', '-C', str(repo), 'archive', '--format=tar', commit],
                          capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive.getmembers():
            if member.issym() or member.islnk() or member.name.startswith('/') or '..' in Path(member.name).parts:
                continue  # never follow links or escape the destination
            archive.extract(member, destination, **SAFE_TAR)
    return destination


def task_tree(task, repo, commit, destination):
    """Export a commit plus the task's setup files: what an editable install would generate
    (a version module, stub install metadata). They are part of every tree the grader or the
    agent sees, and never part of the agent's diff."""
    tree = export(repo, commit, destination)
    for relative, text in (task.get('setup_files') or {}).items():
        path = tree/relative
        if Path(relative).is_absolute() or '..' in Path(relative).parts:
            raise ValueError('setup file must stay inside the tree')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tree


def test_env_paths(task, tree):
    """PYTHONPATH entries the grader uses, and the agent gets, for this task."""
    return [str(Path(tree)/task['pythonpath'])] if task.get('pythonpath') else []


def workspace(task, repo, destination):
    """Agent checkout: base tree as a fresh one-commit repository, so history cannot leak the fix."""
    task_tree(task, repo, task['base'], destination)
    env = dict(clean_env(), GIT_AUTHOR_NAME='bench', GIT_AUTHOR_EMAIL='bench@localhost',
               GIT_COMMITTER_NAME='bench', GIT_COMMITTER_EMAIL='bench@localhost',
               GIT_AUTHOR_DATE='2000-01-01T00:00:00Z', GIT_COMMITTER_DATE='2000-01-01T00:00:00Z')
    for args in (['init', '-q'], ['add', '-A'], ['commit', '-q', '-m', 'Task base']):
        subprocess.run(['git', *args], cwd=destination, env=env, check=True, capture_output=True)
    return destination


def writable_site_packages(python):
    """site-packages directories an agent could install into (shared across trials)."""
    out = subprocess.run([python, '-c', 'import json, site; print(json.dumps(site.getsitepackages()))'],
                         capture_output=True, text=True, check=True).stdout
    return [d for d in json.loads(out) if Path(d).exists() and os.access(d, os.W_OK)]


def clean_env(python=None, home=None, tmp=None):
    """Test environment without provider credentials or user Python settings.

    With python given, its directory leads PATH so `python3`/`pytest` resolve to it. With home
    and tmp given, tests get those instead of the caller's HOME/TMPDIR.
    """
    keep = ('PATH', 'LANG', 'LC_ALL', 'HOME', 'TMPDIR', 'SYSTEMROOT')
    env = {k: v for k, v in os.environ.items() if k in keep}
    env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONHASHSEED='0', PYTHONNOUSERSITE='1')
    if python:
        env['PATH'] = os.pathsep.join([str(Path(python).parent), env.get('PATH', '/usr/bin:/bin')])
    if home:
        env['HOME'] = str(home)
    if tmp:
        env['TMPDIR'] = str(tmp)
    return env


PYTEST_RUN = ('passed', 'failed', 'error', 'errors', 'xfailed', 'xpassed')  # skips and warnings never ran


def is_pytest(command):
    return any('pytest' in part for part in command)


def pytest_counts(output):
    summaries = re.findall(r'^[=\s]*((?:\d+ \w+(?:, )?)+) in [\d.]+s\b', output, re.M)
    return {kind: int(n) for n, kind in re.findall(r'(\d+) (\w+)', summaries[-1])} if summaries else {}


def unittest_counts(output):
    """Tests run and passed from 'Ran N tests' plus the outcome line, e.g. 'OK (skipped=1)'."""
    ran = re.search(r'^Ran (\d+) tests?', output, re.M)
    if not ran:
        return {'run': 0, 'passed': 0, 'skipped': 0}
    outcome = re.search(r'^(?:OK|FAILED)(?: \(([^)]*)\))?\s*$', output, re.M)
    details = {k.strip(): int(v) for k, v in re.findall(r'([a-z ]+)=(\d+)', (outcome and outcome.group(1)) or '')}
    skipped = details.get('skipped', 0)
    not_passed = sum(details.get(k, 0) for k in ('failures', 'errors', 'expected failures', 'unexpected successes'))
    total = int(ran.group(1))
    return {'run': total - skipped, 'passed': max(0, total - skipped - not_passed), 'skipped': skipped}


def unittest_id(test_id):
    """One ID on every Python: 3.11+ appends the method name inside the parentheses."""
    match = re.fullmatch(r'(\S+) \((\S+)\.(\w+)\)', test_id)
    return f'{match.group(1)} ({match.group(2)})' if match and match.group(1) == match.group(3) else test_id


def uncolored(output):
    return re.sub(r'\x1b\[[0-9;]*m', '', output)  # some repos force colored output


def parse_results(command, code, output):
    """Tests run, whether they genuinely failed, and failing test IDs, for unittest or pytest.

    A usage error, crash, empty collection or timeout is not a test failure: it proves nothing
    about the task. A pytest collection error counts, because hidden tests that import a
    missing name fail that way (unittest reports the same case as an ERROR). Skipped tests
    and pytest warnings are not tests run.
    """
    output = uncolored(output)
    if is_pytest(command):
        counts = pytest_counts(output)
        run = sum(counts.get(k, 0) for k in PYTEST_RUN)
        failed = counts.get('failed', 0) + counts.get('error', 0) + counts.get('errors', 0)
        collection = code == 2 and 'error during collection' in output
        ids = re.findall(r'^(?:FAILED|ERROR) (\S+)', output, re.M)
        return run, (code == 1 and failed > 0) or collection, sorted(set(ids))
    ran = re.search(r'^Ran (\d+) tests?', output, re.M)
    ids = re.findall(r'^(?:FAIL|ERROR): (\S+ \([^)]*\))', output, re.M)
    return (unittest_counts(output)['run'], code == 1 and bool(ran) and bool(re.search(r'^FAILED \(', output, re.M)),
            sorted({unittest_id(i) for i in ids}))


def parse_counts(command, output):
    """Tests that passed and tests that were skipped."""
    output = uncolored(output)
    if is_pytest(command):
        counts = pytest_counts(output)
        return {'passed': counts.get('passed', 0), 'skipped': counts.get('skipped', 0)}
    counts = unittest_counts(output)
    return {'passed': counts['passed'], 'skipped': counts['skipped']}


def python_satisfies(python, minimum):
    if not minimum:
        return True
    version = subprocess.run([python, '-c', 'import sys; print("%d.%d" % sys.version_info[:2])'],
                             capture_output=True, text=True, check=True).stdout.strip()
    return tuple(map(int, version.split('.'))) >= tuple(map(int, minimum.split('.')))


def run_tests(command, tree, python, pythonpath=None, home=None, tmp=None):
    env = clean_env(python, home, tmp)
    if pythonpath:
        env['PYTHONPATH'] = str(Path(tree)/pythonpath)
    argv = [python if part == '{python}' else part for part in command]
    started = time.monotonic()
    try:
        result = subprocess.run(argv, cwd=tree, env=env, capture_output=True, text=True, timeout=TIMEOUT)
        code, output = result.returncode, result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        code, output = None, 'timeout'
    run, failed, ids = parse_results(command, code, output)
    counts = parse_counts(command, output)
    return {'exit_code': code, 'seconds': round(time.monotonic() - started, 3), 'tests_run': run,
            'tests_passed': counts['passed'], 'tests_skipped': counts['skipped'], 'tests_failed': failed, 'failing_tests': ids,
            'output_tail': output[-2000:], 'output_sha256': hashlib.sha256(output.encode()).hexdigest()}


def isolated_dirs(scratch):
    """Fresh HOME and TMPDIR for test runs, so nothing outside the tree changes the outcome."""
    dirs = (Path(scratch)/'home', Path(scratch)/'tmp')
    for path in dirs:
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True)
    return dirs


def grade_reason(hidden, suite, expected_hidden_passed=None):
    if hidden['exit_code'] is None or suite['exit_code'] is None:
        return 'grader_timeout'
    if hidden['exit_code'] != 0:
        return 'hidden_tests_failed'
    if hidden['tests_run'] == 0 or (expected_hidden_passed is not None and hidden['tests_passed'] < expected_hidden_passed):
        return 'hidden_tests_not_run'
    if suite['exit_code'] != 0 or suite['tests_run'] == 0:
        return 'suite_failed'
    return 'passed'


def grade(task, tree, repo, python, scratch, expected_hidden_passed=None):
    """Hidden grader: agent's tree with the whole test directory replaced by the reference version.

    Tests run with their own HOME/TMPDIR and no user site-packages. With expected_hidden_passed
    (the reference's count from a preflight), the tree must pass as many hidden tests as the
    reference did: a skipped hidden test was not passed.
    """
    scratch = Path(scratch)
    graded = scratch/'graded'
    if graded.exists():
        shutil.rmtree(graded)
    shutil.copytree(tree, graded, ignore=shutil.ignore_patterns('.git'), symlinks=True)
    test_dir = graded/task['test_dir']
    if test_dir.exists():
        shutil.rmtree(test_dir)
    reference = export(repo, task['reference'], scratch/'reference-tests')
    shutil.copytree(reference/task['test_dir'], test_dir)
    shutil.rmtree(reference)
    home, tmp = isolated_dirs(scratch)
    hidden = run_tests(task['hidden_command'], graded, python, task.get('pythonpath'), home=home, tmp=tmp)
    suite = run_tests(task['suite_command'], graded, python, task.get('pythonpath'), home=home, tmp=tmp)
    reason = grade_reason(hidden, suite, expected_hidden_passed)
    return {'passed': reason == 'passed', 'reason': reason, 'hidden': hidden, 'suite': suite,
            'expected_hidden_passed': expected_hidden_passed}


def validate(task, repo, python, scratch, repeats=3):
    """Base must fail the grader; reference must pass it every time; base's own suite must pass."""
    scratch = Path(scratch)
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    missing = [k for k in REQUIRED if k not in task]
    if missing or task['type'] not in TYPES:
        return {'valid': False, 'reason': 'bad_spec', 'missing': missing}
    if git(repo, 'rev-parse', task['reference'] + '^').strip() != task['base']:
        return {'valid': False, 'reason': 'base_is_not_reference_parent'}
    if not python_satisfies(python, task.get('requires_python')):
        return {'valid': False, 'reason': 'python_too_old', 'requires_python': task.get('requires_python')}
    base = task_tree(task, repo, task['base'], scratch/'base')
    reference = task_tree(task, repo, task['reference'], scratch/'reference')
    home, tmp = isolated_dirs(scratch/'base-env')
    base_suite = run_tests(task['suite_command'], base, python, task.get('pythonpath'), home=home, tmp=tmp)
    base_grade = grade(task, base, repo, python, scratch/'grade-base')
    reference_grades = [grade(task, reference, repo, python, scratch/f'grade-ref-{i}') for i in range(repeats)]
    checks = {'base_suite_passes': base_suite['exit_code'] == 0 and base_suite['tests_run'] > 0,
              'base_fails_hidden_tests': base_grade['hidden']['tests_failed'],
              'reference_passes_every_time': all(g['passed'] for g in reference_grades)}
    shutil.rmtree(scratch)
    return {'valid': all(checks.values()), 'checks': checks,
            'seconds': {'base_suite': base_suite['seconds'],
                        'reference_grade': max(g['hidden']['seconds'] + g['suite']['seconds'] for g in reference_grades)},
            'base_failing_tests': base_grade['hidden']['failing_tests'],
            # A reference that fails even once marks the task flaky; name the tests that did.
            'reference_failures': sorted({f for g in reference_grades if not g['passed']
                                          for f in g['hidden']['failing_tests'] + g['suite']['failing_tests']})}


def load(task_ids=None):
    """All task specs, or those named by an ID or comma-separated IDs."""
    specs = [json.loads(p.read_text()) for p in sorted(TASKS.glob('*/task.json'))]
    wanted = set(task_ids.split(',')) if task_ids else None
    return [s for s in specs if wanted is None or s['id'] in wanted]


def spec_hash(task):
    return hashlib.sha256(json.dumps(task, sort_keys=True).encode()).hexdigest()


SPLITS = ROOT/'bench'/'splits.json'
FINAL_REPOS = ('click', 'packaging', 'boltons', 'humanize')


def make_splits(tasks, final_repos=FINAL_REPOS):
    """Split by repository so no repository is in both sets; hash-lock the final set."""
    splits = {'tuning': {}, 'final': {}}
    for task in sorted(tasks, key=lambda t: t['id']):
        splits['final' if task['repo'] in final_repos else 'tuning'][task['id']] = spec_hash(task)
    lock = hashlib.sha256(json.dumps(splits['final'], sort_keys=True).encode()).hexdigest()
    return {'rule': 'split by repository; final task specs are hash-locked before any policy tuning',
            'final_repos': sorted(final_repos), 'tuning': splits['tuning'], 'final': splits['final'],
            'final_lock_sha256': lock}


def split_of(task_id, splits=None):
    splits = splits or json.loads(SPLITS.read_text())
    return 'final' if task_id in splits['final'] else 'tuning' if task_id in splits['tuning'] else None


def check_lock(tasks, splits=None):
    """Final specs must still hash to their locked values; returns the IDs that changed."""
    splits = splits or json.loads(SPLITS.read_text())
    current = {t['id']: spec_hash(t) for t in tasks}
    return sorted(i for i, h in splits['final'].items() if current.get(i) != h)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    mine = commands.add_parser('mine', help='List candidate fix commits in a cloned repository')
    mine.add_argument('repo', type=Path)
    mine.add_argument('--source', required=True)
    mine.add_argument('--tests', required=True)
    mine.add_argument('--max-source-lines', type=int, default=40)
    mine.add_argument('--since', default='2019-01-01')
    lock = commands.add_parser('split', help='Write bench/splits.json (split by repository, final set hash-locked)')
    lock.add_argument('--force', action='store_true', help='Overwrite an existing lock (only before any tuning)')
    check = commands.add_parser('validate', help='Validate committed task specs against their repositories')
    check.add_argument('--task')
    check.add_argument('--python', default=interpreter())
    check.add_argument('--jobs', type=int, default=1, help='Tasks validated in parallel')
    args = parser.parse_args()
    if args.command == 'split':
        if SPLITS.exists() and not args.force:
            raise SystemExit('bench/splits.json exists; relocking the final set needs --force and a reason in the commit.')
        splits = make_splits(load())
        SPLITS.write_text(json.dumps(splits, indent=1) + '\n')
        print(f"tuning {len(splits['tuning'])}, final {len(splits['final'])}, lock {splits['final_lock_sha256'][:12]}")
        return
    if args.command == 'mine':
        for c in candidates(args.repo, args.source, args.tests, args.max_source_lines, args.since):
            print(json.dumps(c))
        return
    from concurrent.futures import ThreadPoolExecutor
    tasks = load(args.task)
    results = {}

    def one(task):
        return task, validate(task, REPOS/task['repo'], args.python, ROOT/'work'/'bench'/'validate'/task['id'])
    with ThreadPoolExecutor(max(1, args.jobs)) as pool:  # each task has its own scratch dir
        for task, result in pool.map(one, tasks):
            results[task['id']] = dict(result, spec_sha256=spec_hash(task))
            print(task['id'], 'VALID' if result['valid'] else 'INVALID',
                  json.dumps(result.get('checks') or result.get('reason')), flush=True)
    out = ROOT/'runs'/('bench-validate-' + time.strftime('%Y%m%d-%H%M%S') + '.json')
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({'python': sys.version.split()[0], 'tasks': results}, indent=2) + '\n')
    print('report:', out.relative_to(ROOT))
    if not results or not all(r['valid'] for r in results.values()):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
