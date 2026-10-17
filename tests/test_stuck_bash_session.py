"""The stuck detector's Bash paths (heuristic-v3, October 16) end to end: the pinned 2.1.284 client runs scripted Bash
calls in a git workspace, the governor's hooks observe them, and the fixture policy turns any scored recommendation
into an escalation of the forwarded request. The unit tests in test_hooks.py feed recorded payloads to the hooks; these
let the client itself produce them (piped and unpiped test runs, edits made with printf and git checkout).

- The agent's test runs failing alike are observed only: stalled_agent_tests and repeated_agent_failure are journaled,
  the score stays 0, no notice, no escalation. Unpiped failing runs arrive as PostToolUseFailure and are the agent's
  run only, never also an error (in v2 they fed the scored repeated_error).
- Edits going back and forth through Bash are file evidence, so the scored edit_oscillation fires and escalates.

$0: a fake key, loopback only, empty HOME. Skips without node or the pinned client first on PATH. Glue only."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from modelpilot.fixtures import fixture_server
from modelpilot.governed_session import run_session
from modelpilot.m2 import OBSERVED_ONLY

S = 'claude-sonnet-5-5'
RATES = {S: dict(input=2, output=10, read=.2, write_5m=2.5, write_1h=4)}  # configs/sonnet-5-5-rates.json
BASE = 'def add(a, b):\n    return a - b\n'
FIXED = 'def add(a, b):\n    return a + b\n'
FILES = {'calc.py': BASE, 'test_calc.py': ('import unittest\nfrom calc import add\n\n\nclass CalcTest(unittest.TestCase):\n'
                                           '    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n')}


def bash(command):
    return {'tool': 'Bash', 'input': {'command': command, 'description': 'scripted step'}}


COMMIT = bash('git init -q && git add -A && git -c user.name=fixture -c user.email=fixture@example.invalid '
              'commit -qm base')
PIPED = bash('python3 -m unittest 2>&1 | tail -n 30')  # exits 0: PostToolUse
UNPIPED = bash('python3 -m unittest')  # exits 1: PostToolUseFailure
WRITE_FIXED = bash("printf 'def add(a, b):\\n    return a + b\\n' > calc.py")
RESTORE = bash('git checkout -- calc.py')


def client_version():
    claude = shutil.which('claude')
    if not claude or not shutil.which('node'):
        return None
    with tempfile.TemporaryDirectory() as home:
        out = subprocess.run([claude, '--version'], env={'HOME': home, 'PATH': os.environ.get('PATH', '')},
                             capture_output=True, text=True, timeout=60)
    return out.stdout.split()[0] if out.returncode == 0 and out.stdout.strip() else None


@unittest.skipUnless(client_version() == '2.1.284' and shutil.which('git'),
                     'needs node, git and the pinned 2.1.284 client first on PATH')
class StuckBashSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.upstream = fixture_server()
        self.upstream.keep_bodies = True
        self.thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.upstream.shutdown()
        self.upstream.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def run_script(self, script):
        self.upstream.script = script
        run = Path(self.tmp.name)/'run'
        report = run_session(run, shutil.which('claude'), 'sk-ant-offline-fixture-not-a-key',
                             f'http://127.0.0.1:{self.upstream.server_port}', RATES,
                             prompt='Run the tests and report.', instruction='Make the tests pass.', files=FILES,
                             model=S, effort='medium', tools='Bash', max_turns=len(script) + 2, limit_usd=5,
                             policy_upstream=self.upstream)
        main = [(b['model'], (b.get('output_config') or {}).get('effort'))
                for b in map(json.loads, self.upstream.bodies) if b.get('tools')]
        with sqlite3.connect(run/'ledger.sqlite3') as db:
            observations = [json.loads(p) for p, in db.execute(
                "SELECT payload FROM events WHERE kind='observation' ORDER BY seq")]
            journal = [(k, json.loads(p)) for k, p in db.execute('SELECT kind,payload FROM gov_decisions ORDER BY seq')]
        self.assertEqual(report['status'], 'passed', report)
        self.assertEqual(report['hook_errors'], 0, report)
        self.assertEqual(report['client']['tool_names'], ['Bash']*(len(script) - 1), report)
        return run, report, main, observations, journal

    def test_test_runs_failing_alike_are_observed_and_never_escalate(self):
        fix = bash("sed -i '' 's/a - b/a + b/' calc.py && python3 -m unittest 2>&1 | tail -n 30")
        script = [COMMIT, PIPED, PIPED, PIPED, UNPIPED, UNPIPED, UNPIPED, fix, {'text': 'DONE'}]
        run, report, main, observations, journal = self.run_script(script)
        events = [e for k, e in journal if k == 'hook_event']
        self.assertEqual([e['event'] for e in events if e['tool_name'] == 'Bash'],
                         ['PostToolUse']*4 + ['PostToolUseFailure']*3 + ['PostToolUse'], events)
        runs = [o for o in observations if 'agent_suite' in o]
        self.assertEqual([r['agent_suite'] for r in runs],
                         ['python3 -m unittest 2>&1 | tail -n 30']*3 + ['python3 -m unittest']*3 +
                         ["sed -i '' 's/a - b/a + b/' calc.py && python3 -m unittest 2>&1 | tail -n 30"])
        self.assertEqual([r['agent_failures'] for r in runs], [1]*6 + [0])
        # Piped (tool_response) and unpiped ('Exit code 1' in error) output hash alike by their failure signature.
        self.assertEqual(len({r['agent_failure_hash'] for r in runs[:6]}), 1, runs)
        self.assertNotIn('agent_failure_hash', runs[-1])
        # Never also an error, whatever the exit code.
        self.assertFalse([o for o in observations if 'error_hash' in o], observations)
        # The fix made through sed is file evidence, observed before the run it ends with.
        self.assertEqual([o['file'] for o in observations if 'file' in o], ['calc.py'])
        self.assertLess(observations.index(next(o for o in observations if 'file' in o)), observations.index(runs[-1]))
        stuck = [e for k, e in journal if k == 'stuck']
        fired = {name for e in stuck for name, on in e['signals'].items() if on}
        self.assertEqual(fired, set(OBSERVED_ONLY), stuck)
        self.assertEqual({(e['score'], e['recommendation']) for e in stuck}, {(0, 'hold')}, stuck)
        self.assertFalse([e for k, e in journal if k == 'stuck_notice'])
        self.assertEqual(report['policy']['escalations'], [])
        self.assertEqual(main, [(S, 'medium')]*len(script), main)
        self.assertEqual((run/'workspace'/'calc.py').read_text(), FIXED)

    def test_edits_going_back_and_forth_through_bash_escalate(self):
        script = [COMMIT, WRITE_FIXED, RESTORE, WRITE_FIXED, RESTORE, {'text': 'DONE'}]
        run, report, main, observations, journal = self.run_script(script)
        files = [o for o in observations if 'file' in o]
        # git checkout puts calc.py back as it was at the base, out of git's list; the observed set keeps it.
        self.assertEqual([o['file'] for o in files], ['calc.py']*4)
        hashes = [o['content_hash'] for o in files]
        self.assertTrue(hashes[0] == hashes[2] != hashes[1] == hashes[3], hashes)
        stuck = [e for k, e in journal if k == 'stuck']
        self.assertTrue(stuck[-1]['signals']['edit_oscillation'] and stuck[-1]['score'] == 3, stuck[-1])
        self.assertFalse(any(e['signals']['edit_oscillation'] for e in stuck[:-1]), stuck)
        self.assertEqual(len([e for k, e in journal if k == 'stuck_notice']), 1)
        self.assertEqual([(e['action'], e['status']) for e in report['policy']['escalations']],
                         [('increase_effort', 'fixture_confirmed')])
        # The four edits at the client's setting; the request after the fourth escalated one rung.
        self.assertEqual(main, [(S, 'medium')]*5 + [(S, 'high')], main)
        self.assertEqual((run/'workspace'/'calc.py').read_text(), BASE)


if __name__ == '__main__':
    unittest.main()
