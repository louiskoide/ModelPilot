"""Bounded overnight M1 soak against a local fake API: zero provider calls."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import json
import os
from pathlib import Path
import resource
import signal
import threading
import time
from .fixtures import fixture_server, response_for
from .proxy import ProxyServer


def atomic_json(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2)+'\n')
    temp.replace(path)


def exercise(port, model, index):
    kind = index % 4
    request = {'model': model, 'stream': kind in (1, 3), 'max_tokens': 32,
               'messages': [{'role': 'user', 'content': f'Synthetic M1 request {index}'}],
               'metadata': {'test_error': kind == 2, 'test_stream_error': kind == 3}}
    raw = json.dumps(request, indent=index % 3).encode()
    expected_status, _, expected_data = response_for(request)
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    start = time.monotonic()
    try:
        conn.request('POST', '/v1/messages', raw,
                     {'content-type': 'application/json', 'x-api-key': 'synthetic-not-a-key'})
        response = conn.getresponse()
        data = response.read()
        if response.status != expected_status or data != expected_data:
            raise AssertionError('Response status or bytes differ from fixture')
    finally:
        conn.close()
    return {'request_sha256': hashlib.sha256(raw).hexdigest(), 'kind': kind,
            'wall_seconds': time.monotonic()-start}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hours', type=float, default=8)
    parser.add_argument('--interval', type=float, default=10)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--config', type=Path, default=Path('configs/m0.json'))
    args = parser.parse_args()
    if not 0 < args.hours <= 12 or not .1 <= args.interval <= 60:
        parser.error('hours must be >0 and <=12; interval .1..60 seconds')
    args.out.mkdir(parents=True, exist_ok=False)
    config = json.loads(args.config.read_text())
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    upstream = fixture_server()
    proxy = ProxyServer(('127.0.0.1', 0), f'http://127.0.0.1:{upstream.server_port}',
                        args.out/'observations.jsonl', config['rates'])
    servers = [upstream, proxy]
    threads = []
    for server in servers:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        threads.append(thread)
    start = time.monotonic()
    status = {'status': 'running', 'pid': os.getpid(), 'started_unix': time.time(),
              'planned_seconds': args.hours*3600, 'actual_api_cost_usd': 0,
              'traffic': 'synthetic_loopback_only', 'requests_verified': 0, 'batches': 0,
              'proxy_port': proxy.server_port, 'upstream_port': upstream.server_port,
              'failures': 0, 'max_batch_request_seconds': 0}
    def publish():
        status['updated_unix'] = time.time()
        status['elapsed_seconds'] = time.monotonic()-start
        status['max_rss_platform_units'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        status['threads'] = threading.active_count()
        atomic_json(args.out/'status.json', status)
    publish()
    try:
        with ThreadPoolExecutor(max_workers=4) as pool, (args.out/'observations.jsonl').open() as log:
            while time.monotonic()-start < args.hours*3600 and not stop.is_set():
                if (args.out/'STOP').exists():
                    stop.set()
                    break
                base = status['requests_verified']
                futures = [pool.submit(exercise, proxy.server_port,
                                       config['models'][(status['batches']+i)%2], base+i) for i in range(4)]
                results = [future.result() for future in futures]
                # Each client sees connection close only after the proxy log is flushed.
                observations = [json.loads(line) for line in log.readlines()]
                if len(observations) != 4:
                    raise AssertionError('Expected one metadata record per request')
                by_hash = {r['request_sha256']: r for r in observations}
                with upstream.lock:
                    received = {r['sha256']: r for r in upstream.received}
                for result in results:
                    h = result['request_sha256']
                    if h not in by_hash or h not in received:
                        raise AssertionError('Request bytes changed or metadata missing')
                    record = by_hash[h]
                    if record['applied'] or record['decision']['applied']:
                        raise AssertionError('Dry-run policy mutated traffic')
                    if result['kind'] in (0, 1):
                        if not record['usage_complete'] or record['usage']['output_tokens'] != 4 or record['cost_usd'] is None:
                            raise AssertionError('Successful response usage/cost missing or incorrect')
                    elif record['cost_usd'] is not None:
                        raise AssertionError('Failed response incorrectly priced as complete')
                if 'synthetic-not-a-key' in json.dumps(observations) or 'Synthetic M1 request' in json.dumps(observations):
                    raise AssertionError('Credential or prompt leaked into log')
                status['requests_verified'] += 4
                status['batches'] += 1
                status['max_batch_request_seconds'] = max(status['max_batch_request_seconds'], *(r['wall_seconds'] for r in results))
                publish()
                stop.wait(min(args.interval, max(0, args.hours*3600-(time.monotonic()-start))))
        status['status'] = 'stopped' if stop.is_set() else 'completed'
    except Exception as e:
        status['status'] = 'failed'
        status['failures'] += 1
        status['error_type'] = type(e).__name__
        # Only locally raised fixed assertion text; no upstream payloads.
        if isinstance(e, AssertionError):
            status['error'] = str(e)
    finally:
        for server in reversed(servers):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)
        publish()
        print(json.dumps(status, indent=2), flush=True)
    if status['status'] == 'failed':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
