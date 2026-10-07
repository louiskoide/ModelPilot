"""Loopback Messages API pass-through proxy. Policy decisions are logs only.

Two exceptions. A fixture_dispatch.ProxyPolicy applies ladder escalations and is only accepted
when the upstream is the owned in-process fixture server (tests, never live). An
active_policy.ActivePolicy, the ModelPilot benchmark arm only, applies escalations live and
enforces admission: a refused request gets an API-style error from the proxy and is never sent.
That policy may also have the proxy send requests of its own (side_call: consults and handoff
notes), each admitted, settled and logged as kind side_call.
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
        self.problem = None  # why usage is missing or unusable, for the log
        self.input_tokens = None  # a token-counting reply's count

    def fail(self, problem):
        self.invalid = True
        self.problem = self.problem or problem

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
            self.fail('error_event')
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
                    self.fail('unparseable_event')
        if len(self.buffer) > MAX_EVENT:
            self.fail('oversized_response')
            self.buffer = b''

    def finish(self):
        if not self.stream and not self.invalid:
            try:
                obj = json.loads(self.buffer)
                self.usage = obj.get('usage', {})
                self.model = obj.get('model')
                self.complete = isinstance(self.usage, dict) and bool(self.usage)
                count = obj.get('input_tokens')
                self.input_tokens = count if isinstance(count, int) and not isinstance(count, bool) else None
            except (ValueError, TypeError, AttributeError):
                self.fail('unparseable_body')
            else:
                if not self.complete:
                    self.problem = 'no_usage_field'
        if self.stream and self.buffer.strip():
            self.fail('trailing_bytes')
        if self.stream and not self.complete and not self.problem:
            self.problem = 'stream_ended_early'


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


def effective_effort(request):
    """The effort the model runs at: the latest effort-only system message's (per-message effort), else the top-level one."""
    for m in reversed(request.get('messages') or []):
        if (isinstance(m, dict) and m.get('role') == 'system' and isinstance(m.get('output_config'), dict)
                and 'effort' in m['output_config']):
            return m['output_config']['effort']
    return request_effort(request)


def system_text(request):
    """The request's system prompt as one string (a string, or text blocks joined), for marker checks."""
    system = request.get('system')
    if isinstance(system, str):
        return system
    return '\n'.join(b['text'] for b in system if isinstance(b, dict) and isinstance(b.get('text'), str)) \
        if isinstance(system, list) else ''


def cache_ttls(request):
    """The cache lifetimes a request's breakpoints ask for, sorted ('5m' where a breakpoint names none, the API's
    default). Claude Code marks every breakpoint alike: 1h on a subscription or with CLAUDE_CODE_PROMPT_CACHE_TTL=1h,
    else 5m; its compaction request keeps the default whatever the setting (2.1.284, checked at $0)."""
    found = set()
    def walk(node):
        if isinstance(node, dict):
            control = node.get('cache_control')
            if isinstance(control, dict):
                ttl = control.get('ttl', '5m')
                found.add(ttl if ttl in ('5m', '1h') else 'other')
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk({k: request.get(k) for k in ('system', 'tools', 'messages')})
    return sorted(found)


# The instruction Claude Code 2.1.284 appends to the last user message of its compaction request (/compact, or auto
# compaction), which asks the model for a summary that replaces the conversation (checked at $0 against the fixture).
COMPACTION_MARKER = 'CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.'


def compaction_request(request):
    """Whether this is the client's compaction request: the compaction instruction is its last user message (after an
    assistant reply) or that message's last text block (appended to a tool result)."""
    last = next((m for m in reversed(request.get('messages') or []) if isinstance(m, dict) and m.get('role') == 'user'),
                None)
    content = last.get('content') if last else None
    blocks = content if isinstance(content, list) else [{'text': content}]
    return any(isinstance(b, dict) and isinstance(b.get('text'), str) and b['text'].startswith(COMPACTION_MARKER)
               for b in blocks)


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


def filter_catalog(data, allowed):
    """Keep only the allowed models, in upstream order; (None, None) when the body is not a catalog."""
    try:
        body = json.loads(data)
        if not isinstance(body, dict) or not isinstance(body.get('data'), list):
            raise TypeError
        models = body['data']
        kept = [m for m in models if isinstance(m, dict) and m.get('id') in allowed]
    except (ValueError, TypeError):
        return None, None
    body.update(data=kept, has_more=False)
    for field, pick in (('first_id', 0), ('last_id', -1)):
        if field in body:
            body[field] = kept[pick]['id'] if kept else None
    ids = {m['id'] for m in kept}
    return json.dumps(body).encode(), {'kept': sorted(ids), 'dropped': len(models) - len(kept),
                                       'missing': sorted(set(allowed) - ids)}


class ProxyServer(ThreadingHTTPServer):
    # server_close must join request handlers before closing their shared accounting log.
    # Client and upstream socket operations already have 120-second timeouts.
    daemon_threads = False
    def __init__(self, address, upstream, log_path, rates, governor=None, policy=None, catalog=None, system_marker=None):
        """catalog: model ids a model-constrained arm may discover; the model catalog is filtered to them.
        system_marker: text an arm adds to the system prompt; each Messages row records whether it was there."""
        if system_marker is not None and (not isinstance(system_marker, str) or not system_marker.strip()):
            raise ValueError('system_marker must be non-empty text')
        self.system_marker = system_marker
        if catalog is not None and (isinstance(catalog, str) or not catalog or
                                    not all(isinstance(m, str) and m for m in catalog)):
            raise ValueError('catalog must be a non-empty collection of model ids')
        self.catalog = None if catalog is None else tuple(catalog)
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

    @property
    def active(self):
        return bool(self.governor) and getattr(self.policy, 'active', False)

    def admit(self, row, raw, request):
        """Reserve under a fresh shared ID. Dry-run: the decision never blocks forwarding.
        Active: enforced on measured spend (the other arms' stop rule); a refusal reserves nothing."""
        request_id = row['governor_request_id'] = 'mp-' + uuid.uuid4().hex
        gov = self.open_governor()
        try:
            task = self.governor.get('task')
            # The client works from the revision it acknowledged; an undelivered correction is stale work.
            revision = None if task is None else (gov.state.get(task)['ack_revision'] or -1)
            decision = gov.admit(request_id, reservation_estimate(raw, request, self.rates), task, revision,
                                 ttl=600, enforce=self.active, gate='spent' if self.active else 'reservation')
        finally:
            gov.close()
        row['governor'] = {k: decision[k] for k in ('admitted', 'reason', 'estimate_usd', 'enforced')}
        row['governor_status'] = 'reserved' if decision['reserved'] else 'refused'

    def settle(self, row):
        gov = self.open_governor()
        try:
            gov.settle(row['governor_request_id'], row['cost_usd'])
            row['governor_status'] = 'settled'
            if row['status'] == 'ok' and row['tool_count'] and row.get('kind') == 'messages':
                # Wire evidence for an outstanding rebuild plan: the client's main loop ran at this setting.
                # Side requests (titles, summaries) carry no tools and may use another model.
                try:
                    for kind in ('model', 'effort'):
                        if row[kind] not in (None, 'unknown'):
                            gov.observe_setting(kind, row[kind])
                except Exception as e:
                    row['observe_error'] = type(e).__name__  # settlement stands; the plan waits for an explicit ack
        finally:
            gov.close()

    def plan_policy(self, request, side=None):
        gov = self.open_governor()
        try:
            if side is None:
                return self.policy.plan(gov, self.rates, self.governor['task'], request)
            return self.policy.plan(gov, self.rates, self.governor['task'], request, side=side)
        finally:
            gov.close()

    def connection(self, timeout=120):
        origin = self.upstream
        if origin.scheme == 'https':
            return http.client.HTTPSConnection(origin.hostname, context=tls_context(), timeout=timeout)
        return http.client.HTTPConnection(origin.hostname, origin.port, timeout=timeout)

    def side_call(self, headers, purpose, body, timeout):
        """One request the active policy sends itself (a consult or a handoff note), never streamed, with the client's
        own headers (its credentials and betas). Admitted on measured spend like the client's requests, settled with
        its measured cost and logged as kind side_call, so the client-proxy token check sees only the client's rows.
        Returns {'status', 'http_status', 'message' (the parsed reply, or None), 'cost_usd', 'request_id'}."""
        raw = json.dumps(body).encode()
        tools = body.get('tools')
        row = {'started_unix': time.time(), 'kind': 'side_call', 'purpose': purpose, 'path': '/v1/messages',
               'mode': 'active', 'applied': True, 'tool_count': len(tools) if isinstance(tools, list) else 0,
               'request_sha256': hashlib.sha256(raw).hexdigest(),
               'model': body.get('model') if body.get('model') in self.rates else 'unknown', 'effort': request_effort(body),
               'stream': False, 'http_status': None, 'status': 'transport_error', 'cost_usd': None}
        out = {'status': 'refused', 'http_status': None, 'message': None, 'cost_usd': None}
        try:
            self.admit(row, raw, body)
        except Exception as e:
            row.update(governor_status='untracked', governor_error=type(e).__name__)
        out['request_id'] = row.get('governor_request_id')
        if row.get('governor_status') != 'reserved':  # never sent: nothing is billed
            row.update(status='refused', refusal=(row.get('governor') or {}).get('reason') or 'governor_error',
                       cost_usd=0.0, wall_seconds=0.0)
            self.record(row)
            return out
        sent = {k: v for k, v in headers.items() if k.lower() not in ('accept-encoding', 'content-length', 'content-type')}
        sent.update({'Content-Length': str(len(raw)), 'Accept-Encoding': 'identity', 'Content-Type': 'application/json'})
        start = time.monotonic()
        conn = self.connection(timeout)
        try:
            conn.request('POST', '/v1/messages', body=raw, headers=sent)
            response = conn.getresponse()
            row['http_status'] = out['http_status'] = response.status
            provider_id = response.headers.get('request-id', '')
            if PROVIDER_ID.fullmatch(provider_id):
                row['provider_request_id'] = provider_id
            data = response.read(MAX_BODY + 1)
            if len(data) > MAX_BODY:
                raise ValueError('reply too large')
            row['status'] = 'ok' if response.status == 200 else 'upstream_error'
            message = json.loads(data) if response.headers.get('Content-Encoding', 'identity').lower() == 'identity' else None
            if response.status == 200 and isinstance(message, dict):
                usage = message.get('usage') if isinstance(message.get('usage'), dict) else {}
                row.update(usage=logged_usage(usage), usage_complete=bool(usage), stop_reason=message.get('stop_reason'))
                row['cost_usd'] = priced_usage(body, message.get('model'), usage, self.rates)
                if row['cost_usd'] is None:
                    row['usage_problem'] = 'unpriced_usage'
                out['message'] = message
        except Exception as e:
            row['error_type'] = type(e).__name__
        finally:
            conn.close()
            row['wall_seconds'] = time.monotonic() - start
            try:
                self.settle(row)  # unknown cost stays unknown, which halts admission
            except Exception as e:
                row.update(governor_status='settle_failed', governor_error=type(e).__name__)
            self.record(row)
        out.update(status=row['status'], cost_usd=row['cost_usd'])
        return out

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
        return self.server.connection()

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
            if self.server.catalog is not None and response.status == 200:
                data, row['catalog'] = filter_catalog(data, self.server.catalog)
                if data is None:
                    row.update(status='catalog_unreadable', http_status=502)
                    self.send_error(502, 'Unreadable model catalog')
                    sent_headers = True
                    return
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

    def refuse(self, row, reason):
        """Active arm only: answer with an API-style error instead of forwarding. Nothing is billed."""
        body = json.dumps({'type': 'error', 'error': {'type': 'invalid_request_error',
                           'message': f'ModelPilot benchmark arm refused this request ({reason}).'}}).encode()
        try:
            self.send_response_only(400, 'Bad Request')
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True
        # Its own kind, so request counts, accounting and cache attribution see only sent requests.
        # governor_status 'refused' also keeps reconcile_log() from recording a reservation for it.
        row.update(kind='refused', status='refused', refusal=reason, http_status=400, cost_usd=0.0, wall_seconds=0.0,
                   governor_status='refused')
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
            client_headers = clean_headers(self.headers)
            side = (lambda purpose, body, timeout: self.server.side_call(client_headers, purpose, body, timeout)) \
                if self.server.active else None
            try:
                outcome = self.server.plan_policy(request, side)
            except Exception as e:
                outcome = {'status': 'deferred', 'reason': 'policy_error:' + type(e).__name__}
            if outcome['status'] == 'admitted':
                ticket, request = outcome, outcome['request']
                raw = json.dumps(request).encode()
                policy_row = {'kind': ticket['kind'], 'status': 'reserved'}
                if ticket.get('escalation_deferred'):
                    policy_row['escalation_deferred'] = ticket['escalation_deferred']
                if ticket.get('deliveries'):
                    policy_row['deliveries'] = ticket['deliveries']
            else:
                policy_row = {k: outcome[k] for k in ('status', 'reason') if k in outcome}
                if outcome.get('forward') is not None:  # the client's setting, with earlier effort messages kept
                    request = outcome['forward']
                    raw = json.dumps(request).encode()
                    policy_row['rewritten'] = 'effort_messages'
                    if outcome.get('deliveries'):
                        policy_row['deliveries'] = outcome['deliveries']
        headers = clean_headers(self.headers)
        extra_beta = (outcome.get('beta') if governed and self.server.policy is not None else None)
        if extra_beta:
            names = [b.strip() for b in (headers.get('anthropic-beta') or '').split(',') if b.strip()]
            headers = {k: v for k, v in headers.items() if k.lower() != 'anthropic-beta'}
            headers['anthropic-beta'] = ','.join(names + ([extra_beta] if extra_beta not in names else []))
        headers['Content-Length'] = str(len(raw))
        # Negotiate identity so usage inspection does not depend on client compression support.
        headers = {k: v for k, v in headers.items() if k.lower() != 'accept-encoding'}
        headers['Accept-Encoding'] = 'identity'
        tools = request.get('tools')
        mode = self.server.policy.mode if ticket is not None else 'active' if self.server.active else 'dry-run'
        counting = path.path == '/v1/messages/count_tokens'
        row = {'started_unix': time.time(), 'kind': 'count_tokens' if counting else 'messages', 'path': path.path,
               'mode': mode,
               'applied': ticket is not None,
               'tool_count': len(tools) if isinstance(tools, list) else 0,
               'request_sha256': hashlib.sha256(raw).hexdigest(),
               'model': request.get('model') if request.get('model') in self.server.rates else 'unknown',
               'effort': request_effort(request), 'stream': bool(request.get('stream')), 'http_status': None,
               'status': 'transport_error', 'cost_usd': None}
        if self.server.system_marker is not None:
            row['system_marker'] = self.server.system_marker in system_text(request)
        ttls = cache_ttls(request)
        if ttls:
            row['cache_ttl'] = ttls  # what the breakpoints asked for; usage says what was written
        if compaction_request(request):
            row['compaction'] = True
        effective = effective_effort(request)
        if effective != row['effort'] and (effective is None or isinstance(effective, str) and len(effective) <= 16):
            row['effective_effort'] = effective  # set by an effort-only system message (per-message effort)
        if policy_row is not None:
            row['policy'] = policy_row
        if ticket is not None:
            row.update(client_request_sha256=client_sha, governor_request_id=ticket['request_id'],
                       governor_status='reserved')
        elif governed:
            if policy_row and policy_row.get('rewritten'):
                # The client's setting, with the policy's context carried: admitted and settled like any other request.
                row['client_request_sha256'] = client_sha
            stop = self.server.active and outcome['status'] == 'stop'
            if not stop:
                try:
                    self.server.admit(row, raw, request)
                except Exception as e:
                    # Dry-run never blocks traffic; reconcile_log() later records this row's spend.
                    row.update(governor_status='untracked', governor_error=type(e).__name__)
            if self.server.active and row.get('governor_status') != 'reserved':
                # Active arm fails closed: a policy stop, a refused admission, or no admission at all.
                reason = outcome['reason'] if stop else (row.get('governor') or {}).get('reason') or 'governor_error'
                self.refuse(row, reason)
                return
        conn = self.upstream_connection()
        start = time.monotonic()
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
                observer.fail('compressed_response')  # bytes still forwarded; don't parse compressed payloads
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
            if counting:
                # Token counting is free (Anthropic docs) and replies {"input_tokens": N}, with no usage.
                row.update(cost_usd=0.0, input_tokens=observer.input_tokens)
            elif row['status'] == 'ok':
                row['cost_usd'] = measured_cost(observer, request, self.server.rates)
                if row['cost_usd'] is None:
                    row['usage_problem'] = observer.problem or 'unpriced_usage'
            if not counting:
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
