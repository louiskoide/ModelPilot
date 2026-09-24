"""Jev arms for the M6 bench: the per-trial Jev router process and its routing record.

Each Jev trial runs Jev's own proxy (`jev_accounted_launch.mjs --serve`) for the whole trial,
in front of the trial's ModelPilot proxy, as one interactive session would: a resumed
follow-up keeps Jev's routing state. The harness starts its pinned Claude Code against it
with the jev-router sentinel and no --model. The router process gets the TypeSafe key and
never the Anthropic key; the client gets the Anthropic key and never the TypeSafe key.

Routing counts only with a real router exchange and no fail-open marker. Stock Jev on Claude
Code >=2.1.278 rewrites the sentinel without ever deciding (`unrouted_sentinel`), so it runs
as fixed Opus plus router overhead. Router (TypeSafe) cost is unpriced: Jev dollars are
provider cost only, a lower bound.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import time
from .jev_route_check import DECISION, FAILURES, REWRITE, load_decisions, verify_checkout

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT/'modelpilot/jev_accounted_launch.mjs'
# Tiers Jev can serve at the pinned revision (src/config.mjs); Fable needs JEV_ALLOW_FABLE=1, never set here.
JEV_MODELS = ('claude-haiku-4-5-20251001', 'claude-sonnet-5', 'claude-opus-5')
UNDECIDED = re.compile(r'^\[jev\] \w+ no-jev ', re.M)
AUTH_FAILURE = re.compile(r'^\[jev\] routing failed.*(?:\b401\b|authenticat)', re.M | re.I)


class JevCheckoutChanged(RuntimeError):
    """The Jev checkout is missing or no longer the pinned revision (plus exactly its patch)."""


class JevRouterDown(RuntimeError):
    """The trial's Jev router process could not start or has exited."""


def router_env(base, dirs, jev_key):
    """The router process's environment: the trial's HOME/TMPDIR, the TypeSafe key, never the Anthropic key."""
    keep = ('LANG', 'LC_ALL', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'NODE_EXTRA_CA_CERTS')
    env = {name: base[name] for name in keep if name in base}
    node_dir = str(Path(shutil.which('node', path=base.get('PATH')) or 'node').parent)
    env.update(PATH=os.pathsep.join([node_dir, '/usr/bin', '/bin']), HOME=str(dirs['home']), TMPDIR=str(dirs['tmp']),
               JEV_DEBUG='1', JEV_NO_STATUSLINE='1')
    if jev_key:
        env['JEV_API_KEY'] = jev_key
    return env


class JevRouter:
    """One trial's Jev proxy process for an arm from bench.ARMS."""
    def __init__(self, arm, root=ROOT):
        self.variant = arm['variant']
        self.checkout = Path(root)/arm['checkout']
        self.patch_name = arm['patch']
        self.patch = Path(root)/arm['patch'] if arm['patch'] else None
        self.commit = json.loads((ROOT/'configs/jev-baseline.json').read_text())['commit']
        self.proc = None

    @property
    def pid(self):
        return self.proc.pid if self.proc else None

    def problem(self):
        """Why this checkout cannot run the arm, or None."""
        if not (self.checkout/'.git').exists():
            return f'No Jev checkout at {self.checkout}; see docs/jev-baseline-setup.md.'
        if not verify_checkout(self.checkout, self.commit, self.patch):
            what = f'plus exactly {self.patch.name}' if self.patch else 'with no tracked modifications'
            return (f'{self.checkout} is not the pinned revision {what}'
                    + ('; rebuild it with python3 -m modelpilot.jev_compat --prepare-compat.' if self.patch else '.'))
        return None

    def verify(self):
        problem = self.problem()
        if problem:
            raise JevCheckoutChanged(problem + ' Not running more trials.')

    def describe(self):
        return {'variant': self.variant, 'jev_commit': self.commit, 'jev_checkout': str(self.checkout),
                'patch': self.patch_name,
                'patch_sha256': hashlib.sha256(self.patch.read_bytes()).hexdigest() if self.patch else None,
                'launcher': 'accounted-serve'}

    def start(self, upstream_url, dirs, jev_key, stderr_path, stub=None, timeout=30):
        """Start Jev's proxy in front of upstream_url; returns the client env and --add-dir it prints."""
        command = ['node', str(LAUNCHER), '--serve', str(self.checkout), upstream_url] + (['--stub-route', stub] if stub else [])
        with open(stderr_path, 'ab') as stderr:
            # Outside the trial directory, so the harness's between-session reap leaves it running.
            self.proc = subprocess.Popen(command, env=router_env(os.environ, dirs, jev_key), cwd=self.checkout,
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                                         start_new_session=True)
        with selectors.DefaultSelector() as selector:
            selector.register(self.proc.stdout, selectors.EVENT_READ)
            ready = selector.select(timeout)
        line = self.proc.stdout.readline() if ready else b''
        try:
            info = json.loads(line)
        except ValueError:
            self.stop()
            raise JevRouterDown(f'Jev router did not start (see {stderr_path}).')
        return info

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self, grace=5):
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                stream.close()
            except OSError:
                pass

    def collect(self, tmp_dir, dest):
        """Copy the decisions Jev filed under the trial's TMPDIR (deleted after grading) into dest."""
        decisions = load_decisions(tmp_dir)
        Path(dest).write_text(json.dumps(decisions, indent=2) + '\n')
        return decisions


def session_of(at_ms, sessions):
    at = at_ms / 1000
    return next((i for i, s in enumerate(sessions) if s['started_unix'] <= at <= s.get('ended_unix', time.time())), None)


def routing(decisions, stderr, rows, sessions):
    """Per-trial routing record from Jev's decisions and debug log and the proxy rows (the wire)."""
    exchanges = [isinstance((d.get('jev') or {}).get(k), dict) and bool(d['jev'][k])
                 for d in decisions for k in ('request', 'response')]
    fail_open = [marker for marker in FAILURES if marker in stderr]
    if REWRITE.search(stderr) and not decisions and not DECISION.search(stderr) and not UNDECIDED.search(stderr):
        fail_open.append('unrouted_sentinel')
    stub = any(((d.get('jev') or {}).get('request') or {}).get('stub') for d in decisions)
    # The model each session's decisions settled on; every tool-bearing success should be on it.
    by_session = {}
    for d in decisions:
        index = session_of(d.get('at') or 0, sessions)
        if index is not None:
            by_session.setdefault(index, []).append(d)
    on_selection = None
    if by_session:
        on_selection = True
        for index, chosen in by_session.items():
            start, end = sessions[index]['rows']
            served = [r.get('model') for r in rows[start:end]
                      if r.get('kind') == 'messages' and r.get('tool_count', 0) > 0 and r.get('http_status') == 200]
            on_selection = on_selection and all(m == chosen[-1].get('model') for m in served)
    carried = None
    if 0 in by_session and 1 in by_session:
        current = (((by_session[1][0].get('jev') or {}).get('request') or {}).get('state') or {}).get('session', {})
        carried = current.get('current_model') == by_session[0][-1].get('model')
    return {'router': ('stub' if stub else 'typesafe') if decisions else None,
            'decisions': len(decisions), 'extra_decisions': max(0, len(decisions) - len(sessions)),
            'choices': [{'model': d.get('model'), 'reason': d.get('reason'), 'confidence': d.get('confidence'),
                         'current_model': ((((d.get('jev') or {}).get('request') or {}).get('state') or {})
                                           .get('session') or {}).get('current_model')} for d in decisions],
            'routed': bool(decisions) and all(exchanges) and not fail_open,
            'fail_open': fail_open, 'auth_failure': bool(AUTH_FAILURE.search(stderr)),
            'served_models': sorted({r.get('model') for r in rows if r.get('kind') == 'messages'
                                     and r.get('tool_count', 0) > 0 and r.get('http_status') == 200}),
            'continuations_on_selection': on_selection, 'state_carried': carried,
            'router_usage': [u for d in decisions for u in [(((d.get('jev') or {}).get('response') or {}).get('usage'))] if u]}
