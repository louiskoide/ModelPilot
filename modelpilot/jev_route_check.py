"""One-task end-to-end Jev routing preflight through the stock pinned launcher.

Runs real Claude Code via `bin/jev-claude.mjs` with an isolated HOME, TMPDIR and
config directory, no concrete --model (so the jev-router sentinel is routed),
and JEV_DEBUG so the upstream proxy reports decisions and the model that served
each response. Billable: Anthropic generation plus one or more TypeSafe decisions.
"""
import argparse
import getpass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

DECISION = re.compile(r'^\[jev\] (\w+) (\d+)ms p=([0-9.]+) (\w+) -> (\w+) \(([^)]*)\)', re.M)
REWRITE = re.compile(r'^\[jev\] (\w+) rewrite jev-router -> (\S+)$', re.M)
SERVED = re.compile(r'^\[jev\] (\d{3}) served by (\S+)$', re.M)
FAILURES = ('routing failed', 'no-jev', 'could not read Claude model catalog', 'passthrough, could not process body',
            'upstream error', 'no JEV_API_KEY found')


def parse_events(stdout):
    events = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def validate(events, stderr, decisions, token):
    """Routing counts only with a real Jev decision, a sentinel rewrite and wire-confirmed serving model."""
    result = next((e for e in reversed(events) if e.get('type') == 'result'), {})
    tools = [b.get('name') for e in events if e.get('type') == 'assistant'
             for b in e.get('message', {}).get('content', []) if b.get('type') == 'tool_use']
    routed = DECISION.findall(stderr)
    rewrites = [model for _, model in REWRITE.findall(stderr)]
    served = [model for status, model in SERVED.findall(stderr) if status == '200']
    failures = [f for f in FAILURES if f in stderr]
    usage_models = sorted((result.get('modelUsage') or {}).keys())
    decision = decisions[0] if len(decisions) == 1 else {}
    selected = decision.get('model')
    jev = decision.get('jev') or {}
    checks = {
        'task_succeeded': result.get('subtype') == 'success' and not result.get('is_error') and token in str(result.get('result', '')),
        'read_tool_used': 'Read' in tools,
        'one_jev_decision': len(routed) == 1 and len(decisions) == 1,
        'decision_has_exchange': isinstance(jev.get('request'), dict) and bool(jev['request'])
                                 and isinstance(jev.get('response'), dict) and bool(jev['response']),
        'confidence_valid': type(decision.get('confidence')) in (int, float) and 0 <= decision['confidence'] <= 1,
        # The turn's opening request and its tool continuation both carry the sentinel.
        'continuations_stay_on_selection': len(rewrites) >= 2 and set(rewrites) == {selected},
        'selected_model_served': bool(selected) and selected in served,
        'selected_model_billed_by_client': bool(selected) and selected in usage_models,
        'no_fallback_or_errors': not failures,
    }
    hint = None
    if rewrites and not routed:
        hint = ('Sentinel rewritten without any Jev decision: Jev extracted no routable prompt from this client '
                'request shape (run python3 -m modelpilot.jev_compat). This is silent fail-open, not routing.')
    return {'passed': all(checks.values()), 'checks': checks, 'hint': hint, 'selected_model': selected,
            'reason': decision.get('reason'), 'confidence': decision.get('confidence'),
            'jev_ms': int(routed[0][1]) if routed else None, 'rewrites': rewrites, 'served_models': served,
            'fallback_markers': failures, 'tool_names': tools, 'client_cost_usd': result.get('total_cost_usd'),
            'client_model_usage': result.get('modelUsage'), 'num_turns': result.get('num_turns')}


def child_env(base, jev_key, anthropic_key, home, tmp, config, cli_dir):
    """Explicit environment: nothing inherited that could pin a model or leak user settings."""
    keep = ('LANG', 'LC_ALL', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'NODE_EXTRA_CA_CERTS')
    env = {name: base[name] for name in keep if name in base}
    node_dir = str(Path(shutil.which('node', path=base.get('PATH')) or 'node').parent)
    env.update(PATH=os.pathsep.join([str(cli_dir), node_dir, '/usr/bin', '/bin']), HOME=str(home), TMPDIR=str(tmp),
               CLAUDE_CONFIG_DIR=str(config), JEV_API_KEY=jev_key, ANTHROPIC_API_KEY=anthropic_key,
               JEV_DEBUG='1', JEV_NO_STATUSLINE='1', DISABLE_AUTOUPDATER='1',
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1')
    return env


def command(jev, prompt, budget):
    # No --model: a concrete model bypasses Jev. The launcher itself adds --add-dir for its checkout.
    return ['node', str(jev/'bin/jev-claude.mjs'), '-p', prompt, '--output-format', 'stream-json', '--verbose',
            '--max-turns', '5', '--max-budget-usd', f'{budget:.2f}', '--no-session-persistence',
            '--setting-sources', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
            '--tools', 'Read', '--allowedTools', 'Read']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Required: sends billable Anthropic and TypeSafe requests')
    parser.add_argument('--jev-root', type=Path)
    parser.add_argument('--claude', type=Path, help='Claude Code executable (default: work/claude-client, then PATH)')
    parser.add_argument('--budget', type=float, default=.5, help='Claude Code stop threshold in USD; not a hard billing cap')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    pin = json.loads((root/'configs/jev-baseline.json').read_text())
    jev = (args.jev_root or root/'work/jev-router-baseline').resolve()
    local = root/'work/claude-client/node_modules/.bin/claude'
    cli = args.claude.resolve() if args.claude else local if local.exists() else Path(shutil.which('claude') or '')
    if not args.live:
        print(f'Plan: one Read-tool task through stock {jev.name} with the jev-router sentinel; Claude Code stop threshold '
              f'${args.budget:.2f} (not a hard cap), max 5 turns, isolated HOME/config; TypeSafe cost unpriced. Add --live.')
        return
    if not (0 < args.budget <= 2):
        raise SystemExit('Budget threshold must be in (0, 2] USD.')
    if not shutil.which('node') or not cli.name:
        raise SystemExit('Node and the Claude Code CLI are required.')
    if not (jev/'.git').exists():
        raise SystemExit(f'No Jev checkout at {jev}; see docs/jev-baseline-setup.md.')
    revision = subprocess.run(['git', '-C', str(jev), 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True).stdout.strip()
    dirty = subprocess.run(['git', '-C', str(jev), 'status', '--porcelain', '--untracked-files=no'], capture_output=True, text=True, check=True).stdout.strip()
    if revision != pin['commit'] or dirty:
        raise SystemExit('Jev checkout must match the pinned revision with no tracked modifications.')
    jev_key = os.environ.get('JEV_API_KEY') or os.environ.get('TYPESAFE_API_KEY') or getpass.getpass('TypeSafe/Jev API key (hidden): ').strip()
    anthropic_key = os.environ.get('ANTHROPIC_API_KEY') or getpass.getpass('Anthropic API key (hidden): ').strip()
    if not jev_key or any(c.isspace() for c in jev_key):
        raise SystemExit('Missing or malformed TypeSafe key. No requests sent.')
    if not anthropic_key.startswith('sk-ant-') or any(c.isspace() for c in anthropic_key):
        raise SystemExit('Anthropic key format is invalid. No requests sent.')
    run = root/'runs'/('jev-route-'+time.strftime('%Y%m%d-%H%M%S'))
    run.parent.mkdir(exist_ok=True)
    run.mkdir(mode=0o700)
    dirs = {name: run/name for name in ('home', 'tmp', 'config', 'fixture')}
    for path in dirs.values():
        path.mkdir(mode=0o700)
    token = 'JEV_ROUTE_' + uuid.uuid4().hex[:12].upper()
    (dirs['fixture']/'sample.txt').write_text(token + '\n')
    prompt = 'Use the Read tool to read sample.txt in the current directory. Reply with exactly the token in that file.'
    env = child_env(os.environ, jev_key, anthropic_key, dirs['home'], dirs['tmp'], dirs['config'], cli.parent)
    report = {'status': 'failed', 'baseline_commit': revision, 'stock_launcher': True, 'harness_patch': None,
              'router_cost_usd': None, 'cost_complete': False, 'budget_threshold_usd': args.budget,
              'scope': 'Single synthetic Read task; validates routing mechanics, not quality or savings.'}
    print(f'Running billable Jev routing preflight; results: {run}', flush=True)
    started = time.monotonic()
    try:
        version = subprocess.run([str(cli), '--version'], env=env, cwd=dirs['fixture'], capture_output=True, text=True, timeout=30)
        report['client_version'] = version.stdout.strip()
        result = subprocess.run(command(jev, prompt, args.budget), env=env, cwd=dirs['fixture'],
                                capture_output=True, text=True, timeout=300)
        report['wall_seconds'] = round(time.monotonic() - started, 3)
        stdout, stderr = result.stdout, result.stderr
        for key in (jev_key, anthropic_key):
            stdout, stderr = stdout.replace(key, '[REDACTED]'), stderr.replace(key, '[REDACTED]')
        (run/'stdout.jsonl').write_text(stdout)
        (run/'stderr.txt').write_text(stderr)
        decisions = []
        for path in sorted((dirs['tmp']/'jev-claude').glob('*.json')) if (dirs['tmp']/'jev-claude').is_dir() else []:
            try:
                data = json.loads(path.read_text())
            except ValueError:
                continue
            # Each session file holds the latest decision plus a history of every decision.
            if isinstance(data, dict) and 'jev' in data:
                decisions.extend(d for d in data.get('history') or [data] if isinstance(d, dict))
        (run/'decisions.json').write_text(json.dumps(decisions, indent=2))
        validation = validate(parse_events(stdout), stderr, decisions, token)
        report.update(returncode=result.returncode, **validation)
        report['status'] = 'passed' if validation['passed'] and result.returncode == 0 else 'failed'
    except subprocess.TimeoutExpired:
        report['status'] = 'timeout'
    (run/'summary.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    if report['status'] != 'passed':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
