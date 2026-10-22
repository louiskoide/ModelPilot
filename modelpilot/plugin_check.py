"""The Claude Code plugin (`plugin/`) against a real client, at $0: the client runs with `--plugin-dir`, a fake key and
the owned loopback fixture, and this reads what reached the model.

Cases: `control` (no plugin), `plugin`, `plugin_saved_low` (Sonnet 5.5's effort saved as low, what `/effort low` writes
under modelSettings), `plugin_model_opus` (`--model` over the plugin's agent) and `plugin_helper` (helper mode saved as
opus high; the fixture makes the Agent call its note asks for). For each main-loop request it records the model, the
effort, the system prompt's hash and how often the concise text and the helper-mode note arrived in the system prompt
and in the messages; each case also keeps the helper-mode log's events (October 21). `effort_resume` (October 20)
resumes a low-effort session at high, the client's own effort change, and compares the two requests' messages.
`validate` is `claude plugin validate --strict` on the plugin and on the marketplace.

    python3 -m modelpilot.plugin_check --claude work/claude-client/node_modules/.bin/claude --out runs/plugin-check.json
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from .cache_replication import OPUS_5_5, SONNET_5_5
from .fixtures import fixture_server
from .governed_session import client_env

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT/'plugin'
PINNED = ROOT/'work/claude-client/node_modules/.bin/claude'
CONCISE = (ROOT/'bench/prompts/concise.md').read_text().strip()
FAKE_KEY = 'sk-ant-offline-fixture-not-a-key'
SAVED_LOW = {'modelSettings': {SONNET_5_5: {'effortLevel': 'low'}}}
HELPER_NOTE = "ModelPilot helper mode (the user's setting)"
SESSION = '0f5e3c9a-7d1b-4c2e-9a8f-6b4d2e1c0a97'
HELPER = {'args': ('--session-id', SESSION), 'tools': 'Bash,Agent', 'plugin_data': {f'helper-{SESSION}': 'opus high\n'},
          'script': [{'tool': 'Agent', 'input': {'description': 'check', 'prompt': 'Run the check with Bash, then reply DONE.',
                                                 'subagent_type': 'modelpilot:helper-high', 'model': 'opus'}},
                     {'text': 'DONE'}]}
CASES = {'control': {'plugin': False}, 'plugin': {}, 'plugin_saved_low': {'user_settings': SAVED_LOW},
         'plugin_model_opus': {'args': ('--model', OPUS_5_5)}, 'plugin_helper': HELPER}


def texts(value):
    """Every string inside a request field, so the concise text is counted wherever the client put it."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from texts(item)
    elif isinstance(value, list):
        for item in value:
            yield from texts(item)


def summarize(body, tmp_name):
    # The session's own directories appear in it, also in the client's project-directory form ('_' becomes '-').
    system = ''.join(texts(body.get('system'))).replace(tmp_name, 'TMP').replace(tmp_name.replace('_', '-'), 'TMP')
    return {'model': body['model'], 'effort': (body.get('output_config') or {}).get('effort'),
            'system_sha256': hashlib.sha256(system.encode()).hexdigest(), 'system_chars': len(system),
            'concise_in_system': system.count(CONCISE),
            'concise_in_messages': sum(t.count(CONCISE) for t in texts(body['messages'])),
            'helper_note': sum(t.count(HELPER_NOTE) for t in texts([body.get('system'), body['messages']]))}


def capture(cli, plugin=True, args=(), user_settings=None, timeout=180, tools='Bash', plugin_data=None, script=None):
    """One session of the real client against the fixture: by default one Bash call, then DONE. Settings come only from
    the plugin and, if given, a user settings file in the session's own config directory; plugin_data files go in the
    plugin's data directory."""
    cli = Path(cli).absolute()  # the client runs from the session's workspace
    server = fixture_server()
    server.keep_bodies = True
    server.script = script or [{'tool': 'Bash', 'input': {'command': 'true', 'description': 'check'}}, {'text': 'DONE'}]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_name = Path(tmp).name
            dirs = {name: Path(tmp)/name for name in ('workspace', 'home', 'tmp', 'config')}
            for path in dirs.values():
                path.mkdir()
            if user_settings is not None:
                (dirs['config']/'settings.json').write_text(json.dumps(user_settings))
            data = dirs['config']/'plugins/data/modelpilot-inline'  # CLAUDE_PLUGIN_DATA for a --plugin-dir plugin
            for name, text in (plugin_data or {}).items():
                data.mkdir(parents=True, exist_ok=True)
                (data/name).write_text(text)
            env = client_env(os.environ, FAKE_KEY, dirs, cli, {'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{server.server_port}'})
            command = [str(cli), '-p', 'Run the check with Bash, then reply DONE.', '--output-format', 'stream-json',
                       '--verbose', '--max-turns', '4', '--no-session-persistence', '--strict-mcp-config',
                       '--mcp-config', '{"mcpServers":{}}', '--tools', tools, '--allowedTools', tools]
            if plugin:
                command += ['--plugin-dir', str(PLUGIN)]
            done = subprocess.run(command + list(args), env=env, cwd=dirs['workspace'], capture_output=True, text=True,
                                  timeout=timeout, stdin=subprocess.DEVNULL)
            log = data/'helper-log.jsonl'
            log = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    bodies = [json.loads(raw) for raw in server.bodies]
    return {'returncode': done.returncode, 'requests': [summarize(b, tmp_name) for b in bodies if b.get('tools')],
            'helper_log': [{k: e.get(k) for k in ('event', 'helper', 'model', 'effort', 'caller', 'caller_effort')}
                           for e in log]}


def plain(messages):
    """Messages as the cache compares them: no cache markers, string content as one text block."""
    out = []
    for m in messages:
        c = m.get('content')
        c = [{'type': 'text', 'text': c}] if isinstance(c, str) else [
            {k: v for k, v in b.items() if k != 'cache_control'} if isinstance(b, dict) else b for b in c or []]
        out.append(dict(m, content=c))
    return out


def effort_resume(cli, timeout=180):
    """The client's own effort change, plugin on: a session at low effort, then the same session resumed at high. The
    top-level effort of each run's request, the efforts carried on the resumed request's messages, and whether that
    request repeats the first run's last one (system, tools, messages) before adding to it."""
    cli = Path(cli).absolute()
    server = fixture_server()
    server.keep_bodies = True
    server.script = [{'text': 'DONE'}]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    runs = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            dirs = {name: Path(tmp)/name for name in ('workspace', 'home', 'tmp', 'config')}
            for path in dirs.values():
                path.mkdir()
            env = client_env(os.environ, FAKE_KEY, dirs, cli, {'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{server.server_port}'})
            command = [str(cli), '-p', 'Reply DONE.', '--output-format', 'stream-json', '--verbose', '--max-turns', '2',
                       '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--tools', 'Bash', '--plugin-dir', str(PLUGIN)]
            for extra in (['--session-id', SESSION, '--effort', 'low'], ['--resume', SESSION, '--effort', 'high']):
                start = len(server.bodies)
                subprocess.run(command + extra, env=env, cwd=dirs['workspace'], capture_output=True, text=True,
                               timeout=timeout, stdin=subprocess.DEVNULL)
                runs.append([b for b in map(json.loads, server.bodies[start:]) if b.get('tools')])
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    if not all(runs):
        raise RuntimeError('effort_resume: a run sent no main-loop request; see the client flags')
    first, resumed = runs[0][-1], runs[1][0]
    n = len(first['messages'])
    return {'efforts': [(b.get('output_config') or {}).get('effort') for b in (first, resumed)],
            'message_efforts': [[i, m['output_config']['effort']] for i, m in enumerate(resumed['messages'])
                                if 'effort' in (m.get('output_config') or {})],
            'messages': [n, len(resumed['messages'])],
            'prefix_kept': (first.get('system') == resumed.get('system') and first['tools'] == resumed['tools']
                            and plain(first['messages']) == plain(resumed['messages'][:n]))}


def validate(cli):
    """`claude plugin validate --strict` on the plugin and on the marketplace (the repository root)."""
    out = {}
    for name, path in (('plugin', PLUGIN), ('marketplace', ROOT)):
        done = subprocess.run([str(cli), 'plugin', 'validate', '--strict', str(path)], capture_output=True, text=True,
                              timeout=120, stdin=subprocess.DEVNULL)
        out[name] = {'returncode': done.returncode, 'output': (done.stdout + done.stderr).strip()}
    return out


def findings(runs):
    """What the README and tests rely on, read from the cases."""
    main = {name: run['requests'] for name, run in runs.items()}
    single = lambda name, field: sorted({r[field] for r in main[name]})
    return {'models': {name: single(name, 'model') for name in main},
            'efforts': {name: single(name, 'effort') for name in main},
            'system_prompt_unchanged': single('plugin', 'system_sha256') == single('control', 'system_sha256'),
            'concise_once_in_messages': all(r['concise_in_messages'] == 1 for r in main['plugin']),
            'concise_never_in_system': not any(r['concise_in_system'] for rs in main.values() for r in rs),
            'concise_without_plugin': any(r['concise_in_messages'] for r in main['control']),
            'helper': {'settings': sorted({(r['model'], r['effort']) for r in main['plugin_helper']}),
                       'note_once_in_main': all(r['helper_note'] == 1 for r in main['plugin_helper']
                                                if r['model'] == SONNET_5_5),
                       'note_never_in_helper': not any(r['helper_note'] for r in main['plugin_helper']
                                                       if r['model'] != SONNET_5_5),
                       'note_without_mode': any(r['helper_note'] for name, rs in main.items() if name != 'plugin_helper'
                                                for r in rs),
                       'log': log_summary(runs['plugin_helper']['helper_log']),
                       'log_without_mode': any(run['helper_log'] for name, run in runs.items() if name != 'plugin_helper')}}


def log_summary(log):
    """The helper-mode log (hooks/helper-log.sh) of one session: its notes, its launches from the session itself, and
    whether every launch, nested ones included, logged its finish."""
    launches = [e for e in log if e['event'] == 'launch']
    return {'notes': [(e['model'], e['effort']) for e in log if e['event'] == 'note'],
            'session_launches': [(e['helper'], e['model'], e['caller_effort']) for e in launches if not e['caller']],
            'nested_launches': sum(bool(e['caller']) for e in launches),
            'finishes': sum(e['event'] == 'finish' for e in log)}


def check(cli):
    version = subprocess.run([str(cli), '--version'], capture_output=True, text=True, timeout=30,
                             stdin=subprocess.DEVNULL).stdout.strip()
    runs = {name: capture(cli, **kwargs) for name, kwargs in CASES.items()}
    missing = [name for name, run in runs.items() if not run['requests']]
    if missing:
        raise RuntimeError(f'No main-loop request captured for {missing}; see the client flags')
    found = dict(findings(runs), effort_resume=effort_resume(cli))
    return {'client_version': version, 'plugin_version': json.loads((PLUGIN/'.claude-plugin/plugin.json').read_text())['version'],
            'cases': runs, 'findings': found, 'validate': validate(cli)}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--claude', default=str(PINNED), help='Claude Code executable (default: the pinned client)')
    parser.add_argument('--out', help='write the report here as JSON')
    a = parser.parse_args()
    if not Path(a.claude).exists():
        raise SystemExit(f'Claude Code is missing at {a.claude}. Nothing was run.')
    report = check(a.claude)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(report, indent=2) + '\n')
    json.dump({k: report[k] for k in ('client_version', 'plugin_version', 'findings')}, sys.stdout, indent=2)
    print()
    print('validate:', {name: v['returncode'] for name, v in report['validate'].items()})


if __name__ == '__main__':
    main()
