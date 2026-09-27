import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.error
from modelpilot import thinking_probe as tp
from modelpilot.cache_replication import Budget

S, H = 'claude-sonnet-5', 'claude-haiku-4-5-20251001'
O, O5 = 'claude-opus-5-5', 'claude-opus-5'  # O: the policy's top rung; O5: captured client shape only
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
        for suite in tp.SUITES:  # every run gets a fresh run id, so uniqueness matters within a plan
            firsts = [g['request']['system'][0]['text'].split('\n', 1)[0] for g in tp.plan('r', suite, 2, SHAPE)]
            self.assertEqual(len(firsts), len(set(firsts)), suite)
        with self.assertRaises(ValueError):
            tp.plan('r', 'nope', 1, SHAPE)
        with self.assertRaises(ValueError):
            tp.plan('r', 'transitions', 0, SHAPE)

    def test_seeds_mirror_the_captured_client_shape(self):
        for g in tp.plan('r', 'transitions', 1, SHAPE) + tp.plan('r', 'top-rung', 1, SHAPE):
            if g['case'] == 'control/haiku':
                continue
            model, effort = g['source']
            # The arm's client is Sonnet 5: an Opus 5.5 request is its request rewritten, with its headers.
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
        self.assertEqual(g['betas'], SHAPE['requests'][O5]['anthropic_beta'])
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


class MainTests(unittest.TestCase):
    def test_dry_run_writes_the_plan_and_sends_nothing(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(tp.probe, 'send') as send:
            tp.main(['--suite', 'transitions', '--repeats', '2', '--out', str(Path(tmp)/'run')])
            send.assert_not_called()
            plan = json.loads((Path(tmp)/'run'/'plan.json').read_text())
            self.assertEqual((plan['suite'], plan['repeats'], plan['calls'], plan['live']), ('transitions', 2, 58, False))
            self.assertGreater(plan['max_reserve_usd'], 0)
            self.assertEqual(plan['shape']['client_version'], '2.1.282 (Claude Code)')

    def test_live_refuses_without_the_shape_fixture(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(tp.probe, 'send') as send, \
                mock.patch.object(tp, 'SHAPE_FIXTURE', Path(tmp)/'missing.json'):
            with self.assertRaises(SystemExit):
                tp.main(['--suite', 'smoke', '--live', '--out', str(Path(tmp)/'run')])
            send.assert_not_called()
