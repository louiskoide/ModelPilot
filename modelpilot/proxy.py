"""Loopback Messages API pass-through proxy. Policy decisions are logs only.

The one exception is a fixture_dispatch.ProxyPolicy, which applies ladder escalations and is
only accepted when the upstream is the owned in-process fixture server (tests, never live).
"""
import argparse
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid
from .cache_probe import priced_usage, tls_context
from .governor import Governor

HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
       'te', 'trailer', 'transfer-encoding', 'upgrade', 'host', 'content-length'}
MAX_BODY = 32 * 1024 * 1024
MAX_EVENT = 1024 * 1024
PROVIDER_ID = re.compile(r'req_[A-Za-z0-9]{1,128}')


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


USAGE_COUNTERS = ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')
TTL_COUNTERS = ('ephemeral_5m_input_tokens', 'ephemeral_1h_input_tokens')


def counter(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def logged_usage(usage):
    """Numeric counters only, never free-form upstream strings. The cache-write TTL split is
    kept when complete, so a logged row can be re-priced (e.g. as a cold-equivalent cost)."""
    row = {k: v for k, v in usage.items() if k in USAGE_COUNTERS and counter(v)}
    split = usage.get('cache_creation')
    if isinstance(split, dict) and all(counter(split.get(k)) for k in TTL_COUNTERS):
        row['cache_creation'] = {k: split[k] for k in TTL_COUNTERS}
    return row


def request_effort(request):
    """Requested effort, logged because cache entries are separate per model and effort."""
    config = request.get('output_config')
    effort = config.get('effort') if isinstance(config, dict) else None
    return effort if isinstance(effort, str) and len(effort) <= 16 else None


def measured_cost(observer, request, rates):
    if not observer.complete or observer.invalid:
        return None
    return priced_usage(request, observer.model, observer.usage, rates)


def reservation_estimate(raw, request, rates):
    """Pessimistic pre-send reservation: uncached input at the dearest write rate plus full max_tokens.

    Assumes about 3 request bytes per token. An estimate, not a bound (documents and
    server-added tool prompts can exceed it); settlement always uses measured usage.
    """
    rate = rates.get(request.get('model'))
    candidates = [rate] if rate else list(rates.values())
    input_rate = max(max(r['input'], r['write_5m'], r['write_1h']) for r in candidates)
    output_rate = max(r['output'] for r in candidates)
    max_tokens = request.get('max_tokens')
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 0:
        max_tokens = 0
    return (len(raw) / 3 * input_rate + max_tokens * output_rate) / 1e6


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
    # server_close must join request handlers before closing their shared accounting log.
    # Client and upstream socket operations already have 120-second timeouts.
    daemon_threads = False
    def __init__(self, address, upstream, log_path, rates, governor=None, policy=None):
        parsed = urlsplit(upstream)
        if not ((parsed.scheme == 'https' and parsed.netloc == 'api.anthropic.com') or
                (parsed.scheme == 'http' and parsed.hostname == '127.0.0.1')):
            raise ValueError('Upstream must be direct Anthropic HTTPS or loopback HTTP for tests')
        if parsed.path not in ('', '/') or parsed.query or parsed.fragment or parsed.username:
            raise ValueError('Upstream must be an origin')
        self.upstream = parsed
        self.rates = rates
        # Dry-run governor: {'db', 'session', 'limit_usd', 'task'}; opened per request (threads).
        self.governor = dict(governor) if governor else None
        if policy is not None:
            if not self.governor or self.governor.get('task') is None:
                raise ValueError('Policy dispatch needs a governor task')
            policy.check_upstream(parsed)
        self.policy = policy
        if self.governor:
            gov = self.open_governor()
            try:
                if self.governor.get('task') is not None:
                    gov.state.get(self.governor['task'])  # raises for an unknown task
            finally:
                gov.close()
        self.log_lock = threading.Lock()
        # Accepted connections not yet finished, so a proxy that outlives one client session
        # can tell when every request of that session has been logged.
        self.in_flight = 0
        self.idle = threading.Condition()
        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path = log_path
        self.log = os.fdopen(os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), 'a')
        try:
            super().__init__(address, ProxyHandler)
        except Exception:
            self.log.close()
            raise

    def open_governor(self):
        g = self.governor
        return Governor(g['db'], g['session'], g['limit_usd'])

    def admit(self, row, raw, request):
        """Reserve under a fresh shared ID. Dry-run: the decision never blocks forwarding."""
        request_id = row['governor_request_id'] = 'mp-' + uuid.uuid4().hex
        gov = self.open_governor()
        try:
            task = self.governor.get('task')
            # The client works from the revision it acknowledged; an undelivered correction is stale work.
            revision = None if task is None else (gov.state.get(task)['ack_revision'] or -1)
            decision = gov.admit(request_id, reservation_estimate(raw, request, self.rates), task, revision,
                                 ttl=600, enforce=False)
        finally:
            gov.close()
        row['governor'] = {k: decision[k] for k in ('admitted', 'reason', 'estimate_usd', 'enforced')}
        row['governor_status'] = 'reserved'

    def settle(self, row):
        gov = self.open_governor()
        try:
            gov.settle(row['governor_request_id'], row['cost_usd'])
        finally:
            gov.close()
        row['governor_status'] = 'settled'

    def plan_policy(self, request):
        gov = self.open_governor()
        try:
            return self.policy.plan(gov, self.rates, self.governor['task'], request)
        finally:
            gov.close()

    def finish_policy(self, ticket, row, observer):
        gov = self.open_governor()
        try:
            return self.policy.finish(gov, self.rates, ticket, row['cost_usd'], observer.model, observer.usage)
        finally:
            gov.close()

    def process_request(self, request, client_address):
        with self.idle:
            self.in_flight += 1
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.finished_request()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.finished_request()

    def finished_request(self):
        with self.idle:
            self.in_flight -= 1
            self.idle.notify_all()

    def handle_error(self, request, client_address):
        # A client that disconnects before sending a request (e.g. killed at a session timeout)
        # sent nothing to forward or log; anything else still gets the default report.
        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)

    def wait_idle(self, timeout=None):
        """True once no accepted request is still being handled (its row is then logged)."""
        with self.idle:
            return self.idle.wait_for(lambda: self.in_flight == 0, timeout)

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
        governed = self.server.governor is not None and path.path == '/v1/messages'  # count_tokens is free
        ticket, policy_row, client_sha = None, None, hashlib.sha256(raw).hexdigest()
        if governed and self.server.policy is not None:
            try:
                outcome = self.server.plan_policy(request)
            except Exception as e:
                outcome = {'status': 'deferred', 'reason': 'policy_error:' + type(e).__name__}
            if outcome['status'] == 'admitted':
                ticket, request = outcome, outcome['request']
                raw = json.dumps(request).encode()
                policy_row = {'kind': ticket['kind'], 'status': 'reserved'}
            else:
                policy_row = {k: outcome[k] for k in ('status', 'reason') if k in outcome}
        headers = clean_headers(self.headers)
        headers['Content-Length'] = str(len(raw))
        # Negotiate identity so usage inspection does not depend on client compression support.
        headers = {k: v for k, v in headers.items() if k.lower() != 'accept-encoding'}
        headers['Accept-Encoding'] = 'identity'
        conn = self.upstream_connection()
        start = time.monotonic()
        tools = request.get('tools')
        row = {'started_unix': time.time(), 'kind': 'messages', 'mode': 'dry-run' if ticket is None else 'fixture-policy',
               'applied': ticket is not None,
               'tool_count': len(tools) if isinstance(tools, list) else 0,
               'request_sha256': hashlib.sha256(raw).hexdigest(),
               'model': request.get('model') if request.get('model') in self.server.rates else 'unknown',
               'effort': request_effort(request), 'stream': bool(request.get('stream')), 'http_status': None,
               'status': 'transport_error', 'cost_usd': None}
        if policy_row is not None:
            row['policy'] = policy_row
        if ticket is not None:
            row.update(client_request_sha256=client_sha, governor_request_id=ticket['request_id'],
                       governor_status='reserved')
        elif governed:
            try:
                self.server.admit(row, raw, request)
            except Exception as e:
                # Never block traffic; reconcile_log() later records this row's spend.
                row.update(governor_status='untracked', governor_error=type(e).__name__)
        sent_headers = False
        observer = UsageObserver(bool(request.get('stream')))
        try:
            conn.request('POST', self.path, body=raw, headers=headers)
            response = conn.getresponse()
            row['http_status'] = response.status
            provider_id = response.headers.get('request-id', '')
            if PROVIDER_ID.fullmatch(provider_id):
                row['provider_request_id'] = provider_id
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
            row['usage'] = logged_usage(observer.usage)
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
            if ticket is not None:
                try:
                    result = self.server.finish_policy(ticket, row, observer)
                    row['governor_status'] = 'settled'
                    row['policy'].update(status=result['status'])
                except Exception as e:
                    row.update(governor_status='settle_failed', governor_error=type(e).__name__)
            elif governed and row.get('governor_status') == 'reserved':
                try:
                    self.server.settle(row)
                except Exception as e:
                    row.update(governor_status='settle_failed', governor_error=type(e).__name__)
            self.server.record(row)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--config', type=Path, default=Path('configs/m0.json'))
    parser.add_argument('--log', type=Path, default=Path('runs/proxy/observations.jsonl'))
    parser.add_argument('--upstream', default='https://api.anthropic.com', help='Anthropic origin, or loopback HTTP for local tests')
    parser.add_argument('--governor-db', type=Path, help='Record dry-run reservations/settlements in this ledger database')
    parser.add_argument('--governor-session')
    parser.add_argument('--governor-limit-usd', type=float)
    parser.add_argument('--governor-task', help='Ledger task whose acknowledged revision fences requests')
    args = parser.parse_args()
    governor = None
    if args.governor_db:
        if not args.governor_session or args.governor_limit_usd is None:
            parser.error('--governor-db needs --governor-session and --governor-limit-usd')
        governor = {'db': args.governor_db, 'session': args.governor_session,
                    'limit_usd': args.governor_limit_usd, 'task': args.governor_task}
    rates = json.loads(args.config.read_text())['rates']
    with ProxyServer(('127.0.0.1', args.port), args.upstream, args.log, rates, governor) as server:
        print(f'Dry-run proxy on http://127.0.0.1:{server.server_port}; forwarded API calls remain billable', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
