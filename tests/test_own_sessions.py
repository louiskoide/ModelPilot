import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

from modelpilot import own_sessions
from modelpilot.bench import rates
from modelpilot.fixtures import fixture_server

RATES_55 = {'claude-sonnet-5-5': dict(input=2, output=10, read=.1, write_5m=2.5, write_1h=4)}

# Stands in for Claude Code: two main-loop requests through ANTHROPIC_BASE_URL, then its environment, then exit 3.
FAKE_CLIENT = r'''
import json, os, sys, time, urllib.request
base = os.environ['ANTHROPIC_BASE_URL']
for i in range(2):
    body = json.dumps({'model': 'claude-sonnet-5-5', 'max_tokens': 16, 'stream': False,
                       'tools': [{'name': 'Read', 'input_schema': {'type': 'object'}}],
                       'messages': [{'role': 'user', 'content': 'hi'}]}).encode()
    request = urllib.request.Request(base + '/v1/messages', body, {'content-type': 'application/json',
                                     'x-api-key': 'sk-ant-offline-fixture-not-a-key', 'anthropic-version': '2023-06-01'})
    urllib.request.urlopen(request).read()
    time.sleep(.3)
open(sys.argv[1], 'w').write(json.dumps(dict(os.environ)))
sys.exit(3)
'''


def row(start, wall, ttl, tools=1, cost=.01):
    return {'kind': 'messages', 'model': 'claude-sonnet-5-5', 'started_unix': start, 'wall_seconds': wall,
            'cache_ttl': [ttl], 'tool_count': tools, 'cost_usd': cost}


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name)/'observations.jsonl'

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rows):
        self.log.write_text(''.join(json.dumps(r) + '\n' for r in rows))

    def test_gaps_run_from_a_requests_end_and_meet_its_lifetimes_boundary(self):
        self.write([row(0, 10, '5m'), row(339, 5, '5m'), row(674, 5, '1h'), row(679 + 3629, 5, '1h'),
                    row(5000, 5, '1h', tools=0), row(4313 + 3630, 5, '1h', cost=None)])
        s = own_sessions.summarize(self.log)
        self.assertEqual((s['messages'], s['main_loop'], s['unknown_cost_requests']), (6, 5, 1))
        # 329 s after a 5m write and 3,629 s after a 1h one are inside; 330 s and 3,630 s are past.
        self.assertEqual([g['seconds'] for g in s['gaps_past_boundary']], [330.0, 3630.0])
        self.assertEqual(s['longest_gap_seconds'], 3630.0)
        self.assertAlmostEqual(s['known_cost_usd'], .05)

    def test_an_unknown_lifetime_waits_for_the_longest_boundary(self):
        self.write([row(0, 0, None), row(400, 0, None)])
        self.assertEqual(own_sessions.summarize(self.log)['gaps_past_boundary'], [])

    def test_an_empty_or_missing_log(self):
        s = own_sessions.summarize(self.log)
        self.assertEqual((s['messages'], s['longest_gap_seconds'], s['gaps_past_boundary']), (0, None, []))


class SessionTests(unittest.TestCase):
    """A stand-in client and, where installed, the real one, through the proxy to the loopback fixture: $0."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.upstream = fixture_server()
        self.thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.upstream.server_port}'

    def tearDown(self):
        self.upstream.shutdown()
        self.upstream.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def test_the_client_runs_through_the_proxy_with_only_the_base_url_changed(self):
        client = self.root/'client.py'
        client.write_text(FAKE_CLIENT)
        seen = self.root/'env.json'
        env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(self.root), 'MARKER': 'kept'}
        s = own_sessions.run(sys.executable, 'stand-in', self.root/'out', self.url, [str(client), str(seen)], RATES_55,
                             env=env, stdin=subprocess.DEVNULL)
        self.assertEqual(s['returncode'], 3)
        self.assertEqual((s['messages'], s['main_loop'], s['unknown_cost_requests']), (2, 2, 0))
        self.assertGreater(s['known_cost_usd'], 0)
        self.assertEqual(len(self.upstream.received), 2)
        child = json.loads(seen.read_text())
        self.assertTrue(child.pop('ANTHROPIC_BASE_URL').startswith('http://127.0.0.1:'))
        for key in ('__CF_USER_TEXT_ENCODING', 'PWD', 'SHLVL', '_', 'LC_CTYPE'):  # set by the interpreter or OS
            child.pop(key, None)
        self.assertEqual(child, env)
        sessions = [p for p in (self.root/'out').iterdir() if p.is_dir()]
        self.assertEqual(len(sessions), 1)
        self.assertEqual(json.loads((sessions[0]/'summary.json').read_text())['messages'], 2)
        self.assertEqual(len((sessions[0]/'observations.jsonl').read_text().splitlines()), 2)
        self.assertTrue((self.root/'out'/'ledger.sqlite3').exists())

    def test_each_session_gets_its_own_directory_and_shares_the_ledger(self):
        client = self.root/'client.py'
        client.write_text(FAKE_CLIENT)
        env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(self.root)}
        first = own_sessions.run(sys.executable, 'stand-in', self.root/'out', self.url, [str(client), str(self.root/'a')],
                                 RATES_55, env=env, stdin=subprocess.DEVNULL)
        # Directories are named by the second and pid, and a reused name is refused
        # (test_a_session_directory_is_never_reused): start the second session in a later second.
        time.sleep(1.01 - time.time() % 1)
        second = own_sessions.run(sys.executable, 'stand-in', self.root/'out', self.url, [str(client), str(self.root/'b')],
                                  RATES_55, env=env, stdin=subprocess.DEVNULL)
        self.assertNotEqual(first['governor_session'], second['governor_session'])
        self.assertEqual(sorted(p.name for p in (self.root/'out').iterdir() if p.is_file()), ['ledger.sqlite3'])

    @unittest.skipUnless(shutil.which('claude'), 'Claude Code CLI not installed')
    def test_the_real_client_through_the_proxy(self):
        home = self.root/'home'
        home.mkdir()
        env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(home), 'ANTHROPIC_API_KEY': 'sk-ant-offline-fixture-not-a-key',
               'CLAUDE_CODE_MAX_RETRIES': '0', 'DISABLE_AUTOUPDATER': '1'}
        claude, version = own_sessions_client()
        s = own_sessions.run(claude, version, self.root/'out', self.url,
                             ['-p', 'Reply with OK.', '--setting-sources', '', '--max-turns', '1'], rates(),
                             env=env, stdin=subprocess.DEVNULL)
        self.assertEqual(s['returncode'], 0, s)
        self.assertGreaterEqual(s['main_loop'], 1)
        self.assertEqual(s['unknown_cost_requests'], 0, s)


def own_sessions_client():
    from modelpilot.bench import resolve_client
    return resolve_client(shutil.which('claude'))


class PlanTests(unittest.TestCase):
    def test_the_plan_names_what_a_live_run_would_do(self):
        p = own_sessions.plan('/x/claude', '2.1.302 (Claude Code)', Path('/r/runs/own-sessions'), own_sessions.UPSTREAM,
                              ['--model', 'opus'])
        self.assertFalse(p['live'])
        self.assertEqual(p['environment_set'], ['ANTHROPIC_BASE_URL'])
        self.assertIn('--live', p['next'])
        self.assertEqual(p['ledger'], '/r/runs/own-sessions/ledger.sqlite3')

    def test_a_session_directory_is_never_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = own_sessions.session_dir(tmp)
            with unittest.mock.patch.object(own_sessions.os, 'getpid', return_value=int(first.name.rsplit('-', 1)[1])), \
                    unittest.mock.patch.object(own_sessions.datetime, 'datetime') as clock:
                clock.now.return_value.strftime.return_value = first.name.rsplit('-', 1)[0]
                with self.assertRaises(FileExistsError):
                    own_sessions.session_dir(tmp)


if __name__ == '__main__':
    unittest.main()
