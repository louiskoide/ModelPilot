"""Governor test adapter (built October 14 in the cloud batch, offline; off by default, dry-run): the task's test suite
run by the host, never read from the session's output, and recorded as a ledger observation fenced by the task revision
and by the hashes of the files it ran against.

The suite is declared in the governor's settings, never inferred: `MODELPILOT_SUITE` in the hooks' environment (a JSON
object), or `python -m modelpilot.hooks run-suite --spec FILE` from a coordinator. For example:

    {"name": "unit", "command": ["python3", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--junitxml=.mp-junit.xml"],
     "junit": ".mp-junit.xml", "timeout_seconds": 120, "watch": ["src/**/*.py", "tests/**/*.py"]}

- command: an argument list run without a shell, in the workspace (or `cwd` inside it), with no inherited credentials
  (the M3 test adapter's environment rule). Not an OS sandbox: declare only commands and repositories you trust.
- junit: a workspace-relative JUnit XML path the command writes; deleted before each run. With it, the outcome comes from
  the report's counts; without it, from the exit status alone (failures then unknown when it fails).
- watch: workspace-relative globs whose files the run is fenced on (default every .py file).

A result is `pass`, `fail` or `unknown`, never a pass by default: a command that doesn't start, a timeout, a report
missing or unreadable, no test run,
a nonzero exit with no failure in the report, or a watched file changing during the run is unknown. `land` records it
only if the watched files still hash as they did when the run started and the task's revision is the one it ran at with
the lease held (M2's own check), so a result for an older tree or a corrected task is refused, not landed. Observations
only: nothing is blocked, and no model, effort or context changes.
"""
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET

NAME = re.compile(r'[A-Za-z0-9_.-]{1,64}')
MAX_TIMEOUT = 600
MAX_WATCHED = 5000
MAX_FILE = 64 * 1024 * 1024
ENV_KEEP = ('PATH', 'LANG', 'LC_ALL', 'TMPDIR', 'SYSTEMROOT')  # as M3's test role: no provider credentials


def load_spec(value):
    """A validated suite declaration from a JSON string or dict; ValueError on anything else."""
    spec = json.loads(value) if isinstance(value, str) else value
    if not isinstance(spec, dict):
        raise ValueError('a suite declaration is a JSON object')
    unknown = set(spec) - {'name', 'command', 'cwd', 'junit', 'timeout_seconds', 'watch'}
    if unknown:
        raise ValueError(f'unknown suite fields: {sorted(unknown)}')
    command = spec.get('command')
    if not isinstance(command, list) or not command or not all(isinstance(a, str) and a for a in command):
        raise ValueError('command is a non-empty list of arguments (no shell)')
    if not isinstance(spec.get('name'), str) or not NAME.fullmatch(spec['name']):
        raise ValueError('name is 1-64 letters, digits, dots, dashes or underscores')
    timeout = spec.get('timeout_seconds', 120)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= MAX_TIMEOUT:
        raise ValueError(f'timeout_seconds is a number above 0, at most {MAX_TIMEOUT}')
    watch = spec.get('watch', ['**/*.py'])
    if not isinstance(watch, list) or not watch or not all(isinstance(w, str) and w for w in watch):
        raise ValueError('watch is a non-empty list of workspace-relative globs')
    for path in [spec.get('cwd', '.'), spec.get('junit') or '.', *watch]:
        if not isinstance(path, str) or Path(path).is_absolute() or '..' in Path(path).parts:
            raise ValueError('cwd, junit and watch stay inside the workspace')
    return {'name': spec['name'], 'command': list(command), 'cwd': spec.get('cwd', '.'), 'junit': spec.get('junit'),
            'timeout_seconds': timeout, 'watch': list(watch)}


def tree_hash(root, watch):
    """SHA-256 over every watched file's workspace-relative path and content hash, or None when the set is too large
    to hash (more than MAX_WATCHED files, or one over MAX_FILE): then nothing can be fenced and the result is unknown."""
    root = Path(root).resolve()
    paths = set()
    for pattern in watch:
        for found in glob.glob(str(root/pattern), recursive=True):
            path = Path(found)
            if path.is_file() and '.git' not in path.relative_to(root).parts:
                paths.add(path)
    if len(paths) > MAX_WATCHED:
        return None
    digest = hashlib.sha256()
    for path in sorted(paths):
        if path.stat().st_size > MAX_FILE:
            return None
        digest.update(path.relative_to(root).as_posix().encode() + b'\0' + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def junit_counts(path):
    """(tests, failures + errors, skipped) summed over the report's innermost test suites, or None if unreadable."""
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None
    suites = [s for s in root.iter('testsuite') if s.find('testsuite') is None] or [root]
    try:
        tests = sum(int(s.get('tests', 0)) for s in suites)
        failed = sum(int(s.get('failures', 0)) + int(s.get('errors', 0)) for s in suites)
        skipped = sum(int(s.get('skipped', 0)) for s in suites)
    except ValueError:
        return None
    return tests, failed, skipped


def run(spec, root):
    """Run the declared suite once in the workspace. Returns {'suite', 'outcome', 'failures', 'tests', 'exit_code',
    'seconds', 'tree_hash' (the watched files before the run), 'reason' (why unknown)}."""
    root = Path(root).resolve()
    out = {'suite': spec['name'], 'outcome': 'unknown', 'failures': None, 'tests': None, 'exit_code': None,
           'seconds': None, 'tree_hash': tree_hash(root, spec['watch']), 'reason': None}
    if out['tree_hash'] is None:
        return dict(out, reason='watched_set_too_large')
    junit = root/spec['junit'] if spec['junit'] else None
    if junit is not None:
        junit.unlink(missing_ok=True)  # never read a report an earlier run left
    env = {k: v for k, v in os.environ.items() if k in ENV_KEEP}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    started = time.monotonic()
    with tempfile.TemporaryFile() as output:
        try:
            process = subprocess.Popen(spec['command'], cwd=root/spec['cwd'], env=env, stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError:  # no such command or cwd: the suite never ran, which is no result, not a hook failure
            return dict(out, reason='did_not_start')
        try:
            out['exit_code'] = process.wait(timeout=spec['timeout_seconds'])
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            return dict(out, seconds=round(time.monotonic() - started, 3), reason='timeout')
    out['seconds'] = round(time.monotonic() - started, 3)
    if tree_hash(root, spec['watch']) != out['tree_hash']:
        return dict(out, reason='files_changed_during_run')
    if junit is None:
        return dict(out, outcome='pass', failures=0) if out['exit_code'] == 0 else dict(out, outcome='fail')
    counts = junit_counts(junit) if junit.exists() else None
    if counts is None:
        return dict(out, reason='no_junit' if not junit.exists() else 'bad_junit')
    tests, failed, skipped = counts
    out['tests'] = tests
    if failed:
        return dict(out, outcome='fail', failures=failed)
    if tests - skipped <= 0:
        return dict(out, reason='no_tests_ran')
    if out['exit_code'] != 0:
        return dict(out, reason='exit_code_without_failures')
    return dict(out, outcome='pass', failures=0)


def land(gov, task, revision, owner, spec, root, result):
    """Record a run as a ledger observation of `revision`, or refuse it: {'landed': True, 'observation',
    'recommendation'} or {'landed': False, 'reason'}. Refused when a watched file changed since the run started
    (files_changed_since_run) or M2 refuses the revision (stale_revision: corrected, cancelled, unacknowledged, or the
    lease lost). Either way the governor journals it (suite_result / suite_refused), applied False."""
    observation = {'suite': spec['name'], 'suite_outcome': result['outcome']}
    if result['tree_hash']:
        observation['tree_hash'] = result['tree_hash']
    if result['outcome'] != 'unknown' and result['failures'] is not None:
        observation['failures'] = result['failures']
    record = {k: result[k] for k in ('suite', 'outcome', 'failures', 'tests', 'exit_code', 'seconds', 'tree_hash',
                                     'reason')}
    if result['tree_hash'] and tree_hash(root, spec['watch']) != result['tree_hash']:
        gov.note('suite_refused', dict(record, refusal='files_changed_since_run'), task, revision)
        return {'landed': False, 'reason': 'files_changed_since_run'}
    try:
        recommendation = gov.observe(task, revision, owner, observation)
    except ValueError:
        gov.note('suite_refused', dict(record, refusal='stale_revision'), task, revision)
        return {'landed': False, 'reason': 'stale_revision'}
    gov.note('suite_result', record, task, revision)
    return {'landed': True, 'observation': observation, 'recommendation': recommendation}


def last_landed_tree(gov, task, revision):
    """The tree hash of the newest suite observation landed at this revision, or None."""
    found = gov.state.observations(task, revision, 'suite', limit=1)
    return found[0].get('tree_hash') if found else None


def run_and_land(gov, task, owner, spec, root, skip_unchanged=True):
    """One host run at the task's current revision, landed or refused. With skip_unchanged, a tree already observed at
    this revision isn't run again ({'skipped': 'unchanged'})."""
    revision = gov.state.get(task)['revision']
    try:
        gov.state._owned(task, revision, owner)  # M2's landing rule, checked first: a run that can't land isn't run
    except ValueError:
        return {'skipped': 'stale_revision', 'revision': revision}
    if skip_unchanged:
        current = tree_hash(root, spec['watch'])
        if current is not None and current == last_landed_tree(gov, task, revision):
            return {'skipped': 'unchanged', 'revision': revision}
    result = run(spec, root)
    return dict(land(gov, task, revision, owner, spec, root, result), result=result, revision=revision)
