"""Offline end-to-end check of M2-M5 with the governor (October 15, overnight cloud run; docs/governor.md, "Offline
end-to-end check"). One scripted multi-turn session, everything dry-run: the pinned 2.1.284 client -> a recording relay
-> ProxyServer with the governor flags -> a recording relay -> the scripted FixtureServer, with the governor's hooks in
an isolated settings file and the M2 MCP server for expand_output. Beside the session, in the same ledger and governor
session: an M3 worker draft through governed_transport, M4's exact-span verifier on two drafts, one opt-in
execute_fallback, an M5 idle boundary crossed on an injected clock, a coalesced setting change, compaction_check on the
proxy's context notes, and a budget halt after a row of unknown cost.

It checks that the bytes forwarded equal the bytes received both ways, that the proxy log, the governor and the client
agree, that every reservation is settled (or orphaned with unknown cost), that the journal is in order, that a plan is
acknowledged only after its change was observed, and that no stale result lands after the correction. $0: a fake key,
loopback only, empty HOME. Skips without node or the pinned client first on PATH. Glue only; no production code."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from modelpilot.bench import rates as rate_table
from modelpilot.fixtures import fixture_server
from modelpilot.governed_session import client_env, correct_after_first_tool, hook_settings
from modelpilot.governor import Governor, execute_fallback, governed_transport, reconcile_log
from modelpilot.hooks import channel_declaration
from modelpilot.jev_route_check import TOKEN_FIELDS, parse_events
from modelpilot.m4 import sha
from modelpilot.proxy import ProxyServer
from modelpilot.workers import Worker

ROOT = Path(__file__).resolve().parents[1]
KEY = 'sk-ant-offline-fixture-not-a-key'
MODEL, FALLBACK = 'claude-sonnet-5-5', 'claude-opus-5-5'
OWNER = 'claude-client'
LIMIT = 50  # dry-run, never enforced on the client; high enough that the proxy's pessimistic reservations fit
TEXT = 'First token: ALPHA_E2E\nSecond token: BRAVO_E2E\n'
MARKER = 'PAGE_MARKER_' + 'E2E'
CORRECTION = 'CORRECTION_MARKER: report the second token instead.'


def client_version():
    claude = shutil.which('claude')
    if not claude or not shutil.which('node'):
        return None
    with tempfile.TemporaryDirectory() as home:
        out = subprocess.run([claude, '--version'], env={'HOME': home, 'PATH': os.environ.get('PATH', '')},
                             capture_output=True, text=True, timeout=60)
    return out.stdout.split()[0] if out.returncode == 0 and out.stdout.strip() else None


VERSION = client_version()


class RelayHandler(socketserver.BaseRequestHandler):
    def handle(self):
        record = {'up': bytearray(), 'down': bytearray()}
        with self.server.lock:
            self.server.connections.append(record)
        upstream = socket.create_connection(self.server.target)

        def pump(source, sink, kept):
            try:
                while True:
                    data = source.recv(65536)
                    if not data:
                        break
                    kept.extend(data)
                    sink.sendall(data)
            except OSError:
                pass
            finally:
                try:
                    sink.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
        down = threading.Thread(target=pump, args=(upstream, self.request, record['down']), daemon=True)
        down.start()
        pump(self.request, upstream, record['up'])
        down.join(30)
        upstream.close()


class Relay(socketserver.ThreadingTCPServer):
    """A loopback TCP relay that keeps every connection's bytes, both ways, unchanged."""
    daemon_threads = True

    def __init__(self, target):
        self.target, self.connections, self.lock = target, [], threading.Lock()
        super().__init__(('127.0.0.1', 0), RelayHandler)


def messages(data, response=False):
    """(start line, headers, body) for each HTTP message in one direction of a connection: Content-Length, chunked
    (de-chunked) or, for a response, close-delimited."""
    out, rest = [], bytes(data)
    while rest:
        head, sep, rest = rest.partition(b'\r\n\r\n')
        if not sep:
            break
        lines = head.split(b'\r\n')
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(b':')
            headers[name.strip().lower()] = value.strip()
        if headers.get(b'transfer-encoding', b'').lower() == b'chunked':
            body = bytearray()
            while True:
                size, _, rest = rest.partition(b'\r\n')
                size = int(size.split(b';')[0], 16)
                if size == 0:
                    _, _, rest = rest.partition(b'\r\n')
                    break
                body.extend(rest[:size])
                rest = rest[size + 2:]
        elif b'content-length' in headers:
            length = int(headers[b'content-length'])
            body, rest = rest[:length], rest[length:]
        else:
            body, rest = (rest, b'') if response else (b'', rest)
        out.append((lines[0], headers, bytes(body)))
    return out


def exchanges(relay):
    """[(request line, request body, status line, response body)], one per request a relay carried."""
    found = []
    with relay.lock:
        connections = list(relay.connections)
    for c in connections:
        for (line, _, body), (status, _, reply) in zip(messages(c['up']), messages(c['down'], response=True)):
            found.append((line, body, status, reply))
    return found


def reply(model, candidate, input_tokens=120, output_tokens=30):
    """A worker/fallback fixture transport's Messages response carrying a JSON draft."""
    return {'model': model, 'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': json.dumps(candidate)}],
            'usage': {'input_tokens': input_tokens, 'output_tokens': output_tokens, 'cache_creation_input_tokens': 0,
                      'cache_read_input_tokens': 0, 'service_tier': 'standard', 'inference_geo': 'global',
                      'cache_creation': {'ephemeral_5m_input_tokens': 0, 'ephemeral_1h_input_tokens': 0}}}


# Input and output dollars per million tokens, written out from configs/ (October 15), not read by the code under test.
BY_HAND = {MODEL: (2, 10), FALLBACK: (4, 20)}


def by_hand(usage, model):
    """A side reply's dollars (no cache tokens), by hand."""
    return (usage['input_tokens'] * BY_HAND[model][0] + usage['output_tokens'] * BY_HAND[model][1]) / 1e6


@unittest.skipUnless(VERSION == '2.1.284', 'needs node and the pinned 2.1.284 client first on PATH')
class EndToEndDryRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.tmp.name)/'run'
        self.dirs = {name: self.run_dir/name for name in ('workspace', 'home', 'tmp', 'config')}
        for path in self.dirs.values():
            path.mkdir(parents=True)
        (self.dirs['workspace']/'a.txt').write_text(TEXT)
        self.db, self.session = self.run_dir/'ledger.sqlite3', 'e2e-' + uuid.uuid4().hex[:8]
        self.rates = rate_table()
        self.fixture = fixture_server()
        self.fixture.keep_bodies = True
        self.fixture.delay = .3  # leaves the coordinator time to correct between tool steps
        self.upstream_relay = Relay(('127.0.0.1', self.fixture.server_port))
        gov = Governor(self.db, self.session, LIMIT)
        self.task = gov.state.create('e2e', 'Report the first token.')['id']
        gov.state.claim(self.task, 1, OWNER, seconds=3600)
        self.code = gov.declare_channel()
        self.page = gov.state.store_output('x' * 6000 + MARKER + 'y' * 6000)
        gov.close()
        self.log = self.run_dir/'observations.jsonl'
        self.proxy = ProxyServer(('127.0.0.1', 0), f'http://127.0.0.1:{self.upstream_relay.server_address[1]}', self.log,
                                 self.rates, governor={'db': self.db, 'session': self.session, 'limit_usd': LIMIT,
                                                       'task': self.task})
        self.client_relay = Relay(('127.0.0.1', self.proxy.server_port))
        self.servers = [self.fixture, self.upstream_relay, self.proxy, self.client_relay]
        self.threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in self.servers]
        for t in self.threads:
            t.start()
        self.gov = Governor(self.db, self.session, LIMIT)

    def tearDown(self):
        self.gov.close()
        for s in reversed(self.servers):
            s.shutdown()
            s.server_close()
        for t in self.threads:
            t.join()
        self.tmp.cleanup()

    def client(self, prompt, effort, resume=None, session_id=None):
        """One invocation of the real client: stream-json events."""
        settings = self.run_dir/'settings.json'
        settings.write_text(json.dumps(hook_settings()))
        bootstrap = f'import sys; sys.path.insert(0, {str(ROOT)!r}); from modelpilot.m2_mcp import main; main()'
        mcp = self.run_dir/'mcp.json'
        mcp.write_text(json.dumps({'mcpServers': {'modelpilot': {'command': sys.executable,
                                                                 'args': ['-c', bootstrap, '--db', str(self.db)]}}}))
        cli = shutil.which('claude')
        binding = {'MODELPILOT_DB': str(self.db), 'MODELPILOT_SESSION': self.session, 'MODELPILOT_LIMIT_USD': str(LIMIT),
                   'MODELPILOT_TASK': self.task, 'MODELPILOT_OWNER': OWNER,
                   'MODELPILOT_HOOK_ERRORS': str(self.run_dir/'hook-errors.jsonl')}
        env = client_env({k: v for k, v in os.environ.items() if k in ('PATH', 'LANG', 'LC_ALL')}, KEY, self.dirs, cli,
                         binding)
        env['ANTHROPIC_BASE_URL'] = f'http://127.0.0.1:{self.client_relay.server_address[1]}'
        tools = 'Read,Write'
        command = [cli, '-p', prompt, '--model', MODEL, '--effort', effort, '--output-format', 'stream-json',
                   '--verbose', '--max-turns', '8', '--max-budget-usd', '1.00',
                   *(['--resume', resume] if resume else ['--session-id', session_id]),
                   '--setting-sources', '', '--settings', str(settings), '--strict-mcp-config', '--mcp-config', str(mcp),
                   '--tools', tools, '--allowedTools', tools + ',mcp__modelpilot__expand_output']
        result = subprocess.run(command, env=env, cwd=self.dirs['workspace'], capture_output=True, text=True,
                                timeout=240, stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])
        self.assertTrue(self.proxy.wait_idle(60))
        return parse_events(result.stdout)

    def rows(self):
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    def test_one_dry_run_session_through_m2_to_m5(self):
        work, gov = self.dirs['workspace'], self.gov
        # M2: the client reads a file, pages a stored output through the MCP server and writes a note; the coordinator
        # corrects the task after the first tool result and a later hook delivers it.
        self.fixture.script = [
            {'tool': 'Read', 'input': {'file_path': str(work/'a.txt')}},
            {'tool': 'mcp__modelpilot__expand_output', 'input': {'handle': self.page['handle'], 'offset': 6000,
                                                                 'limit': 200}},
            {'tool': 'Write', 'input': {'file_path': str(work/'notes.txt'), 'content': 'draft\n'}},
            {'text': 'DONE BRAVO_E2E'},
            {'text': 'DONE'}]
        done, record = threading.Event(), {'issued': False}
        coordinator = threading.Thread(target=correct_after_first_tool, daemon=True,
                                       args=(self.db, self.session, LIMIT, self.task, CORRECTION, done, record))
        coordinator.start()
        client_session = str(uuid.uuid4())
        prompt = (f'{channel_declaration(self.task, self.code)}\n\nTask instruction: Report the first token.\n\n'
                  'Read a.txt, page the stored output, write notes.txt, then reply with the token.')
        try:
            first = self.client(prompt, 'low', session_id=client_session)
        finally:
            done.set()
            coordinator.join()
        self.assertTrue(record['issued'])
        corrected = record['revision']
        self.assertEqual(gov.state.get(self.task)['ack_revision'], corrected)  # delivered and acknowledged
        bodies = self.fixture.bodies
        self.assertTrue(any(b'CORRECTION_MARKER' in b for b in bodies))
        paged = [json.loads(b) for b in bodies if MARKER.encode() in b]
        self.assertTrue(any(block.get('type') == 'tool_result' and MARKER in json.dumps(block)
                            for body in paged for m in body['messages'] if isinstance(m['content'], list)
                            for block in m['content']), 'the MCP page never reached the model as a tool result')
        self.assertIn('BRAVO_E2E', next(e for e in first if e.get('type') == 'result')['result'])

        # No stale result lands: a draft made at the old revision is rejected, an observation of it refused, and no
        # observation of the old revision was recorded after the correction.
        start = TEXT.index('ALPHA_E2E')
        good = {'start': start, 'end': start + 9, 'answer': 'ALPHA_E2E', 'source_sha256': sha(TEXT)}
        old_evidence = {'revision': corrected - 1, 'text': TEXT, 'current_source_sha256': sha(TEXT)}
        self.assertEqual(gov.review(self.task, 'read', good, old_evidence, corrected - 1, .05)['action'], 'reject_stale')
        with self.assertRaises(ValueError):
            gov.observe(self.task, corrected - 1, OWNER, {'progress': True})
        events = gov.db.execute('SELECT seq,revision,kind FROM events WHERE task=? ORDER BY seq', (self.task,)).fetchall()
        corrected_at = next(e['seq'] for e in events if e['kind'] == 'corrected')
        self.assertFalse([e['seq'] for e in events if e['kind'] == 'observation' and e['revision'] < corrected
                          and e['seq'] > corrected_at])

        # M3/M4: a worker draft through governed_transport, the exact-span verifier on it and on a draft without a span,
        # then one opt-in fallback for the second, admitted (enforced, fenced) and settled.
        sent = []

        def fixture_transport(model, candidate):
            def transport(request, config):
                response = reply(model, candidate)
                sent.append(response)
                return response, 'req_fixture_side'
            return transport
        cascade = gov.state.create('cascade', 'Find the first token in a.txt')['id']
        rev_c = gov.state.claim(cascade, 1, 'coordinator', seconds=3600)['revision']
        evidence = {'revision': rev_c, 'text': TEXT, 'current_source_sha256': sha(TEXT)}
        subtask = gov.state.create('search', 'Find the first token')['id']
        worker = Worker('file_search', root=work, db=self.db, model=MODEL, rates=self.rates,
                        transport=governed_transport(gov, fixture_transport(MODEL, good), self.rates, .01))
        drafted = worker.dispatch(subtask, 1, ['a.txt'], query='First token')
        draft = json.loads(gov.state.expand(drafted['result']['handle'])['text'])
        self.assertEqual(gov.review(cascade, 'read', draft, evidence, rev_c, .05)['action'], 'would_accept')
        spanless = governed_transport(gov, fixture_transport(MODEL, {'answer': 'ALPHA_E2E', 'source_sha256': sha(TEXT)}),
                                      self.rates, .01)({'model': MODEL, 'max_tokens': 64}, {})[0]
        escalated = gov.review(cascade, 'read', json.loads(spanless['content'][0]['text']), evidence, rev_c, .05)
        self.assertEqual((escalated['action'], escalated['reason'], escalated['escalation_affordable']),
                         ('would_escalate', 'source_span_not_verified', True))
        fallback = execute_fallback(gov, escalated, evidence, lambda d: {'model': FALLBACK, 'max_tokens': 64},
                                    lambda r: json.loads(r['content'][0]['text']), fixture_transport(FALLBACK, good),
                                    self.rates, allow_calls=True)
        self.assertEqual((fallback['action'], fallback['fallback']['executed']), ('would_accept', True))
        fallback_row = gov.db.execute('SELECT status,actual,task,revision FROM gov_reservations WHERE request_id=?',
                                      (fallback['fallback']['request_id'],)).fetchone()
        self.assertEqual((fallback_row['status'], fallback_row['task'], fallback_row['revision']), ('settled', cascade, rev_c))
        side_usd = sum(by_hand(r['usage'], r['model']) for r in sent)
        self.assertEqual(len(sent), 3)

        # M5: a coalesced setting change and a queued compaction; the idle boundary crossed on an injected clock (no
        # sleep), the compaction checked on the proxy's context notes; the plan waits for the client to be seen at it.
        last_stop = gov.last_note('client_stop')
        revision = gov.state.get(self.task)['revision']
        gov.queue_change('effort', 'high', revision)
        gov.queue_change('effort', 'medium', revision)  # coalesced: the latest per kind
        gov.queue_change('compact', True, revision)
        self.assertEqual(sorted((c['kind'], c['value']) for c in gov.pending_changes()),
                         [('compact', True), ('effort', 'medium')])
        early = Governor(self.db, self.session, LIMIT, clock=lambda: last_stop['created'] + 300)
        try:
            self.assertEqual(early.plan_rebase(revision, idle_seconds=300)['action'], 'defer')  # inside 330 s
        finally:
            early.close()
        clocked = Governor(self.db, self.session, LIMIT, clock=lambda: last_stop['created'] + 400)
        try:
            plan = clocked.plan_rebase(revision, idle_seconds=clocked.clock() - last_stop['created'])
        finally:
            clocked.close()
        self.assertEqual((plan['action'], plan['trigger'], plan['cache_ttl'], plan['idle_threshold']),
                         ('would_rebase', 'idle', '5m', 330))
        self.assertEqual([(c['kind'], c['value']) for c in plan['changes']], [('effort', 'medium')])
        contexts = [e['payload'] for e in gov.journal('context')]
        self.assertTrue(contexts)
        self.assertEqual((plan['compaction_check']['verdict'], plan['compaction_check']['prefix_tokens'],
                          plan['deferred_changes']), ('does_not_pay', contexts[-1]['prefix_tokens'], ['compact']))
        self.assertEqual([p['plan_id'] for p in gov.outstanding_plans()], [plan['plan_id']])

        final = self.client('Reply with only the word DONE.', 'medium', resume=client_session)
        self.assertEqual(gov.outstanding_plans(), [])
        acknowledged = gov.journal('acknowledge_rebase')
        self.assertEqual([(a['payload']['plan_id'], a['payload']['applied_by']) for a in acknowledged],
                         [(plan['plan_id'], 'observed')])
        self.assertEqual([c['kind'] for c in gov.pending_changes()], ['compact'])  # deferred, still queued
        journal = gov.journal()
        planned = next(e['seq'] for e in journal if e['kind'] == 'plan_rebase' and e['payload']['plan_id'] == plan['plan_id'])
        seen = next(e['seq'] for e in journal if e['kind'] == 'observe_setting' and e['seq'] > planned
                    and e['payload'] == {'kind': 'effort', 'value': 'medium', 'applied': False})
        self.assertLess(seen, acknowledged[0]['seq'])  # acknowledged only after the change was observed on the wire
        medium = [r for r in self.rows() if r.get('kind') == 'messages' and r.get('tool_count') and r.get('effort') == 'medium']
        self.assertTrue(medium)

        # The client's totals before the side request below.
        client_rows = [r for r in self.rows() if r.get('kind') == 'messages']
        result = next(e for e in final if e.get('type') == 'result')
        usage = result.get('modelUsage') or {}
        client_tokens = {wire: sum(m.get(name, 0) for m in usage.values()) for wire, name in TOKEN_FIELDS}
        proxy_tokens = {wire: sum((r.get('usage') or {}).get(wire, 0) for r in client_rows) for wire, _ in TOKEN_FIELDS}
        self.assertEqual(proxy_tokens, client_tokens)  # the resumed session's cumulative totals
        self.assertTrue(all(r['cost_usd'] is not None and r['governor_status'] == 'settled' for r in client_rows))
        proxy_usd = sum(r['cost_usd'] for r in client_rows)
        self.assertAlmostEqual(gov.policy()['spent_usd'], proxy_usd + side_usd, places=9)

        # M5's budget halt: one request through the proxy whose reply has no usage (cost unknown) halts admission.
        body = json.dumps({'model': MODEL, 'max_tokens': 8, 'messages': [{'role': 'user', 'content': 'x'}],
                           'metadata': {'test_no_usage': True}}).encode()
        sock = socket.create_connection(('127.0.0.1', self.client_relay.server_address[1]), timeout=10)
        sock.sendall(b'POST /v1/messages HTTP/1.1\r\nHost: proxy\r\nContent-Type: application/json\r\n'
                     b'x-api-key: ' + KEY.encode() + b'\r\nanthropic-version: 2023-06-01\r\n'
                     b'Content-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
        while sock.recv(65536):
            pass
        sock.close()
        self.assertTrue(self.proxy.wait_idle(30))
        unknown = self.rows()[-1]
        self.assertEqual((unknown['cost_usd'], unknown['usage_problem'], unknown['governor_status']),
                         (None, 'no_usage_field', 'settled'))
        policy = gov.policy()
        self.assertEqual((policy['mode'], policy['cost_complete']), ('halt', False))
        self.assertEqual(gov.admit('after-halt', .01)['reason'], 'cost_unknown')
        halted = gov.review(cascade, 'read', {'answer': 'x'}, evidence, rev_c, .05)
        self.assertEqual(halted['action'], 'would_defer')
        before = len(sent)
        self.assertEqual(execute_fallback(gov, halted, evidence, lambda d: {'model': FALLBACK}, json.loads,
                                          fixture_transport(FALLBACK, good), self.rates, allow_calls=True)['action'],
                         'would_defer')
        self.assertEqual(len(sent), before)  # nothing sent once cost is unknown

        # Dry-run changed nothing: every request and every reply crossed the proxy byte for byte.
        near, far = exchanges(self.client_relay), exchanges(self.upstream_relay)
        # The client's connectivity check (HEAD /api/hello, once per invocation) is answered by the proxy itself (501:
        # no HEAD handler) and never forwarded or logged; every POST is forwarded and logged once.
        local = [(line, status) for line, _, status, _ in near if not line.startswith(b'POST ')]
        self.assertEqual(local, [(b'HEAD /api/hello HTTP/1.1', b"HTTP/1.0 501 Unsupported method ('HEAD')")] * 2)
        near = [x for x in near if x[0].startswith(b'POST ')]
        self.assertEqual(len(near), len(self.rows()))
        self.assertEqual(sorted(hashlib.sha256(b).hexdigest() for _, b, _, _ in near),
                         sorted(hashlib.sha256(b).hexdigest() for _, b, _, _ in far))
        replies = {}
        for _, request, status, response in far:
            replies.setdefault(request, []).append((status.split(b' ')[1], response))
        for _, request, status, response in near:
            upstream_status, upstream_body = replies[request].pop(0)
            self.assertEqual(status.split(b' ')[1], upstream_status)
            self.assertEqual(response, upstream_body)
        self.assertEqual([b for _, b, _, _ in far], self.fixture.bodies)

        # Every reservation settled (unknown ones with no price); the log reconciles with nothing left open.
        reconciled = reconcile_log(gov, self.log)
        self.assertEqual(reconciled['orphans_without_rows'], 0)
        statuses = gov.db.execute('SELECT status,actual FROM gov_reservations WHERE session=?', (self.session,)).fetchall()
        self.assertFalse([s for s in statuses if s['status'] == 'pending'])
        self.assertTrue(all(s['actual'] is None for s in statuses if s['status'] == 'orphaned'))
        self.assertEqual(reconciled['still_unknown'], 1)  # the no-usage row, and nothing else
        # One reservation per proxy row and per side call; the refused 'after-halt' reserved nothing.
        ids = {r['request_id'] for r in gov.db.execute('SELECT request_id FROM gov_reservations WHERE session=?',
                                                         (self.session,))}
        proxied = {r['governor_request_id'] for r in self.rows()}
        self.assertLessEqual(proxied, ids)
        self.assertEqual(len(ids), len(proxied) + len(sent))

        # The journal is in order and nothing was applied.
        journal = gov.journal()
        seqs = [e['seq'] for e in journal]
        self.assertEqual(seqs, sorted(set(seqs)))
        admitted = set()
        for e in journal:
            self.assertIsNot(e['payload'].get('applied'), True, e)
            if e['kind'] == 'admit':
                admitted.add(e['payload']['request_id'])
            if e['kind'] == 'settle':
                self.assertIn(e['payload']['request_id'], admitted)
        delivered = [e for e in journal if e['kind'] == 'deliver_correction']
        self.assertTrue(delivered and all(e['revision'] == corrected for e in delivered))
        self.assertTrue(all(not r.get('applied') for r in self.rows()))
        self.assertFalse((self.run_dir/'hook-errors.jsonl').exists())


if __name__ == '__main__':
    unittest.main()
