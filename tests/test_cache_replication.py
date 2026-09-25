import unittest
from modelpilot import cache_replication as r

class ReplicationTests(unittest.TestCase):
    def test_plan_covers_models_and_haiku_has_no_effort(self):
        groups = r.plan('test')
        self.assertEqual(sum(len(g['steps']) for g in groups), 213)
        for g in groups:
            for _, _, p in g['steps']:
                if 'haiku' in p['model']:
                    self.assertNotIn('output_config', p)
                    self.assertNotIn('thinking', p)
        self.assertEqual(len([g for g in groups if g['name'].startswith('ttl/')]), 27)

    def test_opus_5_5_suite_tests_returns_to_opus(self):
        groups = r.plan('test', 'opus-5-5')
        self.assertEqual(sum(len(g['steps']) for g in groups), 141)
        for g in groups:
            models = [p['model'] for _, _, p in g['steps']]
            self.assertEqual(models[0], r.OPUS_5_5)
            if g['name'].startswith('model/'):
                self.assertEqual(models[-1], r.OPUS_5_5)
            for _, _, p in g['steps']:
                # Opus 5.5 rejects disabled thinking; the suite omits the parameter.
                self.assertNotIn('thinking', p)
                self.assertIn(p['model'], r.RATES)
        self.assertEqual({g['name'].split('/')[3] for g in groups if g['name'].startswith('model/')},
                         set(r.MODELS))

    def test_opus_5_5_rates_use_its_read_multiplier(self):
        self.assertEqual(r.RATES[r.OPUS_5_5], dict(input=4, output=20, write_5m=5, write_1h=8, read=.2))

    def test_unknown_suite_is_refused(self):
        with self.assertRaises(ValueError):
            r.plan('test', 'nope')

    def test_independent_groups_have_distinct_prefixes(self):
        prefixes = []
        for g in r.plan('test') + r.plan('test', 'opus-5-5'):
            p = g['steps'][0][2]
            block = p.get('system', p['messages'][0]['content'])[0]
            prefixes.append(block['text'])
        self.assertEqual(len(prefixes), len(set(prefixes)))

    def test_budget_does_not_send_when_reserve_exceeds_remaining(self):
        b = r.Budget(.01)
        with self.assertRaises(RuntimeError):
            b.reserve(.02)
        self.assertEqual(b.spent, 0)

    def test_execution_logs_usage_and_stops_on_unknown_cost(self):
        import tempfile
        import json
        from pathlib import Path
        group = r.plan('test')[0]
        group = dict(group, steps=group['steps'][:1])
        def fake(payload, config):
            return {'model': 'unexpected-model', 'usage': {
                'input_tokens': 1, 'output_tokens': 1, 'cache_read_input_tokens': 5000,
                'cache_creation_input_tokens': 0}}, 'fake-id'
        with tempfile.TemporaryDirectory() as tmp:
            result = r.execute([group], Path(tmp), r.Budget(5), transport=fake)
            self.assertEqual(result['status'], 'stopped')
            self.assertFalse(result['cost_complete'])
            self.assertEqual(result['calls'], 1)
            self.assertEqual(json.loads((Path(tmp)/'summary.json').read_text())['status'], 'stopped')
