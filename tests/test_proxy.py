import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from modelpilot.governor import Governor, reconcile_log
from modelpilot.proxy import ProxyServer, UsageObserver, forecast, measured_cost
from modelpilot.fixtures import CATALOG, fixture_server, response_for, USAGE

RATES = {'claude-opus-4-6': dict(input=5, output=25, read=.5, write_5m=6.25, write_1h=10),
         'claude-sonnet-4-6': dict(input=3, output=15, read=.3, write_5m=3.75, write_1h=6)}


class ObserverTests(unittest.TestCase):
    def test_cumulative_stream_usage_with_fragmentation(self):
        request = {'model': 'claude-opus-4-6', 'stream': True}
        _, _, data = response_for(request)
        observer = UsageObserver(True)
        for byte in data:
            observer.feed(bytes([byte]))
        observer.finish()
        self.assertTrue(observer.complete)
        self.assertEqual(observer.usage['output_tokens'], 4)
        self.assertAlmostEqual(measured_cost(observer, request, RATES), .007525)

    def test_incomplete_stream_and_unknown_prices_are_not_free(self):
        observer = UsageObserver(True)
        observer.event({'type': 'message_start', 'message': {'usage': USAGE, 'model': 'unknown'}})
        self.assertIsNone(measured_cost(observer, {'model': 'unknown'}, RATES))
        observer.complete = True
        self.assertIsNone(measured_cost(observer, {'model': 'unknown'}, RATES))

    def test_inference_geo_not_applicable_is_standard_price(self):
        # Models without data-residency options (e.g. Haiku 4.5) report "not_available".
        for geo, priced in (('not_available', True), ('global', True), ('us', False)):
            observer = UsageObserver(False)
            observer.feed(json.dumps({'model': 'claude-opus-4-6', 'usage': dict(USAGE, inference_geo=geo)}).encode())
            observer.finish()
            cost = measured_cost(observer, {'model': 'claude-opus-4-6'}, RATES)
            self.assertEqual(cost is not None, priced, geo)

    def test_forecast_accounts_for_rebuild_once_and_never_applies(self):
        result = forecast({'model': 'claude-opus-4-6'}, USAGE, RATES)
        one = result['scenarios'][0]
        self.assertAlmostEqual(one['switch_usd'], (14750*3.75+10*3+4*15)/1e6)
        self.assertFalse(result['applied'])
        self.assertEqual(result['action'], 'hold')


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.log = Path(self.temp.name)/'log.jsonl'
        self.upstream = fixture_server()
        self.proxy = ProxyServer(('127.0.0.1', 0), f'http://127.0.0.1:{self.upstream.server_port}', self.log, RATES)
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

    def call(self, stream=False, **metadata):
        request = {'model': 'claude-opus-4-6', 'stream': stream, 'metadata': metadata,
                   'messages': [{'role': 'user', 'content': 'PRIVATE_PROMPT'}], 'max_tokens': 32}
        raw = json.dumps(request, indent=1).encode()
        conn = http.client.HTTPConnection('127.0.0.1', self.proxy.server_port, timeout=5)
        conn.request('POST', '/v1/messages?beta=true', raw,
                     {'Content-Type':'application/json','x-api-key':'PRIVATE_KEY', 'anthropic-beta':'test-beta', 'Accept-Encoding': 'gzip, br'})
        response = conn.getresponse()
        first = response.read(1)
        started = time.monotonic()
        data = first+response.read()
        elapsed_after_first = time.monotonic()-started
        conn.close()
        for _ in range(100):
            if self.log.exists() and self.log.read_text():
                break
            time.sleep(.005)
        return request, raw, response, data, elapsed_after_first

    def test_json_body_headers_and_usage(self):
        request, raw, response, data, _ = self.call()
        self.assertEqual(data, response_for(request)[2])
        self.assertEqual(response.status, 200)
        received = self.upstream.received[-1]
        self.assertEqual(received['sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(received['key'], 'PRIVATE_KEY')
        self.assertEqual(received['beta'], 'test-beta')
        self.assertEqual(received['accept_encoding'], 'identity')
        self.assertEqual(received['path'], '/v1/messages?beta=true')
        text = self.log.read_text()
        self.assertNotIn('PRIVATE', text)
        self.assertNotIn('synthetic output', text)
        row = json.loads(text)
        self.assertAlmostEqual(row['cost_usd'], .007525)
        self.assertFalse(row['decision']['applied'])

    def test_sse_bytes_forwarded_before_completion(self):
        request, _, _, data, elapsed = self.call(True, slow=True)
        self.assertEqual(data, response_for(request)[2])
        self.assertGreater(elapsed, .1)
        self.assertEqual(json.loads(self.log.read_text())['usage']['output_tokens'], 4)

    def test_http_errors_preserved_no_retry(self):
        request, _, response, data, _ = self.call(test_error=True)
        self.assertEqual(response.status, 429)
        self.assertEqual(response.getheader('retry-after'), '1')
        self.assertEqual(data, response_for(request)[2])
        self.assertEqual(len(self.upstream.received), 1)
        self.assertIsNone(json.loads(self.log.read_text())['cost_usd'])

    def test_stream_error_not_priced_as_success(self):
        request, _, _, data, _ = self.call(True, test_stream_error=True)
        self.assertEqual(data, response_for(request)[2])
        row = json.loads(self.log.read_text())
        self.assertFalse(row['usage_complete'])
        self.assertIsNone(row['cost_usd'])


class GovernedProxyTests(unittest.TestCase):
    """Dry-run governor wiring: every billable proxy request is reserved and settled under one shared ID."""
    call = ProxyTests.call
    rows = lambda self, count=1: JevAccountingProxyTests.rows(self, count)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name)/'state.db'
        self.upstream = fixture_server()
        self.servers = []
        self.start(self.upstream)

    def start(self, server):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append((server, thread))

    def govern(self, limit=1, task=None):
        self.log = Path(self.temp.name)/f'log{len(self.servers)}.jsonl'
        settings = {'db': self.db, 'session': 's', 'limit_usd': limit, 'task': task}
        self.proxy = ProxyServer(('127.0.0.1', 0), f'http://127.0.0.1:{self.upstream.server_port}', self.log, RATES, governor=settings)
        self.start(self.proxy)
        return Governor(self.db, 's', limit)

    def tearDown(self):
        for server, thread in reversed(self.servers):
            server.shutdown()
            server.server_close()
            thread.join()
        self.temp.cleanup()

    def test_streamed_request_settles_its_logged_cost_under_the_shared_id(self):
        gov = self.govern()
        try:
            request, _, _, data, _ = self.call(True)
            self.assertEqual(data, response_for(request)[2])
            row = self.rows()[-1]
            self.assertTrue(row['governor_request_id'].startswith('mp-'))
            self.assertEqual(row['governor_status'], 'settled')
            self.assertEqual(row['provider_request_id'], 'req_fixture1')
            self.assertEqual((row['governor']['admitted'], row['governor']['enforced']), (True, False))
            settle = gov.journal('settle')[-1]['payload']
            self.assertEqual(settle['request_id'], row['governor_request_id'])
            self.assertAlmostEqual(settle['actual_usd'], row['cost_usd'])
            policy = gov.policy()
            self.assertAlmostEqual(policy['spent_usd'], .007525)
            self.assertEqual((policy['reserved_usd'], policy['cost_complete']), (0, True))
            self.assertNotIn('PRIVATE', self.log.read_text())
        finally:
            gov.close()

    def test_http_and_stream_errors_settle_as_unknown(self):
        for metadata in ({'test_error': True}, {'test_stream_error': True}):
            self.db = Path(self.temp.name)/f'{len(self.servers)}.db'
            gov = self.govern()
            try:
                self.call(bool(metadata.get('test_stream_error')), **metadata)
                row = self.rows()[-1]
                self.assertIsNone(row['cost_usd'])
                self.assertEqual(row['governor_status'], 'settled')
                policy = gov.policy()
                self.assertEqual((policy['mode'], policy['cost_complete']), ('halt', False))
            finally:
                gov.close()

    def test_would_refuse_is_still_forwarded_and_counted(self):
        state = Governor(self.db, 's', .0001)
        task = state.state.create('t', 'old')['id']
        rev = state.state.claim(task, 1, 'client')['revision']
        state.state.correct(task, rev, 'new instruction not yet delivered')
        state.close()
        gov = self.govern(limit=.0001, task=task)
        try:
            request, _, response, data, _ = self.call()
            self.assertEqual((response.status, data), (200, response_for(request)[2]))
            row = self.rows()[-1]
            self.assertEqual((row['governor']['admitted'], row['governor']['reason']), (False, 'stale_task'))
            admit = gov.journal('admit')[-1]['payload']
            self.assertEqual((admit['enforced'], admit['reserved'], admit['applied']), (False, True, False))
            self.assertAlmostEqual(gov.policy()['spent_usd'], .007525)
        finally:
            gov.close()

    def test_free_endpoints_are_not_admitted(self):
        gov = self.govern()
        try:
            conn = http.client.HTTPConnection('127.0.0.1', self.proxy.server_port, timeout=5)
            conn.request('POST', '/v1/messages/count_tokens', json.dumps({'model': 'claude-opus-4-6', 'messages': []}),
                         {'Content-Type': 'application/json'})
            conn.getresponse().read()
            conn.close()
            conn = http.client.HTTPConnection('127.0.0.1', self.proxy.server_port, timeout=5)
            conn.request('GET', '/v1/models')
            conn.getresponse().read()
            conn.close()
            rows = self.rows(2)
            self.assertTrue(all('governor_request_id' not in r for r in rows))
            self.assertEqual(gov.journal('admit'), [])
        finally:
            gov.close()

    def test_concurrent_requests_are_all_settled(self):
        gov = self.govern()
        try:
            threads = [threading.Thread(target=self.call, kwargs={'stream': i % 2 == 0}) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            rows = self.rows(8)
            self.assertEqual(len(rows), 8)
            settled = {e['payload']['request_id']: e['payload']['actual_usd'] for e in gov.journal('settle')}
            self.assertEqual(set(settled), {r['governor_request_id'] for r in rows})
            self.assertAlmostEqual(gov.policy()['spent_usd'], sum(r['cost_usd'] for r in rows))
            self.assertEqual(gov.policy()['reserved_usd'], 0)
        finally:
            gov.close()

    def test_governor_failure_never_blocks_traffic_and_log_reconciles(self):
        gov = self.govern()
        try:
            self.proxy.governor['limit_usd'] = 2  # the session was recorded with 1: every open now raises
            request, _, response, data, _ = self.call()
            self.assertEqual((response.status, data), (200, response_for(request)[2]))
            row = self.rows()[-1]
            self.assertEqual((row['governor_status'], row['governor_error']), ('untracked', 'ValueError'))
            self.assertEqual(gov.journal('admit'), [])
            result = reconcile_log(gov, self.log)
            self.assertEqual((result['untracked_recorded'], result['still_unknown']), (1, 0))
            self.assertAlmostEqual(gov.policy()['spent_usd'], row['cost_usd'])
            self.assertEqual(reconcile_log(gov, self.log)['untracked_recorded'], 0)  # idempotent
        finally:
            gov.close()

    def test_unknown_task_is_refused_at_startup(self):
        with self.assertRaises(ValueError):
            ProxyServer(('127.0.0.1', 0), f'http://127.0.0.1:{self.upstream.server_port}', Path(self.temp.name)/'x.jsonl',
                        RATES, governor={'db': self.db, 'session': 's', 'limit_usd': 1, 'task': 'missing'})


class JevAccountingProxyTests(unittest.TestCase):
    """Proxy behind Jev: catalog discovery, chunked uploads and row labels (docs/jev-baseline-setup.md)."""
    setUp, tearDown, call = ProxyTests.setUp, ProxyTests.tearDown, ProxyTests.call

    def rows(self, count=1):
        for _ in range(200):
            text = self.log.read_text() if self.log.exists() else ''
            if len(text.splitlines()) >= count:
                break
            time.sleep(.005)
        return [json.loads(line) for line in text.splitlines()]

    def get(self, path):
        conn = http.client.HTTPConnection('127.0.0.1', self.proxy.server_port, timeout=5)
        conn.request('GET', path, headers={'x-api-key': 'PRIVATE_KEY', 'anthropic-version': '2023-06-01'})
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response, data

    def test_models_catalog_passes_through(self):
        response, data = self.get('/v1/models?limit=100')
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(data), CATALOG)
        received = self.upstream.received[-1]
        self.assertEqual((received['method'], received['path'], received['key']), ('GET', '/v1/models?limit=100', 'PRIVATE_KEY'))
        row = self.rows()[-1]
        self.assertEqual((row['kind'], row['http_status'], row['cost_usd'], row['applied']), ('models', 200, None, False))
        self.assertNotIn('PRIVATE', self.log.read_text())

    def test_other_get_paths_still_404(self):
        for path in ('/v1/models/../x', '/v1/other', '/v1/modelsX', '/v1/models/claude-sonnet-5', 'http://evil/v1/models'):
            response, _ = self.get(path)
            self.assertEqual(response.status, 404, path)
        self.assertEqual(self.upstream.received, [])

    def test_messages_rows_are_labeled_with_tool_count(self):
        self.get('/v1/models')
        self.call()
        rows = self.rows(2)
        messages = [r for r in rows if r['kind'] == 'messages']
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]['tool_count'], 0)
        self.assertAlmostEqual(sum(r['cost_usd'] for r in rows if r['cost_usd'] is not None), .007525)

    def test_chunked_upload_is_accepted_and_forwarded_with_length(self):
        # Jev's proxy deletes Content-Length and streams the rewritten body chunked.
        request = {'model': 'claude-opus-4-6', 'stream': False, 'tools': [{'name': 'Read'}], 'max_tokens': 32,
                   'messages': [{'role': 'user', 'content': 'PRIVATE_PROMPT'}]}
        raw = json.dumps(request).encode()
        conn = http.client.HTTPConnection('127.0.0.1', self.proxy.server_port, timeout=5)
        conn.request('POST', '/v1/messages', body=iter([raw[:20], raw[20:]]), encode_chunked=True,
                     headers={'Content-Type': 'application/json', 'x-api-key': 'PRIVATE_KEY'})
        response = conn.getresponse()
        data = response.read()
        conn.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(data, response_for(request)[2])
        self.assertEqual(self.upstream.received[-1]['sha256'], hashlib.sha256(raw).hexdigest())
        row = self.rows()[-1]
        self.assertEqual((row['kind'], row['tool_count']), ('messages', 1))
        self.assertAlmostEqual(row['cost_usd'], .007525)

    def raw_post(self, headers, body):
        conn = http.client.HTTPConnection('127.0.0.1', self.proxy.server_port, timeout=5)
        conn.putrequest('POST', '/v1/messages')
        for name, value in headers:
            conn.putheader(name, value)
        conn.endheaders()
        conn.send(body)
        status = conn.getresponse().status
        conn.close()
        return status

    def test_ambiguous_or_malformed_chunked_uploads_are_rejected(self):
        # Both framings at once is a request-smuggling shape; never forward it.
        self.assertEqual(self.raw_post([('Transfer-Encoding', 'chunked'), ('Content-Length', '5')], b'5\r\nhello\r\n0\r\n\r\n'), 400)
        self.assertEqual(self.raw_post([('Transfer-Encoding', 'chunked')], b'zz\r\nhello\r\n0\r\n\r\n'), 400)
        self.assertEqual(self.raw_post([('Transfer-Encoding', 'gzip, chunked')], b'0\r\n\r\n'), 400)
        self.assertEqual(self.upstream.received, [])
