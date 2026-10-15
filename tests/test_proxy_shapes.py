"""Request and response shapes the proxy had not been tested on (M1's open gaps: other request shapes, broad client
compatibility, cancellation; docs/m1-results.md). Each reply comes from a local upstream scripted per test, so nothing
leaves the machine. Every test asserts byte-for-byte passthrough and accounting that is either correct or explicitly
unknown (cost_usd None, never a guess). The three bugs this file found on October 14 (expectedFailure tests then) were
fixed in proxy.py and cache_probe.py the same day; their tests now pass."""
import base64
import gzip
import hashlib
import http.client
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest
from modelpilot.bench import accounting
from modelpilot.bench_report import transport_check
from modelpilot.cache_probe import priced_usage
from modelpilot.proxy import MAX_BODY, ProxyServer

MODEL = 'claude-sonnet-4-6'
RATE = dict(input=3, output=15, read=.3, write_5m=3.75, write_1h=6)
RATES = {MODEL: RATE}
USAGE = {'input_tokens': 12, 'output_tokens': 40, 'cache_creation_input_tokens': 300, 'cache_read_input_tokens': 2000,
         'cache_creation': {'ephemeral_5m_input_tokens': 300, 'ephemeral_1h_input_tokens': 0},
         'service_tier': 'standard', 'inference_geo': 'global'}
# USAGE at RATE, written out rather than computed by the code under test.
USAGE_USD = (12 * 3 + 40 * 15 + 2000 * .3 + 300 * 3.75) / 1e6


def sse(*events, sep=b'\n'):
    return b''.join(b'event: ' + e['type'].encode() + sep + b'data: ' + json.dumps(e).encode() + sep + sep for e in events)


def pieces(data, size):
    return [data[i:i + size] for i in range(0, len(data), size)]


def message_start(usage=None):
    return {'type': 'message_start', 'message': {'id': 'msg_shape', 'type': 'message', 'role': 'assistant', 'model': MODEL,
                                                 'content': [], 'stop_reason': None, 'stop_sequence': None,
                                                 'usage': dict(usage or USAGE, output_tokens=1)}}


def message_end(usage=None, stop='end_turn'):
    return [{'type': 'message_delta', 'delta': {'stop_reason': stop, 'stop_sequence': None}, 'usage': dict(usage or USAGE)},
            {'type': 'message_stop'}]


TEXT_STREAM = sse(message_start(), {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
                  {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'Done.'}},
                  {'type': 'content_block_stop', 'index': 0}, *message_end())


class ShapeUpstream(BaseHTTPRequestHandler):
    """Answers each request as server.reply(seen) says: {'status', 'reason', 'headers': [(name, value)], 'pieces',
    'framing': 'chunked' | 'length' | 'close', 'delay' (between pieces), 'wait' (before the status line),
    'end': False (a chunked reply cut before its last chunk), 'drop': True (close without replying), 'reset': True (reset
    the connection after the pieces)}. A GET gets the plan's pieces as one JSON reply, or nothing with 'drop'."""
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers['Content-Length']))
        seen = {'path': self.path, 'body': raw, 'sha256': hashlib.sha256(raw).hexdigest(),
                'headers': [(k.lower(), v) for k, v in self.headers.items()]}
        with self.server.lock:
            self.server.received.append(seen)
        plan = self.server.reply(seen)
        framing, parts = plan.get('framing', 'chunked'), plan.get('pieces', [])
        self.close_connection = plan.get('drop') or framing == 'close' or not plan.get('end', True)
        if plan.get('drop'):
            return
        time.sleep(plan.get('wait', 0))
        finished = False
        try:
            self.send_response_only(plan.get('status', 200), plan.get('reason'))
            for name, value in plan.get('headers', []):
                self.send_header(name, value)
            if framing == 'chunked':
                self.send_header('Transfer-Encoding', 'chunked')
            elif framing == 'length':
                self.send_header('Content-Length', str(sum(map(len, parts))))
            else:
                self.send_header('Connection', 'close')
            self.end_headers()
            for i, part in enumerate(parts):
                if i and plan.get('delay'):
                    time.sleep(plan['delay'])
                self.wfile.write(f'{len(part):x}\r\n'.encode() + part + b'\r\n' if framing == 'chunked' else part)
                self.wfile.flush()
            if plan.get('reset'):  # an RST, not a FIN: close every file on the socket, then the socket with linger 0
                self.close_connection = True
                self.rfile.close()
                self.wfile.close()
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
                self.connection.close()
                self.wfile = io.BytesIO()  # the handler flushes and closes it after do_POST
                return
            if framing == 'chunked' and plan.get('end', True):
                self.wfile.write(b'0\r\n\r\n')
                self.wfile.flush()
            finished = True
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        finally:
            with self.server.lock:
                self.server.finished.append(finished)


    def do_GET(self):
        plan = self.server.reply({'path': self.path, 'headers': [(k.lower(), v) for k, v in self.headers.items()]})
        self.close_connection = True
        if plan.get('drop'):
            return
        body = b''.join(plan.get('pieces', []))
        self.send_response_only(plan.get('status', 200))
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ShapeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.log = Path(self.temp.name)/'log.jsonl'
        self.upstream = ThreadingHTTPServer(('127.0.0.1', 0), ShapeUpstream)
        self.upstream.lock, self.upstream.received, self.upstream.finished = threading.Lock(), [], []
        self.upstream.reply = lambda seen: {'headers': [('Content-Type', 'text/event-stream')], 'pieces': [TEXT_STREAM]}
        self.proxy = ProxyServer(('127.0.0.1', 0), f'http://127.0.0.1:{self.upstream.server_port}', self.log, RATES)
        self.threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (self.upstream, self.proxy)]
        for t in self.threads:
            t.start()

    def tearDown(self):
        for s in (self.proxy, self.upstream):
            s.shutdown()
            s.server_close()
        for t in self.threads:
            t.join()
        self.temp.cleanup()

    def send(self, body, path='/v1/messages', headers=(), conn=None):
        """(status, reason, response headers as (lower name, value) pairs, body bytes) for one request through the proxy."""
        own = conn is None
        conn = conn or http.client.HTTPConnection('127.0.0.1', self.proxy.server_port, timeout=10)
        try:
            conn.putrequest('POST', path, skip_accept_encoding=True)
            for name, value in (('Content-Type', 'application/json'), ('x-api-key', 'sk-ant-offline'),
                                ('anthropic-version', '2023-06-01'), ('Content-Length', str(len(body))), *headers):
                conn.putheader(name, value)
            conn.endheaders(body)
            response = conn.getresponse()
            data = response.read()
            return response.status, response.reason, [(k.lower(), v) for k, v in response.getheaders()], data
        finally:
            if own:
                conn.close()

    def rows(self):
        self.assertTrue(self.proxy.wait_idle(10))
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def reply(self, plan):
        self.upstream.reply = lambda seen: plan

    def assert_unknown(self, row):
        """Explicitly unknown: no dollar figure, and the trial's total is unknown too."""
        self.assertIsNone(row['cost_usd'])
        self.assertIsNone(accounting([row], {})['cost_usd'])
        self.assertEqual(accounting([row], {})['unpriced_requests'], 1)

    def test_a_non_streamed_reply_with_thinking_and_a_tool_call_passes_byte_for_byte_and_is_priced(self):
        reply = json.dumps({'id': 'msg_1', 'type': 'message', 'role': 'assistant', 'model': MODEL, 'content': [
            {'type': 'thinking', 'thinking': 'PRIVATE plan', 'signature': 'EuYBCkQYAiJA' + 'A' * 300},
            {'type': 'redacted_thinking', 'data': 'EmwKAhgB' + 'B' * 200},
            {'type': 'text', 'text': 'Reading it.'},
            {'type': 'tool_use', 'id': 'toolu_1', 'name': 'Read', 'input': {'file_path': '/w/parse.py'}}],
            'stop_reason': 'tool_use', 'stop_sequence': None, 'usage': USAGE}, indent=1).encode()
        self.reply({'headers': [('Content-Type', 'application/json'), ('request-id', 'req_shape1')], 'pieces': [reply],
                    'framing': 'length'})
        # History with signed thinking, as Claude Code resends it; indented so any re-serialization would show.
        request = {'model': MODEL, 'max_tokens': 1024, 'thinking': {'type': 'adaptive'},
                   'tools': [{'name': 'Read', 'input_schema': {'type': 'object'}}],
                   'messages': [{'role': 'user', 'content': 'Fix it'},
                                {'role': 'assistant', 'content': [
                                    {'type': 'thinking', 'thinking': 'PRIVATE earlier', 'signature': 'Sig0' + 'C' * 100},
                                    {'type': 'redacted_thinking', 'data': 'EmwKAhgB' + 'D' * 50},
                                    {'type': 'tool_use', 'id': 'toolu_0', 'name': 'Read', 'input': {'file_path': '/w/a.py'}}]},
                                {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'toolu_0', 'content': 'x = 1'}]}]}
        raw = json.dumps(request, indent=2).encode()
        status, _, headers, data = self.send(raw)
        self.assertEqual((status, data), (200, reply))
        self.assertIn(('request-id', 'req_shape1'), headers)
        self.assertEqual(self.upstream.received[-1]['sha256'], hashlib.sha256(raw).hexdigest())
        row, = self.rows()
        self.assertAlmostEqual(row['cost_usd'], USAGE_USD)
        self.assertEqual((row['stream'], row['usage_complete'], row['tool_count'], row['provider_request_id']),
                         (False, True, 1, 'req_shape1'))
        self.assertNotIn('PRIVATE', self.log.read_text())
        self.assertNotIn('EuYB', self.log.read_text())

    def test_streamed_signed_thinking_and_an_input_json_delta_tool_call_pass_byte_for_byte(self):
        events = [message_start(dict(USAGE, input_tokens=12, cache_read_input_tokens=2000)),
                  {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'thinking', 'thinking': '', 'signature': ''}},
                  {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'thinking_delta', 'thinking': 'The separator '}},
                  {'type': 'ping'},
                  {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'thinking_delta', 'thinking': 'is optional.'}},
                  {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'signature_delta', 'signature': 'EqQBCgIYAhIM' + 'E' * 200}},
                  {'type': 'content_block_stop', 'index': 0},
                  {'type': 'content_block_start', 'index': 1,
                   'content_block': {'type': 'tool_use', 'id': 'toolu_2', 'name': 'Edit', 'input': {}}},
                  {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'input_json_delta', 'partial_json': ''}},
                  {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'input_json_delta', 'partial_json': '{"file_path": "/w/pa'}},
                  {'type': 'content_block_delta', 'index': 1,
                   'delta': {'type': 'input_json_delta', 'partial_json': 'rse.py", "old_string": "a\\n\\nb", "new'}},
                  {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'input_json_delta', 'partial_json': '_string": "c"}'}},
                  {'type': 'content_block_stop', 'index': 1}, *message_end(stop='tool_use')]
        request = json.dumps({'model': MODEL, 'max_tokens': 64, 'stream': True, 'tools': [{'name': 'Edit'}],
                              'messages': [{'role': 'user', 'content': 'fix'}]}).encode()
        for sep in (b'\n', b'\r\n'):
            with self.subTest(separator=sep):
                # An SSE comment (a keep-alive) between events, and the stream split at arbitrary byte positions.
                stream = sse(*events[:4], sep=sep) + b': keep-alive' + sep + sep + sse(*events[4:], sep=sep)
                self.reply({'headers': [('Content-Type', 'text/event-stream')], 'pieces': pieces(stream, 23)})
                status, _, _, data = self.send(request)
                self.assertEqual((status, data), (200, stream))
                row = self.rows()[-1]
                self.assertEqual((row['stream'], row['usage_complete'], row['usage']['output_tokens']), (True, True, 40))
                self.assertAlmostEqual(row['cost_usd'], USAGE_USD)

    def test_image_and_document_blocks_reach_the_upstream_unchanged(self):
        image = base64.b64encode(os.urandom(3 * 1024 * 1024)).decode()
        pdf = base64.b64encode(b'%PDF-1.4\n' + os.urandom(200_000)).decode()
        request = {'model': MODEL, 'max_tokens': 64, 'messages': [{'role': 'user', 'content': [
            {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': image}},
            {'type': 'image', 'source': {'type': 'url', 'url': 'https://example.com/a.png'}},
            {'type': 'document', 'source': {'type': 'base64', 'media_type': 'application/pdf', 'data': pdf}, 'title': 'spec',
             'citations': {'enabled': True}, 'cache_control': {'type': 'ephemeral', 'ttl': '1h'}},
            {'type': 'document', 'source': {'type': 'text', 'media_type': 'text/plain', 'data': 'PRIVATE text document'}},
            {'type': 'text', 'text': 'Describe these.'}]}]}
        raw = json.dumps(request, separators=(', ', ':  ')).encode()
        usage = dict(USAGE, cache_creation={'ephemeral_5m_input_tokens': 0, 'ephemeral_1h_input_tokens': 300})
        reply = json.dumps({'type': 'message', 'model': MODEL, 'content': [], 'usage': usage}).encode()
        self.reply({'headers': [('Content-Type', 'application/json')], 'pieces': [reply], 'framing': 'length'})
        status, _, _, data = self.send(raw)
        self.assertEqual((status, data), (200, reply))
        seen = self.upstream.received[-1]
        self.assertEqual(seen['sha256'], hashlib.sha256(raw).hexdigest())
        self.assertIn(('content-length', str(len(raw))), seen['headers'])
        row, = self.rows()
        self.assertEqual(row['cache_ttl'], ['1h'])
        self.assertAlmostEqual(row['cost_usd'], (12 * 3 + 40 * 15 + 2000 * .3 + 300 * 6) / 1e6)  # 1h writes at the 1h rate
        text = self.log.read_text()
        self.assertNotIn(image[:64], text)
        self.assertNotIn('PRIVATE', text)

    def test_a_body_over_the_limit_is_refused_before_it_is_read(self):
        for headers in ([('Content-Length', str(MAX_BODY + 1))], [('Transfer-Encoding', 'chunked')]):
            with self.subTest(headers=headers):
                with socket.create_connection(('127.0.0.1', self.proxy.server_port), timeout=10) as sock:
                    head = 'POST /v1/messages HTTP/1.1\r\nHost: proxy\r\nContent-Type: application/json\r\n'
                    head += ''.join(f'{k}: {v}\r\n' for k, v in headers) + '\r\n'
                    # A chunked upload announces one chunk over the limit; neither body is ever sent.
                    sock.sendall(head.encode() + (f'{MAX_BODY + 1:x}\r\n'.encode() if headers[0][0] == 'Transfer-Encoding' else b''))
                    reply = sock.makefile('rb').readline()
                self.assertIn(reply.split()[1], (b'413', b'400'))
        self.assertEqual(self.upstream.received, [])

    def test_token_counting_forwards_any_shape_and_is_free_even_when_refused(self):
        request = {'model': MODEL, 'thinking': {'type': 'adaptive'}, 'tools': [{'name': 'Read', 'input_schema': {'type': 'object'}}],
                   'messages': [{'role': 'user', 'content': [
                       {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'iVBORw0KGgo='}},
                       {'type': 'document', 'source': {'type': 'text', 'media_type': 'text/plain', 'data': 'doc'}},
                       {'type': 'text', 'text': 'count me'}]}]}
        raw = json.dumps(request, indent=3).encode()
        counted = b'{"input_tokens": 1534}'
        self.reply({'headers': [('Content-Type', 'application/json')], 'pieces': [counted], 'framing': 'length'})
        self.assertEqual(self.send(raw, '/v1/messages/count_tokens?beta=true')[::3], (200, counted))
        self.assertEqual(self.upstream.received[-1]['sha256'], hashlib.sha256(raw).hexdigest())
        limited = b'{"type":"error","error":{"type":"rate_limit_error","message":"slow down"}}'
        self.reply({'status': 429, 'headers': [('Content-Type', 'application/json'), ('retry-after', '3')], 'pieces': [limited]})
        status, _, headers, data = self.send(raw, '/v1/messages/count_tokens')
        self.assertEqual((status, data), (429, limited))
        self.assertIn(('retry-after', '3'), headers)
        ok, refused = self.rows()
        self.assertEqual((ok['kind'], ok['cost_usd'], ok['input_tokens'], ok['tool_count']), ('count_tokens', 0.0, 1534, 1))
        self.assertEqual((refused['kind'], refused['http_status'], refused['status'], refused['cost_usd'], refused['input_tokens']),
                         ('count_tokens', 429, 'upstream_error', 0.0, None))
        result = accounting([ok, refused], {})
        self.assertEqual((result['requests'], result['count_tokens_requests'], result['unpriced_requests']), (0, 2, 0))

    def test_429_and_529_pass_through_once_with_their_retry_headers_and_stay_unpriced(self):
        for status, reason, kind in ((429, 'Too Many Requests', 'rate_limit_error'), (529, 'Overloaded', 'overloaded_error')):
            for stream in (False, True):
                with self.subTest(status=status, stream=stream):
                    body = json.dumps({'type': 'error', 'error': {'type': kind, 'message': 'try later'}}).encode()
                    sent = [('Content-Type', 'application/json'), ('retry-after', '7'), ('x-should-retry', 'true'),
                            ('anthropic-ratelimit-requests-remaining', '0'), ('request-id', f'req_{status}')]
                    self.reply({'status': status, 'reason': reason, 'headers': sent, 'pieces': [body]})
                    before = len(self.upstream.received)
                    request = json.dumps({'model': MODEL, 'max_tokens': 8, 'stream': stream, 'messages': []}).encode()
                    got, got_reason, headers, data = self.send(request)
                    self.assertEqual((got, got_reason, data), (status, reason, body))
                    for name, value in sent:
                        self.assertIn((name.lower(), value), headers)
                    self.assertEqual(len(self.upstream.received), before + 1)  # never retried by the proxy
                    row = self.rows()[-1]
                    self.assertEqual((row['http_status'], row['status']), (status, 'upstream_error'))
                    self.assert_unknown(row)
                    self.assertEqual(accounting([row], {})['rejected_requests'], 1)

    def test_an_error_event_mid_stream_leaves_the_cost_unknown(self):
        stream = sse(message_start(), {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
                     {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'Half'}},
                     {'type': 'error', 'error': {'type': 'overloaded_error', 'message': 'Overloaded'}})
        self.reply({'headers': [('Content-Type', 'text/event-stream')], 'pieces': pieces(stream, 50), 'framing': 'close'})
        status, _, _, data = self.send(json.dumps({'model': MODEL, 'max_tokens': 8, 'stream': True, 'messages': []}).encode())
        self.assertEqual((status, data), (200, stream))
        row, = self.rows()
        self.assertEqual((row['http_status'], row['status'], row['usage_complete'], row['usage_problem']),
                         (200, 'ok', False, 'error_event'))
        self.assert_unknown(row)
        result = accounting([row], {})
        self.assertEqual((result['rejected_requests'], result['transport_failures']), (0, 0))  # billed work, not a rejection

    def test_a_stream_the_upstream_cuts_short_is_forwarded_as_far_as_it_went_and_unknown(self):
        partial = sse(message_start(), {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
                      {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'Half'}})
        request = json.dumps({'model': MODEL, 'max_tokens': 8, 'stream': True, 'messages': []}).encode()
        for plan, problem in (({'end': False}, None), ({'framing': 'close'}, 'stream_ended_early')):
            with self.subTest(plan=plan):
                self.reply(dict({'headers': [('Content-Type', 'text/event-stream')], 'pieces': pieces(partial, 64)}, **plan))
                try:
                    status, _, _, data = self.send(request)
                except http.client.IncompleteRead as e:  # the client may see the cut too
                    status, data = 200, e.partial
                self.assertEqual((status, data), (200, partial))
                row = self.rows()[-1]
                self.assert_unknown(row)
                if problem:
                    self.assertEqual((row['status'], row['usage_problem']), ('ok', problem))
                else:  # a chunked reply without its last chunk: the transport error is named
                    self.assertEqual((row['http_status'], row['error_type']), (200, 'IncompleteRead'))

    def test_an_upstream_that_closes_without_replying_leaves_a_lost_request_of_unknown_cost(self):
        self.reply({'drop': True})
        try:
            self.send(json.dumps({'model': MODEL, 'max_tokens': 8, 'messages': []}).encode())
        except http.client.RemoteDisconnected:
            pass  # before October 14's fix the client got no reply; it now gets a 502 (test below)
        row, = self.rows()
        self.assertIsNone(row['http_status'])
        self.assert_unknown(row)
        self.assertEqual(accounting([row], {})['transport_failures'], 1)
        self.assertEqual(transport_check([row])['lost_requests'], 1)

    def test_a_client_that_goes_away_mid_stream_cancels_the_upstream_and_leaves_the_cost_unknown(self):
        # A long stream: message_start, then pings every 25 ms for about 2 s, then the end.
        stream = [sse(message_start())] + [sse({'type': 'ping'})] * 80 + [sse(*message_end())]
        request = json.dumps({'model': MODEL, 'max_tokens': 8, 'stream': True, 'messages': []}).encode()
        head = (f'POST /v1/messages HTTP/1.1\r\nHost: proxy\r\nContent-Type: application/json\r\n'
                f'Content-Length: {len(request)}\r\n\r\n').encode()
        for when, wait in (('after the first event', 0), ('before the first byte', .3)):
            with self.subTest(when=when):
                self.upstream.finished.clear()
                self.reply({'headers': [('Content-Type', 'text/event-stream')], 'pieces': stream, 'delay': .025, 'wait': wait})
                sock = socket.create_connection(('127.0.0.1', self.proxy.server_port), timeout=10)
                sock.sendall(head + request)
                got = b''
                while wait == 0 and b'message_start' not in got:
                    got += sock.recv(4096)
                sock.close()
                for _ in range(400):
                    if self.upstream.finished:
                        break
                    time.sleep(.01)
                self.assertEqual(self.upstream.finished, [False])  # the proxy closed the upstream before the stream ended
                row = self.rows()[-1]
                self.assertEqual((row['http_status'], row['status']), (200, 'connection_closed'))
                self.assertLess(row['wall_seconds'], 1.9)
                self.assert_unknown(row)

    def test_a_compressed_reply_is_forwarded_as_sent_and_its_cost_unknown(self):
        body = json.dumps({'type': 'message', 'model': MODEL, 'content': [], 'usage': USAGE}).encode()
        packed = gzip.compress(body)
        self.reply({'headers': [('Content-Type', 'application/json'), ('Content-Encoding', 'gzip')], 'pieces': [packed],
                    'framing': 'length'})  # an upstream that ignores the proxy's identity request
        status, _, headers, data = self.send(json.dumps({'model': MODEL, 'max_tokens': 8, 'messages': []}).encode(),
                                             headers=[('Accept-Encoding', 'gzip, br')])
        self.assertEqual((status, data), (200, packed))
        self.assertIn(('content-encoding', 'gzip'), headers)
        self.assertIn(('accept-encoding', 'identity'), self.upstream.received[-1]['headers'])
        row, = self.rows()
        self.assertEqual((row['response_encoding'], row['usage_problem']), ('gzip', 'compressed_response'))
        self.assert_unknown(row)

    def test_a_compressed_request_body_is_refused_and_never_forwarded(self):
        packed = gzip.compress(json.dumps({'model': MODEL, 'max_tokens': 8, 'messages': []}).encode())
        status, _, _, _ = self.send(packed, headers=[('Content-Encoding', 'gzip')])
        self.assertEqual(status, 400)  # fails closed: the proxy can't read it, so it never sends it
        self.assertEqual(self.upstream.received, [])
        self.assertEqual(self.log.read_text(), '')

    def test_a_keep_alive_client_gets_one_reply_per_connection_and_every_request_is_logged(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.proxy.server_port, timeout=10)
        try:
            for _ in range(3):
                status, _, headers, data = self.send(
                    json.dumps({'model': MODEL, 'max_tokens': 8, 'stream': True, 'messages': []}).encode(),
                    headers=[('Connection', 'keep-alive')], conn=conn)
                self.assertEqual((status, data), (200, TEXT_STREAM))
                self.assertIn(('connection', 'close'), headers)  # the client reconnects for the next one
        finally:
            conn.close()
        rows = self.rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual(len(self.upstream.received), 3)
        for row in rows:
            self.assertAlmostEqual(row['cost_usd'], USAGE_USD)

    def test_an_oversized_event_or_reply_still_passes_and_its_cost_is_unknown(self):
        big = 'x' * (2 * 1024 * 1024)  # well past the 1 MiB bound, which is checked between reads, not per event
        stream = sse(message_start(), {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': big}},
                     {'type': 'content_block_stop', 'index': 0}, *message_end())
        reply = json.dumps({'type': 'message', 'model': MODEL, 'content': [{'type': 'text', 'text': big}], 'usage': USAGE}).encode()
        for stream_mode, body, kind in ((True, stream, 'text/event-stream'), (False, reply, 'application/json')):
            with self.subTest(stream=stream_mode):
                self.reply({'headers': [('Content-Type', kind)], 'pieces': pieces(body, 65536)})
                status, _, _, data = self.send(
                    json.dumps({'model': MODEL, 'max_tokens': 8, 'stream': stream_mode, 'messages': []}).encode())
                self.assertEqual((status, data), (200, body))
                row = self.rows()[-1]
                self.assertEqual(row['usage_problem'], 'oversized_response')
                self.assert_unknown(row)

    # Bugs found. Each test states the behaviour the proxy should have; proxy.py and cache_probe.py are not changed here.

    def test_a_reply_that_ran_server_side_web_search_is_priced_with_its_fee_or_left_unknown(self):
        """Fixed October 14 (an expectedFailure until then): cache_probe.priced_usage priced tokens only and ignored
        usage.server_tool_use, so a reply that ran the API's server-side web search (Claude Code's WebSearch tool; $10 per
        1,000 searches) was logged as a known cost without the search fee, and logged_usage dropped the counter. Now the
        counters are logged and a positive fee-bearing one leaves the cost unknown (no rates table carries the fee)."""
        usage = dict(USAGE, server_tool_use={'web_search_requests': 2})
        streamed = sse(message_start(usage), *message_end(usage))
        replied = json.dumps({'type': 'message', 'model': MODEL, 'content': [], 'usage': usage}).encode()
        for stream_mode, body in ((False, replied), (True, streamed)):
            with self.subTest(stream=stream_mode):
                self.reply({'headers': [('Content-Type', 'application/json')], 'pieces': [body]})
                self.send(json.dumps({'model': MODEL, 'max_tokens': 8, 'stream': stream_mode, 'messages': [],
                                      'tools': [{'type': 'web_search_20250305', 'name': 'web_search'}]}).encode())
                row = self.rows()[-1]
                self.assertTrue(row['cost_usd'] is None or row['cost_usd'] >= USAGE_USD + 2 * 10 / 1000 - 1e-9, row['cost_usd'])
                self.assertIsNone(row['cost_usd'])  # unknown, not the tokens alone
                self.assertEqual(row['usage_problem'], 'unpriced_usage')
                self.assertEqual(row['usage']['server_tool_use'], {'web_search_requests': 2})  # kept for repricing

    def test_unmetered_or_zero_server_tool_counters_keep_the_token_price(self):
        for tools in ({'web_search_requests': 0, 'web_fetch_requests': 3}, {'web_search_requests': 0}):
            with self.subTest(tools=tools):
                usage = dict(USAGE, server_tool_use=tools)
                self.reply({'headers': [('Content-Type', 'application/json')], 'pieces': [json.dumps(
                    {'type': 'message', 'model': MODEL, 'content': [], 'usage': usage}).encode()]})
                self.send(json.dumps({'model': MODEL, 'max_tokens': 8, 'messages': []}).encode())
                row = self.rows()[-1]
                self.assertAlmostEqual(row['cost_usd'], USAGE_USD)  # web fetch isn't metered
                self.assertEqual(row['usage']['server_tool_use'], tools)
        for odd in ({'web_search_requests': -1}, {'code_execution_requests': 1}, {'web_search_requests': '2'}, [1]):
            self.assertIsNone(priced_usage({'model': MODEL}, MODEL, dict(USAGE, server_tool_use=odd), RATES), odd)

    def test_an_upstream_that_closes_without_replying_is_answered_with_a_502(self):
        """Fixed October 14 (an expectedFailure until then): http.client.RemoteDisconnected (an upstream that closes
        before its status line) subclasses ConnectionResetError, so do_POST's branch meant for a client that went away
        took it: the client got no reply instead of a 502, and the row said connection_closed instead of transport_error.
        Accounting was unaffected (cost unknown, counted as lost either way). Only writes to the client now count as the
        client going away (proxy.ClientGone)."""
        self.reply({'drop': True})
        status, _, _, _ = self.send(json.dumps({'model': MODEL, 'max_tokens': 8, 'messages': []}).encode())
        self.assertEqual(status, 502)
        row, = self.rows()
        self.assertEqual((row['status'], row['error_type']), ('transport_error', 'RemoteDisconnected'))
        self.assert_unknown(row)
        self.assertEqual(accounting([row], {})['transport_failures'], 1)

    def test_an_upstream_reset_mid_stream_is_the_upstreams_failure(self):
        self.reply({'headers': [('Content-Type', 'text/event-stream')], 'pieces': [sse(message_start())], 'reset': True})
        try:
            status, _, _, _ = self.send(json.dumps({'model': MODEL, 'max_tokens': 8, 'stream': True, 'messages': []}).encode())
        except (http.client.IncompleteRead, ConnectionError):
            status = 200  # the client sees the cut: the proxy had already sent the upstream's status line
        self.assertEqual(status, 200)
        row = self.rows()[-1]
        self.assertEqual((row['http_status'], row['status'], row['error_type']), (200, 'transport_error', 'ConnectionResetError'))
        self.assert_unknown(row)

    def test_a_catalog_upstream_that_closes_is_answered_with_a_502(self):
        self.reply({'drop': True})
        conn = http.client.HTTPConnection('127.0.0.1', self.proxy.server_port, timeout=10)
        try:
            conn.request('GET', '/v1/models')
            self.assertEqual(conn.getresponse().status, 502)
        finally:
            conn.close()
        row, = self.rows()
        self.assertEqual((row['kind'], row['status'], row['error_type']), ('models', 'transport_error', 'RemoteDisconnected'))

    def test_repeated_headers_are_all_forwarded(self):
        """Fixed October 14 (an expectedFailure until then): proxy.clean_headers built a dict from HTTPMessage.items(), so
        of a header sent more than once only the last value was forwarded: two anthropic-beta headers from a client
        reached the API as one (a beta silently dropped), two Set-Cookie headers from the API reached the client as one.
        Low impact for Claude Code (fetch merges repeats into one header). It now returns (name, value) pairs, sent one
        by one, each repeat as its own header."""
        self.reply({'headers': [('Content-Type', 'text/event-stream'), ('Set-Cookie', 'a=1'), ('Set-Cookie', 'b=2')],
                    'pieces': [TEXT_STREAM]})
        _, _, headers, _ = self.send(json.dumps({'model': MODEL, 'max_tokens': 8, 'stream': True, 'messages': []}).encode(),
                                     headers=[('anthropic-beta', 'one-2026-01-01'), ('anthropic-beta', 'two-2026-01-01')])
        forwarded = [v for k, v in self.upstream.received[-1]['headers'] if k == 'anthropic-beta']
        self.assertEqual(sorted(b.strip() for v in forwarded for b in v.split(',')), ['one-2026-01-01', 'two-2026-01-01'])
        self.assertEqual(forwarded, ['one-2026-01-01', 'two-2026-01-01'])  # two headers, in order, as sent
        self.assertEqual([v for k, v in headers if k == 'set-cookie'], ['a=1', 'b=2'])
        self.assertEqual(sum(k == 'host' for k, _ in self.upstream.received[-1]['headers']), 1)  # Host once, the upstream's


if __name__ == '__main__':
    unittest.main()
