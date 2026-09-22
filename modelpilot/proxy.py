"""Loopback Messages API pass-through proxy. Policy decisions are logs only."""
import argparse
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit
from .cache_probe import cost, tls_context

HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
       'te', 'trailer', 'transfer-encoding', 'upgrade', 'host', 'content-length'}
MAX_BODY = 32 * 1024 * 1024
MAX_EVENT = 1024 * 1024


CHUNK_SIZE = re.compile(rb'[0-9A-Fa-f]{1,8}')


def read_chunked(stream, limit=MAX_BODY):
    """Strict chunked request body; None on any malformed framing or when over limit."""
    body = b''
    while True:
        line = stream.readline(1026)
        if not line.endswith(b'\r\n'):
            return None
        size = line[:-2].split(b';', 1)[0].strip()
        if not CHUNK_SIZE.fullmatch(size):
            return None
        size = int(size, 16)
        if size == 0:
            break
        if len(body) + size > limit:
            return None
        data = stream.read(size + 2)
        if len(data) != size + 2 or not data.endswith(b'\r\n'):
            return None
        body += data[:-2]
    for _ in range(64):  # trailers, discarded
        line = stream.readline(8194)
        if line == b'\r\n':
            return body
        if not line.endswith(b'\r\n'):
            return None
    return None


def clean_headers(headers):
    blocked = HOP | {x.strip().lower() for x in headers.get('Connection', '').split(',')}
    return {k: v for k, v in headers.items() if k.lower() not in blocked}


class UsageObserver:
    """Bounded, best-effort observation; never re-serialize the forwarded stream."""
    def __init__(self, stream):
        self.stream = stream
        self.buffer = b''
        self.usage = {}
        self.complete = False
        self.invalid = False
        self.model = None

    def event(self, obj):
        kind = obj.get('type')
        if kind == 'message_start':
            self.usage.update(obj.get('message', {}).get('usage', {}))
            self.model = obj.get('message', {}).get('model')
        elif kind == 'message_delta':
            # Stream deltas contain cumulative counters, not increments.
            self.usage.update(obj.get('usage', {}))
        elif kind == 'message_stop':
            self.complete = True
        elif kind == 'error':
            self.invalid = True
        # Unknown events, tool blocks and thinking signatures pass through untouched.

    def feed(self, data):
        if self.invalid:
            return
        self.buffer += data
        if self.stream:
            while b'\n\n' in self.buffer or b'\r\n\r\n' in self.buffer:
                candidates = [(self.buffer.find(s), s) for s in (b'\n\n', b'\r\n\r\n') if s in self.buffer]
                end, sep = min(candidates)
                frame, self.buffer = self.buffer[:end], self.buffer[end + len(sep):]
                try:
                    lines = [s[5:].lstrip() for s in frame.splitlines() if s.startswith(b'data:')]
                    if lines:
                        self.event(json.loads(b'\n'.join(lines)))
                except (ValueError, TypeError, AttributeError):
                    self.invalid = True
        if len(self.buffer) > MAX_EVENT:
            self.invalid = True
            self.buffer = b''

    def finish(self):
        if not self.stream and not self.invalid:
            try:
                obj = json.loads(self.buffer)
                self.usage = obj.get('usage', {})
                self.model = obj.get('model')
                self.complete = isinstance(self.usage, dict) and bool(self.usage)
            except (ValueError, TypeError, AttributeError):
                self.invalid = True
        if self.stream and self.buffer.strip():
            self.invalid = True


def measured_cost(observer, request, rates):
    if not observer.complete or observer.invalid:
        return None
    if observer.model != request.get('model'):
        return None  # aliases/fallback need explicit rate mapping, not guessing
    usage = observer.usage
    if usage.get('service_tier', 'standard') != 'standard' or usage.get('inference_geo', 'global') != 'global':
        return None
    if request.get('speed') == 'fast':
        return None
    # Missing TTL breakdown is ambiguous in real traffic; M0's single-TTL fallback is not used.
    if usage.get('cache_creation_input_tokens', 0) and 'cache_creation' not in usage:
        return None
    try:
        return cost(usage, rates.get(observer.model), '5m')
    except (KeyError, TypeError, ValueError):
        return None


def forecast(request, usage, rates):
    """Cost-only sensitivity model. Never authorizes a switch or estimates quality."""
    source = request.get('model')
    base = rates.get(source)
    required = ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens', 'output_tokens')
    if base is None or any(k not in usage for k in required):
        return {'action': 'hold', 'reason': 'missing_usage_or_rates', 'applied': False}
    prefix = usage['cache_read_input_tokens'] + usage['cache_creation_input_tokens']
    tail, output = usage['input_tokens'], usage['output_tokens']
    scenarios = []
    for target, rate in rates.items():
        if target == source:
            continue
        for steps in (1, 5, 20):
            stay = steps * (prefix * base['read'] + tail * base['input'] + output * base['output']) / 1e6
            # Assume target cold; retained target caches and future context growth are UNKNOWN.
            switch = (prefix * rate['write_5m'] + (steps-1) * prefix * rate['read'] +
                      steps * (tail * rate['input'] + output * rate['output'])) / 1e6
            scenarios.append({'target': target, 'steps': steps, 'stay_usd': stay,
                              'switch_usd': switch, 'savings_usd': stay-switch,
                              'would_switch_on_cost_only': switch < stay})
    return {'action': 'hold', 'applied': False, 'reason': 'dry_run_quality_and_horizon_unknown',
            'assumptions': '5m TTL; source warm; target cold; fixed context and token counts; identical quality unverified',
            'scenarios': scenarios}


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, upstream, log_path, rates):
        parsed = urlsplit(upstream)
        if not ((parsed.scheme == 'https' and parsed.netloc == 'api.anthropic.com') or
                (parsed.scheme == 'http' and parsed.hostname == '127.0.0.1')):
            raise ValueError('Upstream must be direct Anthropic HTTPS or loopback HTTP for tests')
        if parsed.path not in ('', '/') or parsed.query or parsed.fragment or parsed.username:
            raise ValueError('Upstream must be an origin')
        self.upstream = parsed
        self.rates = rates
        self.log_lock = threading.Lock()
        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path = log_path
        self.log = os.fdopen(os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), 'a')
        try:
            super().__init__(address, ProxyHandler)
        except Exception:
            self.log.close()
            raise

    def record(self, row):
        with self.log_lock:
            self.log.write(json.dumps(row) + '\n')
            self.log.flush()

    def server_close(self):
        super().server_close()
        self.log.close()


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'  # close-delimited replies preserve incremental SSE
    def log_message(self, *_):
        pass  # default access logs can expose request URLs

    def setup(self):
        super().setup()
        self.connection.settimeout(120)

    def upstream_connection(self):
        origin = self.server.upstream
        if origin.scheme == 'https':
            return http.client.HTTPSConnection(origin.hostname, context=tls_context(), timeout=120)
        return http.client.HTTPConnection(origin.hostname, origin.port, timeout=120)

    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok","mode":"dry-run"}')
            return
        path = urlsplit(self.path)
        # Only the model catalog (Jev's discovery request) passes through; never billed.
        if path.path != '/v1/models' or path.scheme or path.netloc:
            self.send_error(404)
            return
        row = {'started_unix': time.time(), 'kind': 'models', 'mode': 'dry-run', 'applied': False,
               'http_status': None, 'status': 'transport_error', 'cost_usd': None}
        start = time.monotonic()
        sent_headers = False
        conn = self.upstream_connection()
        headers = {k: v for k, v in clean_headers(self.headers).items() if k.lower() != 'accept-encoding'}
        headers['Accept-Encoding'] = 'identity'
        try:
            conn.request('GET', self.path, headers=headers)
            response = conn.getresponse()
            data = response.read(MAX_BODY + 1)
            if len(data) > MAX_BODY:
                raise ValueError('catalog too large')
            row['http_status'] = response.status
            self.send_response_only(response.status, response.reason)
            for k, v in clean_headers(response.headers).items():
                self.send_header(k, v)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Connection', 'close')
            self.end_headers()
            sent_headers = True
            self.wfile.write(data)
            row['status'] = 'ok' if 200 <= response.status < 300 else 'upstream_error'
        except (BrokenPipeError, ConnectionResetError):
            row['status'] = 'connection_closed'
        except Exception as e:
            row['error_type'] = type(e).__name__
            if not sent_headers:
                self.send_error(502, 'Upstream connection failed')
        finally:
            conn.close()
            self.close_connection = True
            row['wall_seconds'] = time.monotonic()-start
            self.server.record(row)

    def do_POST(self):
        path = urlsplit(self.path)
        if path.path not in ('/v1/messages', '/v1/messages/count_tokens') or path.scheme or path.netloc:
            self.send_error(404)
            return
        encodings = self.headers.get_all('Transfer-Encoding', [])
        lengths = self.headers.get_all('Content-Length', [])
        if encodings:
            # Jev's proxy uploads chunked. Anything ambiguous is refused, never forwarded.
            if lengths or len(encodings) != 1 or encodings[0].strip().lower() != 'chunked':
                self.send_error(400, 'Ambiguous or unsupported transfer encoding')
                return
            raw = read_chunked(self.rfile)
            if raw is None:
                self.send_error(400, 'Malformed or oversized chunked body')
                return
        else:
            if len(lengths) != 1:
                self.send_error(411, 'One Content-Length or a chunked body required')
                return
            try:
                size = int(lengths[0])
                if not 0 <= size <= MAX_BODY:
                    raise ValueError()
            except ValueError:
                self.send_error(413)
                return
            raw = self.rfile.read(size)
            if len(raw) != size:
                self.send_error(400)
                return
        try:
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            self.send_error(400)
            return
        headers = clean_headers(self.headers)
        headers['Content-Length'] = str(len(raw))
        # Negotiate identity so usage inspection does not depend on client compression support.
        headers = {k: v for k, v in headers.items() if k.lower() != 'accept-encoding'}
        headers['Accept-Encoding'] = 'identity'
        conn = self.upstream_connection()
        start = time.monotonic()
        tools = request.get('tools')
        row = {'started_unix': time.time(), 'kind': 'messages', 'mode': 'dry-run', 'applied': False,
               'tool_count': len(tools) if isinstance(tools, list) else 0,
               'request_sha256': hashlib.sha256(raw).hexdigest(),
               'model': request.get('model') if request.get('model') in self.server.rates else 'unknown',
               'stream': bool(request.get('stream')), 'http_status': None,
               'status': 'transport_error', 'cost_usd': None}
        sent_headers = False
        observer = UsageObserver(bool(request.get('stream')))
        try:
            conn.request('POST', self.path, body=raw, headers=headers)
            response = conn.getresponse()
            row['http_status'] = response.status
            self.send_response_only(response.status, response.reason)
            for k, v in clean_headers(response.headers).items():
                self.send_header(k, v)
            self.send_header('Connection', 'close')
            self.end_headers()
            sent_headers = True
            row['response_encoding'] = response.headers.get('Content-Encoding', 'identity').lower()
            if row['response_encoding'] != 'identity':
                observer.invalid = True  # bytes still forwarded; don't parse compressed payloads
            first = True
            while True:
                data = response.read1(16384)
                if not data:
                    break
                if first:
                    row['first_byte_seconds'] = time.monotonic()-start
                    first = False
                self.wfile.write(data)
                self.wfile.flush()
                observer.feed(data)
            observer.finish()
            row['status'] = 'ok' if 200 <= response.status < 300 else 'upstream_error'
            row['usage_complete'] = observer.complete and not observer.invalid
            # Retain only numeric counters, never free-form upstream error/content strings.
            row['usage'] = {k: v for k, v in observer.usage.items()
                            if k in ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')
                            and isinstance(v, int) and not isinstance(v, bool) and v >= 0}
            if row['status'] == 'ok':
                row['cost_usd'] = measured_cost(observer, request, self.server.rates)
            row['decision'] = forecast(request, row['usage'], self.server.rates) if row['cost_usd'] is not None else {
                'action': 'hold', 'applied': False, 'reason': 'incomplete_or_unpriced_response'}
        except (BrokenPipeError, ConnectionResetError):
            row['status'] = 'connection_closed'
        except Exception as e:
            row['error_type'] = type(e).__name__
            if not sent_headers:
                self.send_error(502, 'Upstream connection failed')
        finally:
            conn.close()
            self.close_connection = True
            row['wall_seconds'] = time.monotonic()-start
            self.server.record(row)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--config', type=Path, default=Path('configs/m0.json'))
    parser.add_argument('--log', type=Path, default=Path('runs/proxy/observations.jsonl'))
    parser.add_argument('--upstream', default='https://api.anthropic.com', help='Anthropic origin, or loopback HTTP for local tests')
    args = parser.parse_args()
    rates = json.loads(args.config.read_text())['rates']
    with ProxyServer(('127.0.0.1', args.port), args.upstream, args.log, rates) as server:
        print(f'Dry-run proxy on http://127.0.0.1:{server.server_port}; forwarded API calls remain billable', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
