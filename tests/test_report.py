import unittest
from modelpilot.report import summarize


class ReportTests(unittest.TestCase):
    def test_missing_cost_not_reported_as_complete(self):
        rows = [dict(group='trial', step='cold', status='ok', wall_seconds=2, cost_usd=.1),
                dict(group='trial', step='warm', status='error', wall_seconds=4)]
        result = summarize(rows)
        g = result['groups'][0]
        self.assertFalse(g['cost_complete_for_recorded_calls'])
        self.assertEqual(g['known_cost_usd'], .1)
        self.assertEqual(g['errors'], 1)
        self.assertEqual(result['gate'], 'requires_review')

    def test_empty_log_is_not_success(self):
        self.assertEqual(summarize([])['gate'], 'requires_review')
