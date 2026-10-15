"""What the proxy adds (October 15, overnight cloud run; $0, loopback only): time to first byte, per-event latency,
throughput, CPU and peak memory, for request bodies from 1 KB to 30 MB and streams of up to 10,000 events, with 1, 4
and 16 concurrent clients, with and without the governor flags. Each request is timed through the proxy and, in the
same round, straight to the upstream; the difference is the proxy's. Medians and p95 over repeated requests. Connect
time is its own figure: the proxy listens with socketserver's default backlog of 5, so with more clients than that at
once some connects wait for a SYN retry (about 1 s on Linux); `slow_connects` counts those over 0.5 s. A request that
fails (refused, reset) is counted, not retried, and its pair is left out of the differences.

    python3 -m modelpilot.proxy_overhead [--quick] [--repeats 5] --out runs/<new file>.json

The proxy runs as its own process (python -m modelpilot.proxy), so its CPU and peak memory are its own (getrusage of
the child). Machine-dependent: label results with the machine they ran on.
"""
import argparse
import hashlib
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import re
import resource
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
MODEL = 'claude-sonnet-4-6'
RATES = {MODEL: dict(input=3, output=15, read=.3, write_5m=3.75, write_1h=6)}
USAGE = {'input_tokens': 10, 'output_tokens': 20, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0,
         'cache_creation': {'ephemeral_5m_input_tokens': 0, 'ephemeral_1h_input_tokens': 0}}
BODIES = (1_000, 100_000, 1_000_000, 10_000_000, 30_000_000)
STREAMS = (10, 1_000, 10_000)
CLIENTS = (1, 4, 16)


def event(kind, data):
    return b'event: ' + kind.encode() + b'\ndata: ' + json.dumps(data).encode() + b'\n\n'


def stream(events):
    start = {'type': 'message_start', 'message': {'id': 'm', 'type': 'message', 'role': 'assistant', 'model': MODEL,
                                                  'content': [], 'usage': dict(USAGE, output_tokens=1)}}
    body = [event('message_start', start),
            event('content_block_start', {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})]
    body += [event('content_block_delta', {'type': 'content_block_delta', 'index': 0,
                                           'delta': {'type': 'text_delta', 'text': 'token '}})] * events
    body += [event('content_block_stop', {'type': 'content_block_stop', 'index': 0}),
             event('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': USAGE}),
             event('message_stop', {'type': 'message_stop'})]
    return b''.join(body)


class UpstreamServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128  # so the direct baseline never waits on its own backlog


class Upstream(BaseHTTPRequestHandler):
    """Answers at once: a stream of the asked number of events (metadata.events) or a small JSON reply."""
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def do_POST(self):
        remaining = int(self.headers['Content-Length'])
        head = b''
        while remaining:
            chunk = self.rfile.read(min(remaining, 1 << 20))
            if not chunk:
                break
            if len(head) < 4096:
                head += chunk[:4096]
            remaining -= len(chunk)
        found = re.match(rb'\{"events": (\d+)', head)
        events = int(found.group(1)) if found else 0
        data = (self.server.streams.get(events) or stream(events)) if events else json.dumps(
            {'id': 'm', 'type': 'message', 'model': MODEL, 'content': [], 'usage': USAGE}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream' if events else 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def request_body(size, events=0):
    """A Messages request of about `size` bytes; the upstream reads the leading events count."""
    head = '{"events": %d, "model": "%s", "max_tokens": 8, "stream": %s, "messages": [{"role": "user", "content": "' % (
        events, MODEL, 'true' if events else 'false')
    tail = '"}]}'
    return (head + 'x' * max(0, size - len(head) - len(tail)) + tail).encode()


def timed(port, body):
    """One request: seconds to connect; from connected, seconds to the reply's first byte and to its end; the reply
    body's SHA-256 (headers differ: the proxy answers HTTP/1.0, close-delimited; the body must not). Or the error."""
    started = time.perf_counter()
    try:
        sock = socket.create_connection(('127.0.0.1', port), timeout=120)
    except OSError as e:
        return {'error': type(e).__name__}
    connected = time.perf_counter()
    first, received = None, bytearray()
    try:
        with sock:
            sock.sendall(b'POST /v1/messages HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: '
                         + str(len(body)).encode() + b'\r\nConnection: close\r\n\r\n' + body)
            while True:
                chunk = sock.recv(1 << 20)
                if not chunk:
                    break
                if first is None:
                    first = time.perf_counter() - connected
                received += chunk
    except OSError as e:
        return {'error': type(e).__name__, 'connect': connected - started}
    return {'connect': connected - started, 'first': first, 'total': time.perf_counter() - connected,
            'sha': hashlib.sha256(bytes(received).partition(b'\r\n\r\n')[2]).hexdigest()}


def start_proxy(upstream_port, root, governed):
    config = Path(root)/'config.json'
    config.write_text(json.dumps({'rates': RATES}))
    command = [sys.executable, '-m', 'modelpilot.proxy', '--port', '0', '--config', str(config), '--log', str(Path(root)/'log.jsonl'),
               '--upstream', f'http://127.0.0.1:{upstream_port}']
    if governed:
        command += ['--governor-db', str(Path(root)/'state.db'), '--governor-session', 's', '--governor-limit-usd', '1000000']
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'PYTHONPATH': str(ROOT)}
    proxy = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    port = int(proxy.stdout.readline().split('http://127.0.0.1:')[1].split(';')[0])
    return proxy, port


def peak_kib(pid):
    try:
        for line in Path(f'/proc/{pid}/status').read_text().splitlines():
            if line.startswith('VmHWM:'):
                return int(line.split()[1])
    except OSError:
        return None
    return None


def stop(proxy):
    """Stop the proxy; its CPU seconds and peak memory (KiB), as the OS counted them."""
    peak = peak_kib(proxy.pid)
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    proxy.send_signal(signal.SIGINT)
    proxy.wait(30)
    proxy.stdout.close()
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
    if peak is None:  # not Linux: ru_maxrss is the largest child so far (KiB on Linux, bytes on macOS)
        peak = after.ru_maxrss // (1024 if platform.system() == 'Darwin' else 1)
    return cpu, peak


def quantiles(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    return {'median': statistics.median(values), 'p95': values[min(len(values) - 1, round(.95 * (len(values) - 1)))]}


def measure(case, upstream_port, proxy_port, clients, repeats):
    """One case at a concurrency: direct and proxied times per request, and the proxy's additions."""
    body = request_body(case['body'], case.get('events', 0))
    direct, proxied = [], []
    with ThreadPoolExecutor(clients) as pool:
        for _ in range(repeats):
            direct += list(pool.map(lambda _: timed(upstream_port, body), range(clients)))
            proxied += list(pool.map(lambda _: timed(proxy_port, body), range(clients)))
    pairs = [(p, d) for p, d in zip(proxied, direct) if 'error' not in p and 'error' not in d]
    first = [p['first'] - d['first'] for p, d in pairs if p['first'] is not None and d['first'] is not None]
    total = [p['total'] - d['total'] for p, d in pairs]
    out = {'requests': len(proxied), 'added_first_byte_seconds': quantiles(first),
           'added_total_seconds': quantiles(total),
           'direct_total_seconds': quantiles([d['total'] for _, d in pairs]),
           'proxied_total_seconds': quantiles([p['total'] for p, _ in pairs]),
           'direct_connect_seconds': quantiles([d.get('connect') for d in direct]),
           'proxied_connect_seconds': quantiles([p.get('connect') for p in proxied]),
           'slow_connects': {'direct': sum((d.get('connect') or 0) > .5 for d in direct),
                             'proxied': sum((p.get('connect') or 0) > .5 for p in proxied)},
           'failed': {'direct': sorted(d['error'] for d in direct if 'error' in d),
                      'proxied': sorted(p['error'] for p in proxied if 'error' in p)},
           'reply_bodies_match': all(p['sha'] == d['sha'] for p, d in pairs) if pairs else None}
    if case.get('events'):
        out['added_seconds_per_event'] = quantiles([t / case['events'] for t in total])
    else:
        out['proxied_upload_mb_per_s'] = quantiles([case['body'] / 1e6 / p['total'] for p, _ in pairs if p['total']])
    return out


def cases(quick):
    if quick:
        return [{'name': 'body_1KB', 'body': 1_000}, {'name': 'stream_10', 'body': 1_000, 'events': 10}]
    return ([{'name': f'body_{n // 1000}KB' if n < 1_000_000 else f'body_{n // 1_000_000}MB', 'body': n} for n in BODIES] +
            [{'name': f'stream_{n}', 'body': 1_000, 'events': n} for n in STREAMS])


def run(quick=False, repeats=5, clients=CLIENTS):
    upstream = UpstreamServer(('127.0.0.1', 0), Upstream)
    upstream.streams = {n: stream(n) for n in STREAMS}
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    results = []
    try:
        for governed in (False, True):
            with tempfile.TemporaryDirectory() as tmp:
                proxy, port = start_proxy(upstream.server_port, tmp, governed)
                rows = []
                try:
                    for case in cases(quick):
                        for n in (clients if not quick else (1,)):
                            if case['body'] >= 10_000_000 and n > 4:
                                continue  # 16 uploads of 10-30 MB at once measure the machine's memory, not the proxy
                            rows.append(dict(case, clients=n, governed=governed,
                                             **measure(case, upstream.server_port, port, n, repeats)))
                finally:
                    cpu, peak = stop(proxy)
                log_rows = len(Path(tmp, 'log.jsonl').read_text().splitlines())
            results.append({'governed': governed, 'cases': rows, 'proxy_cpu_seconds': round(cpu, 3),
                            'proxy_peak_kib': peak, 'log_rows': log_rows,
                            'requests': sum(r['requests'] for r in rows),
                            'failed_proxied_requests': sum(len(r['failed']['proxied']) for r in rows),
                            'slow_proxied_connects': sum(r['slow_connects']['proxied'] for r in rows)})
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join()
    return {'machine': {'system': platform.system(), 'release': platform.release(), 'cpus': os.cpu_count(),
                        'python': platform.python_version()},
            'repeats': repeats, 'runs': results,
            'scope': 'loopback only: the proxy against an in-process upstream that answers at once; $0'}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--quick', action='store_true', help='two small cases, one client')
    parser.add_argument('--repeats', type=int, default=5)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    result = run(args.quick, args.repeats)
    text = json.dumps(result, indent=1)
    if args.out:
        with args.out.open('x') as f:
            f.write(text + '\n')
    print(text)


if __name__ == '__main__':
    main()
