import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from modelpilot.fixture_dispatch import ProxyPolicy
from modelpilot.fixtures import fixture_server
from modelpilot.governed_session import OWNER, run_session
from modelpilot.proxy import ProxyServer

S, O = 'claude-sonnet-5', 'claude-opus-5'
RATES = {S: dict(input=2, output=10, read=.2, write_5m=2.5, write_1h=4),  # configs/jev-rates.json
         O: dict(input=5, output=25, read=.5, write_5m=6.25, write_1h=10)}
FAIL = {'tool': 'Bash', 'input': {'command': 'exit 3', 'description': 'fail on purpose'}}


class Upstream:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.upstream = fixture_server()
        self.upstream.keep_bodies = True
        self.thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.upstream.server_port}'

    def tearDown(self):
        self.upstream.shutdown()
        self.upstream.server_close()
        self.thread.join()
        self.tmp.cleanup()


class GateTests(Upstream, unittest.TestCase):
    def test_policy_refuses_anything_but_the_owned_fixture_server(self):
        from http.server import ThreadingHTTPServer
        other = ThreadingHTTPServer(('127.0.0.1', 0), None)
        try:
            with self.assertRaises(ValueError):
                ProxyPolicy(other, S, OWNER)
        finally:
            other.server_close()
        policy = ProxyPolicy(self.upstream, S, OWNER)
        log = Path(self.tmp.name)/'log.jsonl'
        governor = {'db': Path(self.tmp.name)/'db', 'session': 's', 'limit_usd': 1, 'task': None}
        for upstream in ('https://api.anthropic.com', 'http://127.0.0.1:1'):
            with self.assertRaises(ValueError):
                ProxyServer(('127.0.0.1', 0), upstream, log, RATES, governor=governor, policy=policy)
        with self.assertRaises(ValueError):  # a policy needs a governed task to fence on
            ProxyServer(('127.0.0.1', 0), self.url, log, RATES, governor=governor, policy=policy)


@unittest.skipUnless(shutil.which('claude'), 'Claude Code CLI not installed')
class RealClientPolicyTests(Upstream, unittest.TestCase):
    """Real Claude Code client, fake key, owned fixture upstream: $0 and no provider traffic."""
    def run_policy(self, script, **kwargs):
        self.upstream.script = script
        run = Path(self.tmp.name)/'run'
        report = run_session(run, shutil.which('claude'), 'sk-ant-offline-fixture-not-a-key', self.url, RATES,
                             prompt='Run the checks and report.', instruction='Make the check pass.',
                             **dict(dict(model=S, effort='medium', tools='Bash', max_turns=12, limit_usd=5,
                                         policy_upstream=self.upstream), **kwargs))
        bodies = [json.loads(b) for b in self.upstream.bodies]
        main = [(b['model'], (b.get('output_config') or {}).get('effort')) for b in bodies if b.get('tools')]
        return run, report, bodies, main

    def test_ladder_escalates_effort_then_model_and_keeps_each_setting(self):
        run, report, bodies, main = self.run_policy([FAIL]*6 + [{'text': 'DONE'}])
        self.assertEqual(report['status'], 'passed', report)
        # Three identical failures -> one effort rung; three more -> one model rung. Each kept afterwards.
        self.assertEqual(main, [(S, 'medium')]*3 + [(S, 'high')]*3 + [(O, 'medium')], main)
        self.assertEqual([(e['action'], e['status']) for e in report['policy']['escalations']],
                         [('increase_effort', 'fixture_confirmed'), ('stronger_model', 'fixture_confirmed')])
        self.assertEqual(report['policy']['kept_requests'], 2)
        self.assertTrue(report['accounting_matches'], report)
        self.assertEqual(report['governor']['still_unknown'], 0)
        # Measured: 6 Sonnet 5 requests + 1 Opus 5 request (100 in / 4 out each).
        self.assertAlmostEqual(report['governor']['spent_usd'], 6*.00024 + .0006)
        # The client saw claude-opus-5 served but priced every request as the Sonnet 5 it asked for.
        self.assertFalse(report['client_cost_matches'])
        self.assertAlmostEqual(report['client']['total_cost_usd'], 7*.00024)
        rows = [json.loads(line) for line in (run/'observations.jsonl').read_text().splitlines()]
        applied = [r for r in rows if r.get('applied')]
        self.assertEqual(len(applied), 4)
        self.assertTrue(all(r['mode'] == 'fixture-policy' and r['request_sha256'] != r['client_request_sha256']
                            for r in applied))
        self.assertTrue(all(r['governor_status'] == 'settled' for r in rows if r.get('kind') == 'messages'))
        # What the client asked for never changed; only the forwarded copy did.
        self.assertEqual(report['client']['result_subtype'], 'success')

    def test_correction_resets_the_kept_setting_with_the_ladder(self):
        self.upstream.delay = .5  # leaves the coordinator time to correct between tool steps
        work = Path(self.tmp.name)/'run'/'workspace'
        read = {'tool': 'Read', 'input': {'file_path': str(work/'a.txt')}}
        run, report, bodies, main = self.run_policy([FAIL]*3 + [read, read, {'text': 'DONE'}], tools='Bash,Read',
                                                    files={'a.txt': 'notes\n'},
                                                    correction='CORRECTION_MARKER: report the notes instead.')
        self.assertEqual(report['status'], 'passed', report)
        self.assertEqual(main[:4], [(S, 'medium')]*3 + [(S, 'high')], main)
        self.assertTrue(report['correction']['delivered'] and report['correction']['acknowledged'], report)
        # The corrected revision starts at level 0 with the client's own setting.
        self.assertEqual(main[-1], (S, 'medium'), main)
        self.assertIn(b'CORRECTION_MARKER', self.upstream.bodies[-1])
        self.assertTrue(report['accounting_matches'], report)

    def test_unaffordable_escalation_forwards_the_client_request_unchanged(self):
        run, report, bodies, main = self.run_policy([FAIL]*3 + [{'text': 'DONE'}], limit_usd=1e-6)
        self.assertEqual(main, [(S, 'medium')]*4, main)
        self.assertEqual(report['policy']['escalations'], [])
        self.assertIn('insufficient_write_reservation', report['policy']['deferrals'])
        self.assertFalse(report['applied'])
        self.assertTrue(report['client_cost_matches'], report)
        self.assertEqual(report['status'], 'passed', report)


if __name__ == '__main__':
    unittest.main()


class ProxyPolicyPathTests(Upstream, unittest.TestCase):
    """The proxy's policy path driven by plain HTTP requests, without a client."""
    def setUp(self):
        super().setUp()
        from modelpilot.governor import Governor
        self.db = Path(self.tmp.name)/'ledger.sqlite3'
        gov = Governor(self.db, 's', 5)
        self.task = gov.state.create('t', 'fix')['id']
        gov.state.claim(self.task, 1, OWNER, seconds=3600)
        for _ in range(3):
            gov.observe(self.task, 1, OWNER, {'error': 'same failure'})
        gov.close()
        self.upstream.script = [{'text': 'ok'}]
        self.log = Path(self.tmp.name)/'observations.jsonl'
        self.proxy = ProxyServer(('127.0.0.1', 0), self.url, self.log, RATES, policy=ProxyPolicy(self.upstream, S, OWNER),
                                 governor={'db': self.db, 'session': 's', 'limit_usd': 5, 'task': self.task})
        self.proxy_thread = threading.Thread(target=self.proxy.serve_forever, daemon=True)
        self.proxy_thread.start()

    def tearDown(self):
        self.proxy.shutdown()
        self.proxy_thread.join()
        self.proxy.server_close()
        super().tearDown()

    def post(self, **extra):
        import http.client
        body = dict(dict(model=S, max_tokens=64, stream=True, output_config={'effort': 'medium'},
                         tools=[{'name': 'Bash', 'input_schema': {'type': 'object'}}],
                         messages=[{'role': 'user', 'content': 'go'}]), **extra)
        conn = http.client.HTTPConnection('127.0.0.1', self.proxy.server_port, timeout=10)
        try:
            conn.request('POST', '/v1/messages', json.dumps(body), {'Content-Type': 'application/json'})
            conn.getresponse().read()
        finally:
            conn.close()

    def rows(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_unknown_escalation_cost_halts_the_policy(self):
        from modelpilot.governor import Governor
        self.upstream.truncate_models = {S}
        self.post()  # escalation to high, but its stream never completes
        self.upstream.truncate_models = set()
        self.post()
        first, second = self.rows()
        self.assertEqual((first['effort'], first['policy']['status']), ('high', 'unknown_outcome'))
        self.assertIsNone(first['cost_usd'])
        # Unknown spend refuses every later policy admission; the client's own request is forwarded.
        self.assertEqual((second['effort'], second['applied'], second['policy']['reason']), ('medium', False, 'unknown_or_invalid_budget'))
        gov = Governor(self.db, 's', 5)
        try:
            self.assertFalse(gov.policy()['cost_complete'])
            self.assertEqual(gov.state.get(self.task)['level'], 0)  # never confirmed
        finally:
            gov.close()

    def test_side_requests_and_other_models_pass_untouched(self):
        self.post(tools=[])  # no tools: not the main loop
        self.post(model=O)
        self.assertEqual([(r['model'], r['applied'], r['policy']['status']) for r in self.rows()],
                         [(S, False, 'not_main_loop'), (O, False, 'not_main_loop')])

    def test_thinking_history_blocks_the_rewrite(self):
        self.post(messages=[{'role': 'user', 'content': 'go'},
                            {'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': 'x', 'signature': 's'}]},
                            {'role': 'user', 'content': 'again'}])
        row, = self.rows()
        self.assertFalse(row['applied'])
        self.assertIn('Thinking history', row['policy']['reason'])
