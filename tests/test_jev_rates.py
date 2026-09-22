import json
import math
from pathlib import Path
import unittest
from modelpilot.proxy import UsageObserver, measured_cost

ROOT = Path(__file__).resolve().parents[1]
# Pinned Jev static tiers (src/config.mjs TIERS), excluding opt-in fable.
JEV_MODELS = {'claude-haiku-4-5-20251001', 'claude-sonnet-5', 'claude-opus-5'}
FIELDS = {'input', 'output', 'write_5m', 'write_1h', 'read'}


class JevRatesTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT/'configs/jev-rates.json').read_text())
        self.rates = self.config['rates']

    def test_exactly_the_jev_tier_models_with_every_rate(self):
        self.assertEqual(set(self.rates), JEV_MODELS)
        for model, rates in self.rates.items():
            self.assertEqual(set(rates), FIELDS, model)
            for value in rates.values():
                self.assertTrue(isinstance(value, (int, float)) and not isinstance(value, bool)
                                and math.isfinite(value) and value >= 0, model)

    def test_provenance_is_recorded(self):
        self.assertTrue(isinstance(self.config.get('source'), str) and self.config['source'])
        self.assertRegex(self.config.get('retrieved', ''), r'^\d{4}-\d{2}-\d{2}$')

    def test_proxy_prices_each_model_and_nothing_else(self):
        usage = {'input_tokens': 100, 'output_tokens': 10, 'cache_read_input_tokens': 1000,
                 'cache_creation_input_tokens': 50, 'cache_creation': {'ephemeral_5m_input_tokens': 50, 'ephemeral_1h_input_tokens': 0}}
        for model, r in self.rates.items():
            observer = UsageObserver(False)
            observer.feed(json.dumps({'model': model, 'usage': usage}).encode())
            observer.finish()
            expected = (100*r['input'] + 10*r['output'] + 1000*r['read'] + 50*r['write_5m']) / 1e6
            self.assertAlmostEqual(measured_cost(observer, {'model': model}, self.rates), expected)
        observer = UsageObserver(False)
        observer.feed(json.dumps({'model': 'claude-fable-5-1', 'usage': usage}).encode())
        observer.finish()
        self.assertIsNone(measured_cost(observer, {'model': 'claude-fable-5-1'}, self.rates))


if __name__ == '__main__': unittest.main()
