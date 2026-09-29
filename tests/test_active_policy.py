"""The ModelPilot arm's active policy in the proxy: plain HTTP to the owned fixture upstream, $0."""
import http.client
import json
from pathlib import Path
import threading
import unittest
from unittest import mock
from modelpilot import policy_actions
from modelpilot.active_policy import ActivePolicy
from modelpilot.governed_session import OWNER
from modelpilot.governor import Governor
from modelpilot.proxy import ProxyServer
from tests.test_policy_session import O, RATES, S, Upstream

H = 'claude-haiku-4-5-20251001'

ONE = 100*2/1e6 + 4*10/1e6  # one scripted Sonnet 5.5 reply: 100 input and 4 output tokens


class ScriptedAdvisor:
    """Stands in for Jev (advisor.JevAdvisor): returns a scripted answer and records each call."""
    live = False

    def __init__(self):
        self.answer, self.calls = None, []

    def ask(self, body, current, catalog, efforts, strip_prefix='', evidence=None, dry=False):
        self.calls.append({'current': current, 'efforts': list(efforts), 'evidence': evidence})
        return self.answer


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

    def start(self, limit=5, client=S):
        # The ledger records one limit per session, so each test uses a fresh session for its limit.
        self.session = f's{limit}'
        self.advisor = ScriptedAdvisor()
        self.proxy = ProxyServer(('127.0.0.1', 0), self.url, self.log, RATES,
                                 policy=ActivePolicy(client, OWNER, advisor=self.advisor),
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

    def advise(self, model, effort, model_p=.9, effort_p=.85):
        """Script Jev's answer: most of the probability on (model, effort), the rest spread evenly."""
        models, efforts = [H, S, O], ['low', 'medium', 'high', 'xhigh', 'max']
        spread = lambda labels, top, p: {x: p if x == top else (1 - p) / (len(labels) - 1) for x in labels}
        self.advisor.answer = {'model': {'choice': model, 'confidence': model_p, 'probabilities': spread(models, model, model_p)},
                               'effort': {'choice': effort, 'confidence': effort_p,
                                          'probabilities': spread(efforts, effort, effort_p)}}

    TURN = [{'role': 'user', 'content': 'go'}]
    CONTINUE = TURN + [{'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 't1', 'name': 'Bash', 'input': {}}]},
                       {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'x'}]}]

    def decisions(self):
        gov = self.gov()
        try:
            return [e['payload'] for e in gov.journal('advisor_decision')]
        finally:
            gov.close()

    def dispatches(self):
        gov = self.gov()
        try:
            return [e['payload'] for e in gov.journal('policy_dispatch')]
        finally:
            gov.close()

    def test_a_turn_start_jumps_straight_to_jevs_setting_and_keeps_it(self):
        self.start()
        self.advise(O, 'xhigh')
        self.post(messages=self.TURN)
        self.post(messages=self.CONTINUE)
        self.post(messages=self.CONTINUE)
        self.assertEqual(self.sent(), [(O, 'xhigh')]*3)  # no Sonnet high or Opus medium on the way
        self.assertEqual(len(self.advisor.calls), 1)  # tool follow-ups are not decision points
        jump, = self.dispatches()
        self.assertEqual((jump['action'], jump['trigger'], jump['status'], jump['source_model'], jump['target_effort']),
                         ('jump', 'turn_start', 'confirmed', S, 'xhigh'))
        decision, = self.decisions()
        self.assertEqual((decision['action'], decision['target'], decision['profile']['warm']), ('jump', [O, 'xhigh'], False))
        self.assertEqual([r['policy']['status'] for r in self.rows()], ['confirmed', 'kept', 'kept'])
        call = self.advisor.calls[0]
        self.assertEqual((call['current'], call['evidence'], call['efforts'][-1]), (S, None, 'max'))

    def test_advice_for_the_current_setting_stays(self):
        self.start()
        self.advise(S, 'medium')
        self.post(messages=self.TURN)
        self.assertEqual(self.sent(), [(S, 'medium')])
        self.assertEqual([(d['action'], d['reason']) for d in self.decisions()], [('stay', 'current_is_cheapest')])
        self.assertEqual(self.dispatches(), [])

    def test_no_advice_stays_and_never_blocks_the_request(self):
        self.start()
        self.advisor.answer = {'error': 'advice_failed: AbortError'}
        self.assertEqual(self.post(messages=self.TURN)[0], 200)
        self.assertEqual(self.sent(), [(S, 'medium')])
        decision, = self.decisions()
        self.assertEqual((decision['action'], decision['reason'], decision['advice']['error']),
                         ('stay', 'advice_unavailable', 'advice_failed: AbortError'))

    def test_a_rejected_typesafe_key_is_recorded_as_an_auth_failure(self):
        self.start()
        self.advisor.answer = {'error': 'advice_failed: APIError', 'status': 401, 'auth': True}
        self.assertEqual(self.post(messages=self.TURN)[0], 200)  # the request itself is never blocked
        decision, = self.decisions()
        self.assertEqual((decision['action'], decision['advice']['status'], decision['advice']['auth']), ('stay', 401, True))

    def test_a_downgrade_in_a_warm_long_session_is_not_worth_its_rewrite(self):
        self.start(client=O)
        long = [{'role': 'user', 'content': 'x' * 300000}]
        self.advise(O, 'high')
        self.post(model=O, output_config={'effort': 'high'}, messages=long)
        self.advise(S, 'medium')  # the next turn looks easy, but moving would rewrite ~75K cached tokens
        turn2 = long + [{'role': 'assistant', 'content': 'done'}, {'role': 'user', 'content': 'next'}]
        self.post(model=O, output_config={'effort': 'high'}, messages=turn2)
        self.assertEqual(self.sent(), [(O, 'high')]*2)
        first, second = self.decisions()
        self.assertEqual((second['trigger'], second['action'], second['reason'], second['profile']['warm'], second['downgrade']),
                         ('turn_start', 'stay', 'not_worth_switching', True, True))
        self.assertGreater(second['required_usd'], second['benefit_usd'])

    def test_stuck_evidence_jumps_to_a_stronger_setting_with_jevs_advice(self):
        self.start()
        self.advise(S, 'medium')
        self.post(messages=self.TURN)  # Jev thinks Sonnet medium is enough: stay
        self.stuck()
        self.post(messages=self.CONTINUE)
        stay, jump = self.decisions()
        self.assertEqual((jump['trigger'], jump['action']), ('stuck_evidence', 'jump'))
        target = tuple(jump['target'])
        self.assertNotEqual(target, (S, 'medium'))
        self.assertEqual(self.sent(), [(S, 'medium'), target])  # one move, straight to the target
        self.assertIn('the same error keeps repeating', self.advisor.calls[-1]['evidence'])
        dispatch, = self.dispatches()
        self.assertEqual((dispatch['trigger'], dispatch['status']), ('stuck_evidence', 'confirmed'))

    def test_stuck_with_nothing_stronger_stops_the_task(self):
        self.start(client=O)
        self.advise(O, 'max')
        self.post(model=O, output_config={'effort': 'max'}, messages=self.TURN)
        self.stuck()
        status, body = self.post(model=O, output_config={'effort': 'max'}, messages=self.CONTINUE)
        self.assertEqual(status, 400)
        self.assertIn(b'policy_stop:no_stronger_setting', body)
        rows = self.rows()
        self.assertEqual((rows[-1]['kind'], rows[-1]['refusal']), ('refused', 'policy_stop:no_stronger_setting'))
        self.assertNotIn('governor_request_id', rows[-1])  # a stop reserves nothing

    def test_an_unaffordable_rebuild_keeps_the_client_setting_within_the_limit(self):
        self.start(limit=.001)
        self.advise(O, 'xhigh')
        # About 10,000 request tokens: the move's rebuild at the dearest write rate is $0.08, over the limit.
        self.assertEqual(self.post(messages=[{'role': 'user', 'content': 'x' * 30000}])[0], 200)
        row, = self.rows()
        self.assertEqual((row['applied'], row['policy']['reason'], row['governor']['admitted']),
                         (False, 'insufficient_write_reservation', True))
        self.assertEqual(self.sent(), [(S, 'medium')])

    def test_the_output_allowance_is_not_reserved_for_a_jump(self):
        # Claude Code sends Sonnet 5.5 max_tokens=128000: $2.56 of Opus 5.5 output, far above a $0.10 limit.
        self.start(limit=.1)
        self.advise(O, 'xhigh')
        self.post(max_tokens=128000, messages=self.TURN)
        self.assertEqual(self.sent(), [(O, 'xhigh')])
        gov = self.gov()
        try:
            reserve, = [e['payload']['estimate_usd'] for e in gov.journal('admit') if e['payload']['admitted']]
        finally:
            gov.close()
        self.assertLess(reserve, .001)  # the jump reserved its rebuild only

    def test_a_deferred_jump_is_recorded_on_the_request_that_keeps_the_setting(self):
        self.start(limit=.001)
        self.advise(O, 'xhigh')
        self.post(messages=self.TURN)  # a small request's rebuild fits
        self.stuck()
        big = self.CONTINUE + [{'role': 'user', 'content': 'x' * 30000}]
        self.post(messages=big)  # the only stronger setting, Opus max: its rebuild does not fit
        last = self.rows()[-1]
        self.assertEqual((last['effort'], last['policy']['kind'], last['policy']['escalation_deferred']),
                         ('xhigh', 'keep_escalated', 'insufficient_write_reservation'))

    @mock.patch.object(policy_actions, 'THINKING_HISTORY_VERIFIED', frozenset({(O, O)}))
    def test_thinking_history_blocks_an_unverified_jump(self):
        self.start()
        self.advise(O, 'xhigh')
        with_thinking = [{'role': 'user', 'content': 'go'},
                         {'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': 'x', 'signature': 's'},
                                                           {'type': 'text', 'text': 'done'}]},
                         {'role': 'user', 'content': 'next'}]
        self.post(messages=with_thinking)
        self.assertEqual(self.sent(), [(S, 'medium')])  # a pair without probe evidence
        row, = self.rows()
        self.assertIn('Thinking history', row['policy']['reason'])

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
