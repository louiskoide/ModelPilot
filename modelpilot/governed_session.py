"""One Claude Code session under the dry-run governor: governed proxy, hooks and a mid-flight correction.

The client works on one ledger task. Every billable request is reserved and settled in the
governor through the proxy. Hooks observe tool results, and a coordinator thread corrects the
task after the first tool result, which the next hook delivers into model context. Nothing
changes the client's model, effort or context. With --live this sends billable requests.
"""
import argparse
import getpass
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from .governor import Governor, reconcile_log
from .hooks import EVENTS
from .jev_route_check import TOKEN_FIELDS, check_anthropic_key, parse_events
from .proxy import ProxyServer

ROOT = Path(__file__).resolve().parents[1]
OWNER = 'claude-client'


def hook_settings(env_python=sys.executable):
    # Absolute module path works wherever the client starts the hook.
    bootstrap = f'import sys; sys.path.insert(0, {str(ROOT)!r}); from modelpilot.hooks import main; main()'
    hooks = {}
    for event in EVENTS:
        entry = {'hooks': [{'type': 'command', 'command': f'{shlex.quote(env_python)} -c {shlex.quote(bootstrap)} {event}'}]}
        if event.startswith('PostToolUse'):
            entry['matcher'] = '*'
        hooks[event] = [entry]
    return {'hooks': hooks}


def client_env(base, key, dirs, cli, binding):
    """Explicit environment: nothing inherited that could pin a model or leak user settings."""
    keep = ('LANG', 'LC_ALL', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'NODE_EXTRA_CA_CERTS')
    env = {name: base[name] for name in keep if name in base}
    node_dir = str(Path(shutil.which('node', path=base.get('PATH')) or 'node').parent)
    env.update(PATH=os.pathsep.join([str(Path(cli).parent), node_dir, '/usr/bin', '/bin']), HOME=str(dirs['home']),
               TMPDIR=str(dirs['tmp']), CLAUDE_CONFIG_DIR=str(dirs['config']), ANTHROPIC_API_KEY=key,
               DISABLE_AUTOUPDATER='1', CLAUDE_CODE_MAX_RETRIES='0', CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1')
    env.update(binding)
    return env


def correct_after_first_tool(db, session, limit, task, correction, done, record):
    """Coordinator: correct the task once the first tool result has been observed by a hook."""
    gov = Governor(db, session, limit)
    try:
        while not done.is_set():
            events = [e for e in gov.journal('hook_event') if e['payload']['event'] == 'PostToolUse']
            if events:
                row = gov.state.get(task)
                corrected = gov.state.correct(task, row['revision'], correction)
                record.update(issued=True, revision=corrected['revision'], after_hook_seq=events[0]['seq'])
                return
            done.wait(.02)
    finally:
        gov.close()


def run_session(run, cli, key, upstream, rates, *, prompt, instruction, correction=None, files=None,
                model='claude-sonnet-4-6', effort='low', tools='Read', limit_usd=5, budget_usd=.5,
                max_turns=6, expect=(), forbid=(), timeout=300):
    run = Path(run)
    run.mkdir(mode=0o700, parents=True)
    dirs = {name: run/name for name in ('workspace', 'home', 'tmp', 'config')}
    for path in dirs.values():
        path.mkdir()
    for name, text in (files or {}).items():
        (dirs['workspace']/name).write_text(text)
    db, session = run/'ledger.sqlite3', 'governed-' + uuid.uuid4().hex[:8]
    gov = Governor(db, session, limit_usd)
    task = gov.state.create('governed session', instruction)['id']
    revision = gov.state.claim(task, 1, OWNER, seconds=3600)['revision']
    gov.close()
    proxy = ProxyServer(('127.0.0.1', 0), upstream, run/'observations.jsonl', rates,
                        governor={'db': db, 'session': session, 'limit_usd': limit_usd, 'task': task})
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    settings = run/'settings.json'
    settings.write_text(json.dumps(hook_settings(), indent=2))
    binding = {'MODELPILOT_DB': str(db), 'MODELPILOT_SESSION': session, 'MODELPILOT_LIMIT_USD': str(limit_usd),
               'MODELPILOT_TASK': task, 'MODELPILOT_OWNER': OWNER, 'MODELPILOT_HOOK_ERRORS': str(run/'hook-errors.jsonl')}
    env = client_env(os.environ, key, dirs, cli, binding)
    env['ANTHROPIC_BASE_URL'] = f'http://127.0.0.1:{proxy.server_port}'
    done, correction_record = threading.Event(), {'issued': False}
    coordinator = None
    if correction:
        coordinator = threading.Thread(target=correct_after_first_tool, daemon=True,
                                       args=(db, session, limit_usd, task, correction, done, correction_record))
        coordinator.start()
    command = [str(cli), '-p', f'Task instruction: {instruction}\n\n{prompt}', '--model', model, '--effort', effort,
               '--output-format', 'stream-json', '--verbose', '--max-turns', str(max_turns),
               '--max-budget-usd', f'{budget_usd:.2f}', '--no-session-persistence', '--setting-sources', '',
               '--settings', str(settings), '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
               '--tools', tools, '--allowedTools', tools]
    report = {'status': 'running', 'mode': 'dry-run', 'model': model, 'effort': effort, 'task_revision_start': revision,
              'limits': f'client stop threshold ${budget_usd:.2f}, max {max_turns} turns; governor limit ${limit_usd} '
                        '(dry-run, never enforced); not a billing cap'}
    stdout = stderr = ''
    try:
        report['client_version'] = subprocess.run([str(cli), '--version'], env=env, capture_output=True, text=True,
                                                  timeout=30, stdin=subprocess.DEVNULL).stdout.strip()
        result = subprocess.run(command, env=env, cwd=dirs['workspace'], capture_output=True, text=True,
                                timeout=timeout, stdin=subprocess.DEVNULL)
        stdout, stderr, report['returncode'] = result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        report['status'] = 'timeout'
    finally:
        done.set()
        if coordinator:
            coordinator.join()
        proxy.shutdown()
        proxy_thread.join()
        proxy.server_close()
        (run/'client.stdout.jsonl').write_text(stdout.replace(key, '[REDACTED]'))
        (run/'client.stderr.txt').write_text(stderr.replace(key, '[REDACTED]'))
        report.update(summarize(run, db, session, limit_usd, task, stdout, correction_record, expect, forbid))
        if report['status'] == 'running':
            report['status'] = 'passed' if passed(report, correction) else 'failed'
        with (run/'summary.json').open('x') as f:
            json.dump(report, f, indent=2)
    return report


def summarize(run, db, session, limit, task, stdout, correction_record, expect, forbid):
    events = parse_events(stdout)
    final = next((e for e in reversed(events) if e.get('type') == 'result'), {})
    answer = final.get('result') or ''
    tools = [b.get('name') for e in events if e.get('type') == 'assistant'
             for b in e.get('message', {}).get('content', []) if b.get('type') == 'tool_use']
    log = run/'observations.jsonl'
    rows = [json.loads(line) for line in log.read_text().splitlines() if line.strip()] if log.exists() else []
    messages = [r for r in rows if r.get('kind') == 'messages']
    ok = [r for r in messages if r.get('http_status') == 200]
    gov = Governor(db, session, limit)
    try:
        reconciled = reconcile_log(gov, log) if log.exists() else None
        policy = gov.policy()
        journal = gov.journal()
        row = gov.state.get(task)
    finally:
        gov.close()
    deliveries = [e['payload'] for e in journal if e['kind'] == 'deliver_correction']
    corrected_revision = correction_record.get('revision')
    proxy_tokens = {wire: sum((r.get('usage') or {}).get(wire, 0) for r in ok) for wire, _ in TOKEN_FIELDS}
    usage = final.get('modelUsage') or {}
    client_tokens = {wire: sum(m.get(name, 0) for m in usage.values() if isinstance(m, dict)) for wire, name in TOKEN_FIELDS}
    known = sum(r['cost_usd'] for r in ok if r.get('cost_usd') is not None)
    client_cost = final.get('total_cost_usd')
    unpriced = sum(r.get('cost_usd') is None for r in messages)
    errors = run/'hook-errors.jsonl'
    return {
        'client': {'result_subtype': final.get('subtype'), 'is_error': final.get('is_error'), 'answer': answer,
                   'tool_names': tools, 'num_turns': final.get('num_turns'), 'total_cost_usd': client_cost},
        'answer_ok': all(t in answer for t in expect) and not any(t in answer for t in forbid),
        'correction': dict(correction_record, deliveries=len(deliveries),
                           delivered=any(d['revision'] == corrected_revision for d in deliveries),
                           acknowledged=corrected_revision is not None and row['ack_revision'] == corrected_revision),
        'proxy': {'requests': len(messages), 'http_statuses': [r.get('http_status') for r in messages],
                  'unsettled': sum(r.get('governor_status') != 'settled' for r in messages),
                  'known_cost_usd': known, 'unpriced': unpriced, 'tokens': proxy_tokens,
                  'would_refuse': [r['governor']['reason'] for r in messages
                                   if r.get('governor') and not r['governor']['admitted']]},
        'governor': {'spent_usd': policy['spent_usd'], 'cost_complete': policy['cost_complete'],
                     'still_unknown': reconciled['still_unknown'] if reconciled else None,
                     'orphans_without_rows': reconciled['orphans_without_rows'] if reconciled else None,
                     'journal_entries': len(journal)},
        'observations': sum(e['kind'] == 'stuck' for e in journal),
        'hook_events': [e['payload']['event'] for e in journal if e['kind'] == 'hook_event'],
        'hook_errors': len(errors.read_text().splitlines()) if errors.exists() else 0,
        'client_tokens': client_tokens,
        'accounting_matches': (bool(ok) and unpriced == 0 and abs(policy['spent_usd'] - known) < 1e-9
                               and isinstance(client_cost, (int, float)) and abs(client_cost - known) < 1e-6
                               and proxy_tokens == client_tokens),
        'applied': any(e['payload'].get('applied') is True for e in journal),
    }


def passed(report, correction):
    client = report['client']
    return (report.get('returncode') == 0 and client['result_subtype'] == 'success' and not client['is_error']
            and report['answer_ok'] and report['proxy']['requests'] > 0 and report['proxy']['unsettled'] == 0
            and all(s == 200 for s in report['proxy']['http_statuses'])
            and report['governor']['still_unknown'] == 0 and report['governor']['cost_complete']
            and report['accounting_matches'] and report['hook_errors'] == 0 and not report['applied']
            and (not correction or (report['correction']['delivered'] and report['correction']['acknowledged'])))


def live_scenario():
    first, second = 'ALPHA_' + uuid.uuid4().hex[:10].upper(), 'BRAVO_' + uuid.uuid4().hex[:10].upper()
    return {'files': {'a.txt': f'First token: {first}\nBefore answering you must read b.txt in this directory.\n',
                      'b.txt': f'Second token: {second}\n'},
            'prompt': 'Read a.txt in the current directory and follow its directions. When done, reply with only '
                      'the token your current task instruction asks for.',
            'instruction': 'Report the first token.',
            'correction': 'Report the second token (from b.txt) instead of the first token.',
            'expect': [second], 'forbid': [first]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Required to send billable API requests')
    parser.add_argument('--claude', type=Path, help='Claude Code executable (default: claude on PATH)')
    parser.add_argument('--model', default='claude-sonnet-4-6')
    parser.add_argument('--effort', default='low')
    parser.add_argument('--budget', type=float, default=.5, help='Client stop threshold, not a billing cap')
    parser.add_argument('--limit-usd', type=float, default=5, help='Dry-run governor limit (never enforced)')
    args = parser.parse_args()
    cli = args.claude or Path(shutil.which('claude') or '')
    if not cli.name or not cli.exists():
        raise SystemExit('Claude Code CLI not found; pass --claude.')
    if not args.live:
        print(f'Prepared one governed {args.model}/{args.effort} session: read two files, one mid-flight ledger '
              f'correction delivered by hook, every request reserved/settled in a dry-run governor. Client stop '
              f'threshold ${args.budget:.2f} (not a hard cap), max 6 turns, no retries, isolated HOME/config. Add --live.')
        return
    key = os.environ.get('ANTHROPIC_API_KEY') or getpass.getpass('Anthropic API key (hidden): ').strip()
    problem = check_anthropic_key(key)
    if problem:
        raise SystemExit(problem + ' No billable requests sent.')
    config = json.loads((ROOT/'configs/m0.json').read_text())
    if args.model not in config['rates']:
        raise SystemExit('Model needs configured rates in configs/m0.json.')
    run = ROOT/'runs'/('governed-session-' + time.strftime('%Y%m%d-%H%M%S'))
    print(f'Running a billable governed session; results: {run}', flush=True)
    report = run_session(run, cli, key, 'https://api.anthropic.com', config['rates'], model=args.model,
                         effort=args.effort, budget_usd=args.budget, limit_usd=args.limit_usd, **live_scenario())
    print(json.dumps(report, indent=2))
    if report['status'] != 'passed':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
