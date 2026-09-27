"""The ModelPilot arm's active policy in the proxy: plain HTTP to the owned fixture upstream, $0."""
import http.client
import json
from pathlib import Path
import threading
import unittest
from unittest import mock
from modelpilot.active_policy import ActivePolicy
from modelpilot.governed_session import OWNER
from modelpilot.governor import Governor
from modelpilot.proxy import ProxyServer
from tests.test_policy_session import O, RATES, S, Upstream

ONE = 100*2/1e6 + 4*10/1e6  # one scripted Sonnet 5 reply: 100 input and 4 output tokens


class ActivePolicyTests(Upstream, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.db = Path(self.tmp.name)/'ledger.sqlite3'
        gov = Governor(self.db, 's', 5)
        self.task = gov.state.create('t', 'fix')['id']
        gov.state.claim(self.task, 1, OWNER, seconds=3600)
        gov.close()
        self.upstream.script = [{'text': 'ok'}]
        self.upstream.keep_bodies = True
        self.log = Path(self.tmp.name)/'observations.jsonl'
        self.proxy = None

    def start(self, limit=5):
        # The ledger records one limit per session, so each test uses a fresh session for its limit.
        self.session = f's{limit}'
        self.proxy = ProxyServer(('127.0.0.1', 0), self.url, self.log, RATES, policy=ActivePolicy(S, OWNER),
                                 governor={'db': self.db, 'session': self.session, 'limit_usd': limit, 'task': self.task})
        self.proxy_thread = threading.Thread(target=self.proxy.serve_forever, daemon=True)
        self.proxy_thread.start()

    def tearDown(self):
        if self.proxy:
            self.proxy.shutdown()
            self.proxy_thread.join()
            self.proxy.server_close()
        super().tearDown()

    def gov(self):
        return Governor(self.db, self.session, self.proxy.governor['limit_usd'])

    def stuck(self, times=3):
        gov = self.gov()
        try:
            for _ in range(times):
                gov.observe(self.task, 1, OWNER, {'error': 'same failure'})
        finally:
            gov.close()

    def post(self, **extra):
        body = dict(dict(model=S, max_tokens=64, stream=True, output_config={'effort': 'medium'},
                         tools=[{'name': 'Bash', 'input_schema': {'type': 'object'}}],
                         messages=[{'role': 'user', 'content': 'go'}]), **extra)
        conn = http.client.HTTPConnection('127.0.0.1', self.proxy.server_port, timeout=10)
        try:
            conn.request('POST', '/v1/messages', json.dumps(body), {'Content-Type': 'application/json'})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def rows(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def sent(self):
        return [(json.loads(b)['model'], json.loads(b)['output_config']['effort']) for b in self.upstream.bodies]

    def test_a_live_upstream_is_accepted_for_the_active_policy_only(self):
        server = ProxyServer(('127.0.0.1', 0), 'https://api.anthropic.com', self.log, RATES, policy=ActivePolicy(S, OWNER),
                             governor={'db': self.db, 'session': 's', 'limit_usd': 5, 'task': self.task})
        server.server_close()  # constructed only: nothing is sent
        with self.assertRaises(ValueError):  # the proxy's own origin rule still applies
            ProxyServer(('127.0.0.1', 0), 'https://example.com', self.log, RATES, policy=ActivePolicy(S, OWNER),
                        governor={'db': self.db, 'session': 's', 'limit_usd': 5, 'task': self.task})

    def test_requests_are_sent_until_measured_spend_reaches_the_limit(self):
        self.start(limit=2.5*ONE)
        # max_tokens=32000 reserves about $0.32 each, far above the limit: the spent gate ignores it.
        statuses = [self.post(max_tokens=32000)[0] for _ in range(4)]
        self.assertEqual(statuses, [200, 200, 200, 400])
        self.assertEqual(len(self.upstream.bodies), 3)  # the refused request never left the proxy
        rows = self.rows()
        self.assertEqual([r['kind'] for r in rows], ['messages']*3 + ['refused'])
        self.assertEqual((rows[-1]['refusal'], rows[-1]['cost_usd'], rows[-1]['governor_status']),
                         ('insufficient_budget', 0.0, 'refused'))
        self.assertTrue(all(r['mode'] == 'active' and r['governor']['enforced'] for r in rows))
        body = self.post()[1]
        self.assertEqual(json.loads(body)['error']['type'], 'invalid_request_error')
        self.assertIn(b'insufficient_budget', body)
        gov = self.gov()
        try:
            self.assertAlmostEqual(gov.policy()['spent_usd'], 3*ONE)
            self.assertEqual(gov.policy()['reserved_usd'], 0)
        finally:
            gov.close()

    def test_side_requests_meet_the_same_limit(self):
        self.start(limit=ONE/2)
        self.assertEqual(self.post(tools=[])[0], 200)
        self.assertEqual(self.post(tools=[])[0], 400)
        self.assertEqual([(r['kind'], r.get('policy', {}).get('status')) for r in self.rows()],
                         [('messages', 'not_main_loop'), ('refused', 'not_main_loop')])

    def test_unknown_cost_halts_every_later_request(self):
        self.start()
        self.upstream.truncate_models = {S}
        self.assertEqual(self.post()[0], 200)  # the stream ends before its usage is complete
        self.upstream.truncate_models = set()
        self.assertEqual(self.post()[0], 400)
        first, second = self.rows()
        self.assertIsNone(first['cost_usd'])
        self.assertEqual((second['kind'], second['refusal']), ('refused', 'cost_unknown'))

    def test_the_ladder_escalates_keeps_each_rung_and_then_stops_the_task(self):
        self.start()
        self.post()
        self.stuck()
        self.post()  # effort rung, sent once and confirmed
        self.post()  # kept
        self.stuck()
        self.post()  # model rung
        self.post()  # kept
        self.stuck()
        status, body = self.post()  # stuck again at the top of the ladder: R2 stops the task
        self.assertEqual(status, 400)
        self.assertIn(b'policy_stop:re_diagnose', body)
        self.assertEqual(self.sent(), [(S, 'medium'), (S, 'high'), (S, 'high'), (O, 'medium'), (O, 'medium')])
        rows = self.rows()
        self.assertEqual([r.get('policy', {}).get('status') for r in rows],
                         ['deferred', 'confirmed', 'kept', 'confirmed', 'kept', 'stop'])
        self.assertEqual((rows[-1]['kind'], rows[-1]['refusal']), ('refused', 'policy_stop:re_diagnose'))
        self.assertNotIn('governor_request_id', rows[-1])  # a stop reserves nothing
        gov = self.gov()
        try:
            self.assertEqual([e['payload']['status'] for e in gov.journal('policy_dispatch')], ['confirmed', 'confirmed'])
            self.assertEqual(len(gov.journal('policy_keep')), 2)
            stop, = gov.journal('policy_stop')
            self.assertEqual((stop['payload']['level'], stop['payload']['model']), (2, O))
            self.assertEqual(gov.journal('fixture_dispatch'), [])
            policy = gov.policy()
            self.assertAlmostEqual(policy['spent_usd'], 3*ONE + 2*(100*4/1e6 + 4*20/1e6))  # 3 Sonnet, 2 Opus 5.5
            self.assertTrue(policy['cost_complete'])
        finally:
            gov.close()

    def test_an_unaffordable_rebuild_keeps_the_client_setting_within_the_limit(self):
        self.start(limit=.001)
        self.stuck()
        # About 10,000 request tokens: the rung's rebuild at the dearest write rate is $0.04, over the limit.
        self.assertEqual(self.post(messages=[{'role': 'user', 'content': 'x' * 30000}])[0], 200)
        row, = self.rows()
        self.assertEqual((row['applied'], row['policy']['reason'], row['governor']['admitted']),
                         (False, 'insufficient_write_reservation', True))
        self.assertEqual(self.sent(), [(S, 'medium')])

    def test_the_output_allowance_is_not_reserved_for_an_escalation(self):
        # Claude Code sends max_tokens=64000: $0.64 of Sonnet 5 output, $1.28 of Opus 5.5, above a $0.10 limit.
        self.start(limit=.1)
        self.stuck()
        self.post(max_tokens=64000)
        self.post(max_tokens=64000)
        self.stuck()
        self.post(max_tokens=64000)
        self.assertEqual(self.sent(), [(S, 'high'), (S, 'high'), (O, 'medium')])
        gov = self.gov()
        try:
            reserves = [e['payload']['estimate_usd'] for e in gov.journal('admit') if e['payload']['admitted']]
        finally:
            gov.close()
        self.assertLess(max(reserves[0], reserves[2]), .001)  # the two rungs reserved their rebuilds only

    def test_a_deferred_escalation_is_recorded_on_the_request_that_keeps_the_rung(self):
        self.start(limit=.001)
        self.stuck()
        self.post()  # effort rung: a small request's rebuild fits
        self.stuck()
        self.post(messages=[{'role': 'user', 'content': 'x' * 30000}])  # the model rung's rebuild does not
        last = self.rows()[-1]
        self.assertEqual((last['effort'], last['policy']['kind'], last['policy']['escalation_deferred']),
                         ('high', 'keep_escalated', 'insufficient_write_reservation'))

    def test_a_governor_failure_fails_closed(self):
        self.start()
        with mock.patch.object(ProxyServer, 'admit', side_effect=RuntimeError('database is locked')):
            self.assertEqual(self.post(tools=[])[0], 400)
        row, = self.rows()
        self.assertEqual((row['kind'], row['refusal'], row['governor_error'], row['governor_status']),
                         ('refused', 'governor_error', 'RuntimeError', 'refused'))
        self.assertEqual(self.upstream.bodies, [])
        gov = self.gov()
        try:
            from modelpilot.governor import reconcile_log
            self.assertEqual(reconcile_log(gov, self.log)['untracked_recorded'], 0)  # never sent, never reserved
        finally:
            gov.close()


if __name__ == '__main__':
    unittest.main()
