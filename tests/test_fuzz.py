"""Seeded fuzzing of the proxy's stream parser and of usage pricing (October 15, overnight cloud run). Standard library
only, fixed seeds; FUZZ_ITERATIONS lengthens the run (the default set takes a few seconds).

- Usage: dicts with missing, extra, null, negative, fractional, boolean and string fields; nested server_tool_use,
  iterations and cache_creation; both cache lifetimes. cache_probe.priced_usage must equal the reference pricer below
  (written here from configs/m0.json's rates and the documented rules, not from the code under test), or be None.
- Streams: valid Messages SSE streams (text, thinking with a signature, tool_use with input_json_delta, server tool
  use, pings, unknown event types, error events), LF or CRLF, SSE comments, an oversized event now and then, cut at
  random byte boundaries and sometimes cut short, through a real ProxyServer. The client must get the upstream's
  bytes exactly; each request gets exactly one log row; the row's cost equals the reference or is None, and is never
  negative; and no handler error stops the server.
"""
import json
import math
import os
from pathlib import Path
import random
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from modelpilot.cache_probe import priced_usage
from modelpilot.proxy import MAX_EVENT, ProxyServer

ROOT = Path(__file__).resolve().parents[1]
RATES = json.loads((ROOT/'configs/m0.json').read_text())['rates']
MODEL, OTHER = 'claude-sonnet-4-6', 'claude-opus-4-6'
ITERATIONS = int(os.environ.get('FUZZ_ITERATIONS') or 0)
COUNTERS = ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')
LIFETIMES = ('ephemeral_5m_input_tokens', 'ephemeral_1h_input_tokens')


def whole(value):
    return type(value) is int and value >= 0


def reference(request, reply_model, usage):
    """The documented pricing rule (cache_probe, docs/m1-results.md), written independently: dollars, or None when any
    input is ambiguous. Every counter a non-negative whole number; the reply from the model asked for, standard tier,
    global (or no) residency, no fast mode; cache writes with their lifetime split, which must add up; server tools
    only counters, and only unmetered ones (web fetch) above zero; no iteration from another model or a fallback."""
    rate = RATES.get(request.get('model'))
    if not isinstance(usage, dict) or reply_model != request.get('model') or rate is None:
        return None
    if not all(whole(usage.get(k)) for k in COUNTERS):
        return None
    if usage.get('service_tier', 'standard') != 'standard' or usage.get('inference_geo', 'global') not in ('global', 'not_available'):
        return None
    if request.get('speed') == 'fast':
        return None
    tools = usage.get('server_tool_use')
    if tools is not None and (not isinstance(tools, dict) or any(not whole(v) or (v > 0 and k != 'web_fetch_requests')
                                                                  for k, v in tools.items())):
        return None
    iterations = usage.get('iterations')
    if isinstance(iterations, list) and any(not isinstance(i, dict) or i.get('type') == 'fallback_message'
                                            or i.get('model', request['model']) != request['model'] for i in iterations):
        return None
    split = usage.get('cache_creation')
    writes = usage['cache_creation_input_tokens']
    if isinstance(split, dict):
        if not all(whole(split.get(k)) for k in LIFETIMES) or split[LIFETIMES[0]] + split[LIFETIMES[1]] != writes:
            return None
        five, hour = split[LIFETIMES[0]], split[LIFETIMES[1]]
    elif writes or split not in (None,):  # writes need their split; a split that isn't one is unreadable
        return None
    else:
        five = hour = 0  # no writes, and no split or a null one: nothing to split
    return (usage['input_tokens'] * rate['input'] + usage['output_tokens'] * rate['output'] +
            usage['cache_read_input_tokens'] * rate['read'] + five * rate['write_5m'] + hour * rate['write_1h']) / 1e6


def odd_value(rng):
    return rng.choice([None, -1, -rng.randint(1, 10**6), 1.5, 0.0, True, False, '12', 'x', [], {}, 10**12])


def random_usage(rng):
    """A usage dict: usually sound, often with one or more fields made odd."""
    usage = {k: rng.choice([0, rng.randint(0, 50), rng.randint(0, 200000)]) for k in COUNTERS}
    hour = rng.randint(0, usage['cache_creation_input_tokens'])
    usage['cache_creation'] = {LIFETIMES[0]: usage['cache_creation_input_tokens'] - hour, LIFETIMES[1]: hour}
    usage.update(service_tier='standard', inference_geo=rng.choice(['global', 'not_available']))
    for _ in range(rng.choice([0, 0, 1, 2, 3])):
        mutation = rng.randrange(12)
        if mutation == 0:
            usage.pop(rng.choice(COUNTERS), None)
        elif mutation == 1:
            usage[rng.choice(COUNTERS)] = odd_value(rng)
        elif mutation == 2:
            usage.pop('cache_creation', None)
        elif mutation == 3:
            usage['cache_creation'] = rng.choice([None, 'x', [], {LIFETIMES[0]: 1}])
        elif mutation == 4 and isinstance(usage.get('cache_creation'), dict):
            usage['cache_creation'][rng.choice(LIFETIMES)] = odd_value(rng)
        elif mutation == 5:
            usage['server_tool_use'] = {rng.choice(['web_search_requests', 'web_fetch_requests', 'code_execution_requests']):
                                        rng.choice([0, 1, 3, odd_value(rng)])}
        elif mutation == 6:
            usage['server_tool_use'] = rng.choice([None, 'x', [], {'web_fetch_requests': {'n': 1}}])
        elif mutation == 7:
            usage['iterations'] = rng.choice([[{'type': 'message', 'model': MODEL}],
                                              [{'type': 'message', 'model': MODEL}, {'type': 'fallback_message', 'model': OTHER}],
                                              [{'type': 'message', 'model': OTHER}], ['x'], 'x', None])
        elif mutation == 8:
            usage[rng.choice(['service_tier', 'inference_geo'])] = rng.choice(['priority', 'batch', 'us', None, 1])
        elif mutation == 9:
            usage['extra_field_' + str(rng.randrange(5))] = odd_value(rng)
        elif mutation == 10:
            usage = rng.choice([None, [], 'usage', 7])
            break
        else:
            usage.pop(rng.choice(['service_tier', 'inference_geo']), None)
    return usage


def agrees(code, ref):
    return code is None or (ref is not None and math.isclose(code, ref, rel_tol=1e-12, abs_tol=1e-15))


class UsageFuzzTests(unittest.TestCase):
    def test_priced_usage_matches_the_reference_or_is_unknown(self):
        rng = random.Random(20261015)
        found = []
        for i in range(ITERATIONS or 20000):
            usage = random_usage(rng)
            request = {'model': MODEL, **({'speed': 'fast'} if rng.random() < .02 else {})}
            reply_model = MODEL if rng.random() < .95 else OTHER
            code = priced_usage(request, reply_model, json.loads(json.dumps(usage)), RATES)
            ref = reference(request, reply_model, usage)
            if not agrees(code, ref) or (code is not None and code < 0):
                found.append((i, usage, code, ref))
        self.assertEqual(found[:5], [], f'{len(found)} disagreements')


class FuzzFindingTests(unittest.TestCase):
    """What the seeded usage fuzz found on October 15, each fixed in cache_probe the same night (every case here
    priced on the old code)."""
    USAGE = {'input_tokens': 100, 'output_tokens': 20, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 50,
             'cache_creation': {'ephemeral_5m_input_tokens': 0, 'ephemeral_1h_input_tokens': 50}}

    def price(self, **changes):
        return priced_usage({'model': MODEL}, MODEL, dict(self.USAGE, **changes), RATES)

    def test_the_sound_reply_is_priced(self):
        self.assertAlmostEqual(self.price(), (100 * 3 + 20 * 15 + 50 * 6) / 1e6)

    def test_a_negative_counter_is_unknown_not_a_negative_cost(self):
        self.assertIsNone(self.price(output_tokens=-55831))
        self.assertIsNone(self.price(input_tokens=-1))

    def test_a_count_that_isnt_a_whole_number_is_unknown(self):
        for value in (True, False, 1.5, 0.0):
            with self.subTest(value=value):
                self.assertIsNone(self.price(output_tokens=value))
        self.assertIsNone(self.price(cache_creation={'ephemeral_5m_input_tokens': 0.0, 'ephemeral_1h_input_tokens': 50}))
        self.assertIsNone(self.price(cache_creation={'ephemeral_5m_input_tokens': -10, 'ephemeral_1h_input_tokens': 60}))

    def test_a_null_lifetime_split_with_writes_is_unknown_not_all_5m(self):
        """Every write was priced at the 5m rate, so 1h writes read cheap (a smaller wrong total)."""
        self.assertIsNone(self.price(cache_creation=None))
        self.assertIsNotNone(self.price(cache_creation=None, cache_creation_input_tokens=0))  # nothing to split

    def test_a_stream_event_whose_usage_isnt_an_object_leaves_the_cost_unknown(self):
        """Found by the long stream run (30,000 replies): a message_delta usage of [] (or a list of pairs) passed
        dict.update, so the reply was priced on message_start's placeholder output count."""
        from modelpilot.proxy import UsageObserver, measured_cost
        start = {'type': 'message_start', 'message': {'model': MODEL, 'usage': dict(self.USAGE, output_tokens=1)}}
        for usage in ([], [['output_tokens', 999]], 'x', None, 7):
            with self.subTest(usage=usage):
                observer = UsageObserver(True)
                for event in (start, {'type': 'message_delta', 'usage': usage}, {'type': 'message_stop'}):
                    observer.feed(b'data: ' + json.dumps(event).encode() + b'\n\n')
                observer.finish()
                self.assertIsNone(measured_cost(observer, {'model': MODEL}, RATES))
        observer = UsageObserver(True)
        for event in (start, {'type': 'message_delta', 'usage': dict(self.USAGE)}, {'type': 'message_stop'}):
            observer.feed(b'data: ' + json.dumps(event).encode() + b'\n\n')
        observer.finish()
        self.assertAlmostEqual(measured_cost(observer, {'model': MODEL}, RATES), self.price())

    def test_an_iteration_from_a_fallback_or_another_model_is_unknown(self):
        """The reply named the model asked for, but its iterations show another model's turn: no one rate fits."""
        self.assertIsNone(self.price(iterations=[{'type': 'message', 'model': MODEL},
                                                 {'type': 'fallback_message', 'model': OTHER}]))
        self.assertIsNone(self.price(iterations=[{'type': 'message', 'model': OTHER}]))
        self.assertIsNotNone(self.price(iterations=[{'type': 'message', 'model': MODEL}]))


SEPARATORS = (b'\n', b'\r\n')


def frame(event, sep, rng):
    data = json.dumps(event).encode()
    lines = [b'event: ' + event.get('type', 'unknown').encode(), b'data: ' + data]
    if rng.random() < .1:
        lines.insert(0, b': an SSE comment')
    return sep.join(lines) + sep + sep


def random_stream(rng):
    """(bytes, the events in order, an oversized event in it) for one streamed reply."""
    sep = rng.choice(SEPARATORS)
    usage = random_usage(rng) if rng.random() < .3 else {k: rng.randint(0, 3000) for k in COUNTERS}
    if isinstance(usage, dict) and 'cache_creation' not in usage and usage.get('cache_creation_input_tokens') == 0:
        pass
    elif isinstance(usage, dict) and 'cache_creation' not in usage and rng.random() < .8:
        writes = usage.get('cache_creation_input_tokens') if whole(usage.get('cache_creation_input_tokens')) else 0
        usage['cache_creation'] = {LIFETIMES[0]: writes, LIFETIMES[1]: 0}
    model = MODEL if rng.random() < .95 else OTHER
    start_usage = dict(usage, output_tokens=1) if isinstance(usage, dict) else usage
    events = [{'type': 'message_start', 'message': {'id': 'msg_fuzz', 'type': 'message', 'role': 'assistant',
                                                    'model': model, 'content': [], 'usage': start_usage}}]
    index = 0
    for _ in range(rng.randint(0, 5)):
        kind = rng.randrange(7)
        if kind == 0:
            events += [{'type': 'content_block_start', 'index': index, 'content_block': {'type': 'text', 'text': ''}},
                       {'type': 'content_block_delta', 'index': index, 'delta': {'type': 'text_delta', 'text': 'ok\n\n' * rng.randint(1, 3)}},
                       {'type': 'content_block_stop', 'index': index}]
        elif kind == 1:
            events += [{'type': 'content_block_start', 'index': index, 'content_block': {'type': 'thinking', 'thinking': ''}},
                       {'type': 'content_block_delta', 'index': index, 'delta': {'type': 'thinking_delta', 'thinking': 'hmm'}},
                       {'type': 'content_block_delta', 'index': index, 'delta': {'type': 'signature_delta', 'signature': 'c2ln' * 40}},
                       {'type': 'content_block_stop', 'index': index}]
        elif kind == 2:
            whole_json = json.dumps({'path': 'a.py', 'n': rng.randint(0, 9)})
            cut = rng.randint(0, len(whole_json))
            events += [{'type': 'content_block_start', 'index': index, 'content_block': {'type': 'tool_use', 'id': 'toolu_f', 'name': 'Read', 'input': {}}},
                       {'type': 'content_block_delta', 'index': index, 'delta': {'type': 'input_json_delta', 'partial_json': whole_json[:cut]}},
                       {'type': 'content_block_delta', 'index': index, 'delta': {'type': 'input_json_delta', 'partial_json': whole_json[cut:]}},
                       {'type': 'content_block_stop', 'index': index}]
        elif kind == 3:
            events += [{'type': 'content_block_start', 'index': index, 'content_block': {'type': 'server_tool_use', 'id': 'srvtoolu_f', 'name': 'web_search', 'input': {}}},
                       {'type': 'content_block_stop', 'index': index},
                       {'type': 'content_block_start', 'index': index + 1, 'content_block': {'type': 'web_search_tool_result', 'tool_use_id': 'srvtoolu_f', 'content': []}},
                       {'type': 'content_block_stop', 'index': index + 1}]
            index += 1
        elif kind == 4:
            events.append({'type': 'ping'})
        elif kind == 5:
            events.append({'type': rng.choice(['future_event', 'annotation']), 'value': 'keep me'})
        else:
            events.append({'type': 'content_block_delta', 'index': index, 'delta': {'type': 'text_delta', 'text': 'x' * (MAX_EVENT + 10)}})
        index += 1
    final = random_usage(rng) if rng.random() < .3 else dict(usage) if isinstance(usage, dict) else usage
    events.append({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': final})
    if rng.random() < .05:
        events.append({'type': 'error', 'error': {'type': 'overloaded_error', 'message': 'Overloaded'}})
    events.append({'type': 'message_stop'})
    data = b''.join(frame(e, sep, rng) for e in events)
    oversized = any(len(json.dumps(e)) > MAX_EVENT - 64 for e in events)
    return data, events, oversized


def stream_reference(request, events, oversized, cut):
    """The cost the reference gives a stream as the documented rules read it: usage merged from message_start and each
    message_delta (cumulative), complete only with message_stop, no error event, not cut short. An oversized event
    (over proxy.MAX_EVENT) is a reason the proxy may give up (unknown), not a different price: the bound is checked
    between reads, so one a little over it can still be parsed and priced (docs/m1-results.md)."""
    if cut or any(e['type'] == 'error' for e in events) or not any(e['type'] == 'message_stop' for e in events):
        return None
    usage, model = {}, None
    for e in events:
        if e['type'] == 'message_start':
            if not isinstance(e['message'].get('usage', {}), dict):
                return None
            usage.update(e['message'].get('usage', {}))
            model = e['message'].get('model')
        elif e['type'] == 'message_delta':
            if not isinstance(e.get('usage', {}), dict):  # present but not an object: unreadable
                return None
            usage.update(e.get('usage', {}))
    return reference(request, model, usage)


class Upstream(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers['Content-Length']))
        data, pieces, cut = self.server.plan
        self.send_response_only(200)
        self.send_header('Content-Type', 'text/event-stream' if pieces else 'application/json')
        self.send_header('Connection', 'close')
        self.end_headers()
        sent = 0
        try:
            for size in pieces or [len(data)]:
                self.wfile.write(data[sent:sent + size])
                self.wfile.flush()
                sent += size
        except OSError:
            pass
        self.close_connection = True


def send(port, body):
    """The raw bytes the proxy sent back, and its status line."""
    sock = socket.create_connection(('127.0.0.1', port), timeout=30)
    sock.sendall(b'POST /v1/messages HTTP/1.1\r\nHost: proxy\r\nContent-Type: application/json\r\nContent-Length: '
                 + str(len(body)).encode() + b'\r\n\r\n' + body)
    got = b''
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        got += chunk
    sock.close()
    head, _, rest = got.partition(b'\r\n\r\n')
    return head.split(b'\r\n')[0], rest


def chunking(rng, n):
    """Random byte boundaries covering n bytes."""
    pieces, left = [], n
    while left > 0:
        size = min(left, rng.choice([1, 2, 7, 37, 512, 4096, 70000]))
        pieces.append(size)
        left -= size
    return pieces


class StreamFuzzTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name)/'log.jsonl'
        self.upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
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
        self.tmp.cleanup()

    def test_streams_pass_byte_for_byte_and_are_priced_as_the_reference_or_unknown(self):
        rng = random.Random(15102026)
        found, sent, rows = [], 0, []
        log = open(self.log, 'a+')  # read as it grows: each request's row once, not the whole log every time
        for i in range(ITERATIONS // 20 or 160):
            streamed = rng.random() < .85
            request = {'model': MODEL, 'max_tokens': 8, 'stream': streamed, 'messages': []}
            if streamed:
                data, events, oversized = random_stream(rng)
                cut = rng.random() < .1
                if cut:
                    data = data[:rng.randint(0, len(data) - 1)]
                expected = stream_reference(request, events, oversized, cut)
                pieces = chunking(rng, len(data))
            else:
                usage = random_usage(rng)
                model = MODEL if rng.random() < .95 else OTHER
                data = json.dumps({'id': 'msg', 'type': 'message', 'model': model, 'content': [], 'usage': usage}).encode()
                expected, pieces = reference(request, model, usage), None
            self.upstream.plan = (data, pieces, False)
            status, body = send(self.proxy.server_port, json.dumps(request).encode())
            sent += 1
            self.assertTrue(self.proxy.wait_idle(30))
            log.seek(0, 1)
            rows += [json.loads(line) for line in log.read().splitlines()]
            if len(rows) != sent:
                found.append((i, 'rows', len(rows), sent))
                break
            row = rows[-1]
            if body != data:
                found.append((i, 'bytes', len(body), len(data)))
            if not agrees(row['cost_usd'], expected) or (row['cost_usd'] is not None and row['cost_usd'] < 0):
                found.append((i, 'cost', row['cost_usd'], expected, row.get('usage_problem')))
        log.close()
        self.assertEqual(found[:5], [], f'{len(found)} findings')
        # No handler error stopped the server: a plain request still goes through, logged once.
        self.upstream.plan = (json.dumps({'model': MODEL, 'content': [], 'usage': {k: 1 for k in COUNTERS}}).encode(), None, False)
        status, _ = send(self.proxy.server_port, json.dumps({'model': MODEL, 'max_tokens': 8, 'messages': []}).encode())
        self.assertIn(b'200', status)
        self.assertTrue(self.proxy.wait_idle(30))
        self.assertEqual(len(self.log.read_text().splitlines()), sent + 1)


if __name__ == '__main__':
    unittest.main()
