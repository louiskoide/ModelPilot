"""Small real Claude Code check through M1. Run from a Terminal with your API key."""
import argparse
import getpass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
import threading
import time
from .proxy import ProxyServer
from .m2 import State


def validate_result(events, expect_tool):
    results = [e for e in events if e.get('type') == 'result']
    tools = [b.get('name') for e in events if e.get('type') == 'assistant'
             for b in e.get('message', {}).get('content', []) if b.get('type') == 'tool_use']
    result = results[-1] if results else {}
    answer = result.get('result', '')
    passed = (result.get('subtype') == 'success' and not result.get('is_error')
              and ('MODEL_PILOT_OK' in answer if expect_tool else answer.strip() in ('OK', 'OK.'))
              and (not expect_tool or 'Read' in tools))
    return {'passed': passed, 'result_subtype': result.get('subtype'),
            'tool_names': tools, 'client_cost_usd': result.get('total_cost_usd'),
            'client_usage': result.get('usage'), 'event_count': len(events)}


def validate_m2(events, expected, required_calls):
    result = validate_result(events, False)
    final = next((e for e in reversed(events) if e.get('type') == 'result'), {})
    calls = [b for e in events if e.get('type') == 'assistant'
             for b in e.get('message', {}).get('content', []) if b.get('type') == 'tool_use']
    successes = {b.get('tool_use_id') for e in events if e.get('type') == 'user'
                 for b in e.get('message', {}).get('content', [])
                 if isinstance(b, dict) and b.get('type') == 'tool_result' and not b.get('is_error', False)}
    verified = all(any(c.get('name') == name and c.get('id') in successes
                       and all(c.get('input', {}).get(k) == v for k, v in values.items())
                       for c in calls) for name, values in required_calls)
    result['passed'] = (final.get('subtype') == 'success' and not final.get('is_error')
                        and all(value in final.get('result', '') for value in expected) and verified)
    result['required_tool_calls_verified'] = verified
    return result


def m2_cases(root, run):
    state = State(run/'m2.sqlite3')
    marker = 'OUTPUT_' + uuid.uuid4().hex
    reference = state.store_output('x'*5000 + '\n' + marker + '\n' + 'y'*5000)
    task = state.create('M2 integration fixture', 'Obsolete instruction')
    task_id = task['id']
    state.claim(task_id, 1, 'fixture-worker', seconds=1800)
    corrected = 'CORRECTION_' + uuid.uuid4().hex
    state.correct(task_id, 1, 'Current verification token: ' + corrected)
    state.acknowledge(task_id, 2, 'fixture-worker')
    for _ in range(3):
        state.observe(task_id, 2, 'fixture-worker', {'error': 'Synthetic repeated failure'})
    state.close()
    # Absolute module path works even when the client starts the server in its fixture directory.
    bootstrap = 'import sys; sys.path.insert(0, ' + repr(str(root)) + '); from modelpilot.m2_mcp import main; main()'
    config = {'mcpServers': {'modelpilot': {'command': sys.executable,
              'args': ['-c', bootstrap, '--db', str(run/'m2.sqlite3')],
              'env': {'ANTHROPIC_API_KEY': '', 'CLAUDE_CODE_OAUTH_TOKEN': ''}}}}
    config_path = run/'mcp.json'
    config_path.write_text(json.dumps(config, indent=2))
    prefix = 'mcp__modelpilot__'
    cases = [
        ('expand_output', f'Call the modelpilot expand_output tool for handle {reference["handle"]}, offset 5000, limit 100. Report the OUTPUT_ verification token from that page. Do not guess.',
         [marker], [(prefix+'expand_output', {'handle': reference['handle'], 'offset': 5000, 'limit': 100})]),
        ('corrected_task', f'Call modelpilot get_task and stuck_recommendation for task {task_id}. Report the current verification token, REVISION=<revision>, and the escalation recommendation. Use the actual tool results.',
         [corrected, 'REVISION=2', 'increase_effort'], [(prefix+'get_task', {'task': task_id}), (prefix+'stuck_recommendation', {'task': task_id})])]
    return config_path, cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', choices=['m1','m2'], default='m1')
    parser.add_argument('--live', action='store_true', help='Required to send billable API requests')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    local = root/'work/claude-client/node_modules/.bin/claude'
    cli = str(local) if local.exists() else shutil.which('claude')
    if not cli:
        raise SystemExit('Claude Code is missing. Install the official CLI first.')
    if not args.live:
        print('Prepared two Sonnet integration checks for '+args.suite+'. Add --live to run.')
        return
    key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not key:
        key = getpass.getpass('Paste your API key (hidden), then press Return: ').strip()
    if not key.startswith('sk-ant-') or any(c.isspace() for c in key):
        raise SystemExit('Key format is invalid. No API requests sent.')
    run = root/'runs'/(args.suite+'-claude-'+time.strftime('%Y%m%d-%H%M%S'))
    run.mkdir(mode=0o700)
    scratch = run/'fixture'
    scratch.mkdir()
    (scratch/'sample.txt').write_text('MODEL_PILOT_OK\n')
    config_dir = run/'client-config'
    config_dir.mkdir()
    mcp_config, m2_checks = m2_cases(root, run) if args.suite == 'm2' else (None, None)
    config = json.loads((root/'configs/m0.json').read_text())
    env = os.environ.copy()
    for name in list(env):
        if name.startswith('ANTHROPIC_') or name.startswith('CLAUDE_CODE_USE_') or name == 'CLAUDE_CODE_OAUTH_TOKEN':
            del env[name]
    env.update(ANTHROPIC_API_KEY=key, CLAUDE_CONFIG_DIR=str(config_dir),
               DISABLE_AUTOUPDATER='1', CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1')
    proxy = ProxyServer(('127.0.0.1', 0), 'https://api.anthropic.com', run/'observations.jsonl', config['rates'])
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    env['ANTHROPIC_BASE_URL'] = f'http://127.0.0.1:{proxy.server_port}'
    report = {'status': 'running', 'cases': [], 'routing_applied': False,
              'limits': 'Two checks; $0.50 client stop threshold per check, not a guaranteed billing ceiling.'}
    print(f'Running real API checks; results: {run}', flush=True)
    try:
        version = subprocess.run([cli, '--version'], env=env, cwd=scratch, capture_output=True, text=True, timeout=30)
        report['client_version'] = version.stdout.strip()
        cases = m2_checks if args.suite == 'm2' else [
            ('text', 'Reply with exactly OK. Do not use any tools.', False),
            ('read_tool', 'Use the Read tool to read sample.txt in the current directory. Reply with exactly the token in that file.', True)]
        for case_spec in cases:
            name, prompt = case_spec[:2]
            expect_tool = args.suite == 'm1' and case_spec[2] is True
            command = [cli, '-p', prompt, '--model', 'claude-sonnet-4-6', '--effort', 'low',
                       '--output-format', 'stream-json', '--verbose', '--include-partial-messages',
                       '--max-turns', '5', '--max-budget-usd', '0.50', '--no-session-persistence',
                       '--setting-sources', '', '--strict-mcp-config', '--mcp-config', str(mcp_config) if mcp_config else '{"mcpServers":{}}',
                       '--tools', 'Read' if expect_tool else '', '--allowedTools', 'mcp__modelpilot__expand_output,mcp__modelpilot__get_task,mcp__modelpilot__stuck_recommendation' if mcp_config else 'Read']
            result = subprocess.run(command, env=env, cwd=scratch, capture_output=True, text=True, timeout=180)
            # Only this isolated synthetic session's output is stored; redact key defensively.
            (run/f'{name}.stdout.jsonl').write_text(result.stdout.replace(key, '[REDACTED]'))
            (run/f'{name}.stderr.txt').write_text(result.stderr.replace(key, '[REDACTED]'))
            events = []
            for line in result.stdout.splitlines():
                try:
                    event = json.loads(line)
                    if isinstance(event, dict):
                        events.append(event)
                except ValueError:
                    pass
            validation = validate_m2(events, case_spec[2], case_spec[3]) if mcp_config else validate_result(events, expect_tool)
            case = dict(name=name, returncode=result.returncode, **validation)
            case['passed'] = case['passed'] and result.returncode == 0
            report['cases'].append(case)
            print(name, 'PASS' if case['passed'] else 'FAIL', flush=True)
            if not case['passed']:
                break
        report['status'] = 'passed' if len(report['cases']) == 2 and all(c['passed'] for c in report['cases']) else 'failed'
    except subprocess.TimeoutExpired:
        report['status'] = 'timeout'
    finally:
        proxy.shutdown()
        thread.join()
        proxy.server_close()
        rows = [json.loads(line) for line in (run/'observations.jsonl').read_text().splitlines() if line.strip()]
        report['proxy_requests'] = len(rows)
        report['proxy_statuses'] = [r['http_status'] for r in rows]
        report['proxy_known_cost_usd'] = sum(r['cost_usd'] for r in rows if r['cost_usd'] is not None)
        report['proxy_unpriced_requests'] = sum(r['cost_usd'] is None for r in rows)
        client_costs = [case['client_cost_usd'] for case in report['cases']]
        report['accounting_matches'] = (bool(rows) and report['proxy_unpriced_requests'] == 0
                                        and all(isinstance(value, (int, float)) for value in client_costs)
                                        and abs(sum(client_costs)-report['proxy_known_cost_usd']) < 0.000001)
        if not rows or not report['accounting_matches'] or any(r['applied'] for r in rows) or any(r['http_status'] != 200 for r in rows):
            report['status'] = 'failed'
        (run/'summary.json').write_text(json.dumps(report, indent=2)+'\n')
        print(json.dumps(report, indent=2), flush=True)
    if report['status'] != 'passed':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
