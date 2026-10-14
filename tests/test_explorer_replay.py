"""Plan item 14's explorer replay ($0): transcript parsing, which results an explorer could replace, and the dollars of
one replayed trial worked out by hand. No model request."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from modelpilot import explorer_replay as er

MAIN = 'claude-sonnet-4-6'
RATES = {MAIN: dict(input=3, output=15, read=.3, write_5m=3.75, write_1h=6),
         er.HELPER: dict(input=.1, output=.5, read=.01, write_5m=.125, write_1h=.2)}


def row(at, created, read, output=50, tools=1, **extra):
    return dict({'kind': 'messages', 'tool_count': tools, 'http_status': 200, 'model': MAIN, 'started_unix': at,
                 'usage': {'input_tokens': 5, 'cache_creation_input_tokens': created, 'cache_read_input_tokens': read,
                           'output_tokens': output}}, **extra)


def call(id_, name, **input_):
    return {'type': 'tool_use', 'id': id_, 'name': name, 'input': input_}


def turn(id_, *calls):
    return {'id': id_, 'calls': list(calls)}


class ParsingTests(unittest.TestCase):
    def test_read_only_bash_commands(self):
        for command, reads in (('sed -n 1,80p pkg/core.py', True), ('cd repo && grep -n parse src/*.py', True),
                               ('cat a.py | head -20', True), ('sed -i s/a/b/ x.py', False), ('cat a > b', False),
                               ('python -m pytest -q', False), ('grep x a.py 2>/dev/null', True), ('', False)):
            self.assertEqual(er.bash_reads(command), reads, command)

    def test_a_transcript_folds_one_responses_events_into_one_turn(self):
        events = [{'type': 'system', 'subtype': 'init'},
                  {'type': 'assistant', 'message': {'id': 'm1', 'content': [{'type': 'thinking', 'thinking': ''}]}},
                  {'type': 'assistant', 'message': {'id': 'm1', 'content': [call('t1', 'Read', file_path='a.py')]}},
                  {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't1',
                                                            'content': [{'type': 'text', 'text': 'abc'}]}]}},
                  {'type': 'assistant', 'message': {'id': 'm2', 'content': [{'type': 'text', 'text': 'Done.'}]}}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'client.stdout.jsonl'
            path.write_text('\n'.join(json.dumps(e) for e in events) + '\nnot json\n')
            turns, results = er.transcript(path)
        self.assertEqual([(t['id'], len(t['calls'])) for t in turns], [('m1', 1), ('m2', 0)])
        self.assertEqual(results, {'t1': 'abc'})

    def test_reads_of_files_edited_later_stay_and_bash_reads_count_only_in_the_wider_scope(self):
        turns = [turn('m1', call('a', 'Read', file_path='/w/core.py'), call('b', 'Grep', pattern='x'),
                      call('c', 'Bash', command='sed -n 1,40p /w/util.py'), call('d', 'Bash', command='pytest -q')),
                 turn('m2', call('e', 'Edit', file_path='/w/core.py'), call('f', 'Read', file_path='/w/util.py')),
                 turn('m3')]
        results = {k: 'x' * 280 for k in 'abcdf'}
        tools, kept = er.candidates(turns, results, 'tools')
        self.assertEqual(([c['tool'] for c in tools], kept), (['Grep', 'Read'], 1))  # core.py's read stays for the edit
        wider, _ = er.candidates(turns, results, 'tools_and_bash')
        self.assertEqual([c['tool'] for c in wider], ['Grep', 'Bash', 'Read'])
        self.assertAlmostEqual(wider[0]['tokens'], 280 / er.BYTES_PER_TOKEN)


class ReplayTests(unittest.TestCase):
    # Three main-loop requests; the first reads a 2,800-byte file (1,000 tokens) the agent never edits.
    ROWS = [row(1, 5000, 0, output=100), row(2, 1500, 5000), row(3, 300, 6500),
            row(2.5, 900, 0, tools=0)]  # a side request (no tools) is not the main loop
    TURNS = [turn('m1', call('a', 'Read', file_path='/w/big.py')), turn('m2', call('b', 'Bash', command='pytest')),
             turn('m3')]
    RESULTS = {'a': 'x' * 2800, 'b': 'ok'}

    def test_one_trial_at_k_200_by_hand(self):
        out = er.replay_trial(self.ROWS, self.TURNS, self.RESULTS, RATES, 200, 'tools', 'all', 'low')
        # 800 tokens saved: written on request 2 (it wrote 1,500), read on request 3.
        self.assertAlmostEqual(out['saving'], 800 * (3.75 + .3) / 1e6)
        # Haiku reads the overhead and the file once and writes the 200-token summary.
        self.assertAlmostEqual(out['helper'], ((2000 + 1000) * .1 + 200 * .5) / 1e6)
        # The 50-token delegation call (output, then growth) and the 1,000-token tool definition in every prefix.
        self.assertAlmostEqual(out['overhead'], (50 * 15 + 50 * (3.75 + .3) + 1000 * (3.75 + .3 + .3)) / 1e6)
        self.assertAlmostEqual(out['net'], out['saving'] - out['helper'] - out['overhead'])
        self.assertEqual(out['replaced'], 1)

    def test_a_summary_longer_than_its_result_costs_and_over_k_skips_it(self):
        small = dict(self.RESULTS, a='x' * 280)  # 100 tokens
        out = er.replay_trial(self.ROWS, self.TURNS, small, RATES, 200, 'tools', 'all', 'low')
        self.assertAlmostEqual(out['saving'], -100 * (3.75 + .3) / 1e6)
        skipped = er.replay_trial(self.ROWS, self.TURNS, small, RATES, 200, 'tools', 'over_k', 'low')
        self.assertEqual((skipped['replaced'], skipped['net']), (0, 0))  # nothing replaced, nothing offered

    def test_a_trial_whose_requests_and_turns_differ_is_left_out(self):
        self.assertIsNone(er.replay_trial(self.ROWS[:2], self.TURNS, self.RESULTS, RATES, 200, 'tools', 'all', 'low'))
        trials = [{'run': 'r', 'task': 't', 'arm': 'sonnet-5.5', 'trial': 0, 'cost': .05, 'rows': self.ROWS,
                   'turns': self.TURNS, 'results': self.RESULTS},
                  {'run': 'r', 'task': 't', 'arm': 'sonnet-5.5', 'trial': 1, 'cost': .05, 'rows': self.ROWS[:2],
                   'turns': self.TURNS, 'results': self.RESULTS},
                  {'run': 'r', 'task': 't', 'arm': 'sonnet-5.5', 'trial': 2, 'cost': .05,
                   'rows': [dict(r, model='unpriced') for r in self.ROWS], 'turns': self.TURNS, 'results': self.RESULTS}]
        report = er.replay(trials, RATES, k_grid=(50, 200, 1600))
        self.assertEqual((report['trials'], len(report['unaligned']), len(report['unpriced'])), (1, 1, 1))
        arm = report['arms']['sonnet-5.5']
        low = arm['readings']['tools/all/low']
        self.assertEqual(set(low['mean_net_usd_per_trial']), {50, 200, 1600})
        self.assertLess(low['mean_net_usd_per_trial'][1600], low['mean_net_usd_per_trial'][50])  # longer summaries pay less
        self.assertIsNone(low['break_even_k'])  # here the tool definition alone outweighs one 1,000-token read
        self.assertEqual(arm['candidates']['tools']['mean_results'], 1)

    def test_the_cli_needs_the_dev_runs(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(er, 'dev_trials', return_value=[]), \
                mock.patch('sys.argv', ['explorer_replay', '--out', str(Path(tmp)/'x.json')]):
            with self.assertRaises(SystemExit) as stop:
                er.main()
            self.assertFalse((Path(tmp)/'x.json').exists())
        self.assertIn('dev split', str(stop.exception))


if __name__ == '__main__':
    unittest.main()
