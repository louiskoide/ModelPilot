import unittest

from modelpilot import exploration

RATES = {'claude-opus-5-5': dict(input=4, output=20, read=.2, write_5m=5, write_1h=8)}


def record(task, switched, passed, cost, request=3):
    entry = {'request': request, 'status': 'admitted', 'target': ['claude-opus-5-5', 'low'],
             'predicted_switch_usd': 0.04} if switched is not None else None
    return {'task': task, 'trial': 0, 'passed': passed, 'complete': True, 'cache': {'cold_equivalent_cost_usd': cost},
            'routing': {'benchmark_eligible': True, 'policy': {
                'exploration': [entry] if entry else [],
                'escalations': [{'trigger': 'exploration', 'status': 'confirmed'}] if switched else []}}}


def opus_row(write):
    return {'kind': 'messages', 'tool_count': 6, 'http_status': 200, 'model': 'claude-opus-5-5', 'started_unix': 2,
            'usage': {'cache_creation': {'ephemeral_5m_input_tokens': write, 'ephemeral_1h_input_tokens': 0}}}


class ExplorationReportTests(unittest.TestCase):
    def test_switch_cost_is_the_first_target_request_rewrite(self):
        self.assertAlmostEqual(exploration.measured_switch_usd([opus_row(10000), dict(opus_row(99), started_unix=5)],
                                                               'claude-opus-5-5', RATES), 10000 * 4.8 / 1e6)
        self.assertIsNone(exploration.measured_switch_usd([], 'claude-opus-5-5', RATES))

    def test_switched_and_unswitched_trials_are_summarized_apart(self):
        views = [exploration.trial_view(record('a', True, True, .2), [opus_row(10000)], RATES),
                 exploration.trial_view(record('b', None, False, .08), [], RATES),
                 exploration.trial_view(record('c', False, True, .07), [], RATES)]  # drawn, but ended before it
        out = exploration.summarize(views)
        self.assertEqual((out['switched']['trials'], out['not_switched']['trials']), (1, 2))
        self.assertAlmostEqual(out['prediction']['median_measured_over_predicted'], 0.048 / 0.04)
        self.assertAlmostEqual(out['not_switched']['mean_cost_usd'], 0.075)


if __name__ == '__main__':
    unittest.main()
