"""modelpilot.keeper_ping against a scripted transport (October 17): the ping ladder after a 400, other errors stop the
run, the waits of --renewal, the budget, and the replay's best setting repriced at the measured ping. $0."""
import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
from modelpilot import keeper_ping as kp


def usage(read=0, write=0, out=1):
    return {'input_tokens': 5, 'output_tokens': out, 'cache_read_input_tokens': read, 'cache_creation_input_tokens': write,
            'cache_creation': {'ephemeral_5m_input_tokens': 0, 'ephemeral_1h_input_tokens': write}}


def refused(code, message='max_tokens too small', kind='invalid_request_error'):
    error = urllib.error.HTTPError('https://api.anthropic.com/v1/messages', code, 'x', {}, io.BytesIO(b''))
    error.safe_api_message, error.api_error_type = message, kind
    return error


class Transport:
    def __init__(self, script):
        self.script, self.sent = list(script), []

    def __call__(self, p, c):
        self.sent.append(p)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return {'usage': step, 'stop_reason': 'max_tokens'}, 'req_test'


class KeeperPingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_with(self, script, renewal=False, budget=1.0):
        transport, waits, clock = Transport(script), [], iter(range(0, 100_000, 10))
        result = kp.execute(self.out, budget, renewal, transport=transport, sleeper=waits.append,
                            clock=lambda: next(clock))
        return result, transport, waits

    def test_a_refused_ping_climbs_the_ladder_and_the_entry_is_read_back(self):
        result, transport, waits = self.run_with([usage(write=8000, out=300), refused(400), usage(read=8000, out=16),
                                                  usage(read=8000, out=200)])
        self.assertEqual([p['max_tokens'] for p in transport.sent], [1024, 1, 16, 1024])  # accepted at 16: no check
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['ping_accepted_max_tokens'], 16)
        self.assertEqual(result['ping_refusals'], [{'max_tokens': 1, 'message': 'max_tokens too small'}])
        self.assertEqual((result['ping_output_tokens'], result['ping_read_share'], result['again_read_share']), (16, 1, 1))
        self.assertAlmostEqual(result['ping_extra_usd'], (15 * 20 + 5 * 4 - 20 * 8) / 1e6, places=9)  # 5 uncached input
        self.assertEqual(result['unknown_cost_requests'], 1)
        self.assertEqual(waits, [])
        self.assertEqual(len((self.out/'rows.jsonl').read_text().splitlines()), 4)
        for p in transport.sent:
            self.assertEqual((p['model'], p['output_config'], p['thinking']), (kp.MODEL, {'effort': 'xhigh'},
                                                                              {'type': 'adaptive'}))
            self.assertEqual(p['system'][0]['cache_control'], {'type': 'ephemeral', 'ttl': '1h'})
        self.assertEqual(len({json.dumps(dict(p, max_tokens=0)) for p in transport.sent}), 1)

    def test_a_ping_at_one_token_keeps_the_replays_go(self):
        result, _, _ = self.run_with([usage(write=8000), usage(read=8000, out=1), usage(read=8000, out=16), usage(read=8000)])
        self.assertAlmostEqual(result['replay_best_net_usd'], kp.REPLAY['net_usd'] - kp.REPLAY['pings'] * (5 * 4 - 20 * 8) / 1e6,
                               places=2)
        self.assertTrue(result['replay_still_go'])

    def test_a_ping_that_rewrites_part_of_the_prefix_can_undo_the_go(self):
        # 256 output tokens alone (the ladder's top) cost about 0.5 cents more and keep the go; a ping that also writes
        # 2,000 tokens doesn't.
        result, _, _ = self.run_with([usage(write=8000), usage(read=6000, write=2000, out=256), usage(read=8000, out=16),
                                      usage(read=8000)])
        self.assertFalse(result['replay_still_go'])

    def test_any_other_error_stops_the_run(self):
        result, transport, _ = self.run_with([usage(write=8000), refused(429, 'rate limited', 'rate_limit_error')])
        self.assertEqual((result['status'], len(transport.sent)), ('error', 2))
        result, transport, _ = self.run_with([refused(400, 'bad', 'invalid_request_error')])
        self.assertEqual((result['status'], len(transport.sent)), ('error', 1))

    def test_no_accepted_ping_skips_again(self):
        result, transport, _ = self.run_with([usage(write=8000)] + [refused(400)] * 4)
        self.assertEqual((result['status'], len(transport.sent)), ('no_ping_accepted', 5))

    def test_renewal_waits_before_the_ping_and_before_again(self):
        result, _, waits = self.run_with([usage(write=8000), usage(read=8000), usage(read=8000, out=16),
                                          usage(read=8000)], renewal=True)
        self.assertEqual(waits, [kp.WAIT_SECONDS, kp.WAIT_SECONDS])
        self.assertIn('kept_past_hour', result)

    def test_a_one_token_ping_is_followed_by_a_sixteen_token_stop_check(self):
        result, transport, _ = self.run_with([usage(write=8000), usage(read=8000, out=0), usage(read=8000, out=16),
                                              usage(read=8000)])
        self.assertEqual([p['max_tokens'] for p in transport.sent], [1024, 1, 16, 1024])
        self.assertEqual(result['stop_check']['output_tokens'], 16)
        self.assertEqual(result['ping_stop_reason'], 'max_tokens')
        result, transport, _ = self.run_with([usage(write=8000), usage(read=8000, out=0), refused(400), usage(read=8000)])
        self.assertEqual((result['status'], result['stop_check']['http_status']), ('complete', 400))

    def test_the_budget_stops_before_sending(self):
        result, transport, _ = self.run_with([usage(write=8000)], budget=0.01)
        self.assertEqual((result['status'], transport.sent), ('budget_stop', []))


if __name__ == '__main__':
    unittest.main()
