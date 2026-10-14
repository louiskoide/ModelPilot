"""One Claude Code session under the dry-run governor: governed proxy, hooks and a mid-flight correction.

The client works on one ledger task. Every billable request is reserved and settled in the
governor through the proxy. Hooks observe tool results, and a coordinator thread corrects the
task after the first tool result, which the next hook delivers into model context. Nothing
changes the client's model, effort or context. With --live this sends billable requests.

--scenario turn-end-rebuild (plan item 12) runs one session as four client invocations: a correction that arrives
during the turn's last model call (only the blocked Stop can deliver it), then, after an idle gap past the hook's
330 s boundary, a rebuild plan shown at the next prompt and acknowledged when the session is seen running it
(/compact, then the planned effort).
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
from .hooks import COMPACTION_CHECK, EVENTS, channel_declaration
from .jev_route_check import TOKEN_FIELDS, check_anthropic_key, parse_events
from .m5 import IDLE_BOUNDARY_SECONDS
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


def correct_after_first_tool(db, session, limit, task, correction, done, record, when=None):
    """Coordinator: correct the task once the first tool result has been observed by a hook, or once
    when() is true (e.g. while the upstream holds the turn's last reply)."""
    gov = Governor(db, session, limit)
    try:
        while not done.is_set():
            events = [e for e in gov.journal('hook_event') if e['payload']['event'] == 'PostToolUse']
            if when() if when else events:
                row = gov.state.get(task)
                corrected = gov.state.correct(task, row['revision'], correction)
                record.update(issued=True, revision=corrected['revision'],
                              after_hook_seq=(events[-1] if when else events[0])['seq'] if events else None)
                return
            done.wait(.02)
    finally:
        gov.close()


def run_session(run, cli, key, upstream, rates, *, prompt, instruction, correction=None, files=None,
                model='claude-sonnet-4-6', effort='low', tools='Read', limit_usd=5, budget_usd=.5,
                max_turns=6, expect=(), forbid=(), timeout=300, policy_upstream=None, correct_when=None):
    """policy_upstream: the owned fixtures.FixtureServer, to apply ladder escalations offline.
    correct_when: a callable; the correction is issued once it returns true instead of after the first tool."""
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
    code = gov.declare_channel()
    gov.close()
    policy = None
    if policy_upstream is not None:
        from .fixture_dispatch import ProxyPolicy
        policy = ProxyPolicy(policy_upstream, model, OWNER)
    proxy = ProxyServer(('127.0.0.1', 0), upstream, run/'observations.jsonl', rates,
                        governor={'db': db, 'session': session, 'limit_usd': limit_usd, 'task': task}, policy=policy)
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    settings = run/'settings.json'
    settings.write_text(json.dumps(hook_settings(), indent=2))
    binding = {'MODELPILOT_DB': str(db), 'MODELPILOT_SESSION': session, 'MODELPILOT_LIMIT_USD': str(limit_usd),
               'MODELPILOT_TASK': task, 'MODELPILOT_OWNER': OWNER, 'MODELPILOT_HOOK_ERRORS': str(run/'hook-errors.jsonl')}
    env = client_env(os.environ, key, dirs, cli, binding)
    env['ANTHROPIC_BASE_URL'] = f'http://127.0.0.1:{proxy.server_port}'
    done, correction_record = threading.Event(), {'issued': False, 'channel_code': code}
    coordinator = None
    if correction:
        coordinator = threading.Thread(target=correct_after_first_tool, daemon=True,
                                       args=(db, session, limit_usd, task, correction, done, correction_record, correct_when))
        coordinator.start()
    # The prompt is the user's: it declares the correction channel before any update can arrive.
    user_prompt = f'{channel_declaration(task, code)}\n\nTask instruction: {instruction}\n\n{prompt}'
    command = [str(cli), '-p', user_prompt, '--model', model, '--effort', effort,
               '--output-format', 'stream-json', '--verbose', '--max-turns', str(max_turns),
               '--max-budget-usd', f'{budget_usd:.2f}', '--no-session-persistence', '--setting-sources', '',
               '--settings', str(settings), '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
               '--tools', tools, '--allowedTools', tools]
    report = {'status': 'running', 'mode': 'dry-run' if policy is None else 'fixture-policy', 'model': model, 'effort': effort, 'task_revision_start': revision,
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
        report.update(summarize(run, db, session, limit_usd, task, stdout, correction_record, expect, forbid,
                                policy is not None))
        if report['status'] == 'running':
            report['status'] = 'passed' if passed(report, correction, policy is not None) else 'failed'
        with (run/'summary.json').open('x') as f:
            json.dump(report, f, indent=2)
    return report


def summarize(run, db, session, limit, task, stdout, correction_record, expect, forbid, policy_mode=False):
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
    client_cost_matches = isinstance(client_cost, (int, float)) and abs(client_cost - known) < 1e-6
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
        # Claude Code 2.1.282 prices and attributes all usage to the model it requested, even when
        # the proxy forwarded another: under the fixture policy only its tokens are comparable.
        'client_cost_matches': client_cost_matches,
        'accounting_matches': (bool(ok) and unpriced == 0 and abs(policy['spent_usd'] - known) < 1e-9
                               and (client_cost_matches or policy_mode) and proxy_tokens == client_tokens),
        'applied': any(e['payload'].get('applied') is True for e in journal),
        'policy': {'escalations': [{k: e['payload'].get(k) for k in ('status', 'action', 'target_model', 'target_effort')}
                                   for e in journal if e['kind'] == 'fixture_dispatch'],
                   'kept_requests': sum(e['kind'] == 'fixture_keep_escalated' and e['payload']['status'] == 'kept'
                                        for e in journal),
                   'unknown_outcomes': sum(e['kind'] in ('fixture_dispatch', 'fixture_keep_escalated')
                                           and e['payload']['status'] == 'unknown_outcome' for e in journal),
                   'forwarded_settings': [[r.get('model'), r.get('effort'), r.get('applied')] for r in messages],
                   'deferrals': sorted({r['policy']['reason'] for r in messages if r.get('policy', {}).get('reason')})},
    }


def passed(report, correction, policy=False):
    client = report['client']
    return (report.get('returncode') == 0 and client['result_subtype'] == 'success' and not client['is_error']
            and report['answer_ok'] and report['proxy']['requests'] > 0 and report['proxy']['unsettled'] == 0
            and all(s == 200 for s in report['proxy']['http_statuses'])
            and report['governor']['still_unknown'] == 0 and report['governor']['cost_complete']
            and report['accounting_matches'] and report['hook_errors'] == 0
            and (report['policy']['unknown_outcomes'] == 0 if policy else not report['applied'])
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


# Plan item 12: live evidence for turn-end delivery and an observed rebuild, in one session of four invocations.
REBUILD_IDLE_SECONDS = IDLE_BOUNDARY_SECONDS + 5  # past the UserPromptSubmit hook's idle boundary and a 5m entry's life
REBUILD_STEPS = ('task', 'follow_up', 'compact', 'rebuilt')


def correct_at_turn_end(db, session, limit, task, correction, done, record):
    """Coordinator: correct the task once the proxy has admitted a request after the first PostToolUse hook began.

    The client sends the tool result only after that hook has run (its context rides on the result), so by then
    the hook can no longer deliver the correction. If the reply ends the turn, only the Stop hook can."""
    gov = Governor(db, session, limit)
    try:
        while not done.is_set():
            journal = gov.journal()
            tool = next((e['seq'] for e in journal if e['kind'] == 'hook_event'
                         and e['payload']['event'] == 'PostToolUse'), None)
            admitted = next((e['seq'] for e in journal if tool is not None and e['kind'] == 'admit' and e['seq'] > tool),
                            None)
            if admitted:
                row = gov.state.get(task)
                corrected = gov.state.correct(task, row['revision'], correction)
                record.update(issued=True, revision=corrected['revision'], after_admit_seq=admitted)
                return
            done.wait(.02)
    finally:
        gov.close()


def wait_idle_gap(db, session, limit, seconds=REBUILD_IDLE_SECONDS, sleep=time.sleep):
    """Sleep until `seconds` have passed since the turn last ended (the hook measures the gap from there)."""
    gov = Governor(db, session, limit)
    try:
        last = gov.last_note('client_stop')
    finally:
        gov.close()
    remaining = seconds - (time.time() - last['created']) if last else seconds
    if remaining > 0:
        sleep(remaining)


def rebuild_scenario():
    first, second = 'ALPHA_' + uuid.uuid4().hex[:10].upper(), 'BRAVO_' + uuid.uuid4().hex[:10].upper()
    return {'files': {'tokens.txt': f'First token: {first}\nSecond token: {second}\n'},
            'prompt': 'Read tokens.txt in the current directory, then reply with only the token your current task '
                      'instruction asks for.',
            'instruction': 'Report the first token.',
            'correction': 'Report the second token instead of the first token.',
            'follow_up': 'Reply with only the word READY.', 'final_prompt': 'Reply with only the word DONE.',
            'expect': [second], 'forbid': [first]}


def run_rebuild_session(run, cli, key, upstream, rates, *, prompt, instruction, correction, follow_up, final_prompt,
                        files=None, expect=(), forbid=(), model='claude-sonnet-5-5', effort='low', target_effort='medium',
                        tools='Read', limit_usd=5, budget_usd=.5, max_turns=6, timeout=300, idle=None):
    """Four invocations of one isolated session through the governed proxy (dry-run; nothing is applied):
    1. task: the task, corrected during the turn's last model call (correct_at_turn_end);
    then the coordinator queues an effort change and a compaction, and the session idles past 330 s (idle(db, session,
    limit), wait_idle_gap by default);
    2. follow_up: --resume at the same setting; UserPromptSubmit plans the rebuild and shows it to the user;
    3. compact: '/compact', as the plan says; the SessionStart hook reports it;
    4. rebuilt: --resume at the planned effort; the proxy sees it and the plan is acknowledged as observed."""
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
    code = gov.declare_channel()
    gov.close()
    proxy = ProxyServer(('127.0.0.1', 0), upstream, run/'observations.jsonl', rates,
                        governor={'db': db, 'session': session, 'limit_usd': limit_usd, 'task': task})
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    settings = run/'settings.json'
    settings.write_text(json.dumps(hook_settings(), indent=2))
    binding = {'MODELPILOT_DB': str(db), 'MODELPILOT_SESSION': session, 'MODELPILOT_LIMIT_USD': str(limit_usd),
               'MODELPILOT_TASK': task, 'MODELPILOT_OWNER': OWNER, 'MODELPILOT_HOOK_ERRORS': str(run/'hook-errors.jsonl'),
               # The scenario tests delivery and observation; on its few thousand tokens compaction never pays.
               COMPACTION_CHECK: 'off'}
    env = client_env(os.environ, key, dirs, cli, binding)
    env['ANTHROPIC_BASE_URL'] = f'http://127.0.0.1:{proxy.server_port}'
    client_session = str(uuid.uuid4())  # persisted only in this run's own config directory, so it can be resumed
    done, record = threading.Event(), {'issued': False, 'channel_code': code}
    coordinator = threading.Thread(target=correct_at_turn_end, daemon=True,
                                   args=(db, session, limit_usd, task, correction, done, record))
    prompts = {'task': f'{channel_declaration(task, code)}\n\nTask instruction: {instruction}\n\n{prompt}',
               'follow_up': follow_up, 'compact': '/compact', 'rebuilt': final_prompt}
    report = {'status': 'running', 'scenario': 'turn-end-rebuild', 'mode': 'dry-run', 'model': model, 'effort': effort,
              'target_effort': target_effort, 'compaction_check': binding[COMPACTION_CHECK],
              'task_revision_start': revision, 'invocations': [],
              'limits': f'client stop threshold ${budget_usd:.2f} per invocation, max {max_turns} turns; governor limit '
                        f'${limit_usd} (dry-run, never enforced); not a billing cap'}
    stdouts = {}
    try:
        report['client_version'] = subprocess.run([str(cli), '--version'], env=env, capture_output=True, text=True,
                                                  timeout=30, stdin=subprocess.DEVNULL).stdout.strip()
        coordinator.start()
        for index, step in enumerate(REBUILD_STEPS):
            if step == 'follow_up':
                gov = Governor(db, session, limit_usd)
                try:
                    current = gov.state.get(task)['revision']
                    report['queued'] = {kind: gov.queue_change(kind, value, current)
                                        for kind, value in (('effort', target_effort), ('compact', True))}
                finally:
                    gov.close()
                started = time.time()
                (idle or wait_idle_gap)(db, session, limit_usd)
                report['idle_wait_seconds'] = round(time.time() - started, 3)
            command = [str(cli), '-p', prompts[step], '--model', model,
                       '--effort', target_effort if step == 'rebuilt' else effort,
                       '--output-format', 'stream-json', '--verbose', '--max-turns', str(max_turns),
                       '--max-budget-usd', f'{budget_usd:.2f}',
                       *(['--session-id', client_session] if index == 0 else ['--resume', client_session]),
                       '--setting-sources', '', '--settings', str(settings), '--strict-mcp-config',
                       '--mcp-config', '{"mcpServers":{}}', '--tools', tools, '--allowedTools', tools]
            entry = {'step': step, 'journal_from': journal_end(db, session, limit_usd), 'rows_from': len(read_log(run))}
            try:
                result = subprocess.run(command, env=env, cwd=dirs['workspace'], capture_output=True, text=True,
                                        timeout=timeout, stdin=subprocess.DEVNULL)
                entry['returncode'] = result.returncode
                stdouts[step] = result.stdout
                (run/f'client.{index + 1}-{step}.stdout.jsonl').write_text(result.stdout.replace(key, '[REDACTED]'))
                (run/f'client.{index + 1}-{step}.stderr.txt').write_text(result.stderr.replace(key, '[REDACTED]'))
            except subprocess.TimeoutExpired:
                entry['returncode'] = None
                report['status'] = 'timeout'
            finally:
                if step == 'task':
                    done.set()
                    coordinator.join()
                proxy.wait_idle(130)
                entry.update(journal_to=journal_end(db, session, limit_usd), rows_to=len(read_log(run)))
                report['invocations'].append(entry)
            if report['status'] == 'timeout' or entry.get('returncode') != 0:
                break  # an invocation that failed ends the session: no idle wait or spend after it
    finally:
        done.set()
        if coordinator.is_alive():
            coordinator.join()
        proxy.shutdown()
        proxy_thread.join()
        proxy.server_close()
        report.update(summarize_rebuild(run, db, session, limit_usd, task, report, stdouts, record, expect, forbid,
                                        target_effort))
        if report['status'] == 'running':
            report['status'] = rebuild_status(report)
        with (run/'summary.json').open('x') as f:
            json.dump(report, f, indent=2)
    return report


def read_log(run):
    log = Path(run)/'observations.jsonl'
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()] if log.exists() else []


def journal_end(db, session, limit):
    gov = Governor(db, session, limit)
    try:
        journal = gov.journal()
    finally:
        gov.close()
    return journal[-1]['seq'] if journal else 0


def summarize_rebuild(run, db, session, limit, task, report, stdouts, record, expect, forbid, target_effort):
    rows = read_log(run)
    messages = [r for r in rows if r.get('kind') == 'messages']
    ok = [r for r in messages if r.get('http_status') == 200]
    gov = Governor(db, session, limit)
    try:
        reconciled = reconcile_log(gov, run/'observations.jsonl') if rows else None
        policy, journal, row = gov.policy(), gov.journal(), gov.state.get(task)
    finally:
        gov.close()
    steps = {e['step']: e for e in report['invocations']}

    def within(entry, step):
        span = steps.get(step)
        return bool(span) and span['journal_from'] < entry['seq'] <= span.get('journal_to', span['journal_from'])

    def finals(step):
        return next((e for e in reversed(parse_events(stdouts.get(step, ''))) if e.get('type') == 'result'), {})

    def main_rows(step):
        span = steps.get(step)
        chosen = rows[span['rows_from']:span['rows_to']] if span and 'rows_to' in span else []
        return sorted((r for r in chosen if r.get('kind') == 'messages' and r.get('tool_count')),
                      key=lambda r: r.get('started_unix', 0))

    def first_request(step):
        first = next(iter(main_rows(step)), None)
        if first is None:
            return None
        usage = first.get('usage') or {}
        return {'model': first.get('model'), 'effort': first.get('effort'), 'http_status': first.get('http_status'),
                'compaction': bool(first.get('compaction')), 'cache_read_input_tokens': usage.get('cache_read_input_tokens'),
                'cache_creation_input_tokens': usage.get('cache_creation_input_tokens'), 'cost_usd': first.get('cost_usd')}

    revision = record.get('revision')
    deliveries = [e for e in journal if e['kind'] == 'deliver_correction' and e['payload']['revision'] == revision]
    answer = finals('task').get('result') or ''
    plans = [e for e in journal if e['kind'] == 'plan_rebase' and e['payload']['action'] == 'would_rebase']
    plan = plans[0]['payload'] if plans else None
    acks = [e for e in journal if e['kind'] == 'acknowledge_rebase']
    ack = next((e for e in acks if plan and e['payload']['plan_id'] == plan['plan_id']), None)
    observed = [e for e in journal if e['kind'] == 'observe_setting']
    proxy_tokens = {wire: sum((r.get('usage') or {}).get(wire, 0) for r in ok) for wire, _ in TOKEN_FIELDS}
    last = finals(REBUILD_STEPS[-1])
    usage = last.get('modelUsage') or {}
    client_tokens = {wire: sum(m.get(name, 0) for m in usage.values() if isinstance(m, dict)) for wire, name in TOKEN_FIELDS}
    known = sum(r['cost_usd'] for r in ok if r.get('cost_usd') is not None)
    errors = Path(run)/'hook-errors.jsonl'
    return {
        'turn_end': {
            'correction_issued': record.get('issued', False), 'channel_code': record.get('channel_code'),
            'revision': revision, 'delivered_by': [e['payload']['event'] for e in deliveries],
            'acknowledged': revision is not None and row['ack_revision'] == revision,
            'stop_blocks': sum(e['kind'] == 'stop_block' for e in journal),
            'stop_events_in_task_turn': sum(e['kind'] == 'hook_event' and e['payload']['event'] == 'Stop'
                                            and within(e, 'task') for e in journal),
            'answer': answer, 'obeyed': bool(answer) and all(t in answer for t in expect)
                                        and not any(t in answer for t in forbid)},
        'rebuild': {
            'plan': {k: plan[k] for k in ('plan_id', 'trigger', 'action')} if plan else None,
            'plan_kinds': sorted(c['kind'] for c in plan['changes']) if plan else [],
            'planned_in': next((s for s in REBUILD_STEPS if plans and within(plans[0], s)), None),
            'compact_observed_in': next((s for s in REBUILD_STEPS for e in observed
                                         if e['payload']['kind'] == 'compact' and within(e, s)), None),
            'acknowledged_by': ack['payload']['applied_by'] if ack else None,
            'acknowledged_in': next((s for s in REBUILD_STEPS if ack and within(ack, s)), None),
            'first_request': {s: first_request(s) for s in REBUILD_STEPS[1:]},
            'compaction_requests': sum(bool(r.get('compaction')) for r in messages),
            'efforts_by_step': {s: sorted({r.get('effort') for r in main_rows(s)} - {None}) for s in REBUILD_STEPS}},
        'proxy': {'requests': len(messages), 'http_statuses': [r.get('http_status') for r in messages],
                  'unsettled': sum(r.get('governor_status') != 'settled' for r in messages),
                  'known_cost_usd': known, 'unpriced': sum(r.get('cost_usd') is None for r in messages),
                  'tokens': proxy_tokens},
        'governor': {'spent_usd': policy['spent_usd'], 'cost_complete': policy['cost_complete'],
                     'still_unknown': reconciled['still_unknown'] if reconciled else None,
                     'orphans_without_rows': reconciled['orphans_without_rows'] if reconciled else None,
                     'journal_entries': len(journal)},
        'client': {s: {'result_subtype': finals(s).get('subtype'), 'is_error': finals(s).get('is_error'),
                       'total_cost_usd': finals(s).get('total_cost_usd')} for s in REBUILD_STEPS},
        'client_tokens_last': client_tokens,
        # A resumed session's client totals are cumulative, so its last invocation's cover the whole session. Tokens must
        # match; cost is reported only, since the pinned client may price Sonnet 5.5 reads at the rate before October 7.
        'client_tokens_match': bool(ok) and proxy_tokens == client_tokens,
        'client_cost_matches': isinstance(last.get('total_cost_usd'), (int, float))
                               and abs(last['total_cost_usd'] - known) < 1e-6,
        'hook_events': [e['payload']['event'] for e in journal if e['kind'] == 'hook_event'],
        'hook_errors': len(errors.read_text().splitlines()) if errors.exists() else 0,
        'governor_matches_proxy': bool(ok) and abs(policy['spent_usd'] - known) < 1e-9,
        'applied': any(e['payload'].get('applied') is True for e in journal),
    }


def rebuild_status(report):
    """passed: both mechanisms seen live with exact accounting. inconclusive: everything held, but the correction
    reached the model through a tool hook (the model called a tool after the correction), so the turn's end was not
    tested. failed: anything else."""
    turn, rebuild = report['turn_end'], report['rebuild']
    sound = (all(e.get('returncode') == 0 for e in report['invocations'])
             and len(report['invocations']) == len(REBUILD_STEPS)
             and all(c['result_subtype'] == 'success' and not c['is_error'] for c in report['client'].values())
             and report['proxy']['requests'] > 0 and report['proxy']['unsettled'] == 0
             and all(s == 200 for s in report['proxy']['http_statuses']) and report['proxy']['unpriced'] == 0
             and report['governor']['still_unknown'] == 0 and report['governor']['cost_complete']
             and report['governor_matches_proxy'] and report['client_tokens_match'] and report['hook_errors'] == 0
             and not report['applied'])
    rebuilt = (rebuild['plan'] is not None and rebuild['plan']['trigger'] == 'idle' and rebuild['planned_in'] == 'follow_up'
               and rebuild['plan_kinds'] == ['compact', 'effort'] and rebuild['compact_observed_in'] == 'compact'
               and rebuild['acknowledged_by'] == 'observed' and rebuild['acknowledged_in'] == 'rebuilt'
               and rebuild['efforts_by_step']['rebuilt'] == [report['target_effort']])
    delivered = turn['correction_issued'] and turn['acknowledged'] and turn['obeyed']
    if not (sound and rebuilt and delivered):
        return 'failed'
    if turn['delivered_by'] == ['Stop'] and turn['stop_blocks'] == 1 and turn['stop_events_in_task_turn'] == 2:
        return 'passed'
    return 'inconclusive'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Required to send billable API requests')
    parser.add_argument('--claude', type=Path, help='Claude Code executable (default: claude on PATH)')
    parser.add_argument('--scenario', choices=('correction', 'turn-end-rebuild'), default='correction',
                        help='correction: delivered by a tool hook (September 22). turn-end-rebuild: plan item 12')
    parser.add_argument('--model', help='Default claude-sonnet-4-6 (correction), claude-sonnet-5-5 (turn-end-rebuild)')
    parser.add_argument('--effort', default='low')
    parser.add_argument('--target-effort', default='medium', help='turn-end-rebuild: the effort the rebuild plan moves to')
    parser.add_argument('--budget', type=float, default=.5, help='Client stop threshold, not a billing cap')
    parser.add_argument('--limit-usd', type=float, default=5, help='Dry-run governor limit (never enforced)')
    args = parser.parse_args()
    cli = args.claude or Path(shutil.which('claude') or '')
    if not cli.name or not cli.exists():
        raise SystemExit('Claude Code CLI not found; pass --claude.')
    if args.scenario == 'turn-end-rebuild':
        return main_rebuild(args, cli)
    args.model = args.model or 'claude-sonnet-4-6'
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


def main_rebuild(args, cli):
    from .bench import client_problem, rates, resolve_client
    model = args.model or 'claude-sonnet-5-5'
    table = rates()
    if model not in table:
        raise SystemExit(f'No configured rates for {model}.')
    version = resolve_client(cli)[1]
    problem = client_problem(version)
    steps = len(REBUILD_STEPS)
    if not args.live:
        print(f'Prepared one governed {model} session of {steps} invocations (dry-run governor, API key, isolated '
              f'HOME/config, no retries): (1) a two-token read at effort {args.effort}, the ledger correction issued '
              'during the turn\'s last model call, so only the blocked Stop can deliver it; (2) after an idle gap of '
              f'{REBUILD_IDLE_SECONDS} s, a resumed prompt whose hook shows the queued rebuild ({args.target_effort} '
              f'effort, /compact); (3) /compact; (4) a resumed prompt at effort {args.target_effort}, which the proxy '
              f'should see and acknowledge. Client stop threshold ${args.budget:.2f} per invocation (at most '
              f'${args.budget * steps:.2f} if every invocation reached it; not a hard cap), max 6 turns each. Expected '
              'about $0.10-0.30 and 7 minutes, most of it the idle gap.' + (f' Client: {problem}' if problem else '')
              + ' Add --live.')
        return
    if problem:
        raise SystemExit(problem + ' No billable requests sent.')
    key = os.environ.get('ANTHROPIC_API_KEY') or getpass.getpass('Anthropic API key (hidden): ').strip()
    problem = check_anthropic_key(key)
    if problem:
        raise SystemExit(problem + ' No billable requests sent.')
    run = ROOT/'runs'/('governed-rebuild-' + time.strftime('%Y%m%d-%H%M%S'))
    print(f'Running a billable governed session ({steps} invocations, about 7 minutes); results: {run}', flush=True)
    report = run_rebuild_session(run, cli, key, 'https://api.anthropic.com', table, model=model, effort=args.effort,
                                 target_effort=args.target_effort, budget_usd=args.budget, limit_usd=args.limit_usd,
                                 **rebuild_scenario())
    print(json.dumps(report, indent=2))
    if report['status'] != 'passed':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
