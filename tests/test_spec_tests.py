"""Spec-written tests (plan item 5's first check): the request, the live write with a scripted transport, and the
readings. $0: nothing is sent."""
import json
from pathlib import Path
import tempfile
import unittest
from modelpilot import bench, bench_tasks, spec_tests

TASK = bench_tasks.load('parse-decimal-grouping')[0]
REPLY = {'model': spec_tests.MODEL, 'stop_reason': 'end_turn',
         'usage': {'input_tokens': 900, 'output_tokens': 3000, 'cache_creation_input_tokens': 0,
                   'cache_read_input_tokens': 0},
         'content': [{'type': 'thinking', 'thinking': ''},
                     {'type': 'text', 'text': 'Here it is.\n```python\nimport unittest\n\nclass T(unittest.TestCase):\n'
                                              '    def test_a(self):\n        pass\n\n    def test_b(self):\n'
                                              '        pass\n```\n'}]}


class SpecTestsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)/'spec'
        self.rates = bench.rates()

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_request_holds_the_instruction_and_nothing_the_grader_has(self):
        payload = spec_tests.request(TASK)
        self.assertEqual((payload['model'], payload['output_config']), ('claude-opus-5-5', {'effort': 'medium'}))
        self.assertNotIn('thinking', payload)  # Opus 5.5 always thinks; disabling it is a 400
        text = json.dumps(payload)
        self.assertIn(TASK['instruction'][:80], text)
        for secret in (TASK['reference'], TASK['base'], 'test_edge', 'hidden'):
            self.assertNotIn(secret, text)

    def test_without_live_only_the_plan_and_prompts_are_written(self):
        sent = []
        plan = spec_tests.write([TASK], self.out, self.rates, transport=lambda p: sent.append(p))
        self.assertEqual((sent, plan['results'], plan['live']), ([], {}, False))
        self.assertTrue((self.out/'prompts'/f"{TASK['id']}.json").exists())
        self.assertGreater(plan['max_cost_usd'], 12000 * 20 / 1e6)  # every output token, at least
        with self.assertRaises(FileExistsError):  # never into an existing directory
            spec_tests.write([TASK], self.out, self.rates)

    def test_a_live_reply_becomes_the_suite_with_its_measured_cost(self):
        plan = spec_tests.write([TASK], self.out, self.rates, live=True, transport=lambda p: (REPLY, 'req_1'))
        row = plan['results'][TASK['id']]
        self.assertEqual((row['status'], row['tests'], row['request_id']), ('written', 2, 'req_1'))
        self.assertAlmostEqual(row['cost_usd'], (900 * 4 + 3000 * 20) / 1e6)
        self.assertTrue(plan['cost_complete'])
        suite = self.out/'suites'/TASK['id']/'test_spec.py'
        self.assertTrue(suite.read_text().startswith('import unittest'))
        self.assertEqual(json.loads((self.out/'responses'/f"{TASK['id']}.json").read_text()), REPLY)

    def test_errors_refusals_and_the_limit_are_recorded_never_retried(self):
        calls = []

        def failing(payload):
            calls.append(payload)
            raise TimeoutError('slow')
        plan = spec_tests.write([TASK], self.out, self.rates, live=True, transport=failing)
        self.assertEqual((len(calls), plan['results'][TASK['id']]['status']), (1, 'error'))
        refused = dict(REPLY, stop_reason='refusal', content=[])
        plan = spec_tests.write([TASK], Path(self.tmp.name)/'refused', self.rates, live=True,
                                transport=lambda p: (refused, 'req_2'))
        self.assertEqual(plan['results'][TASK['id']]['status'], 'no_code_block')
        plan = spec_tests.write([TASK], Path(self.tmp.name)/'capped', self.rates, live=True, limit_usd=.01,
                                transport=lambda p: self.fail('sent past the limit'))
        self.assertEqual(plan['results'][TASK['id']], {'status': 'not_sent', 'reason': 'limit'})

    def test_two_readings_of_a_suite(self):
        ran = {'exit_code': 1, 'tests_run': 3, 'failing_tests': ['test_a (m.T)', 'test_b (m.T)']}
        self.assertEqual(spec_tests.readings(ran, invalid={'test_a (m.T)'}), (True, True))
        self.assertEqual(spec_tests.readings(ran, invalid={'test_a (m.T)', 'test_b (m.T)'}), (True, False))
        self.assertEqual(spec_tests.readings({'exit_code': 0, 'tests_run': 3, 'failing_tests': []}, set()), (False, False))
        self.assertEqual(spec_tests.readings({'error': 'diff_did_not_apply'}, set()), (None, None))
        rows = [{'strict': False, 'f': True}, {'strict': False, 'f': None}, {'strict': True, 'f': False}]
        self.assertEqual(spec_tests.rates_of(rows, 'f'), {'strict_failures': {'flagged': 1, 'of': 1},
                                                          'strict_passes': {'flagged': 0, 'of': 1}})


if __name__ == '__main__':
    unittest.main()
