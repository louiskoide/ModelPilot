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
        source = sum(a + d for p, a, d in files if p.startswith(source_prefix))
        tests = sum(a + d for p, a, d in files if p.startswith(test_prefix))
        other = [p for p, _, _ in files if not p.startswith((source_prefix, test_prefix))
                 and not p.endswith(('.md', '.rst', '.txt'))]
        if commit['parent'] and 0 < source <= max_source_lines and tests > 0 and not other:
            found.append({'commit': commit['sha'], 'date': commit['date'], 'subject': commit['subject'],
                          'source_lines': source, 'test_lines': tests,
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
            archive.extract(member, destination)
    return destination


def workspace(task, repo, destination):
    """Agent checkout: base tree as a fresh one-commit repository, so history cannot leak the fix."""
    export(repo, task['base'], destination)
    env = dict(clean_env(), GIT_AUTHOR_NAME='bench', GIT_AUTHOR_EMAIL='bench@localhost',
               GIT_COMMITTER_NAME='bench', GIT_COMMITTER_EMAIL='bench@localhost',
               GIT_AUTHOR_DATE='2000-01-01T00:00:00Z', GIT_COMMITTER_DATE='2000-01-01T00:00:00Z')
    for args in (['init', '-q'], ['add', '-A'], ['commit', '-q', '-m', 'Task base']):
        subprocess.run(['git', *args], cwd=destination, env=env, check=True, capture_output=True)
    return destination


def clean_env(python=None):
    """Test environment without provider credentials or user Python settings.

    With python given, its directory leads PATH so `python3`/`pytest` resolve to it.
    """
    keep = ('PATH', 'LANG', 'LC_ALL', 'HOME', 'TMPDIR', 'SYSTEMROOT')
    env = {k: v for k, v in os.environ.items() if k in keep}
    env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONHASHSEED='0')
    if python:
        env['PATH'] = os.pathsep.join([str(Path(python).parent), env.get('PATH', '/usr/bin:/bin')])
    return env


def parse_results(command, code, output):
    """Tests run, whether they genuinely failed, and failing test IDs, for unittest or pytest.

    A usage error, crash, empty collection or timeout is not a test failure: it proves nothing
    about the task. A pytest collection error counts, because hidden tests that import a
    missing name fail that way (unittest reports the same case as an ERROR).
    """
    if any('pytest' in part for part in command):
        summaries = re.findall(r'^[=\s]*((?:\d+ \w+(?:, )?)+) in [\d.]+s\b', output, re.M)
        counts = {kind: int(n) for n, kind in re.findall(r'(\d+) (\w+)', summaries[-1])} if summaries else {}
        run = sum(v for k, v in counts.items() if k != 'deselected')
        failed = counts.get('failed', 0) + counts.get('error', 0) + counts.get('errors', 0)
        collection = code == 2 and 'error during collection' in output
        ids = re.findall(r'^(?:FAILED|ERROR) (\S+)', output, re.M)
        return run, (code == 1 and failed > 0) or collection, sorted(set(ids))
    ran = re.search(r'^Ran (\d+) tests?', output, re.M)
    return (int(ran.group(1)) if ran else 0, code == 1 and bool(ran) and bool(re.search(r'^FAILED \(', output, re.M)),
            sorted(set(re.findall(r'^(?:FAIL|ERROR): (\S+ \([^)]*\))', output, re.M))))


def python_satisfies(python, minimum):
    if not minimum:
        return True
    version = subprocess.run([python, '-c', 'import sys; print("%d.%d" % sys.version_info[:2])'],
                             capture_output=True, text=True, check=True).stdout.strip()
    return tuple(map(int, version.split('.'))) >= tuple(map(int, minimum.split('.')))


def run_tests(command, tree, python, pythonpath=None):
    env = clean_env(python)
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
    return {'exit_code': code, 'seconds': round(time.monotonic() - started, 3), 'tests_run': run,
            'tests_failed': failed, 'failing_tests': ids,
            'output_tail': output[-2000:], 'output_sha256': hashlib.sha256(output.encode()).hexdigest()}


def grade(task, tree, repo, python, scratch):
    """Hidden grader: agent's tree with the whole test directory replaced by the reference version."""
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
    hidden = run_tests(task['hidden_command'], graded, python, task.get('pythonpath'))
    suite = run_tests(task['suite_command'], graded, python, task.get('pythonpath'))
    passed = all(r['exit_code'] == 0 and r['tests_run'] > 0 for r in (hidden, suite))
    reason = 'passed' if passed else ('hidden_tests_failed' if hidden['exit_code'] != 0 else 'suite_failed')
    return {'passed': passed, 'reason': reason, 'hidden': hidden, 'suite': suite}


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
    base = export(repo, task['base'], scratch/'base')
    reference = export(repo, task['reference'], scratch/'reference')
    base_suite = run_tests(task['suite_command'], base, python, task.get('pythonpath'))
    base_grade = grade(task, base, repo, python, scratch/'grade-base')
    reference_grades = [grade(task, reference, repo, python, scratch/f'grade-ref-{i}') for i in range(repeats)]
    checks = {'base_suite_passes': base_suite['exit_code'] == 0 and base_suite['tests_run'] > 0,
              'base_fails_hidden_tests': base_grade['hidden']['tests_failed'],
              'reference_passes_every_time': all(g['passed'] for g in reference_grades)}
    shutil.rmtree(scratch)
    return {'valid': all(checks.values()), 'checks': checks,
            'seconds': {'base_suite': base_suite['seconds'],
                        'reference_grade': max(g['hidden']['seconds'] + g['suite']['seconds'] for g in reference_grades)},
            'base_failing_tests': base_grade['hidden']['failing_tests']}


def load(task_id=None):
    specs = [json.loads(p.read_text()) for p in sorted(TASKS.glob('*/task.json'))]
    return [s for s in specs if task_id is None or s['id'] == task_id]


def spec_hash(task):
    return hashlib.sha256(json.dumps(task, sort_keys=True).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    mine = commands.add_parser('mine', help='List candidate fix commits in a cloned repository')
    mine.add_argument('repo', type=Path)
    mine.add_argument('--source', required=True)
    mine.add_argument('--tests', required=True)
    mine.add_argument('--max-source-lines', type=int, default=40)
    mine.add_argument('--since', default='2019-01-01')
    check = commands.add_parser('validate', help='Validate committed task specs against their repositories')
    check.add_argument('--task')
    check.add_argument('--python', default=interpreter())
    args = parser.parse_args()
    if args.command == 'mine':
        for c in candidates(args.repo, args.source, args.tests, args.max_source_lines, args.since):
            print(json.dumps(c))
        return
    results = {}
    for task in load(args.task):
        result = validate(task, REPOS/task['repo'], args.python, ROOT/'work'/'bench'/'validate'/task['id'])
        results[task['id']] = dict(result, spec_sha256=spec_hash(task))
        print(task['id'], 'VALID' if result['valid'] else 'INVALID', json.dumps(result.get('checks')), flush=True)
    out = ROOT/'runs'/('bench-validate-' + time.strftime('%Y%m%d-%H%M%S') + '.json')
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({'python': sys.version.split()[0], 'tasks': results}, indent=2) + '\n')
    print('report:', out.relative_to(ROOT))
    if not results or not all(r['valid'] for r in results.values()):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
