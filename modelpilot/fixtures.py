"""Synthetic loopback upstream used by integration tests and the overnight soak."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import threading
import time

USAGE = {'input_tokens': 10, 'cache_creation_input_tokens': 0,
         'cache_read_input_tokens': 14750, 'output_tokens': 4,
         'service_tier': 'standard', 'inference_geo': 'global'}


CATALOG = {'data': [{'id': 'claude-sonnet-5', 'type': 'model', 'display_name': 'Synthetic Sonnet'}], 'has_more': False}


def response_for(request):
    if request.get('metadata', {}).get('test_error'):
        return 429, 'application/json', b'{"error":{"type":"rate_limit_error","message":"synthetic"}}'
    message = {'type': 'message', 'model': request['model'], 'usage': USAGE,
               'content': [{'type': 'text', 'text': 'synthetic output'}]}
    if not request.get('stream'):
        return 200, 'application/json', json.dumps(message).encode()
    start = dict(message, usage=dict(USAGE, output_tokens=1), content=[])
    events = [{'type': 'message_start', 'message': start}, {'type': 'ping'},
              {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'synthetic output'}},
              {'type': 'future_event', 'value': 'preserve this'},
              {'type': 'message_delta', 'usage': {'output_tokens': 2}},
              {'type': 'message_delta', 'usage': {'output_tokens': 4}},
              {'type': 'message_stop'}]
    if request.get('metadata', {}).get('test_stream_error'):
        events[-1] = {'type': 'error', 'error': {'type': 'overloaded_error'}}
    data = b''.join(b'event: ' + e['type'].encode() + b'\r\ndata: ' + json.dumps(e).encode() + b'\r\n\r\n' for e in events)
    return 200, 'text/event-stream', data


def scripted_response(request, script):
    """Drive a real client offline: step N answers after N tool results in the conversation.

    Each step is {'tool': name, 'input': {...}} (a tool_use turn) or {'text': ...}.
    Steps past the end repeat the last one.
    """
    results = sum(1 for m in request.get('messages', []) if m.get('role') == 'user' and isinstance(m.get('content'), list)
                  for b in m['content'] if isinstance(b, dict) and b.get('type') == 'tool_result')
    step = script[min(results, len(script)-1)]
    usage = dict(USAGE, cache_read_input_tokens=0, input_tokens=100)
    if 'tool' in step:
        block = {'type': 'tool_use', 'id': f'toolu_fixture{results}', 'name': step['tool'], 'input': {}}
        deltas = [{'type': 'input_json_delta', 'partial_json': json.dumps(step['input'])}]
        stop = 'tool_use'
    else:
        block = {'type': 'text', 'text': ''}
        deltas = [{'type': 'text_delta', 'text': step['text']}]
        stop = 'end_turn'
    start = {'type': 'message', 'id': f'msg_fixture{results}', 'role': 'assistant', 'model': request['model'],
             'content': [], 'stop_reason': None, 'stop_sequence': None, 'usage': dict(usage, output_tokens=1)}
    events = ([{'type': 'message_start', 'message': start},
               {'type': 'content_block_start', 'index': 0, 'content_block': block}] +
              [{'type': 'content_block_delta', 'index': 0, 'delta': d} for d in deltas] +
              [{'type': 'content_block_stop', 'index': 0},
               {'type': 'message_delta', 'delta': {'stop_reason': stop, 'stop_sequence': None}, 'usage': {'output_tokens': 4}},
               {'type': 'message_stop'}])
    if not request.get('stream'):
        message = dict(start, content=[dict(block, input=step['input']) if 'tool' in step else dict(block, text=step['text'])],
                       stop_reason=stop, usage=dict(usage, output_tokens=4))
        return 200, 'application/json', json.dumps(message).encode()
    data = b''.join(b'event: ' + e['type'].encode() + b'\r\ndata: ' + json.dumps(e).encode() + b'\r\n\r\n' for e in events)
    return 200, 'text/event-stream', data


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *_):
        pass

    def do_GET(self):
        # Model catalog, as Jev requests it for discovery; anything else is unknown here.
        with self.server.lock:
            self.server.received.append({'method': 'GET', 'path': self.path, 'key': self.headers.get('x-api-key')})
        if not self.path.startswith('/v1/models'):
            self.send_error(404)
            return
        data = json.dumps(CATALOG).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers['Content-Length']))
        with self.server.lock:
            self.server.received.append({'sha256': hashlib.sha256(raw).hexdigest(),
                                         'key': self.headers.get('x-api-key'), 'path': self.path,
                                         'beta': self.headers.get('anthropic-beta'),
                                         'accept_encoding': self.headers.get('accept-encoding')})
            # Bounded memory during overnight requests.
            self.server.received[:] = self.server.received[-32:]
            self.server.count += 1
            request_id = f'req_fixture{self.server.count}'
            if self.server.keep_bodies:
                self.server.bodies.append(raw)
        request = json.loads(raw)
        script = self.server.script
        # Only the tool-bearing conversation follows the script; side requests get plain text.
        if script and request.get('tools') and self.path.split('?', 1)[0] == '/v1/messages':
            time.sleep(self.server.delay)
            status, content_type, data = scripted_response(request, script)
        else:
            status, content_type, data = response_for(request)
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Transfer-Encoding', 'chunked')
        self.send_header('retry-after', '1')
        self.send_header('request-id', request_id)
        self.end_headers()
        try:
            # Split SSE/JSON in arbitrary places; first fragment arrives before a pause.
            for pos in range(0, len(data), 37):
                chunk = data[pos:pos+37]
                self.wfile.write(f'{len(chunk):x}\r\n'.encode()+chunk+b'\r\n')
                self.wfile.flush()
                if pos == 0 and request.get('metadata', {}).get('slow'):
                    time.sleep(.2)
            self.wfile.write(b'0\r\n\r\n')
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def fixture_server():
    server = ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
    server.received = []
    server.count = 0
    server.script = None  # set to a scripted_response() step list to drive a real client
    server.delay = 0  # seconds before each scripted reply
    server.keep_bodies = False  # tests that must see what reached the model set this
    server.bodies = []
    server.lock = threading.Lock()
    return server
