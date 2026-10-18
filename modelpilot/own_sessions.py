"""Your own Claude Code sessions through the dry-run proxy (October 17): the real-work measurement.

Benchmark sessions run back to back, so they can't show what the governor's cache and session levers would be worth:
how often a session idles past its cache lifetime, how large its context is then, and how much it reads (what a
delegated explorer could save). Only real work shows that (`docs/governor.md`, "Idle plans: options").

A run starts the dry-run proxy on a free loopback port (`proxy.ProxyServer`, every model's rates from `bench.rates`,
the dry-run governor recording reservations and settlements), then runs your own Claude Code in this terminal with
ANTHROPIC_BASE_URL pointing at it. Your own settings, login and environment are used otherwise unchanged; no global
configuration is touched. When the client exits, the proxy stops and summary.json is written.

Kept under the ignored runs/own-sessions/:
- one directory per session: the proxy's rows (observations.jsonl: model, effort, usage, cost, timing, request hash;
  no message text) and summary.json (requests, cost, and the gaps between main-loop requests against each one's cache
  lifetime boundary, `m5.idle_boundary`);
- ledger.sqlite3, the governor's ledger shared by every session (reservations, settlements, settings and context
  sizes; hashes and decisions, no text).
The client keeps its own transcripts where it always does (~/.claude/projects).

Dry-run: every request is forwarded unchanged and none is refused (admission is journaled, never enforced). The
client's requests are billed as usual; ModelPilot sends none of its own, unless --keep. Without --live the plan is
printed and nothing starts, since a session sends billable requests.

--keep (October 18, user decision; off by default): the cache keeper (`modelpilot/keeper.py`,
docs/cache-keeper-design.md). While the client is idle, the proxy re-sends its last main-loop request with max_tokens 1
every 55 minutes, for up to 4 hours, so its 1h cache entry is read back instead of rewritten when you return. Pings go
on the session's own login, at most $3 a day at API rates across own sessions, and hold off idle sleep while a request
is held. summary.json's `keeper` then reports the pings' cost against the rewrites the returns avoided, measured.

    PYTHONPATH=<this checkout> python3.12 -P -m modelpilot.own_sessions [--claude PATH] [--keep] [--live] [-- CLIENT ARGS...]

Run it from the project to work on: the client opens in the current directory. -P keeps that directory off the
import path, so another checkout's modelpilot package there can't shadow this one.
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time

from . import keeper as cache_keeper
from . import m5

ROOT = Path(__file__).resolve().parent.parent
OUT_ROOT = ROOT/'runs/own-sessions'
UPSTREAM = 'https://api.anthropic.com'
# Journaled against, never enforced (dry-run). Not a billing cap.
LIMIT_USD = 1000.0


def session_dir(out_root):
    """A new directory for this session, never an existing one."""
    path = Path(out_root)/f"{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    path.mkdir(parents=True)
    return path


def summarize(log):
    """Requests, cost and main-loop gaps from a proxy log. A main-loop request carries tools; a gap runs from one's end
    to the next one's start and is counted past the boundary of the lifetime the earlier one wrote with."""
    rows = []
    if Path(log).exists():
        rows = [json.loads(line) for line in Path(log).read_text().splitlines() if line.strip()]
    messages = [r for r in rows if r.get('kind') == 'messages']
    known = [r['cost_usd'] for r in messages if r.get('cost_usd') is not None]
    main = sorted((r for r in messages if r.get('tool_count') and r.get('started_unix') is not None),
                  key=lambda r: r['started_unix'])
    gaps = []
    for before, after in zip(main, main[1:]):
        ended = before['started_unix'] + (before.get('wall_seconds') or 0)
        ttl = (before.get('cache_ttl') or [None])[-1]
        gaps.append({'seconds': round(after['started_unix'] - ended, 1), 'ttl': ttl,
                     'past_boundary': after['started_unix'] - ended >= m5.idle_boundary(ttl)})
    return {'rows': len(rows), 'messages': len(messages), 'main_loop': len(main),
            'known_cost_usd': round(sum(known), 6), 'unknown_cost_requests': len(messages) - len(known),
            'models': sorted({r['model'] for r in messages if r.get('model')}),
            'longest_gap_seconds': max((g['seconds'] for g in gaps), default=None),
            'gaps_past_boundary': [g for g in gaps if g['past_boundary']]}


def plan(claude, version, out_root, upstream, client_args, keep=False):
    out = {'live': False, 'client': str(claude), 'client_version': version, 'client_args': client_args,
           'upstream': upstream, 'session_dir': str(Path(out_root)/'<YYYYmmdd-HHMMSS>-<pid>'),
           'ledger': str(Path(out_root)/'ledger.sqlite3'), 'environment_set': ['ANTHROPIC_BASE_URL'],
           'mode': 'dry-run: forwarded unchanged, nothing refused or applied',
           'governor_limit_usd': f'{LIMIT_USD} (journaled, never enforced; not a billing cap)',
           'sends': "nothing of its own; the client's requests are billed as usual",
           'keeper': None, 'next': 'add --live to start the session'}
    if keep:
        out['sends'] = ("the client's requests, billed as usual, and the keeper's pings on the same login: the last "
                        'main-loop request again at max_tokens 1, 55 minutes after its entry was last read, for up to '
                        '4 hours idle')
        out['keeper'] = dict(cache_keeper.settings(), holds_off_idle_sleep='while a request is held (caffeinate -i)')
    return out


def run(claude, version, out_root, upstream, client_args, rates, env=None, stdin=None, keep=None):
    """One session: the proxy on a free port, the client in this terminal, the summary when it exits.
    keep: the cache keeper's settings ({} for its defaults), or None for none."""
    from .proxy import ProxyServer, read_log
    out_root = Path(out_root)
    path = session_dir(out_root)
    log = path/'observations.jsonl'
    governor = {'db': out_root/'ledger.sqlite3', 'session': path.name, 'limit_usd': LIMIT_USD, 'task': None}
    summary = {'mode': 'dry-run', 'client': str(claude), 'client_version': version, 'client_args': client_args,
               'upstream': upstream, 'governor_session': path.name, 'started_unix': time.time(),
               'keeper': None if keep is None else cache_keeper.settings(keep)}
    env = dict(os.environ if env is None else env)
    with ProxyServer(('127.0.0.1', 0), upstream, log, rates, governor=governor, keeper=keep) as proxy:
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        env['ANTHROPIC_BASE_URL'] = f'http://127.0.0.1:{proxy.server_port}'
        print(f'ModelPilot dry-run proxy on 127.0.0.1:{proxy.server_port}, logging to {path}', file=sys.stderr,
              flush=True)
        # Ctrl-C belongs to the client. A handler, not SIG_IGN: the child would inherit an ignored signal.
        previous = signal.signal(signal.SIGINT, lambda *_: None) if threading.current_thread() is threading.main_thread() else None
        try:
            summary['returncode'] = subprocess.run([str(claude), *client_args], env=env, stdin=stdin).returncode
        finally:
            if previous is not None:
                signal.signal(signal.SIGINT, previous)
            proxy.shutdown()
            thread.join()
    summary['ended_unix'] = time.time()
    summary.update(summarize(log))
    kept = ''
    if keep is not None:
        summary['keeper'].update(cache_keeper.summary(read_log(log), rates))
        k = summary['keeper']
        net = 'unknown' if k['net_usd'] is None else f"${k['net_usd']:.4f}"
        kept = (f"; keeper: {k['pings']} pings, ${k['ping_cost_usd']:.4f}, {len(k['kept_returns'])} kept returns, "
                f"net {net}")
    (path/'summary.json').write_text(json.dumps(summary, indent=1) + '\n')
    print(f"ModelPilot: {summary['messages']} requests, ${summary['known_cost_usd']:.4f} known, "
          f"{summary['unknown_cost_requests']} unknown, {len(summary['gaps_past_boundary'])} gaps past the cache "
          f"boundary{kept}; {path/'summary.json'}", file=sys.stderr, flush=True)
    return summary


def main(argv=None):
    from .bench import rates, resolve_client
    argv = list(sys.argv[1:] if argv is None else argv)
    client_args = []
    if '--' in argv:
        at = argv.index('--')
        argv, client_args = argv[:at], argv[at + 1:]
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--claude', default=shutil.which('claude'), help='the Claude Code executable (default: yours)')
    parser.add_argument('--out-root', type=Path, default=OUT_ROOT)
    parser.add_argument('--upstream', default=UPSTREAM, help='Anthropic origin, or loopback HTTP for local tests')
    parser.add_argument('--keep', action='store_true',
                        help='keep the idle session\'s 1h cache entry warm with 1-token pings (the cache keeper)')
    parser.add_argument('--live', action='store_true', help="start the session (its requests are billed as usual)")
    args = parser.parse_args(argv)
    if not args.claude:
        raise SystemExit('Claude Code is missing: pass --claude PATH')
    claude, version = resolve_client(args.claude)
    if not args.live:
        print(json.dumps(plan(claude, version, args.out_root, args.upstream, client_args, args.keep), indent=1))
        return
    summary = run(claude, version, args.out_root, args.upstream, client_args, rates(), keep={} if args.keep else None)
    raise SystemExit(summary['returncode'])


if __name__ == '__main__':
    main()
