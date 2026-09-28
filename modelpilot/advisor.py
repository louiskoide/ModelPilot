"""Jev as ModelPilot's advisor: one model and effort prediction per decision point, never applied by Jev.

Runs modelpilot/jev_advisor.mjs against the pinned compat Jev checkout. The process's environment holds only
the TypeSafe key (bench_jev.router_env), never the Anthropic key. A failure or timeout returns the error
instead of advice, which the policy reads as "stay": routing never blocks a request. The prompt never leaves
the bridge's TypeSafe call; only its length and hash come back.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from .bench_jev import router_env

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT/'modelpilot/jev_advisor.mjs'


class JevAdvisor:
    """stub: a {'model': answer, 'effort': answer} dict for offline tests; its trials are never eligible."""

    def __init__(self, checkout=ROOT/'work/jev-router-compat', key=None, stub=None, timeout=20):
        if stub is None and not key:
            raise ValueError('A live advisor needs the TypeSafe key')
        self.checkout, self.key, self.stub, self.timeout = Path(checkout), key, stub, timeout
        self.live = stub is None

    def problem(self):
        """Why the advisor can't run, or None: it needs Node and the pinned compat checkout, exactly."""
        if not shutil.which('node'):
            return 'The Jev advisor needs Node.'
        from .bench_jev import JevRouter
        from .jev_route_check import PATCH
        return JevRouter({'variant': 'compat', 'checkout': str(self.checkout), 'patch': PATCH}).problem()

    def ask(self, body, current, catalog, efforts, strip_prefix='', evidence=None, dry=False):
        payload = {'jev_root': str(self.checkout), 'body': body, 'current': current, 'catalog': catalog,
                   'efforts': list(efforts), 'strip_prefix': strip_prefix, 'evidence': evidence}
        args = ['--dry'] if dry else [] if self.live else ['--stub', json.dumps(self.stub)]
        with tempfile.TemporaryDirectory(prefix='jev-advisor-') as tmp:
            env = router_env(os.environ, {'home': Path(tmp), 'tmp': Path(tmp)}, self.key)
            try:
                done = subprocess.run(['node', str(BRIDGE), *args], input=json.dumps(payload), env=env, cwd=tmp,
                                      capture_output=True, text=True, timeout=self.timeout)
            except (OSError, subprocess.TimeoutExpired) as e:
                return {'error': 'advisor_' + type(e).__name__}
        try:
            out = json.loads(done.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {'error': f'advisor_exit_{done.returncode}'}
        return out if isinstance(out, dict) else {'error': 'advisor_bad_output'}
