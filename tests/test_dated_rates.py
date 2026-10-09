"""Sonnet 5.5's cache reads, $0.20 until October 7 and $0.10 from then: each request at the prices of its day, and
reports on one day's prices for runs from either side of the change (user decision, October 8)."""
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from modelpilot import bench, bench_report, cache_probe, cache_replication, switch_policy
from tests.test_bench_report import record

ROOT = Path(__file__).resolve().parents[1]
S, O = 'claude-sonnet-5-5', 'claude-opus-5-5'
CONFIG = json.loads((ROOT/'configs/sonnet-5-5-rates.json').read_text())
BEFORE = dict(input=2, output=10, write_5m=2.5, write_1h=4, read=.2)
AFTER = dict(BEFORE, read=.1)
CUT = datetime.fromisoformat('2026-10-07T00:00:00-07:00').timestamp()
OCT6_TTL_RUN = datetime.fromisoformat('2026-10-06T22:03:00-07:00').timestamp()  # m0-replication-ttl-1h, API key
OCT7_LATE_FIX = datetime.fromisoformat('2026-10-07T16:30:00-07:00').timestamp()  # late-fix-20261007-160727


def usage(read, write, output=100, uncached=10, hour=0):
    return {'input_tokens': uncached, 'output_tokens': output, 'cache_read_input_tokens': read,
            'cache_creation_input_tokens': write,
            'cache_creation': {'ephemeral_5m_input_tokens': write - hour, 'ephemeral_1h_input_tokens': hour}}


def priced(rate, u):
    return (u['input_tokens'] * rate['input'] + u['output_tokens'] * rate['output'] + u['cache_read_input_tokens'] *
            rate['read'] + u['cache_creation']['ephemeral_5m_input_tokens'] * rate['write_5m'] +
            u['cache_creation']['ephemeral_1h_input_tokens'] * rate['write_1h']) / 1e6


def row(started, u, cost_usd):
    return {'kind': 'messages', 'started_unix': started, 'model': S, 'effort': 'low', 'http_status': 200,
            'tool_count': 3, 'usage': u, 'cost_usd': cost_usd}


class DatedRateTests(unittest.TestCase):
    def test_the_rate_file_dates_the_cut_and_says_why(self):
        self.assertEqual(CONFIG['rates'][S], {'dated': [dict(BEFORE, **{'from': None}),
                                                        dict(AFTER, **{'from': '2026-10-07T00:00:00-07:00'})]})
        self.assertIn('release-notes', CONFIG['source'])
        self.assertIn('no time of day', CONFIG['source'])
        self.assertEqual(bench.rates()[S], CONFIG['rates'][S])
        self.assertEqual(cache_replication.RATES[S], CONFIG['rates'][S])

    def test_each_request_pays_the_prices_of_its_day(self):
        rate = bench.rates()[S]
        for when, expected in ((OCT6_TTL_RUN, BEFORE), (CUT - 1, BEFORE), (CUT, AFTER), (OCT7_LATE_FIX, AFTER),
                               (None, AFTER)):  # None: now
            self.assertEqual({k: cache_probe.dated(rate, when)[k] for k in expected}, expected, when)
        u = usage(100_000, 2000)
        self.assertAlmostEqual(cache_probe.cost(u, rate, '5m', OCT6_TTL_RUN), priced(BEFORE, u))
        self.assertAlmostEqual(cache_probe.cost(u, rate, '5m'), priced(AFTER, u))
        live = dict(u, service_tier='standard', inference_geo='global')
        self.assertAlmostEqual(cache_probe.priced_usage({'model': S}, S, live, bench.rates()), priced(AFTER, u))
        with self.assertRaises(KeyError):  # code that doesn't pick a date fails rather than prices at the wrong one
            rate['read']

    def test_other_rates_pass_through(self):
        flat = bench.rates()[O]
        self.assertIs(cache_probe.dated(flat, OCT6_TTL_RUN), flat)
        self.assertIsNone(cache_probe.dated(None))

    def test_forecasts_use_todays_prices(self):
        self.assertEqual(switch_policy._rate(bench.rates(), S, 10_000)['read'], .1)

    def test_reports_price_recorded_rows_on_their_own_day(self):
        u = usage(100_000, 2000, hour=2000)  # a subscription row: the client marks writes 1h
        rows = [row(OCT6_TTL_RUN, u, 0.0), row(OCT7_LATE_FIX, u, 0.0)]
        parts = bench_report.cost_components(rows, bench.rates())
        self.assertAlmostEqual(parts['cache_read'], 100_000 * (.2 + .1) / 1e6)
        five = dict(u, cache_creation={'ephemeral_5m_input_tokens': 2000, 'ephemeral_1h_input_tokens': 0})
        repriced = bench_report.api_key_equivalent(rows, bench.rates())
        self.assertAlmostEqual(repriced[0]['cost_usd'], priced(BEFORE, five))
        self.assertAlmostEqual(repriced[1]['cost_usd'], priced(AFTER, five))

    def test_price_at_puts_every_request_on_one_days_prices(self):
        u = usage(100_000, 2000)
        rows = [row(OCT6_TTL_RUN, u, priced(BEFORE, u)), dict(row(OCT6_TTL_RUN + 5, usage(0, 0), None), http_status=400)]
        rates = bench_report.on_date(bench.rates(), OCT7_LATE_FIX)
        self.assertEqual(rates[S]['read'], .1)
        out = bench_report.repriced(rows, rates)
        self.assertAlmostEqual(out[0]['cost_usd'], priced(AFTER, u))
        self.assertIsNone(out[1]['cost_usd'])  # unpriced stays unpriced
        no_split = dict(rows[0], usage={k: v for k, v in u.items() if k != 'cache_creation'})
        self.assertIsNone(bench_report.repriced([no_split], rates)[0]['cost_usd'])
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run/'manifest.json').write_text(json.dumps({'seed': 0, 'arms': {'sonnet-5.5': {}}}))
            trial = run/'t0'/'sonnet-5.5'/'0'
            trial.mkdir(parents=True)
            (trial/'trial.json').write_text(json.dumps(record('t0', 'sonnet-5.5')))
            # A cold trial: its first request writes the prefix, its second reads it back (nothing inherited).
            cold, warm = usage(0, 100_000), usage(100_000, 2000)
            trial_rows = [row(OCT6_TTL_RUN, cold, priced(BEFORE, cold)), row(OCT6_TTL_RUN + 5, warm, priced(BEFORE, warm))]
            (trial/'observations.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in trial_rows))
            recorded = bench_report.summary_of([run], bench.rates(), resamples=20)
            today = bench_report.summary_of([run], bench.rates(), resamples=20, price_at='2026-10-08')
        self.assertTrue(recorded['priced_at'].startswith('as recorded'))
        self.assertEqual(today['priced_at'], '2026-10-08')
        self.assertAlmostEqual(recorded['arms'][0]['mean_cost_usd'] - today['arms'][0]['mean_cost_usd'],
                               100_000 * (.2 - .1) / 1e6)


if __name__ == '__main__':
    unittest.main()
