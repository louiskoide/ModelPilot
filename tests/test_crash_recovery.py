"""Crash and kill recovery (October 15, overnight cloud run). Processes are killed with SIGKILL at the worst points: the
proxy mid-stream after forwarding some bytes and before logging, a hook while it writes to M2, a process between the
governor's admit and its settle, and processes racing on one ledger. After each, the ledger passes SQLite's
integrity_check, no reservation is settled twice, open reservations expire to orphaned with unknown cost and
admission halts until they are reconciled with measured evidence (reconcile_log), and the journal reads back. Every
reader of the proxy's JSONL logs takes a cut last line as a request of unknown cost: no crash, never $0. $0: loopback
only, no client, no model request."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import random
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from modelpilot import bench, bench_report, exploration, long_session, midtask_compaction
from modelpilot.governor import Governor, reconcile_log
from modelpilot.proxy import read_log

ROOT = Path(__file__).resolve().parents[1]
MODEL = 'claude-sonnet-4-6'
RATES = {MODEL: dict(input=3, output=15, read=.3, write_5m=3.75, write_1h=6)}
USAGE = {'input_tokens': 10, 'output_tokens': 20, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0,
         'cache_creation': {'ephemeral_5m_input_tokens': 0, 'ephemeral_1h_input_tokens': 0}}
COST = (10 * 3 + 20 * 15) / 1e6
ENV = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'PYTHONPATH': str(ROOT), 'PYTHONDONTWRITEBYTECODE': '1'}


def priced(started, request_id=None):
    row = {'kind': 'messages', 'model': MODEL, 'effort': 'low', 'http_status': 200, 'status': 'ok', 'tool_count': 2,
           'started_unix': started, 'usage': dict(USAGE), 'cost_usd': COST}
    if request_id:
        row.update(governor_request_id=request_id, governor_status='settled')
    return row


def write_log(path, rows, cut=None):
    """JSON lines, then (cut) the first part of one more row, as a proxy killed mid-write leaves it."""
    with open(path, 'w') as f:
        for r in rows:
            f.write(json.dumps(r) + '\n')
        if cut is not None:
            f.write(json.dumps(cut)[:37])


def integrity(db):
    connection = sqlite3.connect(db)
    try:
        return connection.execute('PRAGMA integrity_check').fetchone()[0]
    finally:
        connection.close()


def settles_per_request(gov):
    counts = {}
    for e in gov.journal('settle'):
        counts[e['payload']['request_id']] = counts.get(e['payload']['request_id'], 0) + 1
    return counts


def later(gov, seconds):
    """The governor's clock moved on (no sleep): reservations whose ttl has passed expire."""
    gov.clock = lambda: time.time() + seconds
    return gov


class CutLogTests(unittest.TestCase):
    """Every reader of a proxy log, on a log whose last line was cut mid-write."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_log_turns_a_cut_line_into_a_row_of_unknown_cost(self):
        log = self.root/'observations.jsonl'
        write_log(log, [priced(1)], cut=priced(2))
        rows = read_log(log)
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[1]['kind'], rows[1]['cost_usd'], rows[1]['status']), ('messages', None, 'unreadable_log_row'))
        accounting = bench.accounting(rows, {})
        self.assertIsNone(accounting['cost_usd'])  # unknown, never the first row's cost alone
        self.assertEqual(accounting['unpriced_requests'], 1)
        self.assertIsNone(bench_report.cache_attribution(rows, RATES)['measured_cost_usd'])
        (self.root/'bytes.jsonl').write_bytes(json.dumps(priced(1)).encode() + b'\n{"usage": "\xe2\x82')  # cut mid-character
        self.assertEqual(len(read_log(self.root/'bytes.jsonl')), 2)
        self.assertEqual(read_log(self.root/'missing.jsonl'), [])

    def trial(self, run, record):
        trial = run/'t'/record['arm']/'0'
        trial.mkdir(parents=True)
        (trial/'trial.json').write_text(json.dumps(record))
        write_log(trial/'observations.jsonl', [priced(1)], cut=priced(2))
        return trial

    def test_the_run_readers_take_a_cut_line_as_unknown_without_crashing(self):
        """Fixed October 15: each of these raised JSONDecodeError on a cut last line."""
        run = self.root/'bench-x'
        run.mkdir()
        (run/'manifest.json').write_text(json.dumps({'arms': ['arm'], 'seed': 0, 'shape': 'split'}))
        record = {'task': 't', 'arm': 'arm', 'trial': 0, 'complete': True, 'passed': True, 'shape': 'split',
                  'sessions': [{'rows': [0, 2], 'prompt': 'plan', 'subtask': 0}]}
        self.trial(run, record)
        _, records = bench_report.load_run(run, RATES)
        self.assertIsNone(records[0]['cache']['measured_cost_usd'])
        self.assertIsNone(records[0]['cache']['cold_equivalent_cost_usd'])
        self.assertEqual(records[0]['cost_components']['unpriced_requests'], 1)
        (turn,), = [t for _, t in long_session.load_run(run, RATES)]
        self.assertIsNone(turn['cost_usd'])
        (_, breakdown), = midtask_compaction.load_run(run, RATES)
        self.assertIsNone(breakdown['turn_cost_usd']['plan'])
        explore = self.root/'bench-y'
        self.trial(explore, dict(record, arm=exploration.ARM, cache={'cold_equivalent_cost_usd': None}))
        view, = exploration.load([explore], RATES)
        self.assertIsNone(view['cold_equivalent_usd'])

    def test_reconcile_log_reads_past_a_cut_line_and_leaves_its_reservation_unknown(self):
        gov = Governor(self.root/'state.db', 's', 1)
        try:
            for request_id in ('mp-a', 'mp-b'):
                gov.admit(request_id, .01, enforce=False)
            log = self.root/'observations.jsonl'
            write_log(log, [priced(1, 'mp-a')], cut=priced(2, 'mp-b'))
            result = reconcile_log(gov, log)
            self.assertEqual((result['rows'], result['settled']), (1, 1))
            later(gov, 700)
            policy = gov.policy()
            self.assertEqual((policy['cost_complete'], policy['spent_usd']), (False, COST))  # mp-b: orphaned, unknown
        finally:
            gov.close()


class Upstream(BaseHTTPRequestHandler):
    """message_start, then a ping every 50 ms for 5 s: a long stream to kill the proxy in."""
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers['Content-Length']))
        start = {'type': 'message_start', 'message': {'id': 'm', 'type': 'message', 'role': 'assistant', 'model': MODEL,
                                                      'content': [], 'usage': dict(USAGE, output_tokens=1)}}
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Connection', 'close')
        self.end_headers()
        try:
            self.wfile.write(b'event: message_start\ndata: ' + json.dumps(start).encode() + b'\n\n')
            self.wfile.flush()
            for _ in range(100):
                time.sleep(.05)
                self.wfile.write(b'event: ping\ndata: {"type": "ping"}\n\n')
                self.wfile.flush()
        except OSError:
            pass


def start_proxy(log, db, upstream_port):
    config = Path(tempfile.mkdtemp())/'config.json'
    config.write_text(json.dumps({'rates': RATES}))
    proxy = subprocess.Popen([sys.executable, '-m', 'modelpilot.proxy', '--port', '0', '--config', str(config),
                              '--log', str(log), '--upstream', f'http://127.0.0.1:{upstream_port}', '--governor-db', str(db),
                              '--governor-session', 's', '--governor-limit-usd', '5'],
                             env=ENV, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    line = proxy.stdout.readline()
    return proxy, int(line.split('http://127.0.0.1:')[1].split(';')[0])


def request(port, stream=True):
    body = json.dumps({'model': MODEL, 'max_tokens': 8, 'stream': stream, 'messages': []}).encode()
    sock = socket.create_connection(('127.0.0.1', port), timeout=10)
    sock.sendall(b'POST /v1/messages HTTP/1.1\r\nHost: proxy\r\nContent-Type: application/json\r\n'
                 b'Content-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
    return sock


class KillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root/'state.db'

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_proxy_killed_mid_stream_leaves_an_orphan_that_halts_until_measured(self):
        upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        log = self.root/'observations.jsonl'
        proxy, port = start_proxy(log, self.db, upstream.server_port)
        try:
            sock = request(port)
            got = b''
            while b'message_start' not in got:
                got += sock.recv(4096)  # the client has bytes; the proxy hasn't logged the row
            os.kill(proxy.pid, signal.SIGKILL)
            proxy.wait(10)
            sock.close()
        finally:
            if proxy.poll() is None:
                proxy.kill()
            proxy.stdout.close()
            upstream.shutdown()
            upstream.server_close()
        self.assertEqual(read_log(log), [])  # killed before its row
        self.assertEqual(integrity(self.db), 'ok')
        gov = Governor(self.db, 's', 5)
        try:
            pending = [r['request_id'] for r in gov.db.execute("SELECT request_id FROM gov_reservations WHERE status='pending'")]
            self.assertEqual(len(pending), 1)
            later(gov, 700)
            self.assertEqual((gov.policy()['mode'], gov.policy()['cost_complete']), ('halt', False))
            self.assertEqual(gov.admit('next', .01)['reason'], 'cost_unknown')
            reconciled = reconcile_log(gov, log)
            self.assertEqual((reconciled['orphans_without_rows'], reconciled['still_unknown']), (1, 1))  # no evidence yet
            write_log(log, [priced(1, pending[0])])  # measured evidence for it, e.g. from the provider's console
            self.assertEqual(reconcile_log(gov, log)['settled'], 1)
            self.assertTrue(gov.policy()['cost_complete'])
            self.assertTrue(gov.admit('next-2', .01)['admitted'])
            self.assertEqual(max(settles_per_request(gov).values()), 1)
            self.assertTrue(gov.journal())  # reads back
        finally:
            gov.close()

    def test_a_process_killed_between_admit_and_settle(self):
        child = subprocess.Popen([sys.executable, '-c', (
            'import sys, time\nfrom modelpilot.governor import Governor\n'
            'gov = Governor(sys.argv[1], "s", 1)\ngov.admit("r1", .1, ttl=60)\nprint("admitted", flush=True)\n'
            'time.sleep(30)\ngov.settle("r1", .05)\n'), str(self.db)], env=ENV, cwd=ROOT, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'admitted')
            os.kill(child.pid, signal.SIGKILL)
            child.wait(10)
        finally:
            if child.poll() is None:
                child.kill()
            child.stdout.close()
        self.assertEqual(integrity(self.db), 'ok')
        gov = Governor(self.db, 's', 1)
        try:
            self.assertTrue(gov.policy()['cost_complete'])  # still within its ttl: pending, counted as reserved
            later(gov, 61)
            self.assertEqual(gov.policy()['mode'], 'halt')
            row = gov.db.execute("SELECT status,actual FROM gov_reservations WHERE request_id='r1'").fetchone()
            self.assertEqual((row['status'], row['actual']), ('orphaned', None))
            write_log(self.root/'log.jsonl', [priced(1, 'r1')])
            self.assertEqual(reconcile_log(gov, self.root/'log.jsonl')['settled'], 1)
            gov.settle('r1', COST)  # the same measured value again: idempotent
            with self.assertRaises(ValueError):
                gov.settle('r1', .2)  # a conflicting one is refused
            self.assertEqual(settles_per_request(gov), {'r1': 1})
            self.assertEqual(gov.db.execute("SELECT reconciled FROM gov_reservations WHERE request_id='r1'").fetchone()[0], 1)
            self.assertEqual(gov.policy()['mode'], 'normal')
        finally:
            gov.close()

    def test_a_writer_killed_inside_an_m2_transaction_leaves_nothing_half_written(self):
        gov = Governor(self.db, 's', 1)
        task = gov.state.create('t', 'x')['id']
        gov.close()
        child = subprocess.Popen([sys.executable, '-c', (
            'import sys, time\nfrom modelpilot.m2 import State\nstate = State(sys.argv[1])\n'
            'state.db.execute("BEGIN IMMEDIATE")\n'
            'state._event(sys.argv[2], 1, "observation", {"file": "half-written"})\n'
            'print("inside", flush=True)\ntime.sleep(30)\n'), str(self.db), task], env=ENV, cwd=ROOT,
            stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'inside')
            os.kill(child.pid, signal.SIGKILL)
            child.wait(10)
        finally:
            if child.poll() is None:
                child.kill()
            child.stdout.close()
        self.assertEqual(integrity(self.db), 'ok')
        gov = Governor(self.db, 's', 1)
        try:
            self.assertFalse([e for e in gov.db.execute("SELECT payload FROM events") if 'half-written' in e[0]])
            gov.state.claim(task, 1, 'client')  # writable again: the dead writer's lock is gone
        finally:
            gov.close()

    def test_hooks_killed_at_random_while_writing_to_m2(self):
        gov = Governor(self.db, 's', 1)
        task = gov.state.create('t', 'x')['id']
        revision = gov.state.claim(task, 1, 'client', seconds=3600)['revision']
        gov.state.acknowledge(task, revision, 'client')
        gov.close()
        work = self.root/'work'
        work.mkdir()
        (work/'a.py').write_text('x = 1\n')
        env = dict(ENV, MODELPILOT_DB=str(self.db), MODELPILOT_SESSION='s', MODELPILOT_LIMIT_USD='1',
                   MODELPILOT_TASK=task, MODELPILOT_OWNER='client', MODELPILOT_HOOK_ERRORS=str(self.root/'errors.jsonl'))
        payload = json.dumps({'hook_event_name': 'PostToolUse', 'tool_name': 'Write', 'session_id': 'c', 'cwd': str(work),
                              'tool_input': {'file_path': str(work/'a.py'), 'content': 'x = 1\n'}})
        rng = random.Random(15)
        killed = finished = 0
        for i in range(24):
            hook = subprocess.Popen([sys.executable, '-m', 'modelpilot.hooks', 'PostToolUse'], env=env, cwd=ROOT,
                                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            hook.stdin.write(payload.encode())
            hook.stdin.close()
            time.sleep(rng.uniform(0, .25))
            if hook.poll() is None:
                os.kill(hook.pid, signal.SIGKILL)
                killed += 1
            else:
                finished += 1
            hook.wait(10)
        self.assertTrue(killed and finished, (killed, finished))  # both outcomes happened
        self.assertEqual(integrity(self.db), 'ok')
        gov = Governor(self.db, 's', 1)
        try:
            for (payload_text,) in gov.db.execute('SELECT payload FROM events'):
                json.loads(payload_text)
            journal = gov.journal()  # reads back
            self.assertTrue(all(isinstance(e['payload'], dict) for e in journal))
            row = gov.state.get(task)
            self.assertEqual((row['status'], row['revision'], row['ack_revision']), ('in_progress', revision, revision))
            self.assertTrue(gov.state.observations(task, revision, 'file', limit=1))
        finally:
            gov.close()

    def test_processes_racing_on_one_ledger_one_killed(self):
        script = ('import os, sys\nfrom modelpilot.governor import Governor\n'
                  'gov = Governor(sys.argv[1], "s", 100)\n'
                  'for i in range(int(sys.argv[2])):\n'
                  '    rid = f"{os.getpid()}-{i}"\n'
                  '    gov.admit(rid, .001, ttl=60)\n'
                  '    gov.settle(rid, .001)\n'
                  'print("done", flush=True)\n')
        Governor(self.db, 's', 100).close()
        racers = [subprocess.Popen([sys.executable, '-c', script, str(self.db), '60'], env=ENV, cwd=ROOT,
                                   stdout=subprocess.PIPE, text=True) for _ in range(4)]
        time.sleep(.6)
        victim = racers[0]
        if victim.poll() is None:
            os.kill(victim.pid, signal.SIGKILL)
        outcomes = [r.wait(120) for r in racers]
        for r in racers:
            r.stdout.close()
        self.assertTrue(all(code == 0 for code in outcomes[1:]), outcomes)
        self.assertEqual(integrity(self.db), 'ok')
        gov = Governor(self.db, 's', 100)
        try:
            rows = gov.db.execute('SELECT request_id,status,actual FROM gov_reservations').fetchall()
            settled = [r for r in rows if r['status'] == 'settled']
            self.assertGreaterEqual(len(settled), 3 * 60)
            self.assertEqual(max(settles_per_request(gov).values()), 1)  # never settled twice
            self.assertEqual(set(settles_per_request(gov)), {r['request_id'] for r in settled})
            self.assertAlmostEqual(gov.policy()['spent_usd'], sum(r['actual'] for r in settled))
            later(gov, 61)
            policy = gov.policy()
            orphans = gov.db.execute("SELECT actual FROM gov_reservations WHERE status='orphaned'").fetchall()
            self.assertLessEqual(len(orphans), 1)  # at most the victim's one in flight
            self.assertTrue(all(o['actual'] is None for o in orphans))
            self.assertEqual(policy['cost_complete'], not orphans)
        finally:
            gov.close()


if __name__ == '__main__':
    unittest.main()
