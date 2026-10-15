"""Client-version compatibility matrix (October 15, overnight cloud run). $0, a report only: the pin stays 2.1.284.

    python3 -m modelpilot.client_matrix --versions 2.1.284,2.1.289 [--latest 3] [--install] --out runs/<new dir>

Each version comes from the ignored work/claude-client-<version> (2.1.284 also from work/claude-client); with
--install, a missing one is installed there with npm at that exact version, no login and no global install. It is
then driven only against loopback fixtures, through the proxy, with an empty HOME, a fake key, no inherited
environment and CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1. Per version:

- shape: the request structure (thinking_probe.capture_shape) against tests/fixtures/claude-2.1.284-shape.json (the
  trailing system message, betas, cache_control lifetimes) and whether the proxy still recognises its /compact
  request (proxy.COMPACTION_MARKER);
- hooks: every hook payload's field paths against tests/fixtures/hooks-2.1.280/;
- permissions: the mode the client starts in, and what one Bash call sends and whether it runs;
- failures at CLAUDE_CODE_MAX_RETRIES=0: what the client sends after a 400, a 429, a 529, a stream cut short and a
  stream left idle (the proxy's upstream timeout lowered to 2 s here), its exit code and its result;
- totals: the proxy's tokens against the client's own (bench.accounting's tokens_match).
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from .fixtures import fixture_server, scripted_response

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = '@anthropic-ai/claude-code'
FAKE_KEY = 'sk-ant-offline-fixture-not-a-key'
MODEL = 'claude-sonnet-5-5'
RATES = {MODEL: dict(input=2, output=10, read=.1, write_5m=2.5, write_1h=4)}
SHAPE_FIXTURE = ROOT/'tests/fixtures/claude-2.1.284-shape.json'
HOOK_FIXTURES = ROOT/'tests/fixtures/hooks-2.1.280'
HOOK_EVENTS = ('SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'PostToolUseFailure', 'Stop')
FAILURES = ('http_400', 'http_429', 'http_529', 'cut_stream', 'idle_stream')
IDLE_TIMEOUT = 2  # the proxy's upstream timeout in the idle-stream case (120 s in production)


def client_path(version, root=ROOT):
    """The client binary for a version: work/claude-client-<version>, or work/claude-client when it holds that version."""
    own = root/f'work/claude-client-{version}/node_modules/.bin/claude'
    pinned = root/'work/claude-client/node_modules/.bin/claude'
    if not own.exists() and pinned.exists() and client_version(pinned) == version:
        return pinned
    return own


def client_version(cli):
    with tempfile.TemporaryDirectory() as home:
        out = subprocess.run([str(cli), '--version'], env={'HOME': home, 'PATH': os.environ.get('PATH', '')},
                             capture_output=True, text=True, timeout=60)
    return out.stdout.split(' ')[0].strip() if out.returncode == 0 else None


def install(version, root=ROOT):
    """npm install of exactly this version into the ignored work/claude-client-<version>; nothing global."""
    target = root/f'work/claude-client-{version}'
    target.mkdir(parents=True, exist_ok=True)
    subprocess.run(['npm', 'install', '--prefix', str(target), '--save-exact', '--no-audit', '--no-fund',
                    f'{PACKAGE}@{version}'], check=True, capture_output=True, timeout=600)
    return target/'node_modules/.bin/claude'


def latest(n):
    """The newest n published versions (npm view)."""
    out = subprocess.run(['npm', 'view', PACKAGE, 'versions', '--json'], capture_output=True, text=True, check=True,
                         timeout=120)
    return json.loads(out.stdout)[-n:]


def environment(cli, dirs, base_url, extra=None):
    """Nothing inherited but PATH's node: a fake key, an empty HOME and config, loopback only, no retries."""
    from .governed_session import client_env
    env = client_env({'PATH': os.environ.get('PATH', '')}, FAKE_KEY, dirs, cli, {'ANTHROPIC_BASE_URL': base_url})
    env.update(extra or {})
    return env


def workdirs(root):
    dirs = {name: Path(root)/name for name in ('workspace', 'home', 'tmp', 'config')}
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def invoke(cli, dirs, base_url, prompt, *, session=None, resume=None, tools='', settings=None, timeout=180, extra=None):
    """One -p invocation; (exit code, stream-json events, stderr)."""
    command = [str(cli), '-p', prompt, '--model', MODEL, '--effort', 'low', '--output-format', 'stream-json',
               '--verbose', '--max-turns', '6', '--max-budget-usd', '1.00', '--setting-sources', '',
               '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--tools', tools]
    command += ['--allowedTools', tools] if tools else []
    command += ['--settings', str(settings)] if settings else []
    command += ['--resume', resume] if resume else ['--session-id', session] if session else ['--no-session-persistence']
    try:
        result = subprocess.run(command, env=environment(cli, dirs, base_url, extra), cwd=dirs['workspace'],
                                capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None, [], 'timeout'
    events = []
    for line in result.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    return result.returncode, events, result.stderr[-2000:]


def final_result(events):
    return next((e for e in reversed(events) if e.get('type') == 'result'), {})


class Proxied:
    """The fixture (or another upstream) behind a ProxyServer, both on loopback; rows from the proxy's log."""
    def __init__(self, upstream, root, timeout=None):
        from .proxy import ProxyServer

        class Proxy(ProxyServer):
            def connection(self, t=120):
                return super().connection(timeout or t)
        self.upstream, self.log = upstream, Path(root)/'observations.jsonl'
        self.proxy = Proxy(('127.0.0.1', 0), f'http://127.0.0.1:{upstream.server_port}', self.log, RATES)
        self.threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (upstream, self.proxy)]
        for t in self.threads:
            t.start()
        self.url = f'http://127.0.0.1:{self.proxy.server_port}'

    def rows(self):
        from .proxy import read_log
        self.proxy.wait_idle(30)
        return read_log(self.log)

    def close(self):
        for s in (self.proxy, self.upstream):
            s.shutdown()
            s.server_close()
        for t in self.threads:
            t.join()


def cache_lifetimes(node, found=None):
    found = set() if found is None else found
    if isinstance(node, dict):
        if isinstance(node.get('cache_control'), dict):
            found.add(node['cache_control'].get('ttl', '5m'))
        for value in node.values():
            cache_lifetimes(value, found)
    elif isinstance(node, list):
        for value in node:
            cache_lifetimes(value, found)
    return found


def shape_diff(shape, reference):
    """What changed against the reference shape, per model: keys, betas, the trailing system message, effort."""
    out = {}
    for model, ref in reference['requests'].items():
        got = shape['requests'].get(model)
        if got is None:
            out[model] = {'captured': False}
            continue
        out[model] = {
            'captured': True,
            'top_level_keys': {'added': sorted(set(got['top_level_keys']) - set(ref['top_level_keys'])),
                               'removed': sorted(set(ref['top_level_keys']) - set(got['top_level_keys']))},
            'betas': {'added': sorted(set(got['anthropic_beta']) - set(ref['anthropic_beta'])),
                      'removed': sorted(set(ref['anthropic_beta']) - set(got['anthropic_beta']))},
            'system_after_prompt': got['system_after_prompt'], 'system_after_tool_result': got['system_after_tool_result'],
            'trailing_system_message': bool(got['messages']) and got['messages'][-1]['role'] == 'system',
            'message_roles_same': [m['role'] for m in got['messages']] == [m['role'] for m in ref['messages']],
            'effort': got['effort'], 'context_management_same': got['context_management'] == ref['context_management'],
            'thinking': got['thinking']}
    return out


def shape_probe(cli):
    from .thinking_probe import capture_shape
    try:
        shape = capture_shape(cli)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        return {'error': type(error).__name__, 'detail': str(error)[:200]}
    return {'diff': shape_diff(shape, json.loads(SHAPE_FIXTURE.read_text()))}


def compaction_probe(cli):
    """A turn, then /compact on the resumed session, through the proxy: does the proxy flag the compaction request, and
    which lifetimes do the requests' breakpoints ask for; and do the proxy's tokens match the client's."""
    from .bench import accounting
    with tempfile.TemporaryDirectory() as tmp:
        upstream = fixture_server()
        upstream.keep_bodies = True
        upstream.script = [{'text': 'DONE'}, {'text': 'Summary: done.'}]
        proxied = Proxied(upstream, tmp)
        try:
            dirs = workdirs(tmp)
            session = str(uuid.uuid4())
            first = invoke(cli, dirs, proxied.url, 'Reply DONE.', session=session, tools='Read')
            second = invoke(cli, dirs, proxied.url, '/compact', resume=session, tools='Read')
            rows = proxied.rows()
            bodies = [json.loads(b) for b in upstream.bodies]
        finally:
            proxied.close()
    messages = [r for r in rows if r.get('kind') == 'messages']
    totals = accounting(rows, final_result(second[1]))
    return {'exit_codes': [first[0], second[0]], 'requests': len(messages),
            'compaction_flagged': sum(bool(r.get('compaction')) for r in messages),
            'cache_lifetimes': sorted(set().union(*(cache_lifetimes(b) for b in bodies))) if bodies else [],
            'tokens_match': totals['tokens_match'], 'proxy_tokens': totals['proxy_tokens'],
            'client_tokens': totals['client_tokens']}


def key_paths(node, prefix=''):
    """Field paths of a payload, list items as []; values never kept."""
    if isinstance(node, dict):
        out = set()
        for key, value in node.items():
            out |= {f'{prefix}{key}'} | key_paths(value, f'{prefix}{key}.')
        return out
    if isinstance(node, list):
        return set().union(*(key_paths(v, f'{prefix}[].') for v in node)) if node else set()
    return set()


HOOK_SCRIPT = '''
import json, sys, time
event = sys.argv[1]
payload = json.load(sys.stdin)
name = event + ('-' + payload.get('tool_name', '') if event.startswith('PostToolUse') and event != 'PostToolUseFailure' else '')
open(f"{sys.argv[2]}/{name}-{time.time_ns()}.json", 'w').write(json.dumps(payload))
'''


def hooks_probe(cli):
    """Every hook event's payload field paths against the 2.1.280 recording (Read, Write and a failing Bash)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        dirs = workdirs(tmp)
        (dirs['workspace']/'a.txt').write_text('TOKEN_A\n')
        (tmp/'hook.py').write_text(HOOK_SCRIPT)
        (tmp/'payloads').mkdir()
        hooks = {event: [{'matcher': '*', 'hooks': [{'type': 'command', 'command':
                                                     f'{shutil.which("python3") or "python3"} {tmp/"hook.py"} {event} {tmp/"payloads"}'}]}]
                 for event in HOOK_EVENTS}
        (tmp/'settings.json').write_text(json.dumps({'hooks': hooks}))
        work = dirs['workspace']
        upstream = fixture_server()
        upstream.script = [{'tool': 'Read', 'input': {'file_path': str(work/'a.txt')}},
                           {'tool': 'Write', 'input': {'file_path': str(work/'b.txt'), 'content': 'B\n'}},
                           {'tool': 'Bash', 'input': {'command': 'exit 3', 'description': 'fail on purpose'}},
                           {'text': 'DONE'}]
        proxied = Proxied(upstream, tmp)
        try:
            code, events, _ = invoke(cli, dirs, proxied.url, 'Read a.txt, write b.txt, run the check.',
                                     tools='Read,Write,Bash', settings=tmp/'settings.json')
        finally:
            proxied.close()
        seen = {}
        for path in sorted((tmp/'payloads').glob('*.json')):
            name = path.name.rsplit('-', 1)[0]
            seen.setdefault(name, set()).update(key_paths(json.loads(path.read_text())))
    out = {'exit_code': code, 'events': {}}
    for fixture in sorted(HOOK_FIXTURES.glob('*.json')):
        name = fixture.stem
        reference = key_paths(json.loads(fixture.read_text()))
        got = seen.get(name)
        out['events'][name] = ({'seen': False} if got is None else
                               {'seen': True, 'added': sorted(got - reference), 'removed': sorted(reference - got)})
    out['other_events_seen'] = sorted(set(seen) - {f.stem for f in HOOK_FIXTURES.glob('*.json')})
    return out


def permission_probe(cli):
    """One Bash call that exits 3, the client's default permission mode: the mode it starts in, every request it sends
    (model, tool-bearing, streamed) and whether the command ran (its tool result says exit code 3) or was refused.
    Found October 15: from 2.1.289 the client starts in permissionMode auto and asks a classifier model before Bash,
    which the fixture can't answer, so the call is refused."""
    with tempfile.TemporaryDirectory() as tmp:
        upstream = fixture_server()
        upstream.keep_bodies = True
        upstream.script = [{'tool': 'Bash', 'input': {'command': 'exit 3', 'description': 'fail on purpose'}},
                           {'text': 'DONE'}]
        proxied = Proxied(upstream, tmp)
        try:
            dirs = workdirs(tmp)
            code, events, _ = invoke(cli, dirs, proxied.url, 'Run the check.', tools='Bash')
            rows = proxied.rows()
        finally:
            proxied.close()
    bodies = [json.loads(b) for b in upstream.bodies]
    init = next((e for e in events if e.get('type') == 'system' and e.get('subtype') == 'init'), {})
    results = [b for body in bodies[-1:] for m in body.get('messages', []) if isinstance(m.get('content'), list)
               for b in m['content'] if isinstance(b, dict) and b.get('type') == 'tool_result']
    ran = any('Exit code 3' in json.dumps(b.get('content')) for b in results)
    side = [b for b in bodies if not b.get('tools')]
    return {'exit_code': code, 'permission_mode': init.get('permissionMode'), 'requests': len(bodies),
            'side_requests': len(side), 'side_models': sorted({b.get('model') for b in side}),
            'bash_ran': ran, 'priced_rows': sum(r.get('cost_usd') is not None for r in rows if r.get('kind') == 'messages'),
            'rows': len([r for r in rows if r.get('kind') == 'messages'])}


class FailingUpstream(BaseHTTPRequestHandler):
    """The first tool-bearing Messages request fails as server.failure says; every later one gets the scripted reply."""
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers['Content-Length']))
        request = json.loads(raw)
        main = self.path.split('?', 1)[0] == '/v1/messages' and request.get('tools')
        if main:
            with self.server.lock:
                self.server.main_requests.append({'stream': request.get('stream', False)})
                first = len(self.server.main_requests) == 1
        else:
            first = False
        failure = self.server.failure if first else None
        if failure in ('http_400', 'http_429', 'http_529'):
            status = int(failure[-3:])
            kind = {400: 'invalid_request_error', 429: 'rate_limit_error', 529: 'overloaded_error'}[status]
            body = json.dumps({'type': 'error', 'error': {'type': kind, 'message': 'fixture'}}).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            if status != 400:
                self.send_header('retry-after', '1')
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.split('?', 1)[0] == '/v1/messages/count_tokens':
            status, content_type, data = 200, 'application/json', json.dumps({'input_tokens': 10}).encode()
        else:
            status, content_type, data = scripted_response(request, [{'text': 'DONE'}])
        if failure in ('cut_stream', 'idle_stream') and request.get('stream'):
            head = data[:data.index(b'event: content_block_start')]  # message_start only
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(head)
            self.wfile.flush()
            if failure == 'idle_stream':
                time.sleep(IDLE_TIMEOUT + 3)
            self.close_connection = True
            return
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def failure_probe(cli, failure):
    with tempfile.TemporaryDirectory() as tmp:
        upstream = ThreadingHTTPServer(('127.0.0.1', 0), FailingUpstream)
        upstream.failure, upstream.main_requests, upstream.lock = failure, [], threading.Lock()
        proxied = Proxied(upstream, tmp, timeout=IDLE_TIMEOUT if failure == 'idle_stream' else None)
        try:
            dirs = workdirs(tmp)
            started = time.monotonic()
            code, events, stderr = invoke(cli, dirs, proxied.url, 'Reply DONE.', tools='Read', timeout=120)
            seconds = round(time.monotonic() - started, 1)
            rows = proxied.rows()
        finally:
            proxied.close()
    result = final_result(events)
    sent = upstream.main_requests
    return {'exit_code': code, 'result': result.get('subtype'), 'is_error': result.get('is_error'),
            'main_requests': len(sent), 'resent': len(sent) > 1,
            'resent_without_streaming': any(not r['stream'] for r in sent[1:]), 'seconds': seconds,
            'proxy_rows': [r.get('status') for r in rows if r.get('kind') == 'messages']}


def probe(version, root=ROOT, do_install=False, failures=FAILURES):
    cli = client_path(version, root)
    if not cli.exists() and do_install:
        cli = install(version, root)
    if not cli.exists():
        return {'version': version, 'installed': False}
    seen = client_version(cli)
    out = {'version': version, 'installed': True, 'reports': seen, 'path': str(cli.relative_to(root))
           if cli.is_relative_to(root) else str(cli)}
    if seen != version:
        return dict(out, error='the binary reports another version')
    out['shape'] = shape_probe(cli)
    out['compaction'] = compaction_probe(cli)
    out['hooks'] = hooks_probe(cli)
    out['permissions'] = permission_probe(cli)
    out['failures'] = {f: failure_probe(cli, f) for f in failures}
    return out


def summary_row(entry):
    """One table row: what differs from the pinned 2.1.284."""
    if not entry.get('installed') or entry.get('error'):
        return {'version': entry['version'], 'probed': False}
    shapes = (entry['shape'].get('diff') or {})
    betas = sorted({b for d in shapes.values() if d.get('captured') for b in d['betas']['added']})
    removed = sorted({b for d in shapes.values() if d.get('captured') for b in d['betas']['removed']})
    hooks = entry['hooks']['events']
    return {'version': entry['version'], 'probed': True,
            'shape_captured': all(d.get('captured') for d in shapes.values()) if shapes else False,
            'trailing_system_message': all(d.get('trailing_system_message') for d in shapes.values() if d.get('captured')),
            'betas_added': betas, 'betas_removed': removed,
            'compaction_flagged': entry['compaction']['compaction_flagged'],
            'cache_lifetimes': entry['compaction']['cache_lifetimes'],
            'tokens_match': entry['compaction']['tokens_match'],
            'hook_fields_added': sorted({f'{e}:{p}' for e, h in hooks.items() if h.get('seen') for p in h['added']}),
            'hook_fields_removed': sorted({f'{e}:{p}' for e, h in hooks.items() if h.get('seen') for p in h['removed']}),
            'hook_events_missing': sorted(e for e, h in hooks.items() if not h.get('seen')),
            'permission_mode': entry['permissions']['permission_mode'], 'bash_ran': entry['permissions']['bash_ran'],
            'side_requests_for_one_bash': entry['permissions']['side_requests'],
            'side_models': entry['permissions']['side_models'],
            'failures': {f: {k: v[k] for k in ('exit_code', 'result', 'main_requests', 'resent_without_streaming')}
                         for f, v in entry['failures'].items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--versions', default='2.1.284', help='comma-separated exact versions')
    parser.add_argument('--latest', type=int, default=0, help='also the newest N published versions (npm view)')
    parser.add_argument('--install', action='store_true', help='npm install missing versions into work/claude-client-<v>')
    parser.add_argument('--out', type=Path, required=True, help='a new directory under runs/')
    args = parser.parse_args()
    versions = [v for v in args.versions.split(',') if v]
    versions += [v for v in (latest(args.latest) if args.latest else []) if v not in versions]
    args.out.mkdir(parents=True, exist_ok=False)
    entries = []
    for version in versions:
        entry = probe(version, do_install=args.install)
        (args.out/f'{version}.json').write_text(json.dumps(entry, indent=1) + '\n')
        entries.append(entry)
        print(json.dumps(summary_row(entry)), flush=True)
    table = {'scope': 'loopback fixtures only, $0; a report, not a pin change', 'rows': [summary_row(e) for e in entries]}
    (args.out/'summary.json').write_text(json.dumps(table, indent=1) + '\n')


if __name__ == '__main__':
    main()
