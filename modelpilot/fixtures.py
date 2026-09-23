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
        request = json.loads(raw)
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
    server.lock = threading.Lock()
    return server
