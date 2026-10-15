"""modelpilot.idle_replay on synthetic proxy logs (October 15, overnight cloud run): idle boundaries found per the
request's cache lifetime, the rewrite each paid, other rebuilds by cause, the options' savings at a follow rate, and
unknown transitions counted apart. $0; no recorded run is needed."""
import json
from pathlib import Path
import tempfile
import unittest
from modelpilot import idle_replay

MODEL = 'claude-sonnet-5-5'
RATES = {MODEL: dict(input=2, output=10, read=.1, write_5m=2.5, write_1h=4)}


def row(started, read, write, ttl='5m', wall=2.0, effort='low', model=MODEL):
    hour = write if ttl == '1h' else 0
    return {'kind': 'messages', 'model': model, 'effort': effort, 'http_status': 200, 'status': 'ok', 'tool_count': 3,
            'started_unix': started, 'wall_seconds': wall, 'cache_ttl': [ttl],
            'usage': {'input_tokens': 10, 'output_tokens': 20, 'cache_read_input_tokens': read,
                      'cache_creation_input_tokens': write,
                      'cache_creation': {'ephemeral_5m_input_tokens': write - hour, 'ephemeral_1h_input_tokens': hour}},
            'cost_usd': 0.001}


class IdleReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def trial(self, run, name, sessions):
        """A bench-style trial: sessions given as lists of rows, logged in order with their row ranges."""
        directory = run/'task'/name/'0'
        directory.mkdir(parents=True)
        rows, ranges = [], []
        for session in sessions:
            ranges.append([len(rows), len(rows) + len(session)])
            rows += session
        (directory/'observations.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
        (directory/'trial.json').write_text(json.dumps({'task': 'task', 'arm': name, 'trial': 0,
                                                        'sessions': [{'rows': r} for r in ranges]}))

    def test_a_follow_up_past_the_lifetime_is_a_boundary_and_its_rewrite_is_priced(self):
        run = self.root/'bench-x'
        # 5m: the follow-up 400 s after the last request ended rewrote the 4,000-token prefix (read 0, wrote 4,100).
        self.trial(run, 'expired', [[row(0, 0, 3000), row(10, 3000, 1000)], [row(412, 0, 4100)]])
        # 5m, a 100 s pause: still warm, read back, nothing rewritten.
        self.trial(run, 'warm', [[row(0, 0, 3000)], [row(102, 3000, 200)]])
        # 1h: 400 s is inside the hour.
        self.trial(run, 'hour', [[row(0, 0, 3000, ttl='1h')], [row(402, 3000, 200, ttl='1h')]])
        result = idle_replay.replay([run], RATES)
        self.assertEqual((result['transitions'], result['idle_boundaries'], result['short_pauses']), (3, 1, 2))
        self.assertEqual(result['boundary_rewrites']['tokens'], 4000)
        self.assertAlmostEqual(result['boundary_rewrites']['usd_as_recorded'], 4000 * 2.5 / 1e6)
        expired = next(r for r in result['rows'] if r['unit'].endswith('/expired/0'))['transitions'][0]
        self.assertEqual((expired['gap_seconds'], expired['boundary_seconds'], expired['ttl']), (400.0, 330, '5m'))
        hour = next(r for r in result['rows'] if r['unit'].endswith('/hour/0'))['transitions'][0]
        self.assertEqual((hour['idle_boundary'], hour['boundary_seconds']), (False, 3630))
        self.assertEqual(result['rebuilds_by_cause']['tokens']['rebuild_expired'], 4000)

    def test_option_savings_follow_the_rate_and_the_follow_rate(self):
        run = self.root/'bench-y'
        self.trial(run, 'expired', [[row(0, 0, 4000)], [row(402, 0, 4100)]])
        self.trial(run, 'warm', [[row(0, 0, 1000)], [row(50, 1000, 50)]])
        options = idle_replay.replay([run], RATES, follow=(0, .5, 1))['options']
        sonnet = options['claude-sonnet-5-5/5m']
        self.assertEqual(sonnet['c_active_boundary_request'], round(4000 * 2.5 / 1e6, 6))
        self.assertEqual(sonnet['b_prompt_hold'], {'0': 0.0, '0.5': round(.5 * 4000 * 2.5 / 1e6, 6), '1': round(4000 * 2.5 / 1e6, 6)})
        a = sonnet['a_conditional_notice']['1']
        self.assertEqual((a['saved'], a['early_switch_cost']), (round(4000 * 2.5 / 1e6, 6), round(1000 * 2.5 / 1e6, 6)))
        self.assertAlmostEqual(a['net'], (4000 - 1000) * 2.5 / 1e6)
        self.assertEqual(options['claude-opus-5-5/1h']['c_active_boundary_request'], round(4000 * 8 / 1e6, 6))
        self.assertEqual(sonnet['d_leave_it'], 0.0)

    def test_a_compaction_session_is_not_the_boundary_request(self):
        run = self.root/'bench-c'
        compaction = dict(row(400, 4000, 600), compaction=True)  # a sequence's /compact turn: reads, summarizes
        self.trial(run, 'compact', [[row(0, 0, 4000)], [compaction], [row(410, 0, 1000)]])
        result = idle_replay.replay([run], RATES)
        # Both transitions around a compaction are left out, counted as unknown: the summary turn starts a new prefix.
        self.assertEqual([t['reason'] for t in result['rows'][0]['transitions']], ['no_answered_main_loop_request'] * 2)
        self.assertEqual((result['unknown_transitions'], result['idle_boundaries']), (2, 0))

    def test_unknown_transitions_are_counted_never_priced(self):
        run = self.root/'bench-z'
        lost = dict(row(402, 0, 4100), usage={}, cost_usd=None)
        self.trial(run, 'lost', [[row(0, 0, 4000)], [lost]])
        self.trial(run, 'empty', [[row(0, 0, 4000)], []])
        result = idle_replay.replay([run], RATES)
        self.assertEqual((result['unknown_transitions'], result['idle_boundaries']), (2, 0))
        self.assertEqual(result['boundary_rewrites']['tokens'], 0)

    def test_a_governed_session_splits_at_its_invocations(self):
        session = self.root/'governed-rebuild-x'
        session.mkdir()
        rows = [row(0, 0, 3000), row(336 + 2, 0, 3100), row(400, 3100, 50, effort='medium')]
        (session/'observations.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
        (session/'summary.json').write_text(json.dumps({'invocations': [{'rows_from': 0, 'rows_to': 1},
                                                                         {'rows_from': 1, 'rows_to': 2},
                                                                         {'rows_from': 2, 'rows_to': 3}]}))
        result = idle_replay.replay([session], RATES)
        self.assertEqual((result['transitions'], result['idle_boundaries']), (2, 1))
        first, second = result['rows'][0]['transitions']
        self.assertEqual((first['idle_boundary'], first['rewritten_tokens']), (True, 3000))
        self.assertEqual((second['same_setting'], second['idle_boundary']), (False, False))


if __name__ == '__main__':
    unittest.main()
