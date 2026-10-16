"""modelpilot.timeline on fixture logs (October 15, overnight cloud run): one row per request with its gap, setting,
tokens by class, cost or "unknown", lifetime, idle boundary and write cause; the governor's events interleaved;
totals that keep unknown; no prompt or tool content; a page with no external resources. The last test makes its log
with a governed proxy process in front of fixtures.fixture_server, on loopback. $0."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from modelpilot import timeline
from modelpilot.fixtures import fixture_server
from modelpilot.governor import Governor
from modelpilot.proxy import read_log

ROOT = Path(__file__).resolve().parents[1]

MODEL = 'claude-sonnet-5-5'


def row(started, read, write, ttl='5m', cost=.001, tools=3, effort='low', **extra):
    hour = write if ttl == '1h' else 0
    return dict({'kind': 'messages', 'model': MODEL, 'effort': effort, 'effective_effort': effort, 'http_status': 200,
                 'status': 'ok', 'tool_count': tools, 'started_unix': started, 'wall_seconds': 2.0, 'cache_ttl': [ttl],
                 'usage': {'input_tokens': 7, 'output_tokens': 20, 'cache_read_input_tokens': read,
                           'cache_creation_input_tokens': write,
                           'cache_creation': {'ephemeral_5m_input_tokens': write - hour, 'ephemeral_1h_input_tokens': hour}},
                 'cost_usd': cost}, **extra)


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_causes_boundaries_and_totals(self):
        rows = [row(1000, 0, 3000), row(1010, 3000, 500), row(1012, 0, 50, tools=0),
                row(1400, 0, 3600),  # 388 s after the last main-loop request ended: expired, rebuilt
                row(1410, 3600, 100, effort='high'),  # a new setting: a switch
                row(1420, 3700, 100, effort='high', cost=None, status='transport_error')]
        entries = timeline.annotate(rows)
        self.assertEqual([e['cause'] for e in entries],
                         ['cold start', 'growth', 'side request', 'rebuild after expiry 3,500, growth 100', 'switch', 'growth'])
        self.assertEqual([e['idle_boundary'] for e in entries], [False, False, False, True, False, False])
        page = timeline.render(rows)
        self.assertIn('(after an idle boundary)', page)
        footer = page.split('<tfoot>')[1]
        self.assertIn('>unknown<', footer)  # one unknown cost: the total is unknown, not the known part
        self.assertIn('>42<', footer)  # uncached input over the six requests, all known

    def test_tokens_by_class_and_a_missing_split(self):
        self.assertEqual(timeline.tokens(row(0, 10, 30, ttl='1h')),
                         {'uncached': 7, 'write_5m': 0, 'write_1h': 30, 'read': 10, 'output': 20})
        partial = dict(row(0, 10, 30), usage={'input_tokens': 7, 'cache_creation_input_tokens': 30})
        self.assertEqual(timeline.tokens(partial),
                         {'uncached': 7, 'write_5m': None, 'write_1h': None, 'read': None, 'output': None})

    def test_governor_events_are_interleaved_read_only(self):
        db = self.root/'ledger.sqlite3'
        gov = Governor(db, 's', 5, clock=lambda: 1005)
        task = gov.state.create('t', 'x')['id']
        revision = gov.state.claim(task, 1, 'client', seconds=3600)['revision']
        gov.queue_change('effort', 'high', revision)
        gov.note('client_stop', {}, task)
        gov.close()
        before = db.stat().st_mtime_ns
        events = timeline.governor_events(db)
        self.assertEqual(db.stat().st_mtime_ns, before)
        self.assertEqual([e[1] for e in events], ['change queued', 'turn ended'])
        page = timeline.render([row(1000, 0, 3000), row(1010, 3000, 500)], events)
        self.assertLess(page.index('change queued'), page.index('>growth<'))  # 1005 falls between the two requests

    def test_no_content_and_no_external_resources(self):
        secret = 'PROMPT_TEXT_MUST_NOT_APPEAR'
        rows = [row(1000, 0, 3000, prompt=secret, messages=[{'content': secret}])]
        page = timeline.render(rows)
        self.assertNotIn(secret, page)
        self.assertFalse(re.search(r'<script|<link|src=|@import|url\(', page, re.I))
        log = self.root/'observations.jsonl'
        log.write_text(''.join(json.dumps(r) + '\n' for r in rows) + '{"cut')
        out = self.root/'t.html'
        with mock.patch('sys.argv', ['timeline', str(log), '--out', str(out)]), contextlib.redirect_stdout(io.StringIO()):
            timeline.main()
        self.assertIn('unknown', out.read_text())  # the cut line is a request of unknown cost


def post(port, path, request):
    body = json.dumps(request).encode()
    with socket.create_connection(('127.0.0.1', port), timeout=30) as sock:
        sock.sendall(f'POST {path} HTTP/1.1\r\nHost: proxy\r\nContent-Type: application/json\r\nx-api-key: '
                     f'sk-ant-offline-not-a-key\r\nContent-Length: {len(body)}\r\n\r\n'.encode() + body)
        while sock.recv(65536):
            pass


class ProxyLogTests(unittest.TestCase):
    def test_a_governed_proxy_log_and_its_ledger(self):
        secret = 'PROMPT_FROM_THE_FIXTURE_MUST_NOT_APPEAR'
        upstream = fixture_server()
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, log, db = root/'config.json', root/'observations.jsonl', root/'state.db'
            config.write_text(json.dumps({'rates': {MODEL: dict(input=3, output=15, read=.3, write_5m=3.75, write_1h=6)}}))
            env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'PYTHONPATH': str(ROOT), 'PYTHONDONTWRITEBYTECODE': '1'}
            proxy = subprocess.Popen([sys.executable, '-m', 'modelpilot.proxy', '--port', '0', '--config', str(config),
                                      '--log', str(log), '--upstream', f'http://127.0.0.1:{upstream.server_port}',
                                      '--governor-db', str(db), '--governor-session', 's', '--governor-limit-usd', '5'],
                                     cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            try:
                port = int(proxy.stdout.readline().split('http://127.0.0.1:')[1].split(';')[0])
                message = {'model': MODEL, 'max_tokens': 8, 'messages': [{'role': 'user', 'content': secret}],
                           'tools': [{'name': 'Read', 'description': secret, 'input_schema': {'type': 'object'}}]}
                post(port, '/v1/messages', dict(message, stream=True))
                post(port, '/v1/messages/count_tokens', message)
                gov = Governor(db, 's', 5)
                try:
                    task = gov.state.create('t', secret)['id']
                    revision = gov.state.claim(task, 1, 'client', seconds=3600)['revision']
                    gov.queue_change('effort', 'high', revision)
                    plan = gov.plan_rebase(revision, explicit_boundary=True)
                    gov.acknowledge_rebase(plan['plan_id'], applied_by='observed')
                finally:
                    gov.close()
                post(port, '/v1/messages', dict(message, stream=True))
                post(port, '/v1/messages', dict(message, stream=False, metadata={'test_no_usage': True}))
            finally:
                proxy.send_signal(signal.SIGINT)
                proxy.wait(30)
                proxy.stdout.close()
                upstream.shutdown()
                upstream.server_close()
            out = root/'timeline.html'
            with mock.patch('sys.argv', ['timeline', str(log), '--governor', str(db), '--out', str(out)]), \
                    contextlib.redirect_stdout(io.StringIO()):
                timeline.main()
            page = out.read_text()
            rows = read_log(log)
        self.assertEqual([r['kind'] for r in rows], ['messages', 'count_tokens', 'messages', 'messages'])
        self.assertNotIn(secret, page)
        self.assertEqual(page.count('<tr class="event">'), 3)  # change queued, plan, acknowledged
        self.assertLess(page.index('count_tokens'), page.index('change queued'))
        self.assertLess(page.index('acknowledged: by observed'), page.rindex('>messages<'))
        self.assertIn('plan: explicit_boundary: effort', page)
        footer = page.split('<tfoot>')[1]
        self.assertIn('3 requests', footer)  # count_tokens is free and not a request someone is billed for
        self.assertIn('>unknown<', footer)  # the reply without usage: its cost is unknown, and so is the total
        self.assertEqual(page.count('>none<'), 2)  # the fixture's replies read 14,750 tokens and write none
        # The usage-less reply: five token classes, its cost and its writes; the footer's five totals and its cost.
        self.assertEqual(page.count('>unknown<'), 7 + 6)
        counting = next(line for line in page.split('<tr') if '>count_tokens<' in line)
        self.assertNotIn('unknown', counting)  # free, with no usage to show


if __name__ == '__main__':
    unittest.main()
