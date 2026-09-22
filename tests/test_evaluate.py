import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from modelpilot.evaluate import TASKS, grade, plan, run, summarize

RATES = {'input': 3, 'output': 15, 'write_5m': 3.75, 'write_1h': 6, 'read': .3}
CONFIG = {'models': ['a', 'b'], 'efforts': ['low', 'high'], 'rates': {'a': RATES, 'b': RATES}}


def response(answer):
    return {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': json.dumps({'answer': answer})}]}


class EvaluationTests(unittest.TestCase):
    def test_paired_tasks_and_rotated_order(self):
        schedule = plan(CONFIG)
        self.assertEqual(len(schedule), 16)
        for arm in ('a/low', 'a/high', 'b/low', 'b/high'):
            self.assertEqual([r['task'] for r in schedule if r['arm']['id'] == arm], TASKS)
        self.assertEqual(len({schedule[i]['arm']['id'] for i in (0, 4, 8, 12)}), 4)

    def test_grading_strict_types_and_completion(self):
        self.assertTrue(grade(response(False), False))
        self.assertFalse(grade(response(0), False))
        self.assertFalse(grade(response('36'), 36))
        r = response('cobalt'); r['stop_reason'] = 'max_tokens'
        self.assertFalse(grade(r, 'cobalt'))
        self.assertFalse(grade({'stop_reason': 'end_turn', 'content': []}, None))

    def test_empty_summary_is_not_successful_evidence(self):
        for arm in summarize([], CONFIG):
            self.assertFalse(arm['complete'])
            self.assertIsNone(arm['pass_rate'])

    def sender(self, request, config):
        task = next(t for t in TASKS if t['prompt'] == request['messages'][0]['content'])
        r = response(task['answer'])
        r.update(model=request['model'], usage={'input_tokens': 10, 'output_tokens': 10,
                                              'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0})
        return r, {}

    def test_full_mock_run_persists_comparable_counts_and_costs(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            out = Path(tmp) / 'run'
            report = run(CONFIG, out, self.sender)
            self.assertEqual(report['status'], 'complete')
            self.assertEqual(report['calls'], 16)
            self.assertEqual(len((out / 'observations.jsonl').read_text().splitlines()), 16)
            for arm in report['arms']:
                self.assertEqual(arm['pass_rate'], 1)
                self.assertAlmostEqual(arm['known_cost_usd'], .00072)
                self.assertTrue(arm['complete'])
            with self.assertRaises(FileExistsError): run(CONFIG, out, self.sender)

    def test_unknown_model_cost_stops_without_retry(self):
        def unknown(request, config):
            r, headers = self.sender(request, config)
            r['model'] = 'unexpected'
            return r, headers
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            report = run(CONFIG, Path(tmp) / 'run', unknown)
            self.assertEqual(report['calls'], 1)
            self.assertEqual(report['status'], 'stopped_unknown_cost')
            self.assertFalse(report['arms'][0]['cost_complete'])

    def test_failed_answer_counts_and_stopping_budget(self):
        def wrong(request, config):
            r, headers = self.sender(request, config)
            r['content'] = response('wrong')['content']
            return r, headers
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            report = run(CONFIG, Path(tmp) / 'run', wrong, stop_usd=.0001)
            self.assertEqual(report['status'], 'stopped_budget')
            self.assertEqual(report['calls'], 1)
            self.assertEqual(report['arms'][0]['pass_rate'], 0)

    def test_connection_failure_is_visible_in_summary(self):
        import urllib.error
        def broken(request, config):
            raise urllib.error.URLError(ConnectionRefusedError(61, 'PRIVATE'))
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            report = run(CONFIG, Path(tmp) / 'run', broken)
            self.assertEqual(report['calls'], 1)
            self.assertEqual(report['stop_error']['reason_type'], 'ConnectionRefusedError')
            self.assertNotIn('PRIVATE', json.dumps(report))


if __name__ == '__main__': unittest.main()
