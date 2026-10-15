"""M1's offline gaps (October 14 cloud batch): the pinned real client cancelled mid-stream by a signal, chunked request
bodies, and a stream that goes idle past the proxy's inactivity timeout. $0: a fake key, a loopback upstream, an empty
HOME; the client never reaches a provider. Real-client tests skip without node or the pinned client on PATH."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from modelpilot import fixtures
from modelpilot.bench import accounting
from modelpilot.bench_report import transport_check
from modelpilot.proxy import MAX_BODY, ProxyServer

MODEL = 'claude-sonnet-4-6'
RATES = {MODEL: dict(input=3, output=15, read=.3, write_5m=3.75, write_1h=6)}
CLI = shutil.which('claude')


def client_version():
    if not CLI or not shutil.which('node'):
        return None
    with tempfile.TemporaryDirectory() as home:
        out = subprocess.run([CLI, '--version'], env={'HOME': home, 'PATH': os.environ.get('PATH', '')},
                             capture_output=True, text=True, timeout=60)
    return out.stdout.split()[0] if out.returncode == 0 and out.stdout.strip() else None


VERSION = client_version()


def sse(event):
    return b'event: ' + event['type'].encode() + b'\ndata: ' + json.dumps(event).encode() + b'\n\n'


def start_event(model):
    usage = dict(fixtures.USAGE, cache_read_input_tokens=0, input_tokens=100, output_tokens=1)
    return {'type': 'message_start', 'message': {'id': 'msg_gap', 'type': 'message', 'role': 'assistant', 'model': model,
                                                 'content': [], 'stop_reason': None, 'stop_sequence': None, 'usage': usage}}


def end_events(text='Done.'):
    return [{'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
            {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': text}},
            {'type': 'content_block_stop', 'index': 0},
            {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': {'output_tokens': 4}},
            {'type': 'message_stop'}]


class SlowUpstream(BaseHTTPRequestHandler):
    """Streamed Messages requests get message_start, then a ping every server.tick seconds for server.pings pings (or,
    with server.stall, nothing for that many seconds), then the end. Anything else gets fixtures.response_for's quick
    reply. server.streams records, per streamed request, whether its stream finished."""
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers['Content-Length']))
        request = json.loads(raw)
        with self.server.lock:
            self.server.received.append({'path': self.path, 'stream': bool(request.get('stream')),
                                         'length': self.headers['Content-Length'], 'body': raw})
        if not (request.get('stream') and self.path.split('?', 1)[0] == '/v1/messages'):
            status, kind, data = (200, 'application/json', json.dumps({'input_tokens': 10}).encode()) \
                if 'count_tokens' in self.path else fixtures.response_for(request)
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        index = len(self.server.streams)
        with self.server.lock:
            self.server.streams.append(False)
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(sse(start_event(request['model'])))
            self.wfile.flush()
            self.server.first_event.set()
            if self.server.stall:
                time.sleep(self.server.stall)
            for _ in range(self.server.pings):
                time.sleep(self.server.tick)
                self.wfile.write(sse({'type': 'ping'}))
                self.wfile.flush()
            for event in end_events():
                self.wfile.write(sse(event))
            self.wfile.flush()
            self.server.streams[index] = True
        except (BrokenPipeError, ConnectionResetError):
            pass


def slow_upstream(pings=200, tick=.05, stall=0):
    server = ThreadingHTTPServer(('127.0.0.1', 0), SlowUpstream)
    server.lock, server.received, server.streams = threading.Lock(), [], []
    server.pings, server.tick, server.stall = pings, tick, stall
    server.first_event = threading.Event()
    return server


class Harness(unittest.TestCase):
    proxy_timeout = None  # the proxy's upstream inactivity timeout, lowered by a test (ProxyServer.connection's)

    def start(self, upstream):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.log = self.root/'observations.jsonl'
        self.upstream = upstream
        self.proxy = ProxyServer(('127.0.0.1', 0), f'http://127.0.0.1:{upstream.server_port}', self.log, RATES)
        if self.proxy_timeout is not None:
            real = self.proxy.connection
            self.proxy.connection = lambda timeout=120: real(self.proxy_timeout)
        self.threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (upstream, self.proxy)]
        for t in self.threads:
            t.start()

    def tearDown(self):
        for s in (self.proxy, self.upstream):
            s.shutdown()
            s.server_close()
        for t in self.threads:
            t.join()
        self.tmp.cleanup()

    def rows(self, wait=True):
        if wait:
            self.proxy.wait_idle(10)
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def client(self, prompt='Reply with OK.', retries=0):
        """The pinned client in -p mode through the proxy: fake key, empty HOME and config, no tools, no settings."""
        dirs = {name: self.root/name for name in ('home', 'tmp', 'config', 'work')}
        for path in dirs.values():
            path.mkdir(exist_ok=True)
        env = {'PATH': os.pathsep.join([str(Path(CLI).parent), str(Path(shutil.which('node')).parent), '/usr/bin', '/bin']),
               'HOME': str(dirs['home']), 'TMPDIR': str(dirs['tmp']), 'CLAUDE_CONFIG_DIR': str(dirs['config']),
               'ANTHROPIC_API_KEY': 'sk-ant-offline-fixture-not-a-key', 'DISABLE_AUTOUPDATER': '1',
               'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1', 'CLAUDE_CODE_MAX_RETRIES': str(retries),
               'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{self.proxy.server_port}'}
        command = [CLI, '-p', prompt, '--model', MODEL, '--output-format', 'stream-json', '--verbose', '--max-turns', '1',
                   '--no-session-persistence', '--setting-sources', '', '--strict-mcp-config', '--mcp-config',
                   '{"mcpServers":{}}', '--tools', '']
        return subprocess.Popen(command, env=env, cwd=dirs['work'], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)

    def messages(self, rows):
        return [r for r in rows if r.get('kind') == 'messages']


@unittest.skipUnless(VERSION == '2.1.284', 'needs node and the pinned 2.1.284 client first on PATH')
class RealClientCancellationTests(Harness):
    """The client ended mid-stream by SIGINT, SIGTERM or SIGKILL. Whatever the client does on each signal, the proxy
    must close the upstream before its stream ends, log the cut request as connection_closed with cost unknown, log any
    request the client sends afterwards as its own row, and count nothing twice."""

    def cancel(self, sig):
        self.start(slow_upstream())
        proc = self.client()
        self.assertTrue(self.upstream.first_event.wait(60), 'the client never reached the upstream')
        time.sleep(.3)  # a few pings forwarded: mid-stream
        proc.send_signal(sig)
        try:
            out, _ = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            self.fail(f'the client did not exit after {sig.name}')
        events = []
        for line in out.splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
        return proc.returncode, events, self.rows()

    def check(self, sig):
        code, events, rows = self.cancel(sig)
        messages = self.messages(rows)
        lost = [r for r in messages if r['status'] != 'ok']
        observed = {'signal': sig.name, 'exit': code, 'requests': len(self.upstream.received),
                    'streams': len(self.upstream.streams), 'rows': [r['status'] for r in messages],
                    'result': [e.get('subtype') for e in events if e.get('type') == 'result']}
        self.assertEqual(self.upstream.streams[0], False, observed)  # the proxy closed the upstream before the end
        first = messages[0]
        self.assertEqual((first['http_status'], first['status'], first['cost_usd']), (200, 'connection_closed', None))
        self.assertLess(first['wall_seconds'], 200 * .05)  # well before the stream's ten seconds
        # One row per request that reached the upstream (a retry is its own row), and nothing counted twice.
        self.assertEqual(len(messages), len(self.upstream.received), observed)
        totals = accounting(rows, {})
        self.assertEqual((totals['requests'], totals['unpriced_requests'], totals['cost_usd']),
                         (len(messages), len(lost), None), (observed, totals))
        self.assertEqual(transport_check(rows)['lost_requests'], len(lost))
        return observed

    # What the pinned 2.1.284 did on each signal (October 14), pinned here so a client change shows: none sent a
    # second request; SIGINT ended -p mode with exit 0 and a result of error_during_execution, so a harness must not
    # take its exit code for success; SIGTERM and SIGKILL ended it without a result.
    def test_sigint(self):
        observed = self.check(signal.SIGINT)
        self.assertEqual((observed['exit'], observed['result'], observed['requests']), (0, ['error_during_execution'], 1))

    def test_sigterm(self):
        observed = self.check(signal.SIGTERM)
        self.assertEqual((observed['exit'], observed['result'], observed['requests']), (128 + signal.SIGTERM, [], 1))

    def test_sigkill(self):
        observed = self.check(signal.SIGKILL)
        self.assertEqual((observed['exit'], observed['result'], observed['requests']), (-signal.SIGKILL, [], 1))


class ChunkedUploadTests(Harness):
    """Chunked request bodies (Jev's proxy uploads this way; Claude Code sends Content-Length): forwarded with their
    exact bytes and a Content-Length, or refused with 400 before anything reaches the upstream."""

    def setUp(self):
        self.start(slow_upstream(pings=0))

    def post(self, head, body_parts, read=True):
        sock = socket.create_connection(('127.0.0.1', self.proxy.server_port), timeout=30)
        try:
            sock.sendall(b'POST /v1/messages HTTP/1.1\r\nHost: proxy\r\nContent-Type: application/json\r\n'
                         b'x-api-key: sk-ant-offline\r\n' + head + b'\r\n')
            for part in body_parts:
                try:
                    sock.sendall(part)
                except (BrokenPipeError, ConnectionResetError):
                    break  # the proxy stopped reading: what it answered still counts
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass  # the proxy already answered and closed
            reply = b''
            while read:
                try:
                    data = sock.recv(65536)
                except ConnectionResetError:
                    break
                if not data:
                    break
                reply += data
            return int(reply.split(b' ', 2)[1]) if reply.startswith(b'HTTP/') else None, reply
        finally:
            sock.close()

    @staticmethod
    def chunked(body, size, extension=b'', trailer=b''):
        parts = [b'%x%s\r\n%s\r\n' % (len(body[i:i + size]), extension, body[i:i + size]) for i in range(0, len(body), size)]
        return parts + [b'0\r\n' + trailer + b'\r\n']

    def request(self, stream=False):
        return json.dumps({'model': MODEL, 'max_tokens': 8, 'stream': stream, 'messages': [{'role': 'user',
                           'content': 'chunked ' * 3000}]}).encode()

    def test_a_chunked_body_is_forwarded_whole_with_a_content_length(self):
        body = self.request()
        for size, extension, trailer in ((7, b'', b''), (4096, b';name=value', b'x-trailer: dropped\r\n'),
                                         (len(body), b'', b'')):
            with self.subTest(size=size, extension=extension):
                status, _ = self.post(b'Transfer-Encoding: chunked\r\n', self.chunked(body, size, extension, trailer))
                self.assertEqual(status, 200)
                seen = self.upstream.received[-1]
                self.assertEqual((seen['body'], seen['length']), (body, str(len(body))))
                row = self.rows()[-1]
                self.assertEqual((row['status'], row['http_status']), ('ok', 200))
                self.assertAlmostEqual(row['cost_usd'], (10 * 3 + 4 * 15 + 14750 * .3) / 1e6)  # fixtures.USAGE

    def test_a_chunked_body_over_the_limit_is_refused_before_it_is_forwarded(self):
        # Many 1 MiB chunks: the proxy stops once the total would pass 32 MiB, not after reading it all.
        chunk = b'x' * (1024 * 1024)
        parts = [b'%x\r\n%s\r\n' % (len(chunk), chunk)] * (MAX_BODY // len(chunk) + 1) + [b'0\r\n\r\n']
        status, _ = self.post(b'Transfer-Encoding: chunked\r\n', parts)
        self.assertEqual(status, 400)
        self.assertEqual(self.upstream.received, [])
        self.assertEqual(self.rows(), [])  # refused before any request exists: nothing billed, nothing logged

    def test_ambiguous_or_malformed_chunked_bodies_are_refused(self):
        body = self.request()
        cases = {
            'chunked and a length': (b'Transfer-Encoding: chunked\r\nContent-Length: %d\r\n' % len(body), self.chunked(body, 512)),
            'two encodings': (b'Transfer-Encoding: gzip, chunked\r\n', self.chunked(body, 512)),
            'encoding sent twice': (b'Transfer-Encoding: chunked\r\nTransfer-Encoding: chunked\r\n', self.chunked(body, 512)),
            'size not hex': (b'Transfer-Encoding: chunked\r\n', [b'zz\r\n' + body[:10] + b'\r\n0\r\n\r\n']),
            'short chunk': (b'Transfer-Encoding: chunked\r\n', [b'%x\r\n' % 100 + body[:50] + b'\r\n0\r\n\r\n']),
            'no last chunk': (b'Transfer-Encoding: chunked\r\n', self.chunked(body, 512)[:-1])}
        for name, (head, parts) in cases.items():
            with self.subTest(name):
                status, _ = self.post(head, parts)
                self.assertEqual(status, 400, name)
        self.assertEqual(self.upstream.received, [])
        self.assertEqual(self.rows(), [])


class IdleStreamTests(Harness):
    """An upstream that sends message_start and then nothing for longer than the proxy's inactivity timeout
    (ProxyServer.connection's timeout, 120 s; lowered to 1 s here): the proxy gives up on the upstream, and the row is
    a transport_error of unknown cost."""
    proxy_timeout = 1.0

    def test_the_proxy_gives_up_and_leaves_the_cost_unknown(self):
        self.start(slow_upstream(pings=1, stall=3))
        request = json.dumps({'model': MODEL, 'max_tokens': 8, 'stream': True, 'messages': []}).encode()
        conn = http.client.HTTPConnection('127.0.0.1', self.proxy.server_port, timeout=30)
        try:
            conn.request('POST', '/v1/messages', body=request, headers={'Content-Type': 'application/json'})
            response = conn.getresponse()
            data = response.read()  # the proxy ends the reply after what arrived (close-delimited)
        finally:
            conn.close()
        self.assertEqual(response.status, 200)  # the status line had already gone to the client
        self.assertIn(b'message_start', data)
        self.assertNotIn(b'message_stop', data)
        row = self.rows()[-1]
        self.assertEqual((row['http_status'], row['status'], row['error_type']), (200, 'transport_error', 'TimeoutError'))
        self.assertIsNone(row['cost_usd'])
        self.assertLess(row['wall_seconds'], 2.5)  # gave up after about a second, before the upstream's 3 s stall ended

    @unittest.skipUnless(VERSION == '2.1.284', 'needs node and the pinned 2.1.284 client first on PATH')
    def test_what_the_pinned_client_does_with_a_stream_cut_for_inactivity(self):
        self.start(slow_upstream(pings=1, stall=3))
        proc = self.client(retries=0)
        out, _ = proc.communicate(timeout=120)
        rows = self.messages(self.rows())
        events = [json.loads(line) for line in out.splitlines() if line.startswith('{')]
        observed = {'exit': proc.returncode, 'rows': [(r['status'], r['stream'], r.get('error_type')) for r in rows],
                    'result': [e.get('subtype') for e in events if e.get('type') == 'result']}
        cut = rows[0]
        self.assertEqual((cut['status'], cut['error_type'], cut['cost_usd']), ('transport_error', 'TimeoutError', None), observed)
        self.assertEqual(len(rows), len(self.upstream.received), observed)  # every request is its own row
        for later in rows[1:]:
            self.assertNotEqual(later['started_unix'], cut['started_unix'])
        check = transport_check(self.rows())
        self.assertEqual(check['lost_requests'], 1, observed)
        self.assertEqual(check['sent_after_loss'], len(rows) - 1, observed)
        # What the pinned 2.1.284 did (October 14), with CLAUDE_CODE_MAX_RETRIES=0: it sent the request again without
        # streaming, the retry bench_report.transport_check found in bench-20261011-234003, and the session succeeded
        # (exit 0, result success). The retry is its own row, priced; the cut request stays unknown.
        self.assertEqual(observed['rows'], [('transport_error', True, 'TimeoutError'), ('ok', False, None)])
        self.assertEqual((observed['exit'], observed['result']), (0, ['success']))
        self.assertIsNotNone(rows[1]['cost_usd'])

if __name__ == '__main__':
    unittest.main()
