"""Rate-limit headers (October 18): each reply's usage-limit readings logged with its row, $0 against the fixture."""
import email.message
import http.client
import json
from pathlib import Path
import random
import tempfile
import threading
import time
import unittest

from modelpilot import own_sessions
from modelpilot.fixtures import fixture_server
from modelpilot.proxy import RATE_LIMIT_MAX, ProxyServer, rate_limit_headers

RATES = {'claude-opus-4-6': dict(input=5, output=25, read=.5, write_5m=6.25, write_1h=10)}

# As a subscription's reply carries them: names the 2.1.302 client reads, values made up.
SUBSCRIPTION = [('anthropic-ratelimit-unified-status', 'allowed'),
                ('anthropic-ratelimit-unified-5h-utilization', '0.2375'),
                ('anthropic-ratelimit-unified-5h-reset', '1792371600'),
                ('anthropic-ratelimit-unified-7d-utilization', '0.41'),
                ('anthropic-ratelimit-unified-representative-claim', 'five_hour')]
LOGGED = {'unified-status': 'allowed', 'unified-5h-utilization': '0.2375', 'unified-5h-reset': '1792371600',
          'unified-7d-utilization': '0.41', 'unified-representative-claim': 'five_hour'}


def message(pairs):
    m = email.message.Message()
    for k, v in pairs:
        m[k] = v
    return m


class HeaderTests(unittest.TestCase):
    def test_plain_values_are_kept_as_sent_and_anything_else_is_present_but_unread(self):
        headers = message(SUBSCRIPTION + [
            ('Anthropic-RateLimit-Tokens-Reset', '2026-10-18T21:00:00Z'),  # a key's limits, any case
            ('anthropic-ratelimit-unified-upgrade-paths', 'a, b'), ('anthropic-ratelimit-unified-note', 'free text'),
            ('anthropic-ratelimit-unified-7d_oi-utilization', ' 0.5 '),
            ('request-id', 'req_x'), ('x-anthropic-ratelimit-other', '1'), ('anthropic-ratelimit-', '1')])
        self.assertEqual(rate_limit_headers(headers), dict(LOGGED, **{
            'tokens-reset': '2026-10-18T21:00:00Z', 'unified-upgrade-paths': None, 'unified-note': None,
            'unified-7d_oi-utilization': '0.5'}))
        self.assertEqual(rate_limit_headers(message([('request-id', 'req_x')])), {})

    def test_at_most_a_bounded_number_of_names(self):
        many = message([(f'anthropic-ratelimit-n{i}', str(i)) for i in range(RATE_LIMIT_MAX + 10)])
        self.assertEqual(len(rate_limit_headers(many)), RATE_LIMIT_MAX)


class ProxyRowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.log = Path(self.temp.name)/'log.jsonl'
        self.db = Path(self.temp.name)/'state.db'
        self.upstream = fixture_server()
        self.upstream.response_headers = list(SUBSCRIPTION)
        governor = {'db': self.db, 'session': 's', 'limit_usd': 1, 'task': None}
        self.proxy = ProxyServer(('127.0.0.1', 0), f'http://127.0.0.1:{self.upstream.server_port}', self.log, RATES,
                                 governor=governor)
        self.threads = []
        for s in (self.upstream, self.proxy):
            t = threading.Thread(target=s.serve_forever, daemon=True)
            t.start()
            self.threads.append(t)

    def tearDown(self):
        for s in (self.proxy, self.upstream):
            s.shutdown()
            s.server_close()
        for t in self.threads:
            t.join()
        self.temp.cleanup()

    def rows(self, count):
        for _ in range(200):
            rows = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
            if len(rows) >= count:
                return rows
            time.sleep(.005)
        self.fail('rows not logged')

    def post(self, stream=False, **metadata):
        body = json.dumps({'model': 'claude-opus-4-6', 'max_tokens': 8, 'stream': stream, 'metadata': metadata,
                           'messages': [{'role': 'user', 'content': 'PRIVATE_PROMPT'}]}).encode()
        conn = http.client.HTTPConnection('127.0.0.1', self.proxy.server_port, timeout=5)
        conn.request('POST', '/v1/messages', body, {'Content-Type': 'application/json', 'x-api-key': 'PRIVATE_KEY'})
        response = conn.getresponse()
        response.read()
        conn.close()
        return response

    def test_messages_rows_log_them_and_the_client_still_gets_them(self):
        for stream in (False, True):
            response = self.post(stream)
            self.assertEqual(response.getheader('anthropic-ratelimit-unified-5h-utilization'), '0.2375')
        error = self.post(test_error=True)  # a 429: what the limiter said is the point
        self.assertEqual(error.status, 429)
        self.upstream.response_headers = []
        self.post()
        rows = self.rows(4)
        self.assertEqual([r.get('rate_limit') for r in rows], [LOGGED, LOGGED, LOGGED, None])
        self.assertEqual(rows[2]['status'], 'upstream_error')
        self.assertNotIn('PRIVATE', self.log.read_text())

    def test_a_keeper_ping_logs_them(self):
        body = {'model': 'claude-opus-4-6', 'max_tokens': 1, 'messages': [{'role': 'user', 'content': 'PRIVATE_PROMPT'}]}
        out = self.proxy.side_call([('x-api-key', 'PRIVATE_KEY'), ('anthropic-version', '2023-06-01')], 'cache_keeper',
                                   body, 5, kind='keeper_ping', mode='keeper', id_prefix='mp-keep-')
        self.assertEqual(out['status'], 'ok')
        ping, = self.rows(1)
        self.assertEqual((ping['kind'], ping['rate_limit']), ('keeper_ping', LOGGED))
        self.assertNotIn('PRIVATE', self.log.read_text())


def reading(start, five, reset='A', status='allowed', kind='messages', **more):
    limits = {'unified-status': status, 'unified-5h-utilization': five, 'unified-5h-reset': reset}
    limits.update(more)
    return {'kind': kind, 'started_unix': start, 'tool_count': 1, 'cost_usd': .01, 'rate_limit': limits}


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name)/'observations.jsonl'

    def tearDown(self):
        self.tmp.cleanup()

    def test_readings_statuses_resets_and_the_smallest_step_in_one_window(self):
        rows = [reading(0, '0.20', **{'unified-7d-utilization': '0.40', 'unified-7d-reset': 'X'}),
                reading(1, '0.20'), reading(2, '0.23', kind='keeper_ping'), reading(3, '0.24'),
                reading(4, '0.02', reset='B'),  # a new window: no step across it
                reading(5, 'nan', status='allowed_warning'), reading(6, None),  # no reading, still a reply
                {'kind': 'messages', 'started_unix': 7, 'tool_count': 1, 'cost_usd': .01}]
        random.Random(0).shuffle(rows)  # read in time order, whatever the log's
        self.log.write_text(''.join(json.dumps(r) + '\n' for r in rows))
        s = own_sessions.summarize(self.log)['rate_limit']
        self.assertEqual(s['replies'], 7)
        self.assertEqual(s['statuses'], {'allowed': 6, 'allowed_warning': 1})
        self.assertEqual(s['windows'], {
            '5h': {'readings': 5, 'first': .2, 'last': .02, 'resets': 2, 'smallest_step': .01},
            '7d': {'readings': 1, 'first': .4, 'last': .4, 'resets': 1, 'smallest_step': None}})

    def test_a_session_without_them(self):
        self.assertEqual(own_sessions.summarize(self.log)['rate_limit'], {'replies': 0, 'statuses': {}, 'windows': {}})


if __name__ == '__main__':
    unittest.main()
