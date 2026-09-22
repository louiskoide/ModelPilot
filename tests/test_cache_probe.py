import json
from pathlib import Path
import tempfile
import unittest
from modelpilot import cache_probe as probe


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.c = dict(models=['a', 'b'], efforts=['low', 'high'], repeats=3,
                      prefix_lines=20, max_tokens=32, ttls=['5m', '1h'])

    def test_effort_changes_only_effort(self):
        g = probe.experiments(self.c, 'quick', 'run')[0]
        a, b = g['steps'][1][2], g['steps'][2][2]
        b = dict(b, output_config=a['output_config'])
        self.assertEqual(a, b)

    def test_independent_ttl_prefixes_and_refresh(self):
        groups = probe.experiments(self.c, 'ttl', 'run')
        prefixes = [g['steps'][0][2]['messages'][0]['content'][0]['text'] for g in groups]
        self.assertEqual(len(prefixes), len(set(prefixes)))
        refresh = groups[2]['steps']
        self.assertGreater(sum(s[1] for s in refresh), 300)
        self.assertLess(max(s[1] for s in refresh), 300)

    def test_warmup_and_real_share_only_cached_prefix(self):
        groups = probe.experiments(self.c, 'quick', 'run')
        for g in groups:
            if not g['name'].startswith('shadow/'):
                continue
            warm, real = g['steps'][0][2], g['steps'][-1][2]
            if 'system' in warm:
                self.assertEqual(warm['system'], real['system'])
            else:
                self.assertEqual(warm['messages'][0]['content'][0], real['messages'][0]['content'][0])
            self.assertNotEqual(warm['messages'], real['messages'])

    def test_cost_no_double_count(self):
        u = dict(input_tokens=100, output_tokens=10, cache_read_input_tokens=1000,
                 cache_creation_input_tokens=2000,
                 cache_creation=dict(ephemeral_5m_input_tokens=1500, ephemeral_1h_input_tokens=500))
        rates = dict(input=3, output=15, read=.3, write_5m=3.75, write_1h=6)
        self.assertAlmostEqual(probe.cost(u, rates, '5m'), .009375)
        self.assertIsNone(probe.cost(u, None, '5m'))
        u['cache_creation_input_tokens'] = 999
        with self.assertRaises(ValueError):
            probe.cost(u, rates, '5m')

    def test_zero_usage_not_a_miss_or_a_hit(self):
        self.assertEqual(probe.observe({}), 'unknown')
        self.assertEqual(probe.observe(dict(cache_read_input_tokens=0, cache_creation_input_tokens=0)),
                         'uncached_or_below_minimum')

    def test_failure_is_logged_once_without_secret(self):
        calls = []
        def failing(p, c):
            calls.append(p)
            raise RuntimeError('SECRET')
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'log.jsonl'
            with self.assertRaises(RuntimeError):
                probe.run(self.c, probe.experiments(self.c, 'quick', 'run'), path, 200, failing)
            self.assertEqual(len(calls), 1)
            self.assertNotIn('SECRET', path.read_text())
            self.assertEqual(json.loads(path.read_text())['status'], 'error')

    def test_call_cap_before_transport(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                probe.run(self.c, probe.experiments(self.c, 'quick', 'run'), Path(d) / 'log', 1)
            self.assertFalse((Path(d) / 'log').exists())


if __name__ == '__main__':
    unittest.main()
