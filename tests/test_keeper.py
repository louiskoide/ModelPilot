"""The cache keeper (modelpilot/keeper.py), $0: unit rules, then the proxy against the loopback fixture on an injected
clock, a stand-in client through own_sessions --keep, and, where installed, the pinned real client pausing in a Bash
call while the keeper pings."""
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
import urllib.request

from modelpilot import keeper, own_sessions
from modelpilot.fixtures import fixture_server
from modelpilot.governor import Governor
from modelpilot.proxy import ProxyServer, read_log

MODEL = 'claude-sonnet-5-5'
RATES = {MODEL: dict(input=2, output=10, read=.1, write_5m=2.5, write_1h=4)}
KEY = 'sk-ant-offline-fixture-not-a-key'
SECRET_TEXT = 'KEEPER_PROMPT_TEXT_MARKER'
MINUTE = 60
REAL_TIME = time.time


def request(ttl='1h', tools=True, **extra):
    """A main-loop request in Claude Code's shape: a cached system block, tools, adaptive thinking."""
    control = {'type': 'ephemeral'} if ttl is None else {'type': 'ephemeral', 'ttl': ttl}
    body = {'model': MODEL, 'max_tokens': 32000, 'stream': False, 'thinking': {'type': 'adaptive'},
            'system': [{'type': 'text', 'text': 'You are a test.', 'cache_control': control}],
            'messages': [{'role': 'user', 'content': SECRET_TEXT}]}
    if tools:
        body['tools'] = [{'name': 'Read', 'input_schema': {'type': 'object'}}]
    body.update(extra)
    return body


def post(port, body, key=KEY):
    data = json.dumps(body).encode()
    r = urllib.request.Request(f'http://127.0.0.1:{port}/v1/messages', data,
                               {'content-type': 'application/json', 'x-api-key': key, 'anthropic-version': '2023-06-01'})
    return json.loads(urllib.request.urlopen(r, timeout=30).read())


class Hold:
    """Stands in for caffeinate."""
    made = []

    def __init__(self):
        self.released = False
        Hold.made.append(self)

    def release(self):
        self.released = True


class RuleTests(unittest.TestCase):
    def row(self, **extra):
        r = {'kind': 'messages', 'model': MODEL, 'cache_ttl': ['1h'], 'tool_count': 1, 'cost_usd': .01,
             'usage_complete': True, 'usage': {'input_tokens': 10, 'cache_read_input_tokens': 900,
                                               'cache_creation_input_tokens': 90, 'output_tokens': 5}}
        r.update(extra)
        return r

    def test_what_can_be_kept(self):
        body = request()
        self.assertIsNone(keeper.keepable(body, self.row(), RATES))
        self.assertEqual(keeper.keepable(body, self.row(compaction=True), RATES), 'compaction')
        self.assertEqual(keeper.keepable(body, self.row(cache_ttl=['5m']), RATES), 'not_1h')
        self.assertEqual(keeper.keepable(body, self.row(cache_ttl=['1h', '5m']), RATES), 'not_1h')
        self.assertEqual(keeper.keepable(body, self.row(cache_ttl=[]), RATES), 'not_1h')
        budget = request(thinking={'type': 'enabled', 'budget_tokens': 4000})
        self.assertEqual(keeper.keepable(budget, self.row(), RATES), 'thinking_budget')
        self.assertIsNone(keeper.keepable(request(thinking={'type': 'disabled'}), self.row(), RATES))
        self.assertEqual(keeper.keepable(body, self.row(model='unknown'), RATES), 'unpriced_model')
        self.assertEqual(keeper.keepable(body, self.row(cost_usd=None), RATES), 'usage_unknown')
        self.assertEqual(keeper.keepable(body, self.row(usage_complete=False), RATES), 'usage_unknown')

    def test_a_ping_changes_only_max_tokens_and_stream(self):
        body = request(stream=True)
        ping = keeper.ping_body(body)
        self.assertEqual((ping['max_tokens'], ping['stream']), (1, False))
        self.assertEqual({k: v for k, v in ping.items() if k not in ('max_tokens', 'stream')},
                         {k: v for k, v in body.items() if k not in ('max_tokens', 'stream')})
        self.assertEqual((body['max_tokens'], body['stream']), (32000, True))  # the client's request is untouched

    def test_a_pings_estimate_is_its_read_and_one_output_token(self):
        # 100,000 read at $0.10 and 1 output at $10 per million.
        self.assertAlmostEqual(keeper.ping_estimate(MODEL, 100_000, RATES), .01 + .00001)

    def test_timing_must_ping_inside_the_hour(self):
        proxy = unittest.mock.Mock(governor={'db': 'x'}, policy=None, rates=RATES)
        for bad in ({'every': 3500, 'late': 3400}, {'late': 3700}, {'window': 60}, {'cap_usd': -1}):
            with self.assertRaises(ValueError):
                keeper.Keeper(proxy, **bad)
        with self.assertRaises(ValueError):
            keeper.Keeper(unittest.mock.Mock(governor=None, policy=None, rates=RATES))
        with self.assertRaises(ValueError):
            keeper.Keeper(unittest.mock.Mock(governor={'db': 'x'}, policy=object(), rates=RATES))

    def test_the_cap_counts_only_pings_since_the_day_started_across_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp)/'ledger.sqlite3'
            self.assertEqual(keeper.ledger_spent(db, 0), 0.0)
            clock = [1000.0]
            for session, request_id, estimate, actual, at in (
                    ('a', 'mp-keep-1', .5, .02, 1000), ('b', 'mp-keep-2', .5, .03, 2000),
                    ('b', 'mp-3', .5, 1.0, 2000),  # a client request: not a ping
                    ('a', 'mp-keep-4', .25, None, 3000),  # in flight: its estimate counts
                    ('a', 'mp-keep-5', .5, .04, 10)):  # before the day started
                clock[0] = at
                gov = Governor(db, session, 100, clock=lambda: clock[0])
                gov.admit(request_id, estimate, enforce=False)
                if actual is not None:
                    gov.settle(request_id, actual)
                gov.close()
            self.assertAlmostEqual(keeper.ledger_spent(db, 500), .02 + .03 + .25)

    def test_the_day_starts_at_local_midnight(self):
        now = time.mktime((2026, 10, 18, 15, 30, 0, 0, 0, -1))
        self.assertEqual(keeper.day_start(now), time.mktime((2026, 10, 18, 0, 0, 0, 0, 0, -1)))

    def test_settings_record_numbers_never_hooks(self):
        s = keeper.settings({'every': 1, 'clock': time.time, 'awake': Hold})
        self.assertEqual(s['every_seconds'], 1)
        json.dumps(s)


class ProxyKeeperTests(unittest.TestCase):
    """The keeper inside a ProxyServer, against the loopback fixture. The clock is shifted for the proxy's rows and the
    keeper alike (time.time, patched); the keeper's thread polls hourly, so only the test's tick() calls send pings."""
    def setUp(self):
        clock = unittest.mock.patch('time.time', lambda: REAL_TIME() + self.offset)
        clock.start()
        self.addCleanup(clock.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.upstream = fixture_server()
        self.upstream.keep_bodies = True
        self.up_thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.up_thread.start()
        self.offset = 0.0
        self.spent = 0.0
        Hold.made = []
        self.log = self.root/'observations.jsonl'
        settings = {'clock': lambda: REAL_TIME() + self.offset, 'awake': Hold, 'poll': 3600,
                    'spent_since': lambda since: self.spent}
        self.proxy = ProxyServer(('127.0.0.1', 0), f'http://127.0.0.1:{self.upstream.server_port}', self.log, RATES,
                                 governor={'db': self.root/'ledger.sqlite3', 'session': 's', 'limit_usd': 100,
                                           'task': None}, keeper=settings)
        self.thread = threading.Thread(target=self.proxy.serve_forever, daemon=True)
        self.thread.start()
        self.keeper = self.proxy.keeper
        self.closed = False

    def tearDown(self):
        self.close()
        if self.up_thread.is_alive():
            self.upstream.shutdown()
            self.upstream.server_close()
            self.up_thread.join()
        self.tmp.cleanup()

    def close(self):
        if not self.closed:
            self.closed = True
            self.proxy.shutdown()
            self.thread.join()
            self.proxy.server_close()

    def send(self, body=None):
        out = post(self.proxy.server_port, body or request())
        self.assertTrue(self.proxy.wait_idle(10))
        return out

    def at(self, minutes):
        """Tick with the clock this many minutes after now."""
        self.offset = minutes * MINUTE
        return self.keeper.tick()

    def rows(self, kind):
        return [r for r in read_log(self.log) if r.get('kind') == kind]

    def test_a_ping_is_the_held_request_at_max_tokens_1_and_renews_the_entry(self):
        self.send()
        self.assertIsNone(self.at(30))
        self.assertIsNone(self.at(54.9))
        self.assertEqual(self.at(55.1), 'pinged')
        client, ping = (json.loads(b) for b in self.upstream.bodies)
        self.assertEqual((ping['max_tokens'], ping['stream']), (1, False))
        self.assertEqual({k: v for k, v in ping.items() if k not in ('max_tokens',)},
                         {k: v for k, v in client.items() if k not in ('max_tokens',)})
        self.assertEqual([r['key'] for r in self.upstream.received], [KEY, KEY])  # the client's own login
        [row] = self.rows('keeper_ping')
        self.assertEqual((row['mode'], row['status'], row['http_status'], row['purpose']), ('keeper', 'ok', 200,
                                                                                            'cache_keeper'))
        self.assertTrue(row['governor_request_id'].startswith('mp-keep-'))
        self.assertEqual(row['governor_status'], 'settled')
        self.assertGreater(row['cost_usd'], 0)
        # The next ping is due 55 minutes after this one read the entry, not after the request.
        self.assertIsNone(self.at(109))
        self.assertEqual(self.at(110.2), 'pinged')
        self.assertEqual(len(self.rows('keeper_ping')), 2)

    def test_four_pings_then_the_window_ends(self):
        self.send()
        results = [self.at(m) for m in (55.1, 110.2, 165.3, 220.4)]
        self.assertEqual(results, ['pinged'] * 4)
        self.assertEqual(self.at(240.1), 'window_end')
        self.assertIsNone(self.at(275.5))  # nothing held
        [stop] = self.rows('keeper')
        self.assertEqual((stop['event'], stop['reason'], stop['pings']), ('stopped', 'window_end', 4))
        self.assertTrue(Hold.made[0].released)
        # A new request starts keeping again, with a new hold on sleep.
        self.offset = 0
        self.send()
        self.assertIsNotNone(self.keeper.held)
        self.assertEqual(len(Hold.made), 2)

    def test_a_late_timer_never_pings(self):
        self.send()
        self.assertEqual(self.at(59), 'late')
        self.assertEqual(len(self.upstream.bodies), 1)
        [stop] = self.rows('keeper')
        self.assertEqual((stop['reason'], stop['detail']), ('late', 59 * MINUTE))
        self.assertTrue(Hold.made[0].released)

    def test_the_daily_cap_stops_before_sending(self):
        self.send()
        self.spent = keeper.CAP_USD - keeper.ping_estimate(MODEL, 14760, RATES) / 2
        self.assertEqual(self.at(55.1), 'daily_cap')
        self.assertEqual(len(self.upstream.bodies), 1)
        self.spent, self.offset = 0.0, 0.0
        self.send()  # re-armed by the next request; the cap is checked again at each ping
        self.assertEqual(self.at(55.1), 'pinged')

    def test_a_new_request_replaces_the_held_one_and_restarts_its_window(self):
        self.send()
        self.offset = 50 * MINUTE
        self.send(request(messages=[{'role': 'user', 'content': 'second'}]))
        self.assertIsNone(self.at(56))  # 6 minutes after the newer request
        self.assertEqual(self.at(105.1), 'pinged')
        ping = json.loads(self.upstream.bodies[-1])
        self.assertEqual(ping['messages'][0]['content'], 'second')
        self.assertEqual(self.rows('keeper'), [])  # a renewal, not a stop

    def test_requests_without_tools_leave_the_held_one(self):
        self.send()
        self.send(request(tools=False, messages=[{'role': 'user', 'content': 'title please'}]))
        self.assertEqual(self.at(55.1), 'pinged')
        self.assertEqual(json.loads(self.upstream.bodies[-1])['messages'][0]['content'], SECRET_TEXT)

    def test_a_request_that_cannot_be_kept_ends_keeping(self):
        self.send()
        self.send(request(ttl=None))  # 5m breakpoints
        [stop] = self.rows('keeper')
        self.assertEqual(stop['reason'], 'not_keepable:not_1h')
        self.assertIsNone(self.at(55.1))
        self.assertTrue(Hold.made[0].released)

    def test_no_ping_while_a_request_is_in_flight(self):
        self.send()
        self.keeper.request_started()
        self.assertIsNone(self.at(55.1))
        self.keeper.request_finished({}, [], {'kind': 'messages', 'tool_count': 0})
        self.assertEqual(self.at(55.2), 'pinged')

    def test_a_ping_error_disables_keeping_for_the_process(self):
        self.send()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.up_thread.join()
        self.assertEqual(self.at(55.1), 'ping_error')
        [ping] = self.rows('keeper_ping')
        self.assertEqual(ping['status'], 'transport_error')
        [stop] = self.rows('keeper')
        self.assertEqual((stop['event'], stop['reason']), ('disabled', 'ping_error'))
        self.assertEqual(self.keeper.disabled, 'ping_error')
        self.keeper.request_started()
        self.keeper.request_finished(request(), [], dict(self.rows('messages')[0]))
        self.assertIsNone(self.keeper.held)  # never kept again, no retry

    def test_a_ping_that_reads_less_than_the_held_prompt_disables_keeping(self):
        self.send()
        with unittest.mock.patch.dict('modelpilot.fixtures.USAGE', cache_read_input_tokens=100, input_tokens=14660):
            self.assertEqual(self.at(55.1), 'ping_missed')
        [stop] = self.rows('keeper')
        self.assertEqual((stop['reason'], stop['detail']), ('ping_missed', 100))

    def test_a_ping_of_unknown_cost_disables_keeping(self):
        self.send()
        with unittest.mock.patch.dict('modelpilot.fixtures.USAGE', cache_creation_input_tokens=5):  # no TTL breakdown
            self.assertEqual(self.at(55.1), 'cost_unknown')
        self.assertEqual(self.keeper.disabled, 'cost_unknown')

    def test_the_client_exiting_drops_what_is_held_and_nothing_secret_is_logged(self):
        self.send()
        self.assertEqual(self.at(55.1), 'pinged')
        self.close()
        self.assertIsNone(self.keeper.held)
        self.assertTrue(Hold.made[0].released)
        stop = self.rows('keeper')[-1]
        self.assertEqual((stop['reason'], stop['pings']), ('client_exit', 1))
        text = self.log.read_text()
        self.assertNotIn(KEY, text)
        self.assertNotIn(SECRET_TEXT, text)
        self.assertNotIn(KEY, (self.root/'ledger.sqlite3').read_bytes().decode('latin-1'))


class SummaryTests(unittest.TestCase):
    def rows(self, ping_status='ok', ping_cost=.01, ttl='1h', gap=70):
        before = {'kind': 'messages', 'model': MODEL, 'status': 'ok', 'tool_count': 1, 'started_unix': 0,
                  'wall_seconds': 10, 'cache_ttl': [ttl],
                  'usage': {'input_tokens': 0, 'cache_read_input_tokens': 90_000, 'cache_creation_input_tokens': 10_000,
                            'output_tokens': 50}}
        ping = {'kind': 'keeper_ping', 'status': ping_status, 'started_unix': 10 + 55 * MINUTE, 'cost_usd': ping_cost}
        after = dict(before, started_unix=10 + gap * MINUTE,
                     usage={'input_tokens': 5, 'cache_read_input_tokens': 100_000, 'cache_creation_input_tokens': 300,
                            'output_tokens': 50})
        return [before, ping, after, {'kind': 'keeper', 'reason': 'client_exit'}]

    def test_a_kept_return_avoids_rewriting_what_it_read(self):
        s = keeper.summary(self.rows(), RATES)
        self.assertEqual((s['pings'], s['answered'], s['ping_cost_usd'], s['stops']), (1, 1, .01, {'client_exit': 1}))
        [kept] = s['kept_returns']
        self.assertEqual((kept['pings'], kept['read_tokens']), (1, 100_000))
        self.assertAlmostEqual(s['avoided_rewrite_usd'], 100_000 * (4 - .1) / 1e6)
        self.assertAlmostEqual(s['net_usd'], .39 - .01)

    def test_no_saving_without_an_answered_ping_a_1h_entry_or_a_gap_past_the_hour(self):
        for rows in (self.rows(ping_status='upstream_error'), self.rows(ttl='5m'), self.rows(gap=50)):
            self.assertEqual(keeper.summary(rows, RATES)['kept_returns'], [])

    def test_a_ping_of_unknown_cost_leaves_the_net_unknown(self):
        s = keeper.summary(self.rows(ping_cost=None), RATES)
        self.assertEqual(s['unknown_cost_pings'], 1)
        self.assertIsNone(s['net_usd'])
        self.assertIsNone(keeper.summary(self.rows(), {})['avoided_rewrite_usd'])


# Stands in for Claude Code: a main-loop request, an idle pause the keeper pings in, another request, exit.
FAKE_CLIENT = r'''
import json, os, sys, time, urllib.request
base = os.environ['ANTHROPIC_BASE_URL']
for i in range(2):
    body = json.dumps({'model': 'claude-sonnet-5-5', 'max_tokens': 16, 'stream': False,
                       'system': [{'type': 'text', 'text': 'x', 'cache_control': {'type': 'ephemeral', 'ttl': '1h'}}],
                       'tools': [{'name': 'Read', 'input_schema': {'type': 'object'}}],
                       'messages': [{'role': 'user', 'content': 'hi'}]}).encode()
    request = urllib.request.Request(base + '/v1/messages', body, {'content-type': 'application/json',
                                     'x-api-key': 'sk-ant-offline-fixture-not-a-key', 'anthropic-version': '2023-06-01'})
    urllib.request.urlopen(request).read()
    time.sleep(float(sys.argv[1]) if i == 0 else 0)
'''


class OwnSessionTests(unittest.TestCase):
    """own_sessions --keep, $0: the keeper's own thread on short timings, a loopback fixture."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.upstream = fixture_server()
        self.upstream.keep_bodies = True
        self.thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.upstream.server_port}'
        Hold.made = []

    def tearDown(self):
        self.upstream.shutdown()
        self.upstream.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def test_a_session_with_the_keeper_pings_in_its_pause_and_reports(self):
        client = self.root/'client.py'
        client.write_text(FAKE_CLIENT)
        env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(self.root)}
        keep = {'every': .3, 'late': 30, 'window': 60, 'poll': .05, 'awake': Hold}
        s = own_sessions.run(sys.executable, 'stand-in', self.root/'out', self.url, [str(client), '1.0'], RATES,
                             env=env, stdin=subprocess.DEVNULL, keep=keep)
        k = s['keeper']
        self.assertEqual((k['every_seconds'], k['ping_max_tokens']), (.3, 1))
        self.assertGreaterEqual(k['pings'], 1)
        self.assertEqual(k['answered'], k['pings'])
        self.assertEqual(k['stops'], {'client_exit': 1})
        self.assertEqual(k['kept_returns'], [])  # a 1-second pause is no return past the hour
        self.assertEqual(s['messages'], 2)  # pings are kept out of the client's request count
        self.assertTrue(Hold.made and all(h.released for h in Hold.made))
        pings = [json.loads(b) for b in self.upstream.bodies if json.loads(b)['max_tokens'] == 1]
        self.assertEqual(len(pings), k['pings'])
        saved = json.loads(next((self.root/'out').glob('*/summary.json')).read_text())
        self.assertEqual(saved['keeper']['pings'], k['pings'])

    def test_without_keep_nothing_of_its_own_is_sent(self):
        client = self.root/'client.py'
        client.write_text(FAKE_CLIENT)
        env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(self.root)}
        s = own_sessions.run(sys.executable, 'stand-in', self.root/'out', self.url, [str(client), '.5'], RATES,
                             env=env, stdin=subprocess.DEVNULL)
        self.assertIsNone(s['keeper'])
        self.assertEqual(len(self.upstream.bodies), 2)

    def test_the_plan_says_what_keep_sends(self):
        p = own_sessions.plan('/x/claude', '2.1.302 (Claude Code)', Path('/r'), own_sessions.UPSTREAM, [], keep=True)
        self.assertEqual(p['keeper']['every_seconds'], 55 * 60)
        self.assertEqual(p['keeper']['cap_usd_per_day'], 3.0)
        self.assertIn('max_tokens 1', p['sends'])
        self.assertIsNone(own_sessions.plan('/x/claude', 'v', Path('/r'), own_sessions.UPSTREAM, [])['keeper'])


def client_version():
    claude = shutil.which('claude')
    if not claude or not shutil.which('node'):
        return None
    with tempfile.TemporaryDirectory() as home:
        out = subprocess.run([claude, '--version'], env={'HOME': home, 'PATH': os.environ.get('PATH', '')},
                             capture_output=True, text=True, timeout=60)
    return out.stdout.split()[0] if out.returncode == 0 and out.stdout.strip() else None


VERSION = client_version()


@unittest.skipUnless(VERSION == '2.1.284', 'needs node and the pinned 2.1.284 client first on PATH')
class RealClientTests(unittest.TestCase):
    """The pinned client at 1h lifetimes pauses in a Bash `sleep` while the keeper's thread pings its last request."""
    def test_the_keeper_pings_the_real_clients_request_unchanged_but_for_max_tokens_and_stream(self):
        from modelpilot.bench import rates
        from modelpilot.governed_session import client_env
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dirs = {name: root/name for name in ('workspace', 'home', 'tmp', 'config')}
            for path in dirs.values():
                path.mkdir()
            upstream = fixture_server()
            upstream.keep_bodies = True
            upstream.script = [{'tool': 'Bash', 'input': {'command': 'sleep 2', 'description': 'Pause'}},
                               {'text': 'DONE'}]
            thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            thread.start()
            try:
                cli = shutil.which('claude')
                env = client_env(dict(os.environ), KEY, dirs, cli, {'CLAUDE_CODE_PROMPT_CACHE_TTL': '1h'})
                Hold.made = []
                keep = {'every': .5, 'late': 30, 'window': 60, 'poll': .05, 'awake': Hold}
                cwd = os.getcwd()
                os.chdir(dirs['workspace'])
                try:
                    s = own_sessions.run(cli, VERSION, root/'out', f'http://127.0.0.1:{upstream.server_port}',
                                         ['-p', 'Pause, then reply DONE.', '--model', MODEL, '--max-turns', '4',
                                          '--setting-sources', '', '--tools', 'Bash', '--allowedTools', 'Bash'],
                                         rates(), env=env, stdin=subprocess.DEVNULL, keep=keep)
                finally:
                    os.chdir(cwd)
            finally:
                upstream.shutdown()
                upstream.server_close()
                thread.join()
            self.assertEqual(s['returncode'], 0, s)
            k = s['keeper']
            self.assertGreaterEqual(k['pings'], 1, s)
            self.assertEqual((k['answered'], k['unknown_cost_pings']), (k['pings'], 0))
            bodies = [json.loads(b) for b in upstream.bodies]
            pings = [i for i, b in enumerate(bodies) if b.get('max_tokens') == 1]
            self.assertEqual(len(pings), k['pings'])
            held = [b for b in bodies[:pings[0]] if b.get('tools') and b.get('stream')][-1]
            self.assertEqual({k2: v for k2, v in bodies[pings[0]].items() if k2 not in ('max_tokens', 'stream')},
                             {k2: v for k2, v in held.items() if k2 not in ('max_tokens', 'stream')})


if __name__ == '__main__':
    unittest.main()
