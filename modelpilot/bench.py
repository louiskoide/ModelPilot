"""M6 benchmark harness: repository tasks × arms × trials under identical limits.

Each trial gets a fresh history-free checkout, an isolated HOME/TMPDIR/Claude config, the
same tools, turn limit and stop threshold, and ModelPilot's proxy for wire accounting. After
the client exits, the shared hidden grader runs without provider credentials. Order is
randomized with a recorded seed. Without --live nothing is sent. Agents may run arbitrary
commands inside their checkout: this is not an OS sandbox, so only use trusted task repos.

Session shapes: 'single' is one prompt. 'followup' resumes the same session with FOLLOW_UP
after --gap seconds (0 keeps the prompt cache warm; 330 lets it expire); other trials run
while one waits. Before any request, each task's reference is graded once ($0), and a trial
must pass as many hidden tests as its reference did. Costs are reported cold-equivalent
(see bench_report) next to measured. The client binary is pinned for the whole run.

Jev arms (see bench_jev) run Jev's own proxy for the whole trial in front of the trial's
ModelPilot proxy, with the jev-router sentinel instead of --model. Their dollars are provider
cost only: router (TypeSafe) cost is unpriced, so they are a lower bound.
"""
import argparse
import getpass
import json
import os
from pathlib import Path
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from . import bench_jev, bench_report, bench_tasks
from .governed_session import client_env
from .jev_route_check import PATCH, TOKEN_FIELDS, check_anthropic_key, parse_events
from .proxy import ProxyServer

ROOT = Path(__file__).resolve().parents[1]
TOOLS = 'Read,Edit,Write,Bash,Glob,Grep'
PREAMBLE = ('You are working in a Python repository in the current directory. Complete the task below. '
            'Run the relevant tests with python3 before you finish.\n\nTask: ')
FOLLOW_UP = "Run the repository's full test suite and fix anything that fails because of your change."
SHAPES = ('single', 'followup')
ARMS = {
    'opus-5': {'kind': 'fixed', 'model': 'claude-opus-5'},
    'sonnet-5': {'kind': 'fixed', 'model': 'claude-sonnet-5'},
    'haiku-4.5': {'kind': 'fixed', 'model': 'claude-haiku-4-5-20251001'},
    # Jev picks the served model per turn; the client only sends the sentinel.
    'jev-stock': {'kind': 'jev', 'variant': 'stock', 'model': 'jev-router', 'checkout': 'work/jev-router-baseline',
                  'patch': None},
    'jev-compat': {'kind': 'jev', 'variant': 'compat', 'model': 'jev-router', 'checkout': 'work/jev-router-compat',
                   'patch': PATCH},
    # Registered so plans and reports name it; its launcher arrives with work item 4.
    'modelpilot': {'kind': 'modelpilot', 'policy': 'docs/m6-modelpilot-policy.md'},
}
RUNNABLE = ('fixed', 'jev')
IDLE_SECONDS = 130  # the proxy's upstream socket timeout (120 s) bounds any request still in flight
# Client result subtypes, pinned by the offline tests against Claude Code 2.1.281.
STOPS = {'error_max_turns': 'turn_limit', 'error_max_budget_usd': 'budget_stop'}
# Files outside the restored test directory that can change which tests run, or how.
TEST_CONFIG = ('conftest.py', 'pytest.ini', 'tox.ini', 'setup.cfg', 'pyproject.toml', 'sitecustomize.py', 'usercustomize.py')


class ClientChanged(RuntimeError):
    """The Claude Code binary no longer reports the version the run started with."""


class PreflightError(RuntimeError):
    """A task's reference solution fails its grader in this environment."""


def rates():
    """Rates for every benchmark model: the 5-family table plus M0's 4.6 entries."""
    merged = json.loads((ROOT/'configs/m0.json').read_text())['rates']
    merged.update(json.loads((ROOT/'configs/jev-rates.json').read_text())['rates'])
    return merged


def schedule(tasks, arms, trials, seed):
    order = [(t['id'], a, n) for t in tasks for a in arms for n in range(trials)]
    random.Random(seed).shuffle(order)
    return order


def accounting(rows, final, jev=False):
    """Wire accounting for one trial. Jev arms: provider cost only (router unpriced), and the
    client prices the jev-router sentinel with its own guess, so only its tokens are compared."""
    messages = [r for r in rows if r.get('kind') == 'messages']
    ok = [r for r in messages if r.get('http_status') == 200]
    # Sensitivity only: requests the API answered with an error, counted as free. A transport
    # failure or an unpriced success is never a rejection.
    answered = [r for r in messages if not (isinstance(r.get('http_status'), int) and r['http_status'] != 200)]
    if_free = sum(r['cost_usd'] for r in answered) if answered and all(r.get('cost_usd') is not None for r in answered) else None
    proxy_tokens = {wire: sum((r.get('usage') or {}).get(wire, 0) for r in ok) for wire, _ in TOKEN_FIELDS}
    usage = final.get('modelUsage') or {}
    client_tokens = {wire: sum(m.get(name, 0) for m in usage.values() if isinstance(m, dict)) for wire, name in TOKEN_FIELDS}
    unpriced = sum(r.get('cost_usd') is None for r in messages)
    known = sum(r['cost_usd'] for r in messages if r.get('cost_usd') is not None)
    client = final.get('total_cost_usd')
    out = {'requests': len(messages), 'http_statuses': [r.get('http_status') for r in messages],
           # Answered by the API with an error; a transport failure (no status) is not a rejection.
           'rejected_requests': sum(isinstance(r.get('http_status'), int) and r['http_status'] != 200 for r in messages),
           'transport_failures': sum(r.get('http_status') is None for r in messages),
           'models': [r.get('model') for r in messages], 'unpriced_requests': unpriced,
           # Unknown cost stays unknown: a trial with any unpriced request has no dollar total.
           'cost_usd': known if unpriced == 0 and messages else None, 'known_cost_usd': known,
           'cost_if_rejected_free_usd': if_free, 'rejected_assumption': 'API error responses counted as $0 (unconfirmed)',
           'cost_scope': 'provider_only_router_unpriced' if jev else 'complete',
           'proxy_tokens': proxy_tokens, 'client_tokens': client_tokens,
           'tokens_match': bool(usage) and proxy_tokens == client_tokens,
           'client_cost_usd': client,
           'client_cost_basis': 'sentinel_model_unknown_price' if jev else 'client_model_table',
           'client_cost_matches': None if jev else
           isinstance(client, (int, float)) and unpriced == 0 and abs(client - known) < 1e-6,
           'first_byte_seconds': [r.get('first_byte_seconds') for r in messages]}
    if jev:
        out['router_cost_usd'] = None
    return out


def stop_reason(status, final, rows):
    """How one client session ended."""
    if status == 'timeout':
        return 'timeout'
    subtype = final.get('subtype')
    if subtype == 'success' and not final.get('is_error'):
        return 'success'
    if subtype in STOPS:
        return STOPS[subtype]
    if any(r.get('status') in ('transport_error', 'connection_closed') for r in rows):
        return 'transport_error'
    if final.get('api_error_status'):
        return 'api_error'
    return 'client_error'


def test_config_changes(paths, test_dir):
    """Changed paths the grader does not restore that can change how tests run (flagged for review)."""
    inside = test_dir.rstrip('/') + '/'
    return [p for p in paths if Path(p).name in TEST_CONFIG and not p.startswith(inside)]


def budget_text(usd):
    return ('%.10f' % usd).rstrip('0').rstrip('.')


def client_command(cli, prompt, model, max_turns, budget_usd, session, extra=()):
    # The stop threshold applies per invocation: a resumed session gets its own. No model
    # (Jev arms) leaves the client on the sentinel the router's environment sets.
    return [str(cli), '-p', prompt, *(['--model', model] if model else []), '--output-format', 'stream-json', '--verbose',
            '--max-turns', str(max_turns), '--max-budget-usd', budget_text(budget_usd), '--setting-sources', '',
            '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--tools', TOOLS, '--allowedTools', TOOLS,
            *session, *extra]


def client_version(cli):
    return subprocess.run([str(cli), '--version'], capture_output=True, text=True, timeout=30).stdout.strip()


def resolve_client(cli):
    """The real binary behind a CLI path and its version. The installed `claude` is a symlink
    the auto-updater moves, so a run holds on to the binary it started with."""
    path = Path(cli).resolve(strict=True)
    return path, client_version(path)


def inside(path, root):
    return path == root or path.startswith(root + os.sep)


def leftover_processes(directory):
    """PIDs, other than this process, whose working directory is inside directory.

    Claude Code's Bash tool starts commands in their own process groups, so killing the
    client's group misses background jobs. Their working directory still gives them away.
    """
    root, found = os.path.realpath(directory), set()
    if os.path.isdir('/proc/self'):
        for entry in os.listdir('/proc'):
            if entry.isdigit():
                try:
                    if inside(os.readlink(f'/proc/{entry}/cwd'), root):
                        found.add(int(entry))
                except OSError:
                    continue
    else:
        out = subprocess.run(['lsof', '-a', '-d', 'cwd', '-u', str(os.getuid()), '-F', 'pn'],
                             capture_output=True, text=True).stdout
        pid = None
        for line in out.splitlines():
            if line.startswith('p'):
                pid = int(line[1:])
            elif line.startswith('n') and pid is not None and inside(line[1:], root):
                found.add(pid)
    found.discard(os.getpid())
    return sorted(found)


def reap(directory):
    """Kill leftover processes working inside directory; returns their PIDs."""
    pids = leftover_processes(directory)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return pids


def signal_group(pgid, sig):
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def drain(stream, chunks):
    for chunk in iter(lambda: stream.read1(65536), b''):
        chunks.append(chunk)


def run_client(command, env, cwd, timeout, grace=10, reap_dir=None):
    """One client invocation in its own process group, output kept even on timeout.

    On timeout: SIGTERM, up to grace seconds, then SIGKILL. Afterwards nothing it started
    survives: its group, and any process working inside reap_dir, are killed.
    """
    proc = subprocess.Popen(command, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    chunks = {'stdout': [], 'stderr': []}
    readers = [threading.Thread(target=drain, args=(getattr(proc, name), chunks[name]), daemon=True) for name in chunks]
    for reader in readers:
        reader.start()
    status = 'completed'
    try:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            status = 'timeout'
            signal_group(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass
    finally:
        signal_group(proc.pid, signal.SIGKILL)
        proc.wait()
        if reap_dir:
            reap(reap_dir)
        for reader, stream in zip(readers, (proc.stdout, proc.stderr)):
            reader.join(timeout=grace)
            if not reader.is_alive():  # a pipe some escaped process still holds stays with its reader
                stream.close()
    return {'status': status, 'returncode': proc.returncode,
            **{name: b''.join(parts).decode(errors='replace') for name, parts in chunks.items()}}


def python_version(python):
    return subprocess.run([python, '-c', 'import sys; print(sys.version.split()[0])'], capture_output=True,
                          text=True, check=True).stdout.strip()


def task_repo(task):
    return Path(task['repo_path']) if 'repo_path' in task else bench_tasks.REPOS/task['repo']


def read_rows(log):
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()] if log.exists() else []


def redact(text, key):
    return text.replace(key, '[REDACTED]') if key and len(key) >= 8 else text


class Trial:
    """One task × arm × trial: a first session, an optional follow-up, then grading.

    step() runs the next session and returns True while a follow-up is still due; the last
    step grades. trial.json is rewritten after every step, so a crash keeps the accounting.
    One ModelPilot proxy (and, for Jev arms, one Jev router) serves every session of the trial;
    close() releases them. jev_stub replaces the router's TypeSafe call (offline tests only).
    """
    def __init__(self, task, arm_id, trial_dir, cli, key, upstream, price_table, *, trial=0, python=None,
                 max_turns=30, budget_usd=1.0, timeout=900, grace=10, shape='single', gap=0,
                 expected_hidden_passed=None, client_version=None, jev_key=None, jev_stub=None, router=None,
                 adapter=None):
        arm = ARMS[arm_id]
        if arm['kind'] not in RUNNABLE and adapter is None:
            raise NotImplementedError(f'{arm_id}: launcher not implemented yet (CLAUDE.md work item 4)')
        if shape not in SHAPES:
            raise ValueError(f'unknown shape {shape}')
        if adapter is not None and adapter.arm_id != arm_id:
            raise ValueError('Adapter does not match requested arm')
        if adapter is not None and arm['kind'] == 'jev':
            raise ValueError('Jev arms run through bench_jev, not an adapter')
        self.adapter = adapter
        if adapter is not None:
            arm = dict(arm, model=adapter.model)
        self.task, self.arm = task, arm
        self.dir = Path(trial_dir)
        self.cli, self.api_key, self.upstream, self.rates = cli, key, upstream, price_table
        self.python = python or bench_tasks.interpreter()
        self.limits = {'max_turns': max_turns, 'budget_usd': budget_usd, 'timeout_s': timeout, 'tools': TOOLS}
        self.grace, self.gap, self.expected, self.version = grace, gap, expected_hidden_passed, client_version
        self.prompts = [PREAMBLE + task['instruction']] + ([FOLLOW_UP] if shape == 'followup' else [])
        self.session_id = str(uuid.uuid4())
        self.sessions, self.final = [], {}
        self.started = self.finished = self.router_unavailable = False
        self.proxy = self.proxy_thread = self.route = None
        self.router = (router or bench_jev.JevRouter(arm)) if arm['kind'] == 'jev' else None
        self.jev_key, self.jev_stub = jev_key, jev_stub
        self.record = {'task': task['id'], 'arm': arm_id, 'trial': trial, 'model': arm['model'],
                       'spec_sha256': bench_tasks.spec_hash(task), 'shape': shape,
                       'gap_requested_seconds': gap if shape == 'followup' else None,
                       'expected_hidden_passed': expected_hidden_passed, 'limits': self.limits,
                       'client_path': str(cli), 'client_version': client_version, 'complete': False}
        if self.router:
            self.record.update(self.router.describe())

    @property
    def log(self):
        return self.dir/'observations.jsonl'

    def known_cost(self):
        if self.finished:  # checked before every session of a run; a finished log no longer changes
            return self.record['accounting']['known_cost_usd']
        return sum(r['cost_usd'] for r in read_rows(self.log) if r.get('cost_usd') is not None)

    def step(self):
        if self.version and client_version(self.cli) != self.version:
            raise ClientChanged(f'{self.cli} no longer reports {self.version}; not running more trials.')
        if self.router:
            self.router.verify()  # an agent could edit the checkout through --add-dir or Bash
            if self.started and not self.router.alive():
                raise bench_jev.JevRouterDown(f'Jev router for {self.dir} exited; a harness failure, not a model result.')
        if not self.started:
            self.setup()
        index = len(self.sessions)
        self.run_session(index)
        due = index + 1 < len(self.prompts)
        if due and self.sessions[-1]['stop'] != 'success':
            self.record['follow_up'] = 'skipped_after_' + self.sessions[-1]['stop']
            due = False
        if due:
            self.save('parked')
        else:
            self.finish()
        return due

    def setup(self):
        if self.adapter:
            self.adapter.verify()
        self.started = True
        self.dir.mkdir(mode=0o700, parents=True)
        self.repo = task_repo(self.task)
        self.work = bench_tasks.workspace(self.task, self.repo, self.dir/'workspace')
        dirs = {name: self.dir/name for name in ('home', 'tmp', 'config')}
        for path in dirs.values():
            path.mkdir()
        env = client_env(os.environ, self.api_key, dirs, self.cli, {})
        # The agent's python3/pip/pytest are the grader's interpreter, not whatever the system has.
        env['PATH'] = os.pathsep.join([str(Path(self.python).parent), env['PATH']])
        # Same import paths as the grader, as an editable install would give a developer.
        paths = bench_tasks.test_env_paths(self.task, self.work)
        if paths:
            env['PYTHONPATH'] = os.pathsep.join(paths)
        self.record['python'] = python_version(self.python)
        if self.adapter and hasattr(self.adapter, 'setup'):
            self.adapter.setup(self)
        options = self.adapter.proxy_options() if self.adapter and hasattr(self.adapter, 'proxy_options') else {}
        self.proxy = ProxyServer(('127.0.0.1', 0), self.upstream, self.log, self.rates, **options)
        self.proxy_thread = threading.Thread(target=self.proxy.serve_forever, daemon=True)
        self.proxy_thread.start()
        proxy_url = f'http://127.0.0.1:{self.proxy.server_port}'
        if self.router:
            self.route = self.router.start(proxy_url, dirs, self.jev_key, self.dir/'jev.stderr.txt', stub=self.jev_stub)
            self.record['router'] = self.route.get('router')
            env.update(self.route['env'])  # the router's port and the jev-router sentinel
        else:
            env['ANTHROPIC_BASE_URL'] = proxy_url
        self.env = env

    def run_session(self, index):
        first_row = len(read_rows(self.log))
        # Sessions persist only in the trial's own config dir, so the follow-up can resume them.
        session = ['--session-id', self.session_id] if index == 0 else ['--resume', self.session_id]
        model, extra = (None, ['--add-dir', self.route['add_dir']]) if self.router else (self.arm['model'], [])
        command = client_command(self.cli, self.prompts[index], model, self.limits['max_turns'],
                                 self.limits['budget_usd'], session, extra)
        env = self.env
        if self.adapter:
            command = self.adapter.command(command, env['ANTHROPIC_BASE_URL'])
            env = self.adapter.environment(env)
        started_unix, started = time.time(), time.monotonic()
        try:
            result = run_client(command, env, self.work, self.limits['timeout_s'], grace=self.grace, reap_dir=self.dir)
        finally:
            wall = time.monotonic() - started
            # Every request the session started is logged before its rows are counted.
            idle = self.proxy.wait_idle(IDLE_SECONDS)
        for name, suffix in (('stdout', 'jsonl'), ('stderr', 'txt')):
            with (self.dir/f'client.{name}.{suffix}').open('a') as f:
                f.write(redact(redact(result[name], self.adapter.key) if self.adapter else result[name], self.api_key))
        final = next((e for e in reversed(parse_events(result['stdout'])) if e.get('type') == 'result'), {})
        if final:
            self.final = final  # a resumed session's totals are cumulative for the whole session
        rows = read_rows(self.log)[first_row:]
        messages = sorted((r for r in rows if r.get('kind') == 'messages'), key=lambda r: r.get('started_unix', 0))
        first_read = next(((r.get('usage') or {}).get('cache_read_input_tokens') for r in messages
                           if r.get('http_status') == 200), None)
        record = {'prompt': 'task' if index == 0 else 'follow_up', 'status': result['status'],
                  'returncode': result['returncode'], 'stop': stop_reason(result['status'], final, rows),
                  'subtype': final.get('subtype'), 'num_turns': final.get('num_turns'), 'requests': len(messages),
                  'rows': [first_row, first_row + len(rows)], 'wall_seconds': round(wall, 3),
                  'started_unix': started_unix, 'ended_unix': time.time(), 'first_read_tokens': first_read,
                  'proxy_idle': idle}
        if self.router:
            record['router_pid'] = self.router.pid
            self.router.collect(self.dir/'tmp', self.dir/'decisions.json')
        if index:
            # Other trials may re-warm the shared system prompt during the gap; cost is repriced
            # cold-equivalent, but a physically warm follow-up's latency is not.
            record['physically_warm'] = bool(first_read)
            self.record['gap_seconds'] = round(started_unix - self.sessions[-1]['ended_unix'], 3)
        self.sessions.append(record)

    def save(self, phase):
        rows = read_rows(self.log)
        last = self.sessions[-1] if self.sessions else {}
        if self.router:
            decisions, stderr = self.dir/'decisions.json', self.dir/'jev.stderr.txt'
            routing = bench_jev.routing(json.loads(decisions.read_text()) if decisions.exists() else [],
                                        stderr.read_text(errors='replace') if stderr.exists() else '', rows, self.sessions)
            self.record['routing'] = routing
            self.router_unavailable = routing['auth_failure']
        self.record.update(
            phase=phase, sessions=self.sessions,
            status='timeout' if any(s['status'] == 'timeout' for s in self.sessions) else 'completed',
            wall_seconds=round(sum(s['wall_seconds'] for s in self.sessions), 3),
            client={'subtype': self.final.get('subtype'), 'is_error': self.final.get('is_error'),
                    'num_turns': self.final.get('num_turns'), 'stop': last.get('stop')},
            accounting=self.adapter.accounting(rows, self.final) if self.adapter else
            accounting(rows, self.final, jev=bool(self.router)),
            cache=bench_report.cache_attribution(rows, self.rates))
        if self.adapter and (self.dir/'tmp').exists():
            self.record['routing'] = self.adapter.evidence(self.dir)
        (self.dir/'trial.json').write_text(json.dumps(self.record, indent=2) + '\n')

    def close(self):
        """Stop the trial's router and proxy (idempotent). Afterwards every request is logged."""
        if self.router:
            self.router.stop()  # first, so nothing new reaches the proxy
            stderr = self.dir/'jev.stderr.txt'
            if stderr.exists():
                stderr.write_text(redact(redact(stderr.read_text(errors='replace'), self.api_key), self.jev_key))
        if self.proxy:
            self.proxy.shutdown()
            self.proxy_thread.join()
            # Handler threads are daemons that server_close() does not join: without this wait a
            # request still in flight (billed upstream) could lose its row when the log closes.
            if not self.proxy.wait_idle(IDLE_SECONDS):
                self.record['proxy_busy_at_close'] = True
            self.proxy.server_close()
            self.proxy = None

    def finish(self, stopped=None):
        """Grade the tree as the sessions left it. stopped: the run ended before a due follow-up."""
        if stopped:
            self.record['stopped'] = stopped
        self.close()
        self.save('grading')
        subprocess.run(['git', 'add', '-A', '-N'], cwd=self.work, capture_output=True)  # include new files in the diff
        (self.dir/'agent.diff').write_bytes(subprocess.run(['git', 'diff', '--binary', 'HEAD'], cwd=self.work,
                                                           capture_output=True).stdout)
        changed = subprocess.run(['git', 'diff', '--name-only', 'HEAD'], cwd=self.work, capture_output=True,
                                 text=True).stdout.splitlines()
        self.record['test_config_changed'] = test_config_changes(changed, self.task['test_dir'])
        graded = bench_tasks.grade(self.task, self.work, self.repo, self.python, self.dir/'grade',
                                   expected_hidden_passed=self.expected)
        self.record['grade'] = {'passed': graded['passed'], 'reason': graded['reason'],
                                'failing_tests': sorted(set(graded['hidden']['failing_tests'] + graded['suite']['failing_tests'])),
                                'hidden_exit': graded['hidden']['exit_code'], 'suite_exit': graded['suite']['exit_code'],
                                'hidden_passed': graded['hidden']['tests_passed'],
                                'hidden_skipped': graded['hidden']['tests_skipped']}
        self.record['passed'] = graded['passed']
        reap(self.dir)
        # Keep the diff and records; drop copies that only cost disk.
        for name in ('workspace', 'grade', 'home', 'tmp'):
            shutil.rmtree(self.dir/name, ignore_errors=True)
        self.record['complete'] = stopped is None
        self.finished = True
        self.save('graded')
        return self.record


def run_trial(task, arm_id, trial_dir, cli, key, upstream, price_table, *, sleep=time.sleep, **options):
    """Run one trial start to finish, waiting out the follow-up gap."""
    trial = Trial(task, arm_id, trial_dir, cli, key, upstream, price_table, **options)
    try:
        while trial.step():
            sleep(trial.gap)
    finally:
        trial.close()
    return trial.record


def interleave(trials, gap, *, clock=time.monotonic, sleep=time.sleep, stop=lambda: None):
    """Run trials' sessions one at a time: new trials in the given order, and a parked trial
    as soon as gap seconds have passed since its last session ended. Sleeps only when nothing
    else can run. Returns stop()'s reason if it gave one before a session, else None."""
    pending, parked, sequence = list(trials), [], 0
    while pending or parked:
        parked.sort(key=lambda p: p[:2])
        now = clock()
        if parked and parked[0][0] <= now:
            source = parked
        elif pending:
            source = pending
        else:
            sleep(parked[0][0] - now)
            continue
        reason = stop()
        if reason:
            return reason
        trial = parked.pop(0)[2] if source is parked else pending.pop(0)
        if trial.step():
            parked.append((clock() + gap, sequence, trial))
            sequence += 1
    return None


class LazyTrial:
    """Creates its trial on first use, so a stopped run never touches trials it did not start."""
    def __init__(self, make, executed):
        self.make, self.executed, self.trial = make, executed, None

    def step(self):
        self.trial = self.trial or self.make()
        more = self.trial.step()
        record = self.trial.record or {}
        self.executed.append([record.get('task'), record.get('arm'), record.get('trial'), len(record.get('sessions') or [])])
        if not more:
            print(record.get('task'), record.get('arm'), record.get('trial'), 'PASS' if record.get('passed') else 'FAIL',
                  (record.get('cache') or {}).get('cold_equivalent_cost_usd'), flush=True)
        return more


def preflight(tasks, python, scratch):
    """Grade each task's reference once in this environment ($0) before any billable request.

    Returns each task's hidden tests passed, the bar every trial must meet. A reference that
    fails (a changed environment, a flaky task) stops the run.
    """
    expected, failed = {}, []
    for task in tasks:
        base = Path(scratch)/task['id']
        repo = task_repo(task)
        tree = bench_tasks.task_tree(task, repo, task['reference'], base/'reference')
        graded = bench_tasks.grade(task, tree, repo, python, base/'grade')
        shutil.rmtree(base, ignore_errors=True)
        if graded['passed']:
            expected[task['id']] = {'hidden_passed': graded['hidden']['tests_passed'],
                                    'seconds': round(graded['hidden']['seconds'] + graded['suite']['seconds'], 3)}
        else:
            tests = sorted(set(graded['hidden']['failing_tests'] + graded['suite']['failing_tests']))
            failed.append(f"{task['id']} ({graded['reason']}{': ' + ', '.join(tests) if tests else ''})")
    if failed:
        raise PreflightError('Reference solutions fail their graders here: ' + '; '.join(failed) + '. No requests sent.')
    return expected


def jev_manifest(arms):
    """What a run's Jev arms ran, for the manifest; None without Jev arms."""
    jev = {a: bench_jev.JevRouter(ARMS[a]).describe() for a in arms if ARMS[a]['kind'] == 'jev'}
    return {'arms': jev, 'router_cost': 'unpriced (TypeSafe); Jev dollars are provider cost only, a lower bound',
            'stop_threshold_basis': "--max-budget-usd is priced by Claude Code at its own rate for the jev-router "
                                    'sentinel, not the wire cost of the served model',
            'add_dir': 'the Jev checkout, as the stock launcher passes it'} if jev else None


def run_bench(tasks, arms, trials, seed, out, cli, key, upstream, price_table, *, client_version=None, shape='single',
              gap=0, run_budget=None, expected=None, jev_key=None, trial_factory=None, clock=time.monotonic,
              sleep=time.sleep, **limits):
    out = Path(out)
    out.mkdir(mode=0o700, parents=True)
    order = schedule(tasks, arms, trials, seed)
    by_id = {t['id']: t for t in tasks}
    if client_version is None:
        cli, client_version = resolve_client(cli)
    gap = gap if shape == 'followup' else 0
    manifest = {'seed': seed, 'order': order, 'arms': {a: ARMS[a] for a in arms}, 'trials': trials,
                'tasks': {t['id']: bench_tasks.spec_hash(t) for t in tasks}, 'client': str(cli),
                'client_version': client_version,
                'python': python_version(limits.get('python') or bench_tasks.interpreter()), 'limits': limits,
                'preamble': PREAMBLE, 'tools': TOOLS, 'shape': shape, 'gap_seconds': gap,
                'follow_up_prompt': FOLLOW_UP if shape == 'followup' else None, 'run_budget_usd': run_budget,
                'reference_preflight': expected, 'cost_basis': 'cold-equivalent; measured alongside',
                'jev': jev_manifest(arms),
                'note': 'Stop thresholds are not billing caps: per session, and the run threshold between sessions. No retries.'}
    with (out/'manifest.json').open('x') as f:
        json.dump(manifest, f, indent=2)

    def default_factory(task, arm, trial_dir, n):
        return Trial(task, arm, trial_dir, cli, key, upstream, price_table, trial=n, shape=shape, gap=gap,
                     client_version=client_version, jev_key=jev_key,
                     expected_hidden_passed=((expected or {}).get(task['id']) or {}).get('hidden_passed'), **limits)
    factory = trial_factory or default_factory
    executed = []
    lazy = [LazyTrial(lambda t=t, a=a, n=n: factory(by_id[t], a, out/t/a/str(n), n), executed) for t, a, n in order]

    def spend():
        return sum(item.trial.known_cost() for item in lazy if item.trial)

    def stop():
        # A rejected TypeSafe key would make every later Jev trial fail open onto Opus.
        if any(item.trial.router_unavailable for item in lazy if item.trial):
            return 'jev_router_unavailable'
        return 'run_budget' if run_budget is not None and spend() >= run_budget else None
    reason = error = None
    try:
        reason = interleave(lazy, gap, clock=clock, sleep=sleep, stop=stop)
        for item in lazy:  # started trials whose follow-up the stop cut off
            if reason and item.trial and item.trial.started and not item.trial.finished:
                item.trial.finish(stopped=reason)
    except BaseException as e:
        error = type(e).__name__
        raise
    finally:
        for item in lazy:  # a crash must not leave a trial's proxy or router running
            if item.trial and not item.trial.finished:
                item.trial.close()
        records = [item.trial.record for item in lazy if item.trial and item.trial.finished]
        summary = bench_report.summarize(records, arms, seed=seed)
        summary.update(trials=len(records), complete=reason is None and error is None, stopped=reason, error=error,
                       known_spend_usd=spend(), executed=executed,
                       unknown_cost_trials=[f"{r['task']}/{r['arm']}/{r['trial']}" for r in records
                                            if (r.get('accounting') or {}).get('cost_usd') is None])
        (out/'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--tasks', required=True, help='Comma-separated task IDs from bench/tasks')
    parser.add_argument('--arms', required=True, help='Comma-separated arms: ' + ', '.join(ARMS))
    parser.add_argument('--trials', type=int, default=1)
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--max-turns', type=int, default=30)
    parser.add_argument('--budget', type=float, default=1.0, help='Per-session client stop threshold, not a billing cap')
    parser.add_argument('--shape', choices=SHAPES, default='single')
    parser.add_argument('--gap', type=float, default=0, help='Seconds before the follow-up (followup shape): 0 warm, 330 cold')
    parser.add_argument('--run-budget', type=float, help='Stop starting sessions once known spend reaches this (required with --live)')
    parser.add_argument('--claude', type=Path)
    parser.add_argument('--live', action='store_true', help='Required to send billable requests')
    parser.add_argument('--final', action='store_true', help='Allow final-split tasks (only for the frozen evaluation)')
    args = parser.parse_args()
    tasks = [t for i in args.tasks.split(',') for t in bench_tasks.load(i)]
    arms = args.arms.split(',')
    unknown = [a for a in arms if a not in ARMS]
    if len(tasks) != len(args.tasks.split(',')) or unknown:
        raise SystemExit(f'Unknown task or arm. Arms: {", ".join(ARMS)}')
    if args.gap and args.shape != 'followup':
        raise SystemExit('--gap applies only to --shape followup.')
    if bench_tasks.SPLITS.exists():
        final = [t['id'] for t in tasks if bench_tasks.split_of(t['id']) == 'final']
        if final and not args.final:
            raise SystemExit(f'Final-split tasks {final} need --final: they are reserved for the frozen evaluation.')
        changed = bench_tasks.check_lock(bench_tasks.load())
        if final and changed:
            raise SystemExit(f'Final task specs changed since the lock: {changed}. Not running.')
    blocked = [a for a in arms if ARMS[a]['kind'] not in RUNNABLE]
    if blocked:
        raise SystemExit(f'Not runnable yet: {", ".join(blocked)} (launchers arrive with work item 4).')
    jev_arms = [a for a in arms if ARMS[a]['kind'] == 'jev']
    table = rates()
    served = sorted({m for a in arms for m in (bench_jev.JEV_MODELS if a in jev_arms else [ARMS[a]['model']])})
    missing = [m for m in served if m not in table]
    if missing:
        raise SystemExit(f'No configured rates for {missing}.')
    if jev_arms and not shutil.which('node'):
        raise SystemExit('Jev arms need Node.')
    problems = [p for p in (bench_jev.JevRouter(ARMS[a]).problem() for a in jev_arms) if p]
    if problems:
        raise SystemExit(' '.join(problems))
    if args.live and args.run_budget is None:
        raise SystemExit('--live needs --run-budget: known spend at which no further session starts.')
    python = bench_tasks.interpreter()
    print(f'Preflight: grading {len(tasks)} reference solution(s) with {python} ($0)...', flush=True)
    with tempfile.TemporaryDirectory(prefix='bench-preflight-') as scratch:
        try:
            expected = preflight(tasks, python, scratch)
        except PreflightError as e:
            raise SystemExit(str(e))
    print('Preflight passed: ' + ', '.join(f"{i} ({e['hidden_passed']} hidden)" for i, e in expected.items()), flush=True)
    runs = len(tasks) * len(arms) * args.trials
    sessions = 2 if args.shape == 'followup' else 1
    if not args.live:
        shape = f'follow-up after {args.gap:g} s' if args.shape == 'followup' else 'single prompt'
        ceiling = runs * sessions * args.budget
        if args.run_budget is None:
            run_stop, worst = 'No run stop threshold yet (--live needs --run-budget).', ceiling
        else:
            # No new session starts at the run threshold, but the one running can still add its own.
            run_stop = f'Run stop threshold ${args.run_budget:.2f}: no session starts once known spend reaches it.'
            worst = min(ceiling, args.run_budget + args.budget)
        print(f'Prepared {runs} trials ({len(tasks)} tasks × {len(arms)} arms × {args.trials}), {shape}, seed {args.seed}, '
              f'max {args.max_turns} turns and ${args.budget:.2f} stop threshold per session. {run_stop} Up to about '
              f'${worst:.2f} if sessions reach their thresholds. Thresholds are not billing caps. No retries. Add --live.')
        if jev_arms:
            print(f'Jev arms {", ".join(jev_arms)}: checkouts verified. TypeSafe routing is billed separately and unpriced, '
                  'so their dollars are a lower bound; the client stop threshold is priced on the jev-router sentinel. '
                  'Stock Jev is expected not to route on this client (run as Opus plus router overhead). '
                  '--live asks for a TypeSafe key; consider python3 -m modelpilot.jev_check --live first.')
        return
    cli, version = resolve_client(args.claude or shutil.which('claude') or 'claude')
    writable = bench_tasks.writable_site_packages(python)
    if writable:
        # An agent's `pip install` would otherwise change what every later trial imports.
        raise SystemExit(f'Benchmark site-packages is writable ({writable[0]}). Lock it first: '
                         f'chmod -R a-w {writable[0]}')
    key = os.environ.get('ANTHROPIC_API_KEY') or getpass.getpass('Anthropic API key (hidden): ').strip()
    problem = check_anthropic_key(key)
    if problem:
        raise SystemExit(problem + ' No billable requests sent.')
    jev_key = None
    if jev_arms:
        jev_key = (os.environ.get('JEV_API_KEY') or os.environ.get('TYPESAFE_API_KEY')
                   or getpass.getpass('TypeSafe/Jev API key (hidden): ').strip())
        if not jev_key or any(c.isspace() for c in jev_key):
            raise SystemExit('Missing or malformed TypeSafe key. No billable requests sent.')
    out = ROOT/'runs'/('bench-' + time.strftime('%Y%m%d-%H%M%S'))
    print(f'Running {runs} billable trials with {cli} ({version}); stop at ${args.run_budget:.2f} known spend; '
          f'results: {out}', flush=True)
    summary = run_bench(tasks, arms, args.trials, args.seed, out, cli, key, 'https://api.anthropic.com', table,
                        client_version=version, shape=args.shape, gap=args.gap, run_budget=args.run_budget,
                        expected=expected, jev_key=jev_key, max_turns=args.max_turns, budget_usd=args.budget)
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
