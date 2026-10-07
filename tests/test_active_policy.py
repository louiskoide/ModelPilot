"""The ModelPilot arm's active policy in the proxy: plain HTTP to the owned fixture upstream, $0."""
import http.client
import json
from pathlib import Path
import threading
import unittest
from unittest import mock
from modelpilot import policy_actions, switch_policy
from modelpilot.active_policy import ActivePolicy, reviews_at_finish
from modelpilot.governed_session import OWNER
from modelpilot.governor import Governor
from modelpilot.proxy import ProxyServer
from tests.test_policy_session import O, RATES, S, Upstream

H = 'claude-haiku-4-5-20251001'

ONE = 100*2/1e6 + 4*10/1e6  # one scripted Sonnet 5.5 reply: 100 input and 4 output tokens


def jev_only():
    """The shipped policy with calibration off: these tests script Jev's advice to drive the proxy's moves."""
    cfg = switch_policy.load()
    cfg['calibration']['enabled'] = False
    return cfg


class ScriptedAdvisor:
    """Stands in for Jev (advisor.JevAdvisor): returns a scripted answer and records each call."""
    live = False

    def __init__(self):
        self.answer, self.calls, self.queue = None, [], []

    def ask(self, body, current, catalog, efforts, strip_prefix='', evidence=None, dry=False):
        self.calls.append({'current': current, 'efforts': list(efforts), 'evidence': evidence, 'body': body})
        return self.queue.pop(0) if self.queue else self.answer


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

    def start(self, limit=5, client=S, config=None):
        # The ledger records one limit per session, so each test uses a fresh session for its limit.
        self.session = f's{limit}'
        self.advisor = ScriptedAdvisor()
        self.proxy = ProxyServer(('127.0.0.1', 0), self.url, self.log, RATES,
                                 policy=ActivePolicy(client, OWNER, advisor=self.advisor, config=config or jev_only()),
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

    def test_jev_is_asked_only_where_some_answer_could_move(self):
        self.start(config=switch_policy.load())  # the shipped, calibrated policy
        self.advisor.answer = {'model': {'choice': O, 'confidence': .99, 'probabilities': {O: 1}},
                               'effort': {'choice': 'max', 'confidence': .99, 'probabilities': {'max': 1}}}
        self.assertEqual(self.post()[0], 200)
        self.assertEqual(self.advisor.calls, [])  # no answer moves this turn start at jev_weight 0.03
        self.assertEqual(self.sent(), [(S, 'medium')])
        gov = self.gov()
        try:
            decision, = [e['payload'] for e in gov.journal('advisor_decision')]
        finally:
            gov.close()
        self.assertEqual((decision['action'], decision['reason'], decision['advice']), ('stay', 'jev_cannot_change', None))
        self.assertEqual((decision['gate']['can_change'], decision['gate']['reason']), (False, 'every_answer_stays'))
        self.assertGreater(decision['forecast_usd'], 0)  # a later step still compares spend against it

    def test_with_the_gate_off_jev_is_asked_at_every_point(self):
        cfg = switch_policy.load()
        cfg['jev_gate']['enabled'] = False
        self.start(config=cfg)
        self.advisor.answer = {'model': {'choice': S, 'confidence': .9, 'probabilities': {S: 1}}}
        self.assertEqual(self.post()[0], 200)
        self.assertEqual(len(self.advisor.calls), 1)

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
        self.advise(S, 'medium')  # the next turn looks easy, but moving would rewrite ~107K cached tokens
        turn2 = long + [{'role': 'assistant', 'content': 'done'}, {'role': 'user', 'content': 'next'}]
        self.post(model=O, output_config={'effort': 'high'}, messages=turn2)
        self.assertEqual(self.sent(), [(O, 'high')]*2)
        first, second = self.decisions()
        self.assertEqual((second['trigger'], second['action'], second['reason'], second['profile']['warm']),
                         ('turn_start', 'stay', 'current_is_cheapest', True))
        stay, down = second['candidates']
        self.assertEqual(down['setting'], f'{S}/medium')
        self.assertGreater(down['switch_usd'], .2)  # the rewrite outweighs Sonnet's cheaper requests
        self.assertGreater(down['expected_usd'] - down['switch_usd'], 0)
        self.assertLess(down['expected_usd'] - down['switch_usd'], stay['expected_usd'])  # without it, moving would pay

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

    def suite(self, *failures):
        """Host-run test results, as bench_tools.run_tests records them in the ledger."""
        gov = self.gov()
        try:
            for n in failures:
                gov.observe(self.task, 1, OWNER, {'suite': 'suite', 'failures': n})
        finally:
            gov.close()

    def pme_config(self, **changes):
        cfg = jev_only()
        cfg['per_message_effort'].update(dict(enabled=True), **changes)
        return cfg

    @staticmethod
    def convo(rounds, first='go'):
        """A conversation that grows by one tool round each time, as Claude Code's does."""
        messages = [{'role': 'user', 'content': first}]
        for i in range(rounds):
            messages += [{'role': 'assistant', 'content': [{'type': 'tool_use', 'id': f't{i}', 'name': 'Bash', 'input': {}}]},
                         {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': f't{i}', 'content': 'x'}]}]
        return messages

    def forwarded(self):
        return [json.loads(b) for b in self.upstream.bodies]

    def effort_positions(self, body):
        return [(i, m['output_config']['effort']) for i, m in enumerate(body['messages']) if m.get('output_config')]

    ADAPTIVE = {'type': 'adaptive'}

    def test_per_message_effort_keeps_the_top_level_effort_and_puts_each_message_back(self):
        self.start(config=self.pme_config())
        self.advise(S, 'xhigh')
        self.post(messages=self.convo(0), thinking=self.ADAPTIVE)  # turn start: straight to Sonnet xhigh
        self.post(messages=self.convo(1), thinking=self.ADAPTIVE)
        self.post(messages=self.convo(2), thinking=self.ADAPTIVE)
        bodies = self.forwarded()
        self.assertEqual([b['output_config']['effort'] for b in bodies], ['medium'] * 3)  # the cache key never moves
        self.assertEqual([self.effort_positions(b) for b in bodies], [[(0, 'xhigh')]] * 3)  # first, and kept there
        self.assertEqual([policy_actions.effective_effort(b) for b in bodies], ['xhigh'] * 3)
        rows = self.rows()
        self.assertEqual([(r['effort'], r.get('effective_effort')) for r in rows], [('medium', 'xhigh')] * 3)
        turn, = self.decisions()
        self.assertEqual((turn['action'], turn['target']), ('jump', [S, 'xhigh']))
        switch = next(c for c in turn['candidates'] if c['setting'] == f'{S}/xhigh')
        self.assertEqual(switch['switch_usd'], 0)  # an effort change through a message rewrites nothing

    def test_the_message_goes_after_the_clients_own_effort_message(self):
        """Claude Code 2.1.284 carries its --effort value on the note after the prompt; ours must come later to hold."""
        self.start(config=self.pme_config())
        self.advise(S, 'xhigh')
        note = {'role': 'system', 'content': 'env', 'output_config': {'effort': 'medium'}}
        first = self.convo(0) + [note]
        later = first + self.convo(1)[1:]
        self.post(messages=first, thinking=self.ADAPTIVE)
        self.post(messages=later, thinking=self.ADAPTIVE)
        bodies = self.forwarded()
        self.assertEqual([self.effort_positions(b) for b in bodies],
                         [[(1, 'medium'), (2, 'xhigh')], [(1, 'medium'), (2, 'xhigh')]])
        self.assertEqual([policy_actions.effective_effort(b) for b in bodies], ['xhigh', 'xhigh'])
        self.assertEqual([r.get('effective_effort') for r in self.rows()], ['xhigh', 'xhigh'])

    def test_effort_changes_at_the_next_user_turn_not_at_a_step(self):
        """Inside a turn's tool loop an effort change doesn't take effect (per-message-effort probe): a mid-task step
        keeps the turn's effort, and the next user turn can change it."""
        self.start(config=self.pme_config())
        self.advise(S, 'xhigh')
        self.post(messages=self.convo(0), thinking=self.ADAPTIVE)
        self.suite(2, 0)
        for rounds in (1, 2):
            self.post(messages=self.convo(rounds), thinking=self.ADAPTIVE)
        self.advise(S, 'low', effort_p=.9)
        self.post(messages=self.convo(3), thinking=self.ADAPTIVE)  # the third since the decision: a step
        step = self.decisions()[-1]
        self.assertEqual((step['trigger'], step['action'], step['target']), ('step', 'stay', [S, 'xhigh']))
        self.assertEqual(self.effort_positions(self.forwarded()[-1]), [(0, 'xhigh')])  # nothing added mid-turn
        follow_up = self.convo(4) + [{'role': 'user', 'content': 'Now also handle the empty case.'}]
        self.post(messages=follow_up, thinking=self.ADAPTIVE)  # a new user turn: effort can move
        turn = self.decisions()[-1]
        self.assertEqual((turn['trigger'], turn['target']), ('turn_start', [S, 'low']))
        last = self.forwarded()[-1]
        self.assertEqual(self.effort_positions(last), [(0, 'xhigh'), (10, 'low')])  # before the new user message
        self.assertEqual((last['output_config']['effort'], policy_actions.effective_effort(last)), ('medium', 'low'))

    def test_a_model_jump_carries_its_effort_in_a_message(self):
        self.start(config=self.pme_config())
        self.advise(O, 'xhigh')
        self.post(messages=self.convo(0), thinking=self.ADAPTIVE)
        body, = self.forwarded()
        self.assertEqual((body['model'], body['output_config']['effort'], self.effort_positions(body)),
                         (O, 'medium', [(0, 'xhigh')]))

    def test_a_rewritten_history_drops_the_old_messages(self):
        self.start(config=self.pme_config())
        self.advise(S, 'xhigh')
        self.post(messages=self.convo(0), thinking=self.ADAPTIVE)
        # Claude Code resending an earlier system note as a string is not a rewrite: the message stays.
        noted = self.convo(1) + [{'role': 'system', 'content': [{'type': 'text', 'text': 'n', 'cache_control': {'type': 'ephemeral'}}]}]
        again = self.convo(2)
        again[3:3] = [{'role': 'system', 'content': 'n'}]
        self.post(messages=noted, thinking=self.ADAPTIVE)
        self.post(messages=again, thinking=self.ADAPTIVE)
        self.assertEqual([self.effort_positions(b) for b in self.forwarded()[-2:]], [[(0, 'xhigh')]] * 2)
        compacted = self.convo(2, first='summary of the earlier conversation')
        self.post(messages=compacted, thinking=self.ADAPTIVE)
        # Dropped; mid-turn a new one would not take effect, so the next user turn sets the effort again.
        self.assertEqual(self.effort_positions(self.forwarded()[-1]), [])

    def test_without_adaptive_thinking_or_when_off_the_top_level_effort_is_used(self):
        self.start(config=self.pme_config())
        self.advise(S, 'xhigh')
        self.post(messages=self.convo(0))  # no thinking field: per-message effort is not available
        body, = self.forwarded()
        self.assertEqual((body['output_config']['effort'], self.effort_positions(body)), ('xhigh', []))

    def test_a_configured_beta_is_added_to_the_clients(self):
        beta = 'mid-conversation-output-config-2026-07-01'
        self.start(config=self.pme_config(beta=beta))
        self.advise(S, 'xhigh')
        self.post(messages=self.convo(0), thinking=self.ADAPTIVE)
        self.assertEqual(self.upstream.received[-1]['beta'], beta)

    def test_the_suite_passing_is_a_step_that_can_move_back_down_at_the_turns_effort(self):
        self.start()
        self.advise(O, 'xhigh')
        self.post(messages=self.TURN)  # the turn looks hard: straight to Opus xhigh
        self.suite(2, 0)  # the fix works
        self.post(messages=self.CONTINUE)
        self.post(messages=self.CONTINUE)  # two requests since the turn decision: too soon for a step
        self.advise(S, 'medium')
        self.post(messages=self.CONTINUE)  # the third: a step, and the rest looks routine
        self.post(messages=self.CONTINUE)
        # Back to Sonnet, at the turn's effort: inside the tool loop the effort can't change.
        self.assertEqual(self.sent(), [(O, 'xhigh')]*3 + [(S, 'xhigh')]*2)
        turn, step = self.decisions()
        self.assertEqual((step['trigger'], step['action'], step['target'], step['step']['cause'], step['step']['requests']),
                         ('step', 'jump', [S, 'xhigh'], 'tests_now_pass', 3))
        self.assertIn(f'{O}/xhigh', step['profile']['warm_entries'])
        self.assertEqual(len(self.advisor.calls), 2)
        self.assertIn('the test suite now passes', self.advisor.calls[-1]['evidence'])
        self.assertEqual([(d['trigger'], d['status']) for d in self.dispatches()],
                         [('turn_start', 'confirmed'), ('step', 'confirmed')])
        self.assertEqual(self.rows()[-1]['policy']['kind'], 'keep_escalated')

    def test_the_suite_failing_again_is_a_step(self):
        self.start()
        self.advise(S, 'medium')
        self.post(messages=self.TURN)
        self.suite(0, 3)
        self.advise(O, 'xhigh')
        for _ in range(3):
            self.post(messages=self.CONTINUE)
        self.assertEqual(self.sent(), [(S, 'medium')]*3 + [(O, 'medium')])  # Opus, at the turn's effort
        step = self.decisions()[-1]
        self.assertEqual((step['step']['cause'], step['action']), ('tests_now_fail', 'jump'))
        self.assertIn('the test suite now fails', self.advisor.calls[-1]['evidence'])

    def test_the_agents_own_test_runs_flipping_is_a_step(self):
        """Plan item 2: agents test through Bash (the host tool ran in 2 of 28 trials), so their runs, read from the
        conversation by summary line, are a step too, marked as the agent's."""
        def run(i, output):
            return [{'role': 'assistant', 'content': [{'type': 'tool_use', 'id': f'b{i}', 'name': 'Bash',
                                                       'input': {'command': 'python3 -m pytest -q 2>&1 | tail -3'}}]},
                    {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': f'b{i}', 'content': output}]}]
        failing = self.TURN + run(0, '1 failed, 4 passed in 0.1s')
        self.start()
        self.advise(O, 'xhigh')
        self.post(messages=self.TURN)
        self.post(messages=failing)
        self.post(messages=failing + run(1, 'Ran 1 test in 0.01s'))  # cut off before its outcome: not a run
        self.advise(S, 'medium')
        self.post(messages=failing + run(1, 'Ran 1 test in 0.01s') + run(2, '5 passed in 0.1s'))
        self.assertEqual(self.sent(), [(O, 'xhigh')]*3 + [(S, 'xhigh')])
        turn, step = self.decisions()
        self.assertEqual((turn['agent_runs'], step['agent_runs']), (0, 2))
        self.assertEqual((step['point'], step['step']['cause'], step['step']['source'], step['action']),
                         ('1/step/agent_tests/2', 'tests_now_pass', 'agent', 'jump'))
        self.assertIn("the agent's own test run now passes", self.advisor.calls[-1]['evidence'])

    def test_spending_past_the_forecast_is_a_step_limited_per_revision(self):
        cfg = jev_only()
        cfg['step'].update(overrun_factor=1e-3, max_per_revision=1)  # one scripted reply overruns 0.1% of the forecast
        self.start(config=cfg)
        self.advise(S, 'medium')
        self.post(messages=self.TURN)
        for _ in range(9):
            self.post(messages=self.CONTINUE)
        turn, step = self.decisions()  # the cap: one step per revision
        self.assertEqual((step['trigger'], step['action'], step['reason'], step['step']['cause']),
                         ('step', 'stay', 'current_is_cheapest', 'spend_overrun'))
        self.assertAlmostEqual(step['step']['spent_usd'], 3*ONE)
        self.assertAlmostEqual(step['step']['forecast_usd'], turn['forecast_usd'])
        self.assertIn('since the last decision (forecast $', self.advisor.calls[-1]['evidence'])
        self.assertEqual(self.sent(), [(S, 'medium')]*10)

    def test_steps_can_be_turned_off(self):
        cfg = jev_only()
        cfg['step'].update(enabled=False)
        self.start(config=cfg)
        self.advise(S, 'medium')
        self.post(messages=self.TURN)
        self.suite(2, 0)
        for _ in range(4):
            self.post(messages=self.CONTINUE)
        self.assertEqual([d['trigger'] for d in self.decisions()], ['turn_start'])

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
        self.advise(S, 'xhigh')
        self.post(messages=self.TURN)  # a small request's rebuild fits
        self.stuck()
        big = self.CONTINUE + [{'role': 'user', 'content': 'x' * 30000}]
        self.post(messages=big)  # the only stronger setting in the turn, Opus xhigh: its rebuild does not fit
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


    # Delegation (docs/m6-modelpilot-policy.md, "Delegation"): consults and handoff notes the proxy sends itself.

    def delegating(self, consult=None, note=None):
        cfg = jev_only()
        return switch_policy.with_overrides(cfg, {'delegation': {'consult': dict({'enabled': False}, **(consult or {})),
                                                                 'handoff_note': dict({'enabled': False}, **(note or {}))}})

    def side_bodies(self):
        return [json.loads(b) for b in self.upstream.bodies if not json.loads(b).get('stream')]

    def main_bodies(self):
        return [json.loads(b) for b in self.upstream.bodies if json.loads(b).get('stream')]

    def delegated(self):
        gov = self.gov()
        try:
            return [e['payload'] for e in gov.journal('delegation')]
        finally:
            gov.close()

    def review_run(self, cfg, limit=5):
        """A turn start, two more requests, the host-run suite passing for the first time, then a request."""
        self.start(limit=limit, config=cfg)
        self.advise(S, 'medium')
        self.post(messages=self.TURN)
        for _ in range(2):
            self.post(messages=self.CONTINUE)
        self.suite(0)
        return self.post(messages=self.CONTINUE)

    def test_a_forced_review_consults_once_and_its_advice_stays_in_place(self):
        status, _ = self.review_run(self.delegating(consult={'enabled': True, 'force': ['tests_pass']}))
        self.assertEqual(status, 200)
        # Jev says the current setting is enough, so the forced review goes to the strongest model at Jev's effort.
        consult, = self.side_bodies()
        self.assertEqual((consult['model'], consult['output_config'], consult['stream']), (O, {'effort': 'medium'}, False))
        self.assertNotIn('tools', consult)
        brief = consult['messages'][0]['content']
        self.assertIn('## Task\nfix', brief)
        self.assertIn('passes. Review the change', brief)
        step = self.decisions()[-1]
        self.assertEqual((step['trigger'], step['step']['cause'], step['action'], step['reason'], step['consult']),
                         ('step', 'tests_pass', 'consult', 'forced', [O, 'medium']))
        self.assertEqual(step['target'], [S, 'medium'])  # the main conversation stays where it is
        entry, = self.delegated()
        self.assertEqual((entry['kind'], entry['status'], entry['delivered'], entry['advice']),
                         ('consult', 'ok', True, 'synthetic output'))
        fourth = self.main_bodies()[-1]
        self.assertEqual(fourth['messages'][3]['role'], 'system')
        self.assertIn('synthetic output', fourth['messages'][3]['content'])
        # Later requests carry it at the same place; the suite passing again is no new review.
        self.suite(0)
        later = self.CONTINUE + [{'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 't2', 'name': 'Bash', 'input': {}}]},
                                 {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't2', 'content': 'y'}]}]
        for _ in range(3):
            self.post(messages=later)
        fifth = self.main_bodies()[-1]
        self.assertEqual(fifth['messages'][:4], fourth['messages'])
        self.assertEqual(len(self.side_bodies()), 1)
        self.assertEqual([self.rows()[i]['policy'].get('deliveries') for i in (-1,)], [1])
        # The consult is billed: a side_call row the governor admitted and settled, outside the client's own rows.
        side, = [r for r in self.rows() if r['kind'] == 'side_call']
        self.assertEqual((side['purpose'], side['model'], side['status'], side['governor_status']),
                         ('consult', O, 'ok', 'settled'))
        # Requests forwarded at the client's setting with the advice carried are admitted and settled too.
        self.assertEqual({r['governor_status'] for r in self.rows() if r['kind'] == 'messages'}, {'settled'})
        self.assertGreater(side['cost_usd'], 0)
        gov = self.gov()
        try:
            main = sum(r['cost_usd'] for r in self.rows() if r['kind'] == 'messages')
            self.assertAlmostEqual(gov.policy()['spent_usd'], main + side['cost_usd'])
        finally:
            gov.close()

    def test_jev_chooses_who_answers_the_brief(self):
        cfg = self.delegating(consult={'enabled': True, 'force': ['tests_pass']})
        self.start(config=cfg)
        self.advise(S, 'medium')
        self.post(messages=self.TURN)
        for _ in range(2):
            self.post(messages=self.CONTINUE)
        self.suite(0)
        step = self.advisor.answer
        self.advise(S, 'xhigh')  # the brief, read on its own, needs only Sonnet 5.5 at xhigh
        self.advisor.queue = [step, self.advisor.answer]
        self.post(messages=self.CONTINUE)
        consult, = self.side_bodies()
        self.assertEqual((consult['model'], consult['output_config']['effort']), (S, 'xhigh'))
        self.assertIn('## Task', self.advisor.calls[-1]['body']['messages'][0]['content'])
        entry, = self.delegated()
        self.assertEqual((entry['gate_setting'], entry['setting']), ([O, 'medium'], [S, 'xhigh']))

    def test_a_consult_past_the_limit_is_never_sent(self):
        self.review_run(self.delegating(consult={'enabled': True, 'force': ['tests_pass']}), limit=3*ONE)
        self.assertEqual(self.side_bodies(), [])
        entry, = self.delegated()
        self.assertEqual((entry['status'], entry['delivered'], entry['cost_usd']), ('refused', False, None))
        side, = [r for r in self.rows() if r['kind'] == 'side_call']
        self.assertEqual((side['status'], side['refusal'], side['cost_usd']), ('refused', 'insufficient_budget', 0.0))

    def test_without_delegation_a_passing_suite_is_no_step(self):
        self.review_run(jev_only())
        self.assertEqual([d['trigger'] for d in self.decisions()], ['turn_start'])
        self.assertEqual(self.side_bodies(), [])

    # Claude Code 2.1.284 continues a turn whose Stop hook blocked with the reason as a user message, then its own
    # trailing system note (captured offline against the fixture, October 5).
    FINISHED = CONTINUE + [{'role': 'assistant', 'content': [{'type': 'text', 'text': 'Done.'}]}]
    HELD = FINISHED + [{'role': 'user', 'content': 'Stop hook feedback:\n[ModelPilot] Task t is unchanged.'},
                       {'role': 'system', 'content': [{'type': 'text', 'text': 'Stop hook blocking error from command: "x": '
                                                       '[ModelPilot] Task t is unchanged.', 'cache_control': {'type': 'ephemeral'}}]}]

    def hold_finish(self):
        """What hooks.review_block journals when it holds the agent's finish."""
        gov = self.gov()
        try:
            gov.note('review_block', {'revision': 1, 'stop_hook_active': False}, self.task, 1)
        finally:
            gov.close()

    def test_a_held_finish_is_reviewed_on_the_request_that_continues_the_turn(self):
        self.start(config=self.delegating(consult={'enabled': True, 'force': ['agent_finish']}))
        self.advise(S, 'medium')
        self.post(messages=self.TURN)
        self.post(messages=self.CONTINUE)  # one request since the turn start: no ordinary step could be due
        self.hold_finish()
        status, _ = self.post(messages=self.HELD)
        self.assertEqual(status, 200)
        # A review step, not a turn start, though the request ends with a user message that isn't a tool result.
        self.assertEqual([d['trigger'] for d in self.decisions()], ['turn_start', 'step'])
        step = self.decisions()[-1]
        self.assertEqual((step['step']['cause'], step['action'], step['reason'], step['consult']),
                         ('agent_finish', 'consult', 'forced', [O, 'medium']))
        self.assertEqual(step['step']['requests'], 2)  # the turn start's own request and the one after it
        self.assertIn('the agent ended its turn', self.advisor.calls[1]['evidence'])
        consult, = self.side_bodies()
        brief = consult['messages'][0]['content']
        self.assertIn('The agent has ended its turn and is about to finish.', brief)
        self.assertIn('The agent has not run the host test tool yet.', brief)  # the review doesn't need run_tests
        # The advice joins Claude Code's trailing note after the block's reason; the client's breakpoint moves onto it.
        held = self.main_bodies()[-1]['messages']
        self.assertEqual(len(held), len(self.HELD))
        blocks = held[-1]['content']
        self.assertEqual(blocks[0], {'type': 'text', 'text': self.HELD[-1]['content'][0]['text']})
        self.assertIn('synthetic output', blocks[1]['text'])
        self.assertEqual(blocks[1]['cache_control'], {'type': 'ephemeral'})
        # The agent works on: the advice stays in place, and there is no second review in this turn.
        later = self.HELD + [{'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 't9', 'name': 'Bash', 'input': {}}]},
                             {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't9', 'content': 'y'}]}]
        self.post(messages=later)
        self.assertEqual([b['text'] for b in self.main_bodies()[-1]['messages'][len(self.HELD) - 1]['content']],
                         [b['text'] for b in blocks])
        self.assertEqual(len(self.side_bodies()), 1)
        entry, = self.delegated()
        self.assertEqual((entry['trigger'], entry['delivered'], entry['point']), ('step', True, step['point']))
        self.assertTrue(step['point'].startswith('1/step/finish/'))

    def test_a_held_finish_never_restarts_a_stopped_task(self):
        cfg = self.delegating(consult={'enabled': True, 'force': ['agent_finish']})
        self.start(client=O, config=cfg)
        self.advise(O, 'max')
        self.post(model=O, output_config={'effort': 'max'}, messages=self.TURN)
        self.stuck()
        self.assertEqual(self.post(model=O, output_config={'effort': 'max'}, messages=self.CONTINUE)[0], 400)
        self.hold_finish()  # in case the client runs its Stop hook after the refusal
        status, body = self.post(model=O, output_config={'effort': 'max'}, messages=self.HELD)
        self.assertEqual(status, 400)
        self.assertIn(b'policy_stop:no_stronger_setting', body)
        self.assertNotIn('step', [d['trigger'] for d in self.decisions()])
        self.assertEqual(self.side_bodies(), [])

    def test_without_the_finish_review_a_held_finish_is_a_turn_start(self):
        self.start(config=self.delegating(consult={'enabled': True, 'force': ['tests_pass']}))
        self.assertFalse(reviews_at_finish(self.proxy.policy.config))
        self.advise(S, 'medium')
        self.post(messages=self.TURN)
        self.hold_finish()
        self.post(messages=self.HELD)
        self.assertEqual([d['trigger'] for d in self.decisions()], ['turn_start', 'turn_start'])
        self.assertEqual(self.side_bodies(), [])

    def test_the_delegate_arm_reviews_at_the_finish_and_the_modelpilot_arm_never_does(self):
        from modelpilot.bench import ARMS
        arm = lambda name: switch_policy.with_overrides(switch_policy.load(), ARMS[name].get('policy_overrides'))
        self.assertTrue(reviews_at_finish(arm('modelpilot-delegate')))
        self.assertFalse(reviews_at_finish(arm('modelpilot')))
        self.assertFalse(reviews_at_finish(switch_policy.with_overrides(
            switch_policy.load(), {'delegation': {'consult': {'enabled': True, 'force': ['agent_finish'],
                                                              'at': ['stuck_evidence']}}})))

    def test_a_handoff_note_travels_with_a_mid_task_switch(self):
        self.start(config=self.delegating(note={'enabled': True}))
        self.advise(S, 'medium')
        self.post(messages=self.TURN)
        self.stuck()
        self.advise(O, 'medium')
        self.post(messages=self.CONTINUE)
        note, = self.side_bodies()
        # The model being left, on the request it would have sent: its cached prefix, the tools, and the instruction.
        self.assertEqual((note['model'], note['output_config'], note['tools']), (S, {'effort': 'medium'}, self.main_bodies()[0]['tools']))
        self.assertEqual(note['messages'][:3], self.CONTINUE)
        self.assertIn('Do not call any tool', note['messages'][3]['content'])
        self.assertEqual(self.sent()[-1], (O, 'medium'))
        first = self.main_bodies()[-1]
        self.assertIn('which wrote this handoff note', first['messages'][3]['content'])
        self.assertIn('synthetic output', first['messages'][3]['content'])
        later = self.CONTINUE + [{'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 't2', 'name': 'Bash', 'input': {}}]},
                                 {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't2', 'content': 'y'}]}]
        self.post(messages=later)
        self.assertEqual(self.main_bodies()[-1]['messages'][:4], first['messages'])
        entry, = self.delegated()
        self.assertEqual((entry['kind'], entry['source'], entry['target'], entry['delivered']),
                         ('handoff_note', [S, 'medium'], [O, 'medium'], True))
        side, = [r for r in self.rows() if r['kind'] == 'side_call']
        self.assertEqual((side['purpose'], side['model'], side['tool_count']), ('handoff_note', S, 1))

    def test_a_turn_start_jump_carries_no_note(self):
        self.start(config=self.delegating(note={'enabled': True}))
        self.advise(O, 'xhigh')
        self.post(messages=self.TURN)  # nothing has been done yet: nothing to hand off
        self.assertEqual(self.sent(), [(O, 'xhigh')])
        self.assertEqual(self.side_bodies(), [])




class WarmEntryReachTests(unittest.TestCase):
    """A warm entry counts for a return only within the measured reach (runs/thinking-probe-returns-20260930-091128)."""
    def test_entries_need_to_be_warm_and_within_reach(self):
        policy = ActivePolicy(S, OWNER)
        reach, ttl = policy.config['return_reuse']['max_positions'], policy.config['cache_ttl_seconds']
        policy.sent('t', (S, 'medium'), 1000.0, 9000, 40)
        policy.sent('t', (O, 'xhigh'), 1010.0, 9500, 50)
        # With per-message effort a model's cache doesn't depend on effort, so each model's newest entry is also '/*'.
        self.assertEqual(policy.entries('t', 1020.0, 40 + reach),
                         {f'{S}/medium': 9000, f'{O}/xhigh': 9500, f'{S}/*': 9000, f'{O}/*': 9500})
        self.assertEqual(policy.entries('t', 1020.0, 41 + reach), {f'{O}/xhigh': 9500, f'{O}/*': 9500})  # too far
        self.assertEqual(policy.entries('t', 1000.0 + ttl + 1, 50), {f'{O}/xhigh': 9500, f'{O}/*': 9500})  # expired
        self.assertEqual(policy.entries('other', 1020.0, 40), {})


class CacheLifetimeTests(unittest.TestCase):
    """Warmth and the write rate follow the lifetime the client's breakpoints ask for (plan item 6)."""
    def request(self, ttl=None):
        control = {'type': 'ephemeral', **({'ttl': ttl} if ttl else {})}
        return {'model': S, 'system': [{'type': 'text', 'text': 'x' * 2800, 'cache_control': control}],
                'tools': [{'name': 'Read'}], 'messages': [{'role': 'user', 'content': 'Fix it.'}]}

    def test_an_hour_entry_stays_warm_past_five_minutes_but_returns_still_count_within_five(self):
        policy = ActivePolicy(S, OWNER)
        five, hour = policy.config['cache_lifetime_seconds']['5m'], policy.config['cache_lifetime_seconds']['1h']
        self.assertEqual((five, hour), (300, 3600))
        policy.sent('a', (S, 'low'), 1000.0, 9000, 10, '1h')
        policy.sent('b', (S, 'low'), 1000.0, 9000, 10)  # no marker: the configured 5m
        self.assertTrue(policy.warm('a', (S, 'low'), 1000.0 + 600))
        self.assertFalse(policy.warm('b', (S, 'low'), 1000.0 + 600))
        self.assertFalse(policy.warm('a', (S, 'low'), 1000.0 + hour + 1))
        self.assertEqual(policy.entries('a', 1000.0 + 600, 10), {})  # returns: measured for 5m entries only

    def test_hour_writes_are_priced_at_the_hour_rate(self):
        from modelpilot.active_policy import write_ttl
        cfg = switch_policy.load()
        self.assertEqual((write_ttl(self.request('1h')), write_ttl(self.request())), ('1h', None))
        hour = switch_policy.profile(cfg, self.request('1h'), False, write_ttl='1h')
        five = switch_policy.profile(cfg, self.request(), False)
        self.assertEqual((hour['write_ttl'], five['write_ttl']), ('1h', '5m'))
        rates = {S: RATES[S]}
        self.assertGreater(switch_policy.run_cost(cfg, rates, (S, 'low'), hour),
                           switch_policy.run_cost(cfg, rates, (S, 'low'), five))
        warm = dict(hour, warm=True)
        self.assertAlmostEqual(switch_policy.switch_cost(cfg, {O: RATES[O]}, (S, 'low'), (O, 'low'), warm),
                               warm['prefix_tokens'] * (RATES[O]['write_1h'] - RATES[O]['read']) / 1e6)


if __name__ == '__main__':
    unittest.main()
