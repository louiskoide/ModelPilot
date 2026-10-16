"""Unknown cost never becomes $0 (October 15, overnight cloud run; docs/bench-adapters.md, "Unknown-cost audit"). Each
function that sums or averages dollars or tokens gets one input where everything is known and the same input with one
row's cost unknown or one usage field missing. With the gap it must return unknown, or a figure explicitly labelled
as partial (a count of what it left out); never a smaller known total, never a silent drop. $0, no network."""
import unittest
from unittest import mock
from modelpilot import bench, bench_report, exploration, explorer_replay, long_session, midtask_compaction
from modelpilot.governor import Governor
from modelpilot.m5 import Budget

MODEL = 'claude-sonnet-4-6'
RATE = dict(input=3, output=15, read=.3, write_5m=3.75, write_1h=6)
RATES = {MODEL: RATE}
USAGE = {'input_tokens': 10, 'output_tokens': 20, 'cache_read_input_tokens': 1000, 'cache_creation_input_tokens': 100,
         'cache_creation': {'ephemeral_5m_input_tokens': 100, 'ephemeral_1h_input_tokens': 0}}
COST = (10 * 3 + 20 * 15 + 1000 * .3 + 100 * 3.75) / 1e6


def row(started, cost=COST, usage=USAGE, tools=3, **extra):
    return dict({'kind': 'messages', 'model': MODEL, 'effort': 'low', 'http_status': 200, 'status': 'ok',
                 'started_unix': started, 'tool_count': tools, 'usage': dict(usage), 'cost_usd': cost}, **extra)


def lost(started):
    """A request that lost its connection before any usage arrived."""
    return {'kind': 'messages', 'model': MODEL, 'effort': 'low', 'http_status': None, 'status': 'transport_error',
            'started_unix': started, 'tool_count': 3, 'usage': {}, 'cost_usd': None}


def without(usage, key):
    return {k: v for k, v in usage.items() if k != key}


class BenchAccountingTests(unittest.TestCase):
    final = {'modelUsage': {MODEL: {'inputTokens': 20, 'outputTokens': 40, 'cacheReadInputTokens': 2000,
                                    'cacheCreationInputTokens': 200}}, 'total_cost_usd': 2 * COST}

    def test_one_unpriced_row_leaves_the_total_unknown(self):
        known = bench.accounting([row(1), row(2)], self.final)
        self.assertAlmostEqual(known['cost_usd'], 2 * COST)
        gap = bench.accounting([row(1), row(2, cost=None)], self.final)
        self.assertIsNone(gap['cost_usd'])
        self.assertEqual((gap['unpriced_requests'], gap['known_cost_usd']), (1, COST))  # labelled known

    def test_a_missing_usage_counter_is_not_summed_as_zero(self):
        """Fixed October 15: proxy_tokens summed a 200 with a missing counter as 0 and nothing said so."""
        known = bench.accounting([row(1), row(2)], self.final)
        self.assertTrue(known['proxy_tokens_complete'] and known['tokens_match'])
        gap = bench.accounting([row(1), row(2, cost=None, usage=without(USAGE, 'output_tokens'))], self.final)
        self.assertFalse(gap['proxy_tokens_complete'])
        self.assertFalse(gap['tokens_match'])
        cut = bench.accounting([row(1), row(2, cost=None, usage_complete=False)], self.final)
        self.assertFalse(cut['proxy_tokens_complete'])


class BenchReportTests(unittest.TestCase):
    def test_cold_equivalent_and_measured_cost_stay_unknown(self):
        known = bench_report.cache_attribution([row(1), row(2)], RATES)
        self.assertIsNotNone(known['measured_cost_usd'])
        for rows in ([row(1), row(2, cost=None)], [row(1), lost(2)],
                     [row(1), {'kind': 'side_call', 'status': 'ok', 'cost_usd': None, 'started_unix': 3}]):
            gap = bench_report.cache_attribution(rows, RATES)
            self.assertIsNone(gap['measured_cost_usd'], rows)
            self.assertIsNone(gap['cold_equivalent_cost_usd'], rows)

    def test_repricing_never_prices_an_unknown_row(self):
        for reprice in (lambda rows: bench_report.repriced(rows, RATES),
                        lambda rows: bench_report.api_key_equivalent(rows, RATES)):
            hour = dict(USAGE, cache_creation={'ephemeral_5m_input_tokens': 0, 'ephemeral_1h_input_tokens': 100})
            out = reprice([row(1, cost=None, usage=hour)])
            self.assertIsNone(out[0]['cost_usd'])
        self.assertIsNone(bench_report.repriced([row(1, usage=without(USAGE, 'cache_creation'))], RATES)[0]['cost_usd'])

    def test_cost_components_say_how_many_requests_they_leave_out(self):
        """Fixed October 15: the components covered priced requests only, with nothing saying any were left out."""
        known = bench_report.cost_components([row(1), row(2)], RATES)
        self.assertEqual(known['unpriced_requests'], 0)
        gap = bench_report.cost_components([row(1), row(2, cost=None)], RATES)
        self.assertEqual(gap['unpriced_requests'], 1)
        self.assertAlmostEqual(sum(gap[k] for k in ('input', 'cache_write', 'cache_read', 'output')), COST)
        refused = bench_report.cost_components([row(1), dict(row(2, cost=0.0), kind='refused', status='refused')], RATES)
        self.assertEqual(refused['unpriced_requests'], 0)  # never sent

    def test_write_sources_count_requests_whose_writes_are_unknown(self):
        """Fixed October 15: a request with no cache counts was skipped, and the totals read as complete."""
        known = bench_report.write_sources([row(1), row(2)], RATES)
        self.assertEqual(known['unmeasured_requests'], 0)
        gap = bench_report.write_sources([row(1), lost(2), row(3)], RATES)
        self.assertEqual(gap['unmeasured_requests'], 1)
        totals = bench_report.write_source_totals([{'write_sources': gap}, {'write_sources': known}])
        self.assertEqual(totals['unmeasured_requests'], 1)
        no_split = bench_report.write_sources([row(1, cost=None, usage=without(USAGE, 'cache_creation'))], RATES)
        self.assertIsNone(no_split['usd']['cold_start'])

    def test_cells_metrics_and_the_arm_summary_label_unknown_trials(self):
        def record(task, trial, cost):
            return {'task': task, 'arm': 'a', 'trial': trial, 'passed': True, 'complete': True, 'wall_seconds': 1,
                    'accounting': {'cost_usd': cost}, 'cache': {'cold_equivalent_cost_usd': cost}}
        records = [record('t1', 0, .1), record('t1', 1, None), record('t2', 0, .3)]
        cells = bench_report.cells(records)
        self.assertIsNone(cells['t1']['cost'])  # one unknown trial makes its task's cell unknown
        summary = bench_report.arm_summary('a', records, [], cells, __import__('random').Random(0), 10)
        self.assertEqual((summary['unpriced_trials'], summary['cold_equivalent_unknown_trials'], summary['unknown_cost']),
                         (1, 1, ['t1/1']))
        self.assertAlmostEqual(summary['mean_cost_usd'], .3)  # over the priced task, with the unknown one listed

    def test_paired_summary_counts_tasks_it_leaves_out(self):
        a = {'t1': {'n': 1, 'passes': 1, 'cost': .1, 'wall': 1}, 't2': {'n': 1, 'passes': 1, 'cost': None, 'wall': 1}}
        b = {'t1': {'n': 1, 'passes': 1, 'cost': .2, 'wall': 1}, 't2': {'n': 1, 'passes': 1, 'cost': .2, 'wall': 1}}
        out = bench_report.pair_summary('a', 'b', a, b, __import__('random').Random(0), 10)
        self.assertEqual((out['tasks'], out['dollar_tasks'], out['excluded_unpriced_tasks']), (2, 1, 1))

    def test_router_cost_without_a_record_or_usage_is_unknown(self):
        rate = {'input_usd_per_mtok': 1, 'output_usd_per_mtok': 2, 'per_decision_usd': .001}
        self.assertIsNone(bench_report.router_cost({}, rate))
        self.assertIsNone(bench_report.router_cost({'decisions': 2, 'router_usage': [{'input_tokens': 5, 'output_tokens': 1}]},
                                                   rate))
        self.assertIsNone(bench_report.router_cost({'decisions': 1, 'router_usage': [{'input_tokens': 5}]}, rate))


class LongSessionTests(unittest.TestCase):
    def turn(self, prompt, subtask, cold, compaction_usd=0.0, output=None):
        return {'prompt': prompt, 'subtask': subtask, 'cold_equivalent_usd': cold, 'return_read_fraction': None,
                'compaction_cost_usd': compaction_usd if prompt == 'compact' else 0.0,
                'compaction_output_tokens': output if prompt == 'compact' else 0}

    def items(self, compaction_usd, output, cold=.2):
        record = {'complete': True, 'shape': 'sequence', 'arm': 'a', 'cache': {'cold_equivalent_cost_usd': .5},
                  'sequence': {'tasks': ['x', 'y']}, 'subtasks': [{'task': 'x', 'passed': True}, {'task': 'y', 'passed': True}]}
        return [(record, [self.turn('task', 0, .1), self.turn('compact', 0, .05, compaction_usd, output),
                          self.turn('task', 1, cold)])]

    def test_an_unknown_compaction_turn_is_not_counted_as_zero(self):
        """Fixed October 15: report summed compaction turns with `or 0`, so an unknown one counted $0."""
        known = long_session.report(self.items(.05, 4000))['arms']['a']
        self.assertAlmostEqual(known['compaction_usd'], .05)
        gap = long_session.report(self.items(None, 4000))['arms']['a']
        self.assertIsNone(gap['compaction_usd'])
        tokens = long_session.report(self.items(.05, None))['arms']['a']
        self.assertIsNone(tokens['compaction_output_tokens'])

    def test_a_compaction_request_without_an_output_count_is_unknown(self):
        """Fixed October 15: turns() summed a missing output count as 0."""
        record = {'sessions': [{'rows': [0, 1], 'prompt': 'compact', 'subtask': 0}]}
        known = long_session.turns(record, [row(1, compaction=True)], RATES)[0]
        self.assertEqual(known['compaction_output_tokens'], 20)
        gap = long_session.turns(record, [row(1, cost=None, compaction=True, usage=without(USAGE, 'output_tokens'))], RATES)[0]
        self.assertIsNone(gap['compaction_output_tokens'])
        self.assertIsNone(gap['compaction_cost_usd'])
        self.assertIsNone(gap['cost_usd'])

    def test_unpriced_subtasks_are_counted_where_the_pairs_leave_them_out(self):
        first = self.items(.05, 4000)
        second = [(dict(r, arm='b'), t) for r, t in self.items(.05, 4000, cold=None)]
        out = long_session.report(first + second, resamples=10)
        self.assertEqual(out['arms']['b']['unknown_cost_subtasks'], ['y'])
        pair, = out['pairs']
        self.assertEqual((pair['subtasks'], pair['excluded_unpriced_subtasks']), (1, 1))


class MidtaskCompactionTests(unittest.TestCase):
    def item(self, compaction_usd, fix_usd):
        record = {'arm': 'a'}
        return (record, {'complete': True, 'ineligible': None, 'passed': True, 'plan_turn_edited': False,
                         'turn_cost_usd': {'plan': .1, 'compact': .02, 'fix': fix_usd}, 'compaction_usd': compaction_usd,
                         'compaction_output_tokens': 4000, 'fix_first_write_tokens': 5000})

    def test_means_say_how_many_trials_they_leave_out(self):
        """Fixed October 15: report's means dropped unknown trials without a count."""
        out = midtask_compaction.report([self.item(.02, .3), self.item(None, None)])['a']
        self.assertAlmostEqual(out['mean_compaction_usd'], .02)
        self.assertEqual(out['unknown']['compaction_usd'], 1)
        self.assertEqual(out['unknown']['turn_usd'], {'plan': 0, 'compact': 0, 'fix': 1})
        clean = midtask_compaction.report([self.item(.02, .3)])['a']
        self.assertEqual(clean['unknown']['compaction_usd'], 0)


class ReplayAndExplorationTests(unittest.TestCase):
    def test_a_missing_lifetime_count_leaves_the_switch_cost_unknown(self):
        """Fixed October 15: measured_switch_usd read a missing 1h (or 5m) count as 0."""
        usage = dict(USAGE, cache_creation={'ephemeral_5m_input_tokens': 100, 'ephemeral_1h_input_tokens': 0})
        self.assertIsNotNone(exploration.measured_switch_usd([row(1, usage=usage)], MODEL, RATES))
        partial = dict(USAGE, cache_creation={'ephemeral_5m_input_tokens': 100})
        self.assertIsNone(exploration.measured_switch_usd([row(1, usage=partial)], MODEL, RATES))

    def test_exploration_groups_count_their_priced_trials(self):
        views = [{'complete': True, 'eligible': True, 'switched': False, 'passed': True, 'cold_equivalent_usd': c,
                  'predicted_switch_usd': None, 'measured_switch_usd': None} for c in (.1, None)]
        group = exploration.summarize(views)['not_switched']
        self.assertEqual((group['trials'], group['priced'], group['mean_cost_usd']), (2, 1, .1))

    def test_the_explorer_replay_counts_unpriced_trials(self):
        """Fixed October 15: mean_trial_cost_usd left unpriced trials out without a count."""
        trials = [{'task': 't', 'arm': 'a', 'trial': i, 'cost': cost, 'rows': [], 'turns': [], 'results': {}}
                  for i, cost in enumerate((.1, None))]
        with mock.patch.object(explorer_replay, 'main_rows', lambda rows: [{'model': MODEL}]), \
                mock.patch.object(explorer_replay, 'candidates', lambda *a: ([], 0)), \
                mock.patch.object(explorer_replay, 'replay_trial', lambda *a: {'net': 0, 'saving': 0}):
            for t in trials:
                t['turns'] = [{}]
                t['rows'] = [{'model': MODEL}]
            out = explorer_replay.replay(trials, RATES, k_grid=(200,))
        self.assertEqual((out['arms']['a']['mean_trial_cost_usd'], out['arms']['a']['unpriced_trials']), (.1, 1))


class GovernorAndBudgetTests(unittest.TestCase):
    def test_an_unknown_settlement_halts_and_spend_is_labelled_incomplete(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            gov = Governor(Path(tmp)/'state.db', 's', 1)
            try:
                for request_id, cost in (('a', .1), ('b', None)):
                    gov.admit(request_id, .01)
                    gov.settle(request_id, cost)
                policy = gov.policy()
                self.assertEqual((policy['spent_usd'], policy['cost_complete'], policy['mode']), (.1, False, 'halt'))
                self.assertEqual(gov.admit('c', .01)['reason'], 'cost_unknown')
                gov.admit('d', .01, ttl=1, enforce=False)  # reserved even while halted, as the dry-run proxy does
                gov.clock = lambda: __import__('time').time() + 5  # past the reservation's expiry
                self.assertEqual(gov.recover()['orphaned'], 1)
                self.assertIsNone(gov.db.execute("SELECT actual FROM gov_reservations WHERE request_id='d'").fetchone()[0])
            finally:
                gov.close()

    def test_m5_budget_halts_on_unknown(self):
        budget = Budget(1)
        self.assertTrue(budget.reserve('a', .1))
        budget.settle('a', None)
        self.assertEqual((budget.policy()['cost_complete'], budget.policy()['mode']), (False, 'halt'))
        self.assertFalse(budget.reserve('b', .1))



if __name__ == '__main__':
    unittest.main()
