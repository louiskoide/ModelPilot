import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.error
from modelpilot import thinking_probe as tp
from modelpilot.cache_replication import Budget

S, H = 'claude-sonnet-5-5', 'claude-haiku-4-5-20251001'  # S: the policy's middle tier since September 28
O, O5, S5 = 'claude-opus-5-5', 'claude-opus-5', 'claude-sonnet-5'  # O: the top rung; O5, S5: earlier runs' tiers
SHAPE = json.loads(tp.SHAPE_FIXTURE.read_text())
USAGE = {'input_tokens': 50, 'output_tokens': 200, 'cache_creation_input_tokens': 7000, 'cache_read_input_tokens': 0,
         'cache_creation': {'ephemeral_5m_input_tokens': 7000, 'ephemeral_1h_input_tokens': 0},
         'service_tier': 'standard', 'inference_geo': 'global'}
SECRETS = ('PRIVATE-THOUGHT', 'SIG-SECRET', '424242', 'SECRET-ANSWER')


def seed_response(model, tool=True, thinking=True):
    content = [{'type': 'thinking', 'thinking': 'PRIVATE-THOUGHT', 'signature': 'SIG-SECRET'}] if thinking else []
    content.append({'type': 'tool_use', 'id': 'toolu_1', 'name': 'record_answer', 'input': {'value': 424242}}
                   if tool else {'type': 'text', 'text': 'SECRET-ANSWER'})
    return {'model': model, 'stop_reason': 'tool_use' if tool else 'end_turn', 'content': content, 'usage': dict(USAGE)}


def http_error(code, message='messages.2.content.0: rejected'):
    error = urllib.error.HTTPError('https://api.anthropic.com/v1/messages', code, 'error', {}, None)
    error.safe_api_message = message
    return error


def refusal(model, tool=True):
    return {'model': model, 'stop_reason': 'refusal', 'content': [], 'usage': dict(USAGE, output_tokens=0),
            'stop_details': {'type': 'refusal', 'category': 'cyber', 'explanation': 'Declined sk-ant-api03-LEAK'}}


class Fake:
    """Scripted provider: seeds think (and call the tool in the tool shape); later turns answer in text."""
    def __init__(self, reject=lambda p, c: None, seed=None, returned=None, reply=None):
        self.calls, self.reject, self.seed, self.returned, self.reply = [], reject, seed, returned, reply

    def __call__(self, p, c):
        self.calls.append((copy.deepcopy(p), dict(c)))
        error = self.reject(p, c)
        if error is not None:
            raise error
        model = self.returned or p['model']
        if not any(m['role'] == 'assistant' for m in p['messages']):
            tool = p['messages'][0]['content'][0]['text'] == tp.PROMPTS['tool_continuation']
            response = (self.seed or seed_response)(model, tool=tool)
        elif self.reply:
            response = self.reply(model)
        else:
            response = {'model': model, 'stop_reason': 'end_turn', 'usage': dict(USAGE),
                        'content': [{'type': 'thinking', 'thinking': 'PRIVATE-THOUGHT', 'signature': 'SIG-SECRET'},
                                    {'type': 'text', 'text': 'SECRET-ANSWER'}]}
        return response, 'req_fake'


def group(groups, name, shape='tool_continuation', repeat=0):
    return next(g for g in groups if g['case'] == name and g['shape'] == shape and g['repeat'] == repeat)


class PlanTests(unittest.TestCase):
    def test_plan_counts_and_unique_prefixes(self):
        self.assertEqual(tp.planned_calls(tp.plan('r', 'smoke', 1, SHAPE)), 4)
        # 2 repeats x (2 shapes x 9 two-request groups + 1 Haiku control) = 74, less the 4 Haiku
        # transition groups per repeat (8 requests) that the transform refuses at plan time: nothing is sent.
        self.assertEqual(tp.planned_calls(tp.plan('r', 'transitions', 2, SHAPE)), 58)
        self.assertEqual(tp.planned_calls(tp.plan('r', 'opus-5-5-effort', 2, SHAPE)), 12)
        # top-rung: 2 repeats x 2 shapes x (2 controls + 3 Opus 5.5 transitions) x 2 requests.
        self.assertEqual(tp.planned_calls(tp.plan('r', 'top-rung', 2, SHAPE)), 40)
        self.assertEqual({g['case'] for g in tp.plan('r', 'top-rung', 1, SHAPE)},
                         {'control/sonnet', 'control/opus', 'model_up', 'model_down', 'effort_up/opus'})
        # sonnet-5-5: 2 repeats x 2 shapes x (2 controls + 4 Sonnet 5.5 transitions) x 2 requests.
        self.assertEqual(tp.planned_calls(tp.plan('r', 'sonnet-5-5', 2, SHAPE)), 48)
        self.assertEqual({(g['case'], tuple(g['source']), tuple(g['target'])) for g in tp.plan('r', 'sonnet-5-5', 1, SHAPE)},
                         {('control/sonnet', (S, 'medium'), (S, 'medium')), ('control/opus', (O, 'medium'), (O, 'medium')),
                          ('effort_up/sonnet', (S, 'medium'), (S, 'high')), ('effort_down/sonnet', (S, 'high'), (S, 'medium')),
                          ('model_up', (S, 'high'), (O, 'medium')), ('model_down', (O, 'medium'), (S, 'medium'))})
        for suite in tp.SUITES:  # every run gets a fresh run id, so uniqueness matters within a plan
            firsts = [g['request']['system'][0]['text'].split('\n', 1)[0] for g in tp.plan('r', suite, 2, SHAPE)]
            self.assertEqual(len(firsts), len(set(firsts)), suite)
        with self.assertRaises(ValueError):
            tp.plan('r', 'nope', 1, SHAPE)
        with self.assertRaises(ValueError):
            tp.plan('r', 'transitions', 0, SHAPE)

    def test_seeds_mirror_the_captured_client_shape(self):
        for g in tp.plan('r', 'transitions', 1, SHAPE) + tp.plan('r', 'top-rung', 1, SHAPE) + tp.plan('r', 'sonnet-5-5', 1, SHAPE):
            if g['case'] == 'control/haiku':
                continue
            model, effort = g['source']
            # The arm's client is Sonnet 5.5: an Opus 5.5 request is its request rewritten, with its headers.
            spec = SHAPE['requests'][S if model == O else model]
            p = g['request']
            self.assertEqual((p['model'], p['output_config']), (model, {'effort': effort}))
            self.assertNotEqual(model, H)
            self.assertEqual(p['thinking'], spec['thinking'])
            self.assertEqual(p['context_management'], spec['context_management'])
            self.assertEqual(g['betas'], spec['anthropic_beta'])
            self.assertEqual(p['system'][0]['cache_control'], {'type': 'ephemeral'})
            self.assertEqual([m['role'] for m in p['messages']], ['user', 'system'])
            self.assertEqual(p['messages'][-1]['content'][-1]['cache_control'], {'type': 'ephemeral'})
            self.assertEqual([t['name'] for t in p['tools']], ['record_answer'])

    def test_unbuildable_transitions_are_refused_at_plan_time(self):
        refused = [g for g in tp.plan('r', 'transitions', 1, SHAPE) if not g['steps']]
        self.assertEqual(sorted((g['case'], g['shape']) for g in refused),
                         [(case, shape) for case in ('to_haiku/opus', 'to_haiku/sonnet') for shape in ('new_turn', 'tool_continuation')])
        for g in refused:
            self.assertIn('Only trailing system messages', g['refused'])

    def test_haiku_control_goes_through_the_real_transform(self):
        g = group(tp.plan('r', 'transitions', 1, SHAPE), 'control/haiku', shape='single_turn')
        p = g['request']
        self.assertEqual(p['model'], H)
        self.assertNotIn('thinking', p)
        self.assertNotIn('output_config', p)
        self.assertNotIn('context_management', p)  # its only edit was clear_thinking
        self.assertEqual([m['role'] for m in p['messages']], ['user'])  # trailing system note relocated
        self.assertEqual(p['messages'][0]['content'][-1]['text'], tp.SYSTEM_NOTE)
        self.assertEqual(g['betas'], SHAPE['requests'][S]['anthropic_beta'])
        self.assertEqual(len(g['steps']), 1)


class SwitchedRequestTests(unittest.TestCase):
    def setUp(self):
        self.groups = tp.plan('r', 'transitions', 1, SHAPE)

    def test_tool_continuation_replays_seed_content_unchanged(self):
        g = group(self.groups, 'model_up')
        seed, response = copy.deepcopy(g['request']), seed_response(S)
        before = copy.deepcopy(response)
        p = tp.switched_request(g, response)
        self.assertEqual(g['request'], seed)
        self.assertEqual(response, before)
        self.assertEqual((p['model'], p['output_config']['effort']), (O, 'medium'))
        self.assertEqual([m['role'] for m in p['messages']], ['user', 'system', 'assistant', 'user', 'system'])
        self.assertEqual(p['messages'][2]['content'], before['content'])  # signature and all, byte-identical
        self.assertEqual(p['messages'][3]['content'], [{'type': 'tool_result', 'tool_use_id': 'toolu_1',
                                                        'content': tp.TOOL_RESULT}])
        self.assertEqual(p['messages'][1]['content'], tp.SYSTEM_NOTE)  # the old marker moves to the tail
        self.assertEqual(p['messages'][-1]['content'][-1]['cache_control'], {'type': 'ephemeral'})
        self.assertEqual(p['max_tokens'], tp.SWITCH_MAX_TOKENS)

    def test_new_turn_appends_a_user_message(self):
        g = group(self.groups, 'model_down', shape='new_turn')
        p = tp.switched_request(g, seed_response(O, tool=False))
        self.assertEqual((p['model'], p['output_config']['effort']), (S, 'medium'))
        self.assertEqual(p['messages'][3], {'role': 'user', 'content': [{'type': 'text', 'text': tp.FOLLOW_UP}]})

    def test_haiku_targets_hit_the_transform_refusal_for_mid_history_system_messages(self):
        g = group(self.groups, 'to_haiku/sonnet')
        with self.assertRaises(ValueError):
            tp.switched_request(g, seed_response(S))

    def test_opus_5_5_effort_is_edited_directly(self):
        groups = tp.plan('r', 'opus-5-5-effort', 1, SHAPE)
        g = group(groups, 'o55/high_to_low', shape='new_turn')
        self.assertEqual(g['request']['model'], tp.OPUS_5_5)
        self.assertEqual(g['betas'], SHAPE['requests'][O]['anthropic_beta'])  # the modelpilot-o55 client's own
        p = tp.switched_request(g, seed_response(tp.OPUS_5_5, tool=False))
        self.assertEqual((p['model'], p['output_config']['effort']), (tp.OPUS_5_5, 'low'))
        self.assertEqual(p['thinking'], g['request']['thinking'])


class ExecuteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_probe(self, groups, fake, budget=100, suite='transitions', repeats=1):
        return tp.execute(groups, self.out, Budget(budget), suite, repeats, transport=fake)

    def verdict(self, result, case, shape='tool_continuation', repeat=0):
        return next(v for v in result['verdicts'] if v['case'] == case and v['shape'] == shape and v['repeat'] == repeat)

    def test_everything_accepted_verifies_every_buildable_pair(self):
        fake = Fake()
        result = self.run_probe(tp.plan('r', 'transitions', 1, SHAPE), fake)
        self.assertEqual(result['status'], 'complete')
        self.assertTrue(result['cost_complete'])
        self.assertEqual(self.verdict(result, 'to_haiku/sonnet')['verdict'], 'transform_refused')
        self.assertEqual(self.verdict(result, 'model_up', 'new_turn')['verdict'], 'accepted')
        self.assertEqual(result['verified_transitions'], [[O, O], [O, S], [S, O], [S, S]])
        # The 4 Haiku transitions are refused at plan time, so neither their seeds nor switches are sent.
        self.assertEqual(len(fake.calls), 29)
        self.assertEqual((result['calls'], result['planned_calls'], result['transform_refused']), (29, 29, 4))
        self.assertEqual(sum(p['model'] == H for p, _ in fake.calls), 1)  # only the Haiku control

    def test_switched_request_carries_the_source_models_betas(self):
        fake = Fake()
        groups = [group(tp.plan('r', 'transitions', 1, SHAPE), 'model_up')]
        self.run_probe(groups, fake)
        self.assertEqual([c['anthropic_beta'] for _, c in fake.calls],
                         [','.join(SHAPE['requests'][S]['anthropic_beta'])]*2)

    def test_seed_without_thinking_is_inconclusive_and_skips_the_switch(self):
        fake = Fake(seed=lambda model, tool: seed_response(model, tool, thinking=False))
        groups = [group(tp.plan('r', 'transitions', 1, SHAPE), 'model_up')]
        result = self.run_probe(groups, fake)
        self.assertEqual(len(fake.calls), 1)
        v = self.verdict(result, 'model_up')
        self.assertEqual((v['verdict'], v['reason']), ('inconclusive', 'seed_without_thinking'))

    def test_seed_refusal_is_inconclusive_and_names_the_category(self):
        fake = Fake(seed=refusal)
        groups = [group(tp.plan('r', 'transitions', 1, SHAPE), 'model_down')]
        result = self.run_probe(groups, fake)
        self.assertEqual(len(fake.calls), 1)
        v = self.verdict(result, 'model_down')
        self.assertEqual((v['verdict'], v['reason']), ('inconclusive', 'seed_refused:cyber'))
        row = json.loads((self.out/'observations.jsonl').read_text().splitlines()[0])
        self.assertEqual(row['stop_details']['category'], 'cyber')
        self.assertNotIn('LEAK', json.dumps(row))
        self.assertEqual(result['refusals'], 1)

    def test_switched_refusal_is_never_accepted(self):
        fake = Fake(reply=refusal)
        groups = [group(tp.plan('r', 'transitions', 1, SHAPE), name) for name in ('control/sonnet', 'effort_up/sonnet')]
        result = self.run_probe(groups, fake)
        self.assertEqual(self.verdict(result, 'control/sonnet')['verdict'], 'refused')
        # Its control was refused too, so the transition cannot be attributed either way.
        v = self.verdict(result, 'effort_up/sonnet')
        self.assertEqual((v['verdict'], v['reason'], v['observed']), ('inconclusive', 'control_not_accepted', 'refused'))
        self.assertEqual((result['refusals'], result['verified_transitions']), (2, []))
        fake = Fake(reply=lambda model: refusal(model) if len(fake.calls) == 4 else Fake().__call__(
            {'model': model, 'messages': [{'role': 'assistant'}]}, {})[0])
        with tempfile.TemporaryDirectory() as tmp:
            result = tp.execute(groups, Path(tmp), Budget(100), 'transitions', 1, transport=fake)
        v = self.verdict(result, 'effort_up/sonnet')
        self.assertEqual((v['verdict'], v['reason']), ('refused', 'refused:cyber'))

    def test_new_turn_seed_that_calls_a_tool_is_inconclusive(self):
        fake = Fake(seed=lambda model, tool: seed_response(model, tool=True))
        groups = [group(tp.plan('r', 'transitions', 1, SHAPE), 'model_up', shape='new_turn')]
        result = self.run_probe(groups, fake)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(self.verdict(result, 'model_up', 'new_turn')['reason'], 'seed_stop_reason_tool_use')

    def test_switched_400_is_rejected_and_the_run_continues(self):
        # Calls: control/sonnet 1-2, control/opus 3-4, model_up 5-6 (its switched request is rejected), model_down 7-8.
        fake = Fake(lambda p, c: http_error(400, 'Invalid signature in thinking block sk-ant-api03-LEAK')
                    if len(fake.calls) == 6 else None)
        groups = [group(tp.plan('r', 'transitions', 1, SHAPE), name)
                  for name in ('control/sonnet', 'control/opus', 'model_up', 'model_down')]
        result = self.run_probe(groups, fake)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(len(fake.calls), 8)
        v = self.verdict(result, 'model_up')
        self.assertEqual(v['verdict'], 'rejected')
        self.assertIn('Invalid signature', v['api_error'])
        self.assertNotIn('LEAK', json.dumps(result))
        self.assertEqual(self.verdict(result, 'model_down')['verdict'], 'accepted')
        self.assertFalse(result['cost_complete'])
        self.assertEqual(result['rejected_requests'], 1)
        self.assertGreater(result['budget_charged_usd'], result['known_cost_usd'])
        row = [json.loads(line) for line in (self.out/'observations.jsonl').read_text().splitlines()][5]
        self.assertEqual((row['status'], row['http_status'], row['cost_usd']), ('rejected', 400, None))

    def test_an_account_error_on_a_switched_request_stops_the_run_and_is_no_verdict(self):
        # runs/thinking-probe-sonnet-5-5-20260928-150023: the credit ran out on a switched request, which was
        # recorded as a rejected transition.
        credit = 'Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing.'
        fake = Fake(lambda p, c: http_error(400, credit) if len(fake.calls) == 6 else None)
        groups = [group(tp.plan('r', 'transitions', 1, SHAPE), name)
                  for name in ('control/sonnet', 'control/opus', 'model_up', 'model_down')]
        summary = self.run_probe(groups, fake)
        self.assertEqual((summary['status'], summary['error'], len(fake.calls)), ('stopped', 'HTTPError', 6))
        self.assertNotIn('model_up', {v['case'] for v in summary['verdicts'] if v['verdict'] == 'rejected'})
        row = [json.loads(line) for line in (self.out/'observations.jsonl').read_text().splitlines()][5]
        self.assertEqual((row['status'], row['http_status']), ('error', 400))

    def test_control_rejection_makes_the_transition_inconclusive(self):
        fake = Fake(lambda p, c: http_error(400) if len(fake.calls) == 2 else None)  # control/opus switched
        groups = [group(tp.plan('r', 'transitions', 1, SHAPE), name) for name in ('control/opus', 'model_up')]
        result = self.run_probe(groups, fake)
        self.assertEqual(self.verdict(result, 'control/opus')['verdict'], 'rejected')
        v = self.verdict(result, 'model_up')
        self.assertEqual((v['verdict'], v['reason']), ('inconclusive', 'control_not_accepted'))

    def test_seed_400_stops_the_run(self):
        fake = Fake(lambda p, c: http_error(400))
        result = self.run_probe(tp.plan('r', 'smoke', 1, SHAPE), fake)
        self.assertEqual((result['status'], result['calls'], len(fake.calls)), ('stopped', 1, 1))
        self.assertEqual(json.loads((self.out/'summary.json').read_text())['status'], 'stopped')

    def test_non_400_errors_stop_the_run(self):
        for error in (http_error(401, 'authentication'), http_error(429), urllib.error.URLError(OSError('down'))):
            with tempfile.TemporaryDirectory() as tmp:
                fake = Fake(lambda p, c, error=error: error if len(fake.calls) == 2 else None)
                result = tp.execute(tp.plan('r', 'smoke', 1, SHAPE), Path(tmp), Budget(100), 'smoke', 1, transport=fake)
                self.assertEqual((result['status'], len(fake.calls)), ('stopped', 2), error)
                self.assertTrue(result['error'])

    def test_unpriced_response_stops_the_run(self):
        fake = Fake(returned='claude-other')
        result = self.run_probe(tp.plan('r', 'smoke', 1, SHAPE), fake)
        self.assertEqual((result['status'], len(fake.calls)), ('stopped', 1))
        self.assertFalse(result['cost_complete'])

    def test_budget_admission_stops_before_sending(self):
        fake = Fake()
        result = self.run_probe(tp.plan('r', 'smoke', 1, SHAPE), fake, budget=.0001)
        self.assertEqual((result['status'], len(fake.calls)), ('stopped', 0))

    def test_logs_never_contain_thinking_signatures_tool_inputs_or_answers(self):
        self.run_probe(tp.plan('r', 'transitions', 1, SHAPE), Fake())
        for name in ('observations.jsonl', 'summary.json'):
            text = (self.out/name).read_text()
            for secret in SECRETS:
                self.assertNotIn(secret, text, name)
        row = json.loads((self.out/'observations.jsonl').read_text().splitlines()[0])
        self.assertEqual((row['thinking_blocks'], row['signature_present'], row['tool_use']), (1, True, True))
        self.assertEqual(row['content_types'], ['thinking', 'tool_use'])


def marks(p):
    return [i for i, m in enumerate(p['messages']) if tp._cache_marks(m.get('content'))]


class Stepper:
    """Scripted provider for return groups: every reply thinks and calls the tool; later requests read `read(p)`
    and write 300. Seeds write 7000, so home's entry is 7000 tokens."""
    def __init__(self, read=lambda p: 7000, reject=lambda p: None, reply=None):
        self.calls, self.read, self.reject, self.reply = [], read, reject, reply

    def __call__(self, p, c):
        self.calls.append((copy.deepcopy(p), dict(c)))
        if self.reject(p):
            raise http_error(400)
        turn = sum(m['role'] == 'assistant' for m in p['messages'])
        if turn == 0:
            return seed_response(p['model']), 'req_fake'
        if self.reply:
            return self.reply(p), 'req_fake'
        usage = dict(USAGE, cache_read_input_tokens=self.read(p), cache_creation_input_tokens=300,
                     cache_creation={'ephemeral_5m_input_tokens': 300, 'ephemeral_1h_input_tokens': 0})
        return {'model': p['model'], 'stop_reason': 'tool_use', 'usage': usage, 'content': [
            {'type': 'thinking', 'thinking': 'PRIVATE-THOUGHT', 'signature': 'SIG-SECRET'},
            {'type': 'tool_use', 'id': f'toolu_{turn + 1}', 'name': 'record_answer', 'input': {'value': 424242}}]}, 'req_fake'


class ReturnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_returns(self, fake, repeats=1, budget=100):
        return tp.execute(tp.plan('r', 'returns', repeats, SHAPE), self.out, Budget(budget), 'returns', repeats,
                          transport=fake)

    def verdict(self, result, case, repeat=0):
        return next(v for v in result['verdicts'] if v['case'] == case and v['repeat'] == repeat)

    def test_plan_goes_home_away_and_back(self):
        groups = tp.plan('r', 'returns', 2, SHAPE)
        # 2 repeats x (3 + 8 + 8 + 3) requests.
        self.assertEqual(tp.planned_calls(groups), 44)
        g = group(groups, 'return/effort_far_anchored')
        self.assertEqual((g['steps'], g['anchored'], g['source'], g['target']),
                         (['seed'] + ['away']*6 + ['return'], True, [S, 'medium'], [S, 'high']))
        self.assertEqual(group(groups, 'return/model_near')['target'], [O, 'medium'])
        self.assertEqual(len({g['request']['system'][0]['text'] for g in groups}), len(groups))
        self.assertGreater(tp.max_reserve(groups), tp.max_reserve(tp.plan('r', 'sonnet-5-5', 1, SHAPE)) / 4)

    def test_continuations_mirror_the_client(self):
        g = group(tp.plan('r', 'returns', 1, SHAPE), 'return/effort_near')
        p = tp.continue_request(g, g['request'], seed_response(S))
        self.assertEqual([m['role'] for m in p['messages']], ['user', 'system', 'assistant', 'user', 'system'])
        self.assertEqual(p['messages'][3]['content'], [{'type': 'tool_result', 'tool_use_id': 'toolu_1', 'content': tp.NEXT_STEP}])
        self.assertEqual(marks(p), [4])  # the moving marker; the earlier system note is now a string
        p = tp.continue_request(g, g['request'], seed_response(S, tool=False))
        self.assertEqual(p['messages'][3]['content'], [{'type': 'text', 'text': tp.CONTINUE}])

    def test_positions_collapse_tool_runs_and_count_strings_once(self):
        messages = [{'role': 'user', 'content': [{'type': 'text'}]},
                    {'role': 'system', 'content': 'note'},
                    {'role': 'assistant', 'content': [{'type': 'thinking'}, {'type': 'text'}, {'type': 'tool_use'}, {'type': 'tool_use'}]},
                    {'role': 'user', 'content': [{'type': 'tool_result'}, {'type': 'tool_result'}]},
                    {'role': 'system', 'content': [{'type': 'text'}]}]
        self.assertEqual(tp.positions_after(messages, 0), 1 + 3 + 1 + 1)
        self.assertEqual(tp.positions_after(messages, 4), 0)

    def test_anchor_restores_the_marker_and_respects_the_limit(self):
        g = group(tp.plan('r', 'returns', 1, SHAPE), 'return/effort_far_anchored')
        p = tp.continue_request(g, g['request'], seed_response(S))
        anchored = tp.with_anchor(p, 1)
        self.assertEqual(marks(anchored), [1, 4])
        self.assertEqual(anchored['messages'][1]['content'][0]['text'], p['messages'][1]['content'])
        self.assertEqual(marks(p), [4])  # the input is not modified
        crowded = copy.deepcopy(anchored)
        crowded['system'] = [dict(b, cache_control={'type': 'ephemeral'}) for b in crowded['system'] * 3]
        with self.assertRaises(ValueError):
            tp.with_anchor(crowded, 3)

    def test_returns_go_back_to_the_home_setting_and_measure_the_read(self):
        # Home's entry (7000) is read by a return that anchors it or is near; the far one reads only 6800.
        def read(p):
            home = p['output_config']['effort'] == 'medium' and p['model'] == S
            return 7000 if home and (len(marks(p)) == 2 or len(p['messages']) < 10) else 6800
        fake = Stepper(read)
        result = self.run_returns(fake)
        self.assertEqual((result['status'], result['calls']), ('complete', 22))
        sent = [(p['model'], p['output_config']['effort']) for p, _ in fake.calls]
        self.assertEqual(sent[:3], [(S, 'medium'), (S, 'high'), (S, 'medium')])
        self.assertEqual(sent[3:11], [(S, 'medium')] + [(S, 'high')]*6 + [(S, 'medium')])
        self.assertEqual(sent[19:], [(S, 'medium'), (O, 'medium'), (S, 'medium')])
        # The Opus request keeps the client's (Sonnet) betas, as the proxy would.
        self.assertEqual(fake.calls[20][1]['anthropic_beta'], ','.join(SHAPE['requests'][S]['anthropic_beta']))
        near, far, anchored = (self.verdict(result, f'return/effort_{c}') for c in ('near', 'far', 'far_anchored'))
        self.assertEqual((near['verdict'], near['return_reuse'], near['home_entry_tokens'], near['return_read']),
                         ('accepted', 'entry_read', 7000, 7000))
        self.assertEqual((near['positions_since_home_marker'], near['predicted_reachable']), (8, True))
        self.assertEqual((far['return_reuse'], far['positions_since_home_marker'], far['predicted_reachable']),
                         ('partial', 28, False))
        self.assertEqual((anchored['return_reuse'], anchored['predicted_reachable']), ('entry_read', True))
        self.assertEqual(marks(fake.calls[18][0]), [1, 22])  # the anchored return: seed's marker and the moving one
        self.assertEqual(far['away_reads'], [6800]*6)
        findings = result['return_findings']
        self.assertEqual((findings['return/effort_near']['all_read'], findings['return/effort_far']['all_read']), (True, False))
        self.assertEqual(findings['return/effort_far']['positions'], [28])
        self.assertEqual(result['verified_transitions'], [])
        for name in ('observations.jsonl', 'summary.json'):
            text = (self.out/name).read_text()
            for secret in SECRETS:
                self.assertNotIn(secret, text, name)

    def test_a_rejected_away_request_ends_the_group_and_the_run_continues(self):
        fake = Stepper(reject=lambda p: p['model'] == O)
        result = self.run_returns(fake)
        model = self.verdict(result, 'return/model_near')
        self.assertEqual((result['status'], model['verdict'], model['reason']), ('complete', 'rejected', 'away1_rejected'))
        self.assertNotIn('return_reuse', model)
        self.assertFalse(result['cost_complete'])

    def test_a_refused_away_request_is_inconclusive(self):
        def reply(p):
            return refusal(p['model']) if p['output_config']['effort'] == 'high' else Stepper()(p, {})[0]
        result = self.run_returns(Stepper(reply=reply))
        near = self.verdict(result, 'return/effort_near')
        self.assertEqual((near['verdict'], near['reason']), ('inconclusive', 'away1_refused:cyber'))
        self.assertEqual(self.verdict(result, 'return/model_near')['verdict'], 'accepted')

    def test_dry_run_plans_the_returns_suite(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(tp.probe, 'send') as send:
            tp.main(['--suite', 'returns', '--out', str(Path(tmp)/'run')])
            send.assert_not_called()
            plan = json.loads((Path(tmp)/'run'/'plan.json').read_text())
            self.assertEqual((plan['repeats'], plan['calls']), (2, 44))
            self.assertEqual(plan['return_cases']['return/effort_far'], [[S, 'medium'], [S, 'high'], 6, False])


class VerifiedTransitionTests(unittest.TestCase):
    def summary(self, repeats=2, suite='transitions'):
        verdicts = []
        for repeat in range(repeats):
            for shape in tp.SHAPES:
                for case, (source, target) in tp.TRANSITIONS.items():
                    verdicts.append({'case': case, 'shape': shape, 'repeat': repeat, 'source': list(source),
                                     'target': list(target), 'verdict': 'accepted'})
        return {'suite': suite, 'repeats': repeats, 'verdicts': verdicts}

    def test_every_repeat_and_both_shapes_are_required(self):
        s = self.summary()
        self.assertEqual(tp.verified_transitions(s), [[O, H], [O, O], [O, S], [S, H], [S, O], [S, S]])
        s['verdicts'][0]['verdict'] = 'rejected'  # effort_up/sonnet, one repeat, one shape
        self.assertNotIn([S, S], tp.verified_transitions(s))
        s = self.summary()
        s['verdicts'] = [v for v in s['verdicts'] if not (v['case'] == 'model_up' and v['shape'] == 'new_turn')]
        self.assertNotIn([S, O], tp.verified_transitions(s))
        s = self.summary()
        s['verdicts'] = [v for v in s['verdicts'] if not (v['case'] == 'model_up' and v['repeat'] == 1)]
        self.assertNotIn([S, O], tp.verified_transitions(s))

    def test_other_suites_verify_nothing(self):
        self.assertEqual(tp.verified_transitions(self.summary(suite='smoke')), [])

    def test_top_rung_suite_verifies_only_its_pairs(self):
        s = self.summary(suite='top-rung')
        s['verdicts'] = [v for v in s['verdicts'] if v['case'] in tp.TOP_RUNG]
        self.assertEqual(tp.verified_transitions(s), [[O, O], [O, S], [S, O]])
        s['verdicts'] = [v for v in s['verdicts'] if not (v['case'] == 'model_down' and v['repeat'] == 1)]
        self.assertEqual(tp.verified_transitions(s), [[O, O], [S, O]])

    def test_the_old_opus_5_run_still_verifies_only_sonnet_effort(self):
        # Verdicts recorded against Opus 5 (runs/thinking-probe-transitions-20260926-131953) never count for Opus 5.5.
        s = self.summary()
        for v in s['verdicts']:
            v['source'] = [O5 if m == O else m for m in v['source'][:1]] + v['source'][1:]
            v['target'] = [O5 if m == O else m for m in v['target'][:1]] + v['target'][1:]
            if O5 in (v['source'][0], v['target'][0]):
                v['verdict'] = 'inconclusive'
        self.assertEqual(tp.verified_transitions(s), [[S, H], [S, S]])

    def test_sonnet_5_5_suite_verifies_only_its_pairs(self):
        s = self.summary(suite='sonnet-5-5')
        s['verdicts'] = [v for v in s['verdicts'] if v['case'] in tp.SONNET_5_5_CASES]
        self.assertEqual(tp.verified_transitions(s), [[O, S], [S, O], [S, S]])
        s['verdicts'][0]['verdict'] = 'rejected'  # effort_up/sonnet
        self.assertEqual(tp.verified_transitions(s), [[O, S], [S, O]])

    def test_earlier_runs_keep_the_pairs_they_recorded(self):
        # Runs before September 28 recorded Sonnet 5; retargeting the probe's "sonnet" must not move their evidence.
        s = self.summary(suite='top-rung')
        s['verdicts'] = [v for v in s['verdicts'] if v['case'] in tp.TOP_RUNG]
        for v in s['verdicts']:
            for side in ('source', 'target'):
                v[side] = [S5 if v[side][0] == S else v[side][0]] + v[side][1:]
        self.assertEqual(tp.verified_transitions(s), [[O, O], [O, S5], [S5, O]])
        s['verdicts'] = [v for v in s['verdicts'] if not (v['case'] == 'model_down' and v['repeat'] == 1)]
        self.assertEqual(tp.verified_transitions(s), [[O, O], [S5, O]])


class MainTests(unittest.TestCase):
    def test_dry_run_writes_the_plan_and_sends_nothing(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(tp.probe, 'send') as send:
            tp.main(['--suite', 'transitions', '--repeats', '2', '--out', str(Path(tmp)/'run')])
            send.assert_not_called()
            plan = json.loads((Path(tmp)/'run'/'plan.json').read_text())
            self.assertEqual((plan['suite'], plan['repeats'], plan['calls'], plan['live']), ('transitions', 2, 58, False))
            self.assertGreater(plan['max_reserve_usd'], 0)
            self.assertEqual(plan['shape']['client_version'], '2.1.284 (Claude Code)')

    def test_live_refuses_without_the_shape_fixture(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(tp.probe, 'send') as send, \
                mock.patch.object(tp, 'SHAPE_FIXTURE', Path(tmp)/'missing.json'):
            with self.assertRaises(SystemExit):
                tp.main(['--suite', 'smoke', '--live', '--out', str(Path(tmp)/'run')])
            send.assert_not_called()


class EffortTransport:
    """Scripted provider for per-message-effort groups. Thinking follows the effective effort (the latest effort
    message, else the top-level value). The cache keeps the previous request's entry unless the top-level effort
    left the seed's, which rewrites the messages (tools and system, 7000 tokens, stay)."""
    THINK = {'low': 10, 'medium': 100, 'high': 200, 'xhigh': 400}

    def __init__(self, reject=lambda p: False):
        self.calls, self.entries, self.reject = [], {}, reject

    def __call__(self, p, c):
        self.calls.append((copy.deepcopy(p), dict(c)))
        if self.reject(p):
            raise http_error(400)
        key = p['system'][0]['text']
        pm = [m['output_config']['effort'] for m in p['messages'] if m.get('role') == 'system' and m.get('output_config')]
        effort = pm[-1] if pm else p['output_config']['effort']
        first = key not in self.entries
        read = 0 if first else self.entries[key] if p['output_config']['effort'] == 'medium' else 7000
        write = 7000 if first else 300
        self.entries[key] = read + write
        usage = dict(USAGE, cache_read_input_tokens=read, cache_creation_input_tokens=write,
                     cache_creation={'ephemeral_5m_input_tokens': write, 'ephemeral_1h_input_tokens': 0},
                     output_tokens_details={'thinking_tokens': self.THINK[effort]})
        turn = sum(m['role'] == 'assistant' for m in p['messages'])
        return {'model': p['model'], 'stop_reason': 'tool_use', 'usage': usage, 'content': [
            {'type': 'thinking', 'thinking': 'PRIVATE-THOUGHT', 'signature': 'SIG-SECRET'},
            {'type': 'tool_use', 'id': f'toolu_{turn + 1}', 'name': 'record_answer', 'input': {'value': 424242}}]}, 'req_fake'


class PerMessageEffortTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_suite(self, fake):
        return tp.execute(tp.plan('r', 'per-message-effort', 1, SHAPE), self.out, Budget(100), 'per-message-effort', 1,
                          transport=fake)

    def test_plan(self):
        groups = tp.plan('r', 'per-message-effort', 2, SHAPE)
        self.assertEqual(tp.planned_calls(groups), 2 * len(tp.EFFORT_CASES) * 4)
        g = group(groups, 'effort/pm_xhigh_after')
        self.assertEqual((g['effort_mode'], g['placement'], g['target'], g['steps']),
                         ('pm', 'after_result', [S, 'xhigh'], ['seed', 'step1', 'step2', 'step3']))
        self.assertEqual(group(groups, 'effort/opus_pm_low')['source'], [O, 'medium'])
        self.assertNotEqual(tp.step_puzzle(1), tp.step_puzzle(2))

    def test_effort_messages_go_where_they_were_first_sent(self):
        messages = [{'role': 'user'}, {'role': 'system'}, {'role': 'assistant'}, {'role': 'user'}, {'role': 'system'}]
        self.assertEqual((tp.effort_anchor(messages, 'before_result'), tp.effort_anchor(messages, 'after_result')), (3, 4))
        request = {'output_config': {'effort': 'medium'}, 'messages': messages}
        out = tp.with_effort_messages(request, [(3, 'high'), (1, 'low')])
        self.assertEqual([m.get('output_config', {}).get('effort') for m in out['messages']],
                         [None, 'low', None, None, 'high', None, None])
        self.assertEqual(out['messages'][1], {'role': 'system', 'content': [], 'output_config': {'effort': 'low'}})
        self.assertEqual((out['output_config'], len(request['messages'])), ({'effort': 'medium'}, 5))

    def test_per_message_changes_keep_the_cache_and_top_level_ones_rewrite(self):
        fake = EffortTransport()
        result = self.run_suite(fake)
        self.assertEqual((result['status'], result['calls']), ('complete', 4 * len(tp.EFFORT_CASES)))
        found = result['effort_findings']
        self.assertEqual(found['effort/pm_xhigh']['mean_thinking_by_step'], [100, 100, 400, 400])
        self.assertEqual(found['effort/pm_low']['mean_thinking_by_step'], [100, 100, 10, 10])
        self.assertEqual(found['effort/top_high']['mean_thinking_by_step'], [100, 100, 200, 200])
        self.assertEqual((found['effort/pm_xhigh']['step2_cache'], found['effort/pm_xhigh']['step3_cache']), (['kept'], ['kept']))
        self.assertEqual(found['effort/top_xhigh']['step2_cache'], ['rewritten'])
        self.assertEqual(found['effort/control']['step2_cache'], ['kept'])
        # The per-message request: top-level effort as the client sent it, the effort message before the tool result,
        # and at the same place again one request later.
        pm = [p for p in fake.calls if 'effort/pm_xhigh/tool_continuation' in p[0]['system'][0]['text']]
        step2, step3 = pm[2][0], pm[3][0]
        for p in (step2, step3):
            self.assertEqual(p['output_config']['effort'], 'medium')
            index = next(i for i, m in enumerate(p['messages']) if m.get('output_config'))
            self.assertEqual(p['messages'][index + 1]['role'], 'user')
            self.assertEqual(p['messages'][index - 1]['role'], 'assistant')
        self.assertEqual(step3['messages'][:len(step2['messages'])], step2['messages'][:len(step2['messages']) - 1] +
                         [step3['messages'][len(step2['messages']) - 1]])  # history only grows (the note is restrung)
        top = [p for p, _ in fake.calls if 'effort/top_high/tool_continuation' in p['system'][0]['text']]
        self.assertEqual([p['output_config']['effort'] for p in top], ['medium', 'medium', 'high', 'high'])
        for name in ('observations.jsonl', 'summary.json'):
            text = (self.out/name).read_text()
            for secret in SECRETS:
                self.assertNotIn(secret, text, name)

    def test_a_rejected_placement_ends_its_group_and_the_run_continues(self):
        def before_result(p):
            return any(m.get('output_config') and p['messages'][i + 1]['role'] == 'user'
                       for i, m in enumerate(p['messages'][:-1]))
        result = self.run_suite(EffortTransport(reject=before_result))
        self.assertEqual(result['status'], 'complete')
        found = result['effort_findings']
        self.assertEqual(found['effort/pm_high']['verdicts'], ['rejected'])
        self.assertEqual(found['effort/pm_xhigh_after']['verdicts'], ['accepted'])
        self.assertFalse(result['cost_complete'])
