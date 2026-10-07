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


class OneHourSuiteTests(unittest.TestCase):
    def test_each_kind_marks_its_lifetimes_and_waits(self):
        groups = r.plan('run', 'ttl-1h')
        self.assertEqual(len(groups), 3 * len(r.TTL_1H_MODELS) * len(r.TTL_1H_KINDS))
        self.assertTrue(all(g['name'].startswith('ttl/') for g in groups))  # execute() honours the gaps of ttl/ groups
        for g in groups:
            kind = g['name'].split('/')[-1]
            self.assertEqual([(r.marked_ttl(p), delay) for _, delay, p in g['steps']], r.TTL_1H_KINDS[kind])
        self.assertEqual(len({g['steps'][0][2]['messages'][0]['content'][0]['text'][:80] for g in groups}), len(groups))

    def test_the_summary_counts_repeats_that_read_at_their_last_step(self):
        def row(repeat, kind, since, observation, five=0, hour=0, model=r.SONNET_5_5):
            return {'group': f'ttl/1h/{repeat}/{model}/{kind}', 'ttl': '1h', 'since_previous_start_seconds': since,
                    'observation': observation,
                    'usage': {'cache_creation': {'ephemeral_5m_input_tokens': five, 'ephemeral_1h_input_tokens': hour}}}
        rows = [row(0, 'before', None, 'write', hour=5000), row(0, 'before', 3001.2, 'hit'),
                row(1, 'before', None, 'write', hour=5000), row(1, 'before', 3000.4, 'write', hour=5000),
                row(2, 'before', None, 'write', hour=5000)]  # stopped before its last step
        cell = r.ttl_summary(rows)[r.SONNET_5_5]['before']
        self.assertEqual((cell['complete'], cell['last_read']), (2, 1))
        self.assertEqual(cell['repeats']['0'], [['1h', None, 'write', 0, 5000], ['1h', 3001, 'hit', 0, 0]])
