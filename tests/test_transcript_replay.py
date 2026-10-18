"""modelpilot.transcript_replay on synthetic transcripts (October 17): one request per response and no message text
kept, a copied response kept with its session, the keeper's pings, a resume's saving, a session start's shared saving
scaled by the match rate, the precondition, and the decision gated on it. $0; no transcript of the user's is read."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from modelpilot import transcript_replay as tr

OPUS = 'claude-opus-5-5'
RATES = {OPUS: dict(input=4, output=20, read=.2, write_5m=5, write_1h=8)}
T0 = 1_790_000_000


def record(t, mid, read, write, out=10, sidechain=False, model=OPUS):
    usage = {'input_tokens': 10, 'output_tokens': out, 'cache_read_input_tokens': read,
             'cache_creation_input_tokens': write,
             'cache_creation': {'ephemeral_5m_input_tokens': 0, 'ephemeral_1h_input_tokens': write}}
    return {'type': 'assistant', 'timestamp': datetime.fromtimestamp(t, timezone.utc).isoformat().replace('+00:00', 'Z'),
            'isSidechain': sidechain, 'sessionId': 's',
            'message': {'id': mid, 'model': model, 'role': 'assistant', 'usage': usage,
                        'content': [{'type': 'text', 'text': 'SECRET-TEXT'}]}}


class TranscriptReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def session(self, name, records, project='proj', extra=''):
        path = self.root/project/f'{name}.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(r) + '\n' for r in records) + extra)

    def replay(self):
        return tr.replay(tr.load(self.root)[0], RATES)

    def test_one_request_per_response_and_no_message_text(self):
        self.session('a', [record(T0, 'm1', 0, 1000, out=5), record(T0 + 3, 'm1', 0, 1000, out=50),
                           record(T0 + 9, 'm2', 1000, 10, sidechain=True), record(T0 + 9, 'm3', 0, 0, model='<synthetic>')],
                     extra='{not json\n')
        requests, skipped = tr.load(self.root)
        self.assertEqual([r.usage['output_tokens'] for r in requests], [50, 10])
        self.assertEqual((requests[0].start, requests[0].end), (T0, T0 + 3))
        self.assertEqual([r.sidechain for r in requests], [False, True])
        self.assertEqual(skipped, {'unparsed_lines': 1, 'synthetic': 1, 'copies': 0})
        self.assertNotIn('SECRET-TEXT', repr(requests))

    def test_a_copied_response_stays_with_the_session_that_made_it(self):
        self.session('a', [record(T0, 'm1', 0, 1000)])
        self.session('b', [record(T0, 'm1', 0, 1000), record(T0 + 7200, 'm2', 0, 1500)])
        requests, skipped = tr.load(self.root)
        self.assertEqual([(r.session, r.usage['cache_creation_input_tokens']) for r in requests],
                         [('proj/a.jsonl', 1000), ('proj/b.jsonl', 1500)])
        self.assertEqual(skipped['copies'], 1)

    def test_pings(self):
        self.assertEqual(tr.pings(3000, 1), (0, True))
        self.assertEqual(tr.pings(3700, 1), (1, True))
        self.assertEqual(tr.pings(8000, 1), (1, False))
        self.assertEqual(tr.pings(7200, 2), (2, True))
        self.assertEqual(tr.pings(None, 24), (26, False))

    def test_a_resume_saves_the_rewrite_when_the_keeper_bridges_it(self):
        self.session('a', [record(T0, 'm1', 0, 100_000), record(T0 + 7200, 'm2', 0, 102_000)])
        result = self.replay()
        by = {s['setting']: s for s in result['settings']}
        ping = lambda kept: (kept * .2 + 20 * 8 + 20) / 1e6
        shared = 4 * ping(0)  # no warm start, so the shared prefix's size is unknown and its pings read nothing
        self.assertAlmostEqual(by['K_all(2h)']['saved_usd'], .78, places=4)
        self.assertAlmostEqual(by['K_all(2h)']['pings_usd'], 2 * ping(100_010) + 2 * ping(102_010) + shared, places=4)
        self.assertEqual(by['K_all(1h)']['saved_usd'], 0)
        self.assertEqual(by['K_shared(2h)']['saved_usd'], 0)
        self.assertEqual(result['resumes_after_55_min'], 1)
        self.assertAlmostEqual(result['upper_bound_usd'], .78 - 2 * ping(100_010), places=4)

    def test_a_cold_start_saves_the_shared_prefix_scaled_by_the_match_rate(self):
        self.session('a', [record(T0, 'a1', 0, 114_000)])
        self.session('b', [record(T0 + 600, 'b1', 100_000, 14_000)])   # warm: S = 100,000
        self.session('c', [record(T0 + 1200, 'c1', 0, 50_000)])        # close but cold: another prefix
        self.session('d', [record(T0 + 6200, 'd1', 0, 114_000)])       # 5,000 s after the last request
        result = self.replay()
        self.assertEqual(result['match_rate'], {'warm': 1, 'of': 2, 'rate': .5})
        by = {s['setting']: s for s in result['settings']}
        self.assertAlmostEqual(by['K_shared(1h)']['saved_usd'], .5 * 100_000 * 7.8 / 1e6, places=4)
        self.assertEqual(result['starts_after_55_min'], 1)

    def test_the_precondition_counts_warm_starts_past_an_hour_with_no_hour_long_pause(self):
        self.session('w', [record(T0, 'w1', 0, 100_000)] +
                     [record(T0 + 3000 * k, f'w{k + 1}', 100_000, 500) for k in range(1, 3)])
        self.session('x', [record(T0 + 7000, 'x1', 100_000, 5_000)])   # 7,000 s after the write: counts
        self.session('y', [record(T0 + 7000 + 4000, 'y1', 100_000, 5_000)], project='other')  # after a 4,000 s pause
        result = self.replay()
        self.assertEqual(result['precondition']['warm_starts_past_an_hour'], 1)
        self.assertFalse(result['precondition']['met'])

    def test_no_go_without_the_precondition_whatever_the_saving(self):
        self.session('a', [record(T0, 'm1', 0, 100_000), record(T0 + 7200, 'm2', 0, 102_000)])
        result = self.replay()
        self.assertGreater(result['best_share'], tr.GO_SHARE)
        self.assertEqual(result['decision'], 'no-go')


if __name__ == '__main__':
    unittest.main()
