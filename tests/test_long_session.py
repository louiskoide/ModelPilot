import json
import os
from pathlib import Path
import shutil
import sys
import threading
import unittest
from modelpilot import bench, bench_report, bench_tasks, long_session, proxy
from modelpilot.fixtures import fixture_server
from tests import test_bench_tasks as synthetic
from tests.test_bench import RATES, Clock, FakeTrial


class PlanTests(unittest.TestCase):
    def test_task_turns_are_gap_seconds_apart(self):
        self.assertEqual(long_session.plan(3, 600), [{'kind': 'task', 'index': 0, 'gap_before': 0},
                                                     {'kind': 'task', 'index': 1, 'gap_before': 600},
                                                     {'kind': 'task', 'index': 2, 'gap_before': 600}])

    def test_warm_compaction_comes_before_the_gap_and_cold_after_it(self):
        warm = [(s['kind'], s['index'], s['gap_before']) for s in long_session.plan(3, 600, 'warm')]
        self.assertEqual(warm, [('task', 0, 0), ('compact', 0, 0), ('task', 1, 600), ('compact', 1, 0), ('task', 2, 600)])
        cold = [(s['kind'], s['index'], s['gap_before']) for s in long_session.plan(3, 600, 'cold')]
        self.assertEqual(cold, [('task', 0, 0), ('compact', 0, 600), ('task', 1, 0), ('compact', 1, 600), ('task', 2, 0)])
        with self.assertRaises(ValueError):
            long_session.plan(3, 0, 'sometimes')


class SequenceTests(unittest.TestCase):
    def test_sequences_follow_the_rule(self):
        tasks = [{'id': f'{repo}-{n}', 'repo': repo} for repo, count in (('a', 2), ('b', 3), ('c', 6), ('d', 8), ('e', 11))
                 for n in range(count)]
        splits = {'tuning': [t['id'] for t in tasks if t['id'] != 'e-10'], 'final': ['e-10']}
        made = long_session.make_sequences(tasks, splits, when=lambda t: -int(t['id'].split('-')[1]))
        self.assertEqual({k: len(v) for k, v in made.items()},
                         {'b': 3, 'c-a': 3, 'c-b': 3, 'd-a': 4, 'd-b': 4, 'e-a': 5, 'e-b': 5})
        self.assertEqual(made['b'], ['b-2', 'b-1', 'b-0'])  # base-commit order (here: newest id first)
        self.assertNotIn('e-10', sum(made.values(), []))  # final tasks never

    def test_the_committed_sequences_cover_each_tuning_task_once(self):
        data = json.loads(long_session.SEQUENCES.read_text())
        self.assertEqual(data['rule'], long_session.SEQUENCE_RULE)
        listed = [t for ids in data['sequences'].values() for t in ids]
        self.assertEqual(len(listed), len(set(listed)))
        tuning = json.loads(bench_tasks.SPLITS.read_text())['tuning']
        repos = {}
        for task in bench_tasks.load():
            if task['id'] in tuning and not task.get('batch'):
                repos.setdefault(task['repo'], []).append(task['id'])
        expected = sorted(t for ids in repos.values() if len(ids) >= long_session.MIN_TASKS for t in ids)
        self.assertEqual(sorted(listed), expected)
        for sequence in long_session.load():
            self.assertTrue(long_session.MIN_TASKS <= len(sequence['tasks']) <= long_session.MAX_TASKS)
        with self.assertRaises(ValueError):
            long_session.load(['no-such-sequence'])

    def test_delegation_arms_and_out_of_range_autocompact_are_refused(self):
        sequence = {'id': 's', 'tasks': [{'id': 'x', 'instruction': 'Do x.', 'repo': 'r'}] * 3}
        with self.assertRaises(ValueError):
            long_session.SequenceTrial(sequence, 'modelpilot-delegate', '/nonexistent', 'claude', None, 'http://x', RATES)
        with self.assertRaises(ValueError):
            long_session.SequenceTrial(sequence, 'sonnet-5', '/nonexistent', 'claude', None, 'http://x', RATES,
                                       autocompact=50_000)


class GapTests(unittest.TestCase):
    def test_a_trial_parks_for_its_own_next_gap(self):
        clock = Clock()
        short, long = FakeTrial('S', clock, sessions=2), FakeTrial('L', clock, sessions=2)
        short.next_gap, long.next_gap = (lambda: 50), (lambda: 1000)
        bench.interleave([long, short], 330, clock=clock, sleep=clock.sleep)
        self.assertEqual(clock.log, [('L', 1), ('S', 1), ('S', 2), ('L', 2)])
        self.assertEqual(clock.sleeps, [50, 750])  # S waits its own 50 s from t=200, L its 1000 s from t=100


class MarkerTests(unittest.TestCase):
    def test_cache_ttls_and_compaction_requests_are_read_from_the_body(self):
        request = {'system': [{'type': 'text', 'text': 'a', 'cache_control': {'type': 'ephemeral', 'ttl': '1h'}}],
                   'messages': [{'role': 'user', 'content': [
                       {'type': 'text', 'text': 'b', 'cache_control': {'type': 'ephemeral'}},
                       {'type': 'text', 'text': proxy.COMPACTION_MARKER + '\n- Do NOT use Read.'}]}]}
        self.assertEqual(proxy.cache_ttls(request), ['1h', '5m'])
        self.assertTrue(proxy.compaction_request(request))
        request['messages'].append({'role': 'user', 'content': 'go on'})
        self.assertFalse(proxy.compaction_request(request))
        # After an assistant reply the instruction is a user message of its own.
        request['messages'] += [{'role': 'assistant', 'content': 'Done.'},
                                {'role': 'user', 'content': proxy.COMPACTION_MARKER + '\n\nSummarize.'}]
        self.assertTrue(proxy.compaction_request(request))
        self.assertEqual(proxy.cache_ttls({'messages': [{'role': 'user', 'content': 'x'}]}), [])

    def test_the_ttl_check_leaves_out_compaction_requests(self):
        rows = [{'kind': 'messages', 'tool_count': 6, 'cache_ttl': ['1h']},
                {'kind': 'messages', 'tool_count': 6, 'cache_ttl': ['5m'], 'compaction': True},
                {'kind': 'messages', 'tool_count': 0, 'cache_ttl': ['5m']}]  # a side request
        self.assertEqual(bench.ttl_check(rows, '1h'), {'requested': '1h', 'marked': {'1h': 1}, 'compaction_requests': 1,
                                                       'applied': True})
        self.assertFalse(bench.ttl_check(rows + [{'kind': 'messages', 'tool_count': 6, 'cache_ttl': ['5m']}], '1h')['applied'])

    def test_an_arm_that_sets_its_ttl_is_priced_as_sent_on_a_subscription(self):
        usage = {'input_tokens': 2, 'output_tokens': 100, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 8000,
                 'cache_creation': {'ephemeral_5m_input_tokens': 0, 'ephemeral_1h_input_tokens': 8000}}
        rows = [{'kind': 'messages', 'http_status': 200, 'model': 'claude-sonnet-5-5', 'usage': usage,
                 'cost_usd': (2*2 + 100*10 + 8000*4) / 1e6}]
        self.assertIs(bench_report.priced_rows({'auth': 'subscription', 'prompt_cache_ttl': '1h'}, rows, RATES), rows)
        self.assertIsNot(bench_report.priced_rows({'auth': 'subscription'}, rows, RATES), rows)
        out = bench.subscription_accounting(rows, {}, RATES, ttl='1h')
        self.assertAlmostEqual(out['cost_usd'], rows[0]['cost_usd'])
        self.assertEqual((out['cost_scope'], out['billing']), ('complete', 'subscription'))  # no repricing label
        self.assertEqual(bench_report.ineligible_reason({'ttl_check': {'applied': False}}), 'ttl_not_applied')


class TurnTests(unittest.TestCase):
    def test_turns_split_cost_first_reads_and_compaction_by_session(self):
        def row(t, read, write, cost, **extra):
            return dict({'kind': 'messages', 'tool_count': 6, 'http_status': 200, 'started_unix': t, 'cost_usd': cost,
                         'usage': {'cache_read_input_tokens': read, 'cache_creation_input_tokens': write,
                                   'output_tokens': 50}}, **extra)
        rows = [row(1, 0, 9000, .03), row(2, 9000, 500, .01),  # task 1: cold start
                row(3, 9500, 300, .02, compaction=True, cache_ttl=['5m']),  # compaction while warm
                row(700, 0, 4000, .02, cache_ttl=['1h'])]  # task 2 after the gap: the compacted prefix is written
        record = {'auth': 'api_key', 'sessions': [
            {'prompt': 'task', 'subtask': 0, 'rows': [0, 2], 'requests': 2, 'stop': 'success', 'gap_seconds': None},
            {'prompt': 'compact', 'subtask': 0, 'rows': [2, 3], 'requests': 1, 'stop': 'success', 'gap_seconds': 1.0},
            {'prompt': 'task', 'subtask': 1, 'rows': [3, 4], 'requests': 1, 'stop': 'success', 'gap_seconds': 600.0}]}
        out = long_session.turns(record, rows, RATES)
        self.assertEqual([t['prompt'] for t in out], ['task', 'compact', 'task'])
        self.assertAlmostEqual(out[0]['cost_usd'], .04)
        self.assertEqual((out[1]['compaction_requests'], out[1]['compaction_cost_usd'], out[1]['compaction_output_tokens']),
                         (1, .02, 50))
        self.assertEqual((out[2]['first_read_tokens'], out[2]['first_write_tokens'], out[2]['first_write_ttl']),
                         (0, 4000, ['1h']))
        self.assertEqual(out[0]['compaction_cost_usd'], 0.0)


class ReportTests(unittest.TestCase):
    def trial(self, arm, sequence, costs, passed, compact_cost=None):
        tasks = [f'{sequence}-{i}' for i in range(len(costs))]
        turn_list = []
        for i, cost in enumerate(costs):
            if i and compact_cost is not None:
                turn_list.append({'prompt': 'compact', 'subtask': i - 1, 'cold_equivalent_usd': compact_cost,
                                  'compaction_cost_usd': compact_cost, 'compaction_output_tokens': 900})
            turn_list.append({'prompt': 'task', 'subtask': i, 'cold_equivalent_usd': cost,
                              'return_read_fraction': None if i == 0 else (.95 if arm.endswith('1h') else .2)})
        record = {'arm': arm, 'task': sequence, 'shape': 'sequence', 'complete': True,
                  'sequence': {'tasks': tasks}, 'cache': {'cold_equivalent_cost_usd': sum(costs) + (compact_cost or 0) * 2},
                  'subtasks': [{'task': t, 'passed': ok} for t, ok in zip(tasks, passed)]}
        return record, turn_list

    def test_subtasks_pair_across_arms_with_compaction_charged_to_the_next_task(self):
        trials = []
        for n in range(12):
            trials.append(self.trial('a-5m', f's{n}', [.05, .08, .09], [True, True, n % 4 != 0]))
            trials.append(self.trial('b-1h', f's{n}', [.06, .06, .06], [True, True, True], compact_cost=.01))
        out = long_session.report(trials, resamples=500)
        a, b = out['arms']['a-5m'], out['arms']['b-1h']
        self.assertEqual((a['subtasks'], a['subtasks_passed'], b['subtasks_passed']), (36, 33, 36))
        self.assertEqual((a['returns'], a['returns_reading_conversation'], b['returns_reading_conversation']), (24, 0, 24))
        self.assertEqual((b['compaction_turns'], a['compaction_turns']), (24, 0))
        self.assertAlmostEqual(b['compaction_usd'], .24)
        pair = out['pairs'][0]
        self.assertEqual((pair['arms'], pair['subtasks']), (['a-5m', 'b-1h'], 36))
        # Per subtask: a costs .05/.08/.09, b .06/.07/.07 with its compactions; mean difference +0.0067.
        self.assertAlmostEqual(pair['cost_per_subtask_usd']['estimate'], (-.01 + .01 + .02) / 3)
        self.assertTrue(pair['cost_per_subtask_usd']['shows_difference'])  # 36 pairs, every one the same
        self.assertAlmostEqual(pair['pass_rate']['estimate'], -3 / 36)


def fix_steps(work, index):
    """One task turn's scripted agent: read, fix and test task{index+1}/, then finish."""
    path = str(work/long_session.directory(index)/'pkg'/'__init__.py')
    return [{'tool': 'Read', 'input': {'file_path': path}},
            {'tool': 'Write', 'input': {'file_path': path, 'content': synthetic.FIXED}},
            {'tool': 'Bash', 'input': {'command': f'echo "$PYTHONPATH" >> {work.parent}/paths.txt && '
                                                  f'cd {long_session.directory(index)} && python3 -m unittest -q',
                                       'description': 'tests'}},
            {'text': 'Done.'}]


@unittest.skipUnless(shutil.which('claude'), 'Claude Code CLI not installed')
class OfflineSequenceTests(unittest.TestCase):
    """Real client, fake key, scripted loopback upstream, synthetic repository: $0, no provider traffic."""
    def setUp(self):
        self.repo_case = synthetic.BenchTaskTests('test_valid_task_fails_on_base_and_passes_on_reference')
        self.repo_case.setUp()
        base = dict(self.repo_case.task, repo_path=str(self.repo_case.repo), pythonpath='.')
        self.sequence = {'id': 'synthetic-seq', 'tasks': [dict(base, id=f'synthetic-{n}') for n in range(3)]}
        self.upstream = fixture_server()
        self.upstream.keep_bodies = True
        self.thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.thread.start()
        self.out = self.repo_case.root/'bench'
        self.cli, self.version = bench.resolve_client(shutil.which('claude'))

    def tearDown(self):
        self.upstream.shutdown()
        self.upstream.server_close()
        self.thread.join()
        self.repo_case.tearDown()

    def run_sequence(self, name, script, arm, rates=RATES, **options):
        self.upstream.script = script
        trial = long_session.SequenceTrial(self.sequence, arm, self.out/name, self.cli, 'sk-ant-offline-not-a-key',
                                           f'http://127.0.0.1:{self.upstream.server_port}', rates,
                                           **dict(dict(python=sys.executable, max_turns=8, client_version=self.version),
                                                  **options))
        try:
            while trial.step():
                pass
        finally:
            trial.close()
        return trial.record

    def test_each_task_turn_works_in_its_own_checkout_and_every_subtask_is_graded(self):
        work = self.out/'fixes'/'workspace'
        script = [step for i in range(3) for step in fix_steps(work, i)]
        record = self.run_sequence('fixes', script, 'sonnet-5.5-low-concise-1h')
        self.assertEqual([s['prompt'] for s in record['sessions']], ['task'] * 3)
        self.assertEqual([s['stop'] for s in record['sessions']], ['success'] * 3)
        self.assertEqual(([s['passed'] for s in record['subtasks']], record['passed'], record['subtasks_passed']),
                         ([True] * 3, True, 3))
        self.assertFalse(any(s['changed_after_turn'] for s in record['subtasks']))
        self.assertEqual((self.out/'fixes'/'paths.txt').read_text().split(),
                         [str(work/long_session.directory(i)) for i in range(3)])  # each turn its own import path
        for i in range(3):
            self.assertIn(b'items[-1]', (self.out/'fixes'/'subtasks'/long_session.directory(i)/'agent.diff').read_bytes())
        self.assertFalse(work.exists())
        # One session: each task turn resumes the last, with that task's instruction in the given directory.
        bodies = [json.loads(b) for b in self.upstream.bodies]
        main = [b for b in bodies if b.get('tools')]
        for i in range(3):
            self.assertTrue(any('task' + str(i + 1) + '/' in json.dumps(b['messages'][-3:]) for b in main))
        self.assertIn('Fix last().', json.dumps(main[-1]['messages']))
        self.assertEqual(sum(json.dumps(m).count('a repository of its own') for m in main[-1]['messages']), 3)
        # The arm's cache lifetime reached every request, and the accounting holds across the three invocations.
        self.assertEqual(record['ttl_check'], {'requested': '1h', 'marked': {'1h': 12}, 'compaction_requests': 0,
                                               'applied': True})
        self.assertTrue(all(proxy.cache_ttls(b) == ['1h'] for b in main))
        self.assertTrue(record['accounting']['tokens_match'], record['accounting'])
        rows = [json.loads(l) for l in (self.out/'fixes'/'observations.jsonl').read_text().splitlines()]
        turns = long_session.turns(record, rows, RATES)
        self.assertAlmostEqual(sum(t['cost_usd'] for t in turns), record['accounting']['cost_usd'])
        self.assertEqual(record['sequence']['tasks'], ['synthetic-0', 'synthetic-1', 'synthetic-2'])

    def test_a_compaction_turn_follows_each_task_but_the_last(self):
        record = self.run_sequence('compact', [{'text': 'Done.'}], 'sonnet-5.5-low-concise-1h', compact='warm')
        self.assertEqual([s['prompt'] for s in record['sessions']], ['task', 'compact', 'task', 'compact', 'task'])
        self.assertEqual([s['stop'] for s in record['sessions']], ['success'] * 5)
        rows = [json.loads(l) for l in (self.out/'compact'/'observations.jsonl').read_text().splitlines()]
        turns = long_session.turns(record, rows, RATES)
        self.assertEqual([t['compaction_requests'] for t in turns], [0, 1, 0, 1, 0])
        # The client marks its compaction request with the default 5m whatever the arm asks for.
        self.assertEqual([r['cache_ttl'] for r in rows if r.get('compaction')], [['5m'], ['5m']])
        self.assertEqual(record['ttl_check']['compaction_requests'], 2)
        self.assertTrue(record['ttl_check']['applied'])
        # Nothing was fixed: each subtask fails on its own hidden test.
        self.assertEqual([s['grade']['reason'] for s in record['subtasks']], ['hidden_tests_failed'] * 3)
        self.assertFalse(record['passed'])

    def test_the_set_lifetime_holds_on_a_subscription_too(self):
        token = 'sk-ant-oat01-offline-fixture-not-a-token-1234'
        for arm, ttl in (('sonnet-5.5-low-concise-5m', '5m'), ('sonnet-5.5-low-concise-1h', '1h')):
            record = self.run_sequence(arm, [{'text': 'Done.'}], arm, auth='subscription', oauth_token=token)
            self.assertEqual(record['ttl_check']['marked'], {ttl: 3}, arm)
            self.assertTrue(record['ttl_check']['applied'], arm)
            self.assertIn('none', record['accounting']['repricing'])  # priced as sent: the lifetime is the variable

    @unittest.skipUnless(shutil.which('node'), 'Node not installed (the advisor bridge)')
    def test_the_modelpilot_arm_decides_at_each_task_turn(self):
        from unittest import mock
        from modelpilot import fixtures, switch_policy
        from tests.test_bench_jev import ACCOUNT_CATALOG, POLICY_TIERS
        config = switch_policy.load()
        stub = {'model': {'choice': 'claude-sonnet-5-5', 'confidence': .9,
                          'probabilities': {m: .9 if m == 'claude-sonnet-5-5' else .05 for m in POLICY_TIERS}},
                'effort': {'choice': 'low', 'confidence': .9,
                           'probabilities': {e: .9 if e == 'low' else .025 for e in config['effort_order']}}}
        with mock.patch.object(switch_policy, 'load', return_value=config):
            adapter = bench.arm_adapter('modelpilot', 1, 3, advisor_stub=stub)
            with mock.patch.object(fixtures, 'CATALOG', ACCOUNT_CATALOG):
                record = self.run_sequence('modelpilot', [{'text': 'Done.'}], 'modelpilot', adapter=adapter,
                                           rates=dict(RATES, **{'claude-opus-5-5': dict(input=4, output=20, read=.2,
                                                                                         write_5m=5, write_1h=8)}))
        self.assertEqual([s['stop'] for s in record['sessions']], ['success'] * 3)
        decisions = record['routing']['policy']['decisions']
        self.assertEqual([d['trigger'] for d in decisions], ['turn_start'] * 3)  # one per task turn
        self.assertTrue(all(d['action'] == 'stay' for d in decisions))
        self.assertEqual(record['sequence']['tasks'], ['synthetic-0', 'synthetic-1', 'synthetic-2'])

    def test_a_turn_that_does_not_succeed_ends_the_sequence(self):
        loop = [{'tool': 'Read', 'input': {'file_path': '/dev/null'}}] * 4 + [{'text': 'Done.'}]
        record = self.run_sequence('stopped', loop, 'sonnet-5.5-low-concise-5m', max_turns=1)
        self.assertEqual([s['stop'] for s in record['sessions']], ['turn_limit'])
        self.assertEqual(record['follow_up'], 'skipped_after_turn_limit')
        self.assertEqual([s.get('reason') for s in record['subtasks']][1:], ['not_reached'] * 2)
        self.assertFalse(record['passed'])


if __name__ == '__main__':
    unittest.main()
