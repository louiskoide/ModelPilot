"""Requirements checklist (the miss detector's next candidate): requests, the live steps with scripted transports, the
score and the estimate. $0: nothing is sent."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from modelpilot import bench, bench_tasks, miss_checklist as mc

TASK = bench_tasks.load('parse-decimal-grouping')[0]
CHECKS = [{'id': 'R1', 'requirement': 'Grouped numbers parse.', 'check': 'The pattern accepts separators.'},
          {'id': 'R2', 'requirement': 'Ungrouped numbers still parse.', 'check': 'Separators stay optional.'}]
USAGE = {'input_tokens': 900, 'output_tokens': 1200, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0}


def reply(text, model='claude-haiku-5-5'):
    return {'model': model, 'stop_reason': 'end_turn', 'usage': USAGE,
            'content': [{'type': 'thinking', 'thinking': ''}, {'type': 'text', 'text': text}]}


def listed(checks=CHECKS):
    return reply('Here are the checks.\n```json\n' + json.dumps(checks) + '\n```\n')


class ChecklistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.out = self.root/'checklist'
        self.rates = bench.rates()

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_requests_hold_what_each_step_may_see_and_nothing_the_grader_has(self):
        for model, name in (('haiku', 'claude-haiku-5-5'), ('sonnet', 'claude-sonnet-5-5')):
            payload = mc.list_request(TASK, model)
            self.assertEqual((payload['model'], payload['output_config']), (name, {'effort': 'low'}))
            self.assertNotIn('thinking', payload)
            text = json.dumps(payload)
            self.assertIn(TASK['instruction'][:80], text)
            for secret in (TASK['reference'], TASK['base'], 'hidden', 'test_edge'):
                self.assertNotIn(secret, text)
        judged = json.dumps(mc.judge_request(TASK, CHECKS, '+DIFF_MARKER\n', 'haiku'))
        for shown in ('DIFF_MARKER', 'Grouped numbers parse.', TASK['instruction'][:80]):
            self.assertIn(shown, judged)
        for secret in (TASK['reference'], 'strict', 'passed'):
            self.assertNotIn(secret, judged)

    def test_without_live_only_the_prompts_and_plan_are_written(self):
        sent = []
        plan = mc.list_checks([TASK], self.out, self.rates, transport=lambda p: sent.append(p))
        self.assertEqual((sent, plan['results'], plan['live']), ([], {}, False))
        self.assertTrue((self.out/'prompts'/f"{TASK['id']}.json").exists())
        self.assertGreater(plan['max_cost_usd'], mc.LIST_MAX_TOKENS * .5 / 1e6)  # every output token, at least
        with self.assertRaises(FileExistsError):  # never into an existing directory
            mc.list_checks([TASK], self.out, self.rates)

    def test_a_live_checklist_is_saved_with_its_measured_cost(self):
        plan = mc.list_checks([TASK], self.out, self.rates, live=True, transport=lambda p: (listed(), 'req_1'))
        row = plan['results'][TASK['id']]
        self.assertEqual((row['status'], row['checks'], row['request_id']), ('listed', 2, 'req_1'))
        self.assertAlmostEqual(row['cost_usd'], (900 * .1 + 1200 * .5) / 1e6)  # Haiku 5.5's short-prompt tier
        self.assertTrue(plan['cost_complete'])
        self.assertEqual(json.loads((self.out/'checklists'/f"{TASK['id']}.json").read_text()), CHECKS)

    def test_unusable_replies_errors_and_the_limit_are_recorded_never_retried(self):
        calls = []

        def failing(payload):
            calls.append(payload)
            raise TimeoutError('slow')
        plan = mc.list_checks([TASK], self.root/'error', self.rates, live=True, transport=failing)
        self.assertEqual((len(calls), plan['results'][TASK['id']]['status'], plan['cost_complete']), (1, 'error', False))
        for text in ('No JSON here.', '```json\n[{"id": "R1"}]\n```', '```json\n' + json.dumps(CHECKS * 6) + '\n```'):
            plan = mc.list_checks([TASK], self.root/str(len(calls)), self.rates, live=True,
                                  transport=lambda p, text=text: (calls.append(p) or reply(text), 'req'))
            self.assertEqual(plan['results'][TASK['id']]['status'], 'unusable_reply', text)
        plan = mc.list_checks([TASK], self.root/'capped', self.rates, live=True, limit_usd=.0001,
                              transport=lambda p: self.fail('sent past the limit'))
        self.assertEqual(plan['results'][TASK['id']], {'status': 'not_sent', 'reason': 'limit'})

    def trials(self):
        diffs = self.root/'diffs'
        diffs.mkdir()
        rows = []
        for i, (task, size, strict) in enumerate(((TASK['id'], 40, False), (TASK['id'], 40, True),
                                                  (TASK['id'], mc.MAX_DIFF_BYTES + 1, True),
                                                  ('tomli-hex-escape', 40, False))):
            diff = diffs/f'{i}.diff'
            diff.write_text(f'+change {i}\n' + 'x' * max(0, size - 10))
            rows.append({'run': 'bench-x', 'task': task, 'arm': 'a', 'trial': i, 'strict_pass': strict, 'diff': str(diff)})
        return rows

    def test_judge_sends_each_diff_once_with_the_checklist_and_never_the_label(self):
        mc.list_checks([TASK], self.out, self.rates, live=True, transport=lambda p: (listed(), 'req_1'))
        sent = []
        verdicts = [{'id': 'R1', 'verdict': 'met', 'evidence': '+change'}, {'id': 'R9', 'verdict': 'met'}]

        def transport(payload):
            sent.append(payload)
            return reply('```json\n' + json.dumps(verdicts) + '\n```'), f'req_{len(sent)}'
        report = mc.judge(self.out, self.trials(), self.rates, live=True, transport=transport)
        statuses = {k.split('__')[-1]: (r.get('status') or report['trials'][k]['status'])
                    for k, r in dict(report['trials'], **report['results']).items()}
        self.assertEqual(statuses, {'0': 'judged', '1': 'judged', '2': 'diff_too_large', '3': 'no_checklist'})
        self.assertEqual(len(sent), 2)
        for payload in sent:
            self.assertNotIn('strict', json.dumps(payload))
        self.assertIn('+change 0', json.dumps(sent[0]))
        judged = report['results']['bench-x__parse-decimal-grouping__a__0']['verdicts']
        self.assertEqual([(v['id'], v['verdict']) for v in judged], [('R1', 'met'), ('R2', 'missing')])
        self.assertEqual(json.loads((self.out/'judge'/'trials.json').read_text())[0]['strict_pass'], False)
        with self.assertRaises(FileExistsError):
            mc.judge(self.out, [], self.rates)  # once per checklist directory

    def test_score_reads_each_rule_overall_per_task_and_with_each_task_left_out(self):
        judge = self.out/'judge'
        judge.mkdir(parents=True)
        rows = [('t1', False, ['not_met', 'met']), ('t1', True, ['met', 'unclear']), ('t2', False, ['unclear', 'missing']),
                ('t2', True, ['met', 'met']), ('t3', False, None)]
        trials = [{'run': 'r', 'task': t, 'arm': 'a', 'trial': i, 'strict_pass': s} for i, (t, s, _) in enumerate(rows)]
        results = {mc.trial_key(tr): ({'status': 'judged', 'verdicts': [{'id': f'R{j}', 'verdict': v} for j, v in enumerate(vs)]}
                                      if vs else {'status': 'error'}) for tr, (_, _, vs) in zip(trials, rows)}
        (judge/'trials.json').write_text(json.dumps(trials))
        (judge/'plan.json').write_text(json.dumps({'results': results}))
        report = mc.score(self.out)
        self.assertEqual((report['judged'], report['unjudged'], report['unjudged_misses']), (4, 1, 1))
        strict, lenient = report['rules']['not_met>=1'], report['rules']['not_met_or_unclear>=1']
        self.assertEqual(strict['overall'], {'misses_flagged': 1, 'misses': 2, 'passes_flagged': 0, 'passes': 2})
        self.assertEqual(lenient['overall'], {'misses_flagged': 2, 'misses': 2, 'passes_flagged': 1, 'passes': 2})
        self.assertEqual(lenient['per_task']['t2'], {'misses_flagged': 1, 'misses': 1, 'passes_flagged': 0, 'passes': 1})
        self.assertEqual(lenient['leave_one_task_out']['t1'], lenient['per_task']['t2'])
        self.assertEqual(report['rules']['not_met_or_unclear>=2']['overall']['misses_flagged'], 1)
        with self.assertRaises(FileExistsError):  # never overwrite a score
            mc.score(self.out)

    def test_replies_are_parsed_strictly(self):
        self.assertEqual(mc.json_block('x\n```json\n[1]\n```'), [1])
        self.assertEqual(mc.json_block('Checks: [{"id": "R1"}] done'), [{'id': 'R1'}])
        self.assertIsNone(mc.json_block('none'))
        self.assertEqual(mc.valid_checks([dict(c, extra=1) for c in CHECKS]), CHECKS)
        for bad in ([], CHECKS * 6, [CHECKS[0], CHECKS[0]], [dict(CHECKS[0], check=' ')], {'id': 'R1'}):
            self.assertIsNone(mc.valid_checks(bad), bad)
        given = [{'id': 'R2', 'verdict': 'not_met', 'evidence': 'none'}, {'id': 'R1', 'verdict': 'maybe'}]
        self.assertEqual([v['verdict'] for v in mc.verdicts_for(CHECKS, given)], ['missing', 'not_met'])
        self.assertIsNone(mc.verdicts_for(CHECKS, {'R1': 'met'}))

    def test_the_estimate_and_its_break_even_against_the_late_fix(self):
        trials = [{'task': TASK['id'], 'diff_bytes': 6000}] * 10
        haiku, sonnet = (mc.estimate([TASK], trials, self.rates, m) for m in ('haiku', 'sonnet'))
        for e in (haiku, sonnet):
            self.assertEqual((e['tasks'], e['trials']), (1, 10))
            self.assertLess(e['expected_usd']['total'], e['max_usd']['total'])
        self.assertLess(haiku['expected_usd']['total'], sonnet['expected_usd']['total'] / 10)
        # (check + flags x fix) / misses caught, per trial: (0.01 + (0.05 + 0.9 x 0.1) x 0.056) / 0.05
        self.assertAlmostEqual(mc.break_even(.01, .1, .5, .1), (.01 + .14 * .056) / .05)
        self.assertIsNone(mc.break_even(.01, .1, 0, .1))
        self.assertEqual(len(mc.break_even_grid(.01, .1)), 9)

    def test_the_cli_needs_the_dev_runs(self):
        with mock.patch.object(mc, 'dev_trials', return_value=[]), mock.patch('sys.argv', ['miss_checklist', 'plan']):
            with self.assertRaises(SystemExit) as stop:
                mc.main()
        self.assertIn('dev split', str(stop.exception))


if __name__ == '__main__':
    unittest.main()
