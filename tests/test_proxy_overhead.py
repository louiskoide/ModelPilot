"""modelpilot.proxy_overhead's smoke test (October 15, overnight cloud run): two small cases, one client, with and
without the governor flags, through a real proxy process on loopback; under 5 s. $0."""
import time
import unittest
from modelpilot import proxy_overhead as po


class SmokeTests(unittest.TestCase):
    def test_a_quick_run_measures_both_modes_and_forwards_every_reply_unchanged(self):
        started = time.monotonic()
        result = po.run(quick=True, repeats=1)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual([r['governed'] for r in result['runs']], [False, True])
        for run in result['runs']:
            self.assertEqual(run['log_rows'], run['requests'])  # one proxy row per proxied request
            self.assertGreater(run['proxy_cpu_seconds'], 0)
            self.assertTrue(run['proxy_peak_kib'] and run['proxy_peak_kib'] > 0)
            for case in run['cases']:
                self.assertTrue(case['reply_bodies_match'], case['name'])
                self.assertIsNotNone(case['added_first_byte_seconds'])
            self.assertIn('added_seconds_per_event', next(c for c in run['cases'] if c.get('events')))

    def test_request_bodies_have_the_asked_size_and_event_count(self):
        body = po.request_body(100_000, events=7)
        self.assertEqual(len(body), 100_000)
        self.assertTrue(body.startswith(b'{"events": 7,'))
        self.assertEqual(po.quantiles([3, 1, 2]), {'median': 2, 'p95': 3})
        self.assertIsNone(po.quantiles([]))


if __name__ == '__main__':
    unittest.main()
