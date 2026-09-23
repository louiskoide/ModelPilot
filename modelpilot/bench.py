"""M6 benchmark harness: repository tasks × arms × trials under identical limits.

Each trial gets a fresh history-free checkout, an isolated HOME/TMPDIR/Claude config, the
same tools, turn limit and stop threshold, and ModelPilot's proxy for wire accounting. After
the client exits, the shared hidden grader runs without provider credentials. Order is
randomized with a recorded seed. Without --live nothing is sent. Agents may run arbitrary
commands inside their checkout: this is not an OS sandbox, so only use trusted task repos.
"""
import argparse
import getpass
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import threading
import time
from . import bench_tasks
from .governed_session import client_env
from .jev_route_check import TOKEN_FIELDS, check_anthropic_key, parse_events
from .proxy import ProxyServer

ROOT = Path(__file__).resolve().parents[1]
TOOLS = 'Read,Edit,Write,Bash,Glob,Grep'
PREAMBLE = ('You are working in a Python repository in the current directory. Complete the task below. '
            'Run the relevant tests with python3 before you finish.\n\nTask: ')
ARMS = {
    'opus-5': {'kind': 'fixed', 'model': 'claude-opus-5'},
    'sonnet-5': {'kind': 'fixed', 'model': 'claude-sonnet-5'},
    'haiku-4.5': {'kind': 'fixed', 'model': 'claude-haiku-4-5-20251001'},
    # Registered so plans and reports name them; their launchers arrive with work item 4.
    'jev-stock': {'kind': 'jev', 'variant': 'stock'},
    'jev-compat': {'kind': 'jev', 'variant': 'compat'},
    'modelpilot': {'kind': 'modelpilot', 'policy': 'docs/m6-modelpilot-policy.md'},
}
RUNNABLE = ('fixed',)


def rates():
    """Rates for every benchmark model: the 5-family table plus M0's 4.6 entries."""
    merged = json.loads((ROOT/'configs/m0.json').read_text())['rates']
    merged.update(json.loads((ROOT/'configs/jev-rates.json').read_text())['rates'])
    return merged


def schedule(tasks, arms, trials, seed):
    order = [(t['id'], a, n) for t in tasks for a in arms for n in range(trials)]
    random.Random(seed).shuffle(order)
    return order


def accounting(rows, final):
    messages = [r for r in rows if r.get('kind') == 'messages']
    ok = [r for r in messages if r.get('http_status') == 200]
    proxy_tokens = {wire: sum((r.get('usage') or {}).get(wire, 0) for r in ok) for wire, _ in TOKEN_FIELDS}
    usage = final.get('modelUsage') or {}
    client_tokens = {wire: sum(m.get(name, 0) for m in usage.values() if isinstance(m, dict)) for wire, name in TOKEN_FIELDS}
    unpriced = sum(r.get('cost_usd') is None for r in messages)
    known = sum(r['cost_usd'] for r in messages if r.get('cost_usd') is not None)
    client = final.get('total_cost_usd')
    return {'requests': len(messages), 'http_statuses': [r.get('http_status') for r in messages],
            'rejected_requests': sum(r.get('http_status') != 200 for r in messages),
            'models': [r.get('model') for r in messages], 'unpriced_requests': unpriced,
            # Unknown cost stays unknown: a trial with any unpriced request has no dollar total.
            'cost_usd': known if unpriced == 0 and messages else None, 'known_cost_usd': known,
            'proxy_tokens': proxy_tokens, 'client_tokens': client_tokens,
            'tokens_match': bool(usage) and proxy_tokens == client_tokens,
            'client_cost_usd': client,
            'client_cost_matches': isinstance(client, (int, float)) and unpriced == 0 and abs(client - known) < 1e-6,
            'first_byte_seconds': [r.get('first_byte_seconds') for r in messages]}


def run_trial(task, arm_id, trial_dir, cli, key, upstream, price_table, *, python=None,
              max_turns=30, budget_usd=1.0, timeout=900):
    arm = ARMS[arm_id]
    python = python or bench_tasks.interpreter()
    if arm['kind'] not in RUNNABLE:
        raise NotImplementedError(f'{arm_id}: launcher not implemented yet (CLAUDE.md work item 4)')
    trial_dir = Path(trial_dir)
    trial_dir.mkdir(mode=0o700, parents=True)
    repo = bench_tasks.REPOS/task['repo'] if 'repo_path' not in task else Path(task['repo_path'])
    work = bench_tasks.workspace(task, repo, trial_dir/'workspace')
    dirs = {name: trial_dir/name for name in ('home', 'tmp', 'config')}
    for path in dirs.values():
        path.mkdir()
    proxy = ProxyServer(('127.0.0.1', 0), upstream, trial_dir/'observations.jsonl', price_table)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    env = client_env(os.environ, key, dirs, cli, {})
    # The agent's python3/pip/pytest are the grader's interpreter, not whatever the system has.
    env['PATH'] = os.pathsep.join([str(Path(python).parent), env['PATH']])
    env['ANTHROPIC_BASE_URL'] = f'http://127.0.0.1:{proxy.server_port}'
    command = [str(cli), '-p', PREAMBLE + task['instruction'], '--model', arm['model'],
               '--output-format', 'stream-json', '--verbose', '--max-turns', str(max_turns),
               '--max-budget-usd', f'{budget_usd:.2f}', '--no-session-persistence', '--setting-sources', '',
               '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--tools', TOOLS, '--allowedTools', TOOLS]
    record = {'task': task['id'], 'arm': arm_id, 'model': arm['model'], 'spec_sha256': bench_tasks.spec_hash(task),
              'python': python_version(python),
              'limits': {'max_turns': max_turns, 'budget_usd': budget_usd, 'timeout_s': timeout, 'tools': TOOLS}}
    stdout = stderr = ''
    started = time.monotonic()
    try:
        result = subprocess.run(command, env=env, cwd=work, capture_output=True, text=True, timeout=timeout,
                                stdin=subprocess.DEVNULL)
        stdout, stderr, record['returncode'] = result.stdout, result.stderr, result.returncode
        record['status'] = 'completed'
    except subprocess.TimeoutExpired:
        record['status'] = 'timeout'
    finally:
        record['wall_seconds'] = round(time.monotonic() - started, 3)
        proxy.shutdown()
        thread.join()
        proxy.server_close()
    (trial_dir/'client.stdout.jsonl').write_text(stdout.replace(key, '[REDACTED]'))
    (trial_dir/'client.stderr.txt').write_text(stderr.replace(key, '[REDACTED]'))
    events = parse_events(stdout)
    final = next((e for e in reversed(events) if e.get('type') == 'result'), {})
    record['client'] = {'subtype': final.get('subtype'), 'is_error': final.get('is_error'),
                        'num_turns': final.get('num_turns'), 'stop': final.get('subtype')}
    log = trial_dir/'observations.jsonl'
    rows = [json.loads(line) for line in log.read_text().splitlines() if line.strip()] if log.exists() else []
    record['accounting'] = accounting(rows, final)
    subprocess.run(['git', 'add', '-A', '-N'], cwd=work, capture_output=True)  # include new files in the diff
    (trial_dir/'agent.diff').write_bytes(subprocess.run(['git', 'diff', '--binary', 'HEAD'], cwd=work,
                                                        capture_output=True).stdout)
    graded = bench_tasks.grade(task, work, repo, python, trial_dir/'grade')
    record['grade'] = {'passed': graded['passed'], 'reason': graded['reason'],
                       'failing_tests': sorted(set(graded['hidden']['failing_tests'] + graded['suite']['failing_tests'])),
                       'hidden_exit': graded['hidden']['exit_code'], 'suite_exit': graded['suite']['exit_code']}
    record['passed'] = graded['passed']
    # Keep the diff and records; drop copies that only cost disk.
    for name in ('workspace', 'grade', 'home', 'tmp'):
        shutil.rmtree(trial_dir/name, ignore_errors=True)
    (trial_dir/'trial.json').write_text(json.dumps(record, indent=2) + '\n')
    return record


def python_version(python):
    return subprocess.run([python, '-c', 'import sys; print(sys.version.split()[0])'], capture_output=True,
                          text=True, check=True).stdout.strip()


def summarize(records, arms):
    out = []
    for arm in arms:
        rows = [r for r in records if r['arm'] == arm]
        priced = [r for r in rows if r['accounting']['cost_usd'] is not None]
        passes = sum(r['passed'] for r in rows)
        cost = sum(r['accounting']['cost_usd'] for r in priced)
        out.append({'arm': arm, 'trials': len(rows), 'passes': passes,
                    'pass_rate': passes / len(rows) if rows else None,
                    'cost_usd_priced_trials': cost, 'unpriced_trials': len(rows) - len(priced),
                    # Only meaningful when every trial is priced; otherwise stays unknown.
                    'cost_per_pass_usd': cost / passes if passes and len(priced) == len(rows) else None,
                    'wall_seconds': sum(r['wall_seconds'] for r in rows)})
    return out


def run_bench(tasks, arms, trials, seed, out, cli, key, upstream, price_table, **limits):
    out = Path(out)
    out.mkdir(mode=0o700, parents=True)
    order = schedule(tasks, arms, trials, seed)
    by_id = {t['id']: t for t in tasks}
    version = subprocess.run([str(cli), '--version'], capture_output=True, text=True, timeout=30).stdout.strip()
    manifest = {'seed': seed, 'order': order, 'arms': {a: ARMS[a] for a in arms}, 'trials': trials,
                'tasks': {t['id']: bench_tasks.spec_hash(t) for t in tasks}, 'client_version': version,
                'python': python_version(limits.get('python') or bench_tasks.interpreter()), 'limits': limits, 'preamble': PREAMBLE, 'tools': TOOLS,
                'note': 'Stop thresholds are not billing caps. No retries.'}
    with (out/'manifest.json').open('x') as f:
        json.dump(manifest, f, indent=2)
    records = []
    for task_id, arm, trial in order:
        record = run_trial(by_id[task_id], arm, out/task_id/arm/str(trial), cli, key, upstream, price_table, **limits)
        records.append(dict(record, trial=trial))
        print(task_id, arm, trial, 'PASS' if record['passed'] else 'FAIL',
              record['accounting']['cost_usd'], flush=True)
    summary = {'arms': summarize(records, arms), 'trials': len(records),
               'claims': 'Pilot-scale numbers; no winner or savings claim without the full paired analysis.'}
    (out/'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tasks', required=True, help='Comma-separated task IDs from bench/tasks')
    parser.add_argument('--arms', required=True, help='Comma-separated arms: ' + ', '.join(ARMS))
    parser.add_argument('--trials', type=int, default=1)
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--max-turns', type=int, default=30)
    parser.add_argument('--budget', type=float, default=1.0, help='Per-trial client stop threshold, not a billing cap')
    parser.add_argument('--claude', type=Path)
    parser.add_argument('--live', action='store_true', help='Required to send billable requests')
    args = parser.parse_args()
    tasks = [t for i in args.tasks.split(',') for t in bench_tasks.load(i)]
    arms = args.arms.split(',')
    unknown = [a for a in arms if a not in ARMS]
    if len(tasks) != len(args.tasks.split(',')) or unknown:
        raise SystemExit(f'Unknown task or arm. Arms: {", ".join(ARMS)}')
    blocked = [a for a in arms if ARMS[a]['kind'] not in RUNNABLE]
    if blocked:
        raise SystemExit(f'Not runnable yet: {", ".join(blocked)} (launchers arrive with work item 4).')
    table = rates()
    missing = [ARMS[a]['model'] for a in arms if ARMS[a]['model'] not in table]
    if missing:
        raise SystemExit(f'No configured rates for {missing}.')
    runs = len(tasks) * len(arms) * args.trials
    if not args.live:
        print(f'Prepared {runs} trials ({len(tasks)} tasks × {len(arms)} arms × {args.trials}), seed {args.seed}, '
              f'max {args.max_turns} turns and ${args.budget:.2f} stop threshold per trial (not a billing cap), '
              f'up to about ${runs*args.budget:.2f} in total if every trial reaches its threshold. No retries. Add --live.')
        return
    cli = args.claude or Path(shutil.which('claude') or '')
    if not cli.name or not cli.exists():
        raise SystemExit('Claude Code CLI not found; pass --claude.')
    key = os.environ.get('ANTHROPIC_API_KEY') or getpass.getpass('Anthropic API key (hidden): ').strip()
    problem = check_anthropic_key(key)
    if problem:
        raise SystemExit(problem + ' No billable requests sent.')
    out = ROOT/'runs'/('bench-' + time.strftime('%Y%m%d-%H%M%S'))
    print(f'Running {runs} billable trials; results: {out}', flush=True)
    summary = run_bench(tasks, arms, args.trials, args.seed, out, cli, key, 'https://api.anthropic.com', table,
                        max_turns=args.max_turns, budget_usd=args.budget)
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
