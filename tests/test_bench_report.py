import json
from pathlib import Path
import tempfile
import unittest
from modelpilot import bench_report
from modelpilot.bench_report import cache_attribution, summarize
from modelpilot.cache_probe import cost

RATES = {'claude-sonnet-5': dict(input=2, output=10, read=.2, write_5m=2.5, write_1h=4),
         'claude-opus-5': dict(input=5, output=25, read=.5, write_5m=6.25, write_1h=10)}


def row(started, read, write, *, model='claude-sonnet-5', effort=None, ttl='5m', split=True, status=200,
        input_tokens=2, output=134):
    usage = {'input_tokens': input_tokens, 'output_tokens': output,
             'cache_read_input_tokens': read, 'cache_creation_input_tokens': write}
    if split:
        usage['cache_creation'] = {'ephemeral_5m_input_tokens': write if ttl == '5m' else 0,
                                   'ephemeral_1h_input_tokens': write if ttl == '1h' else 0}
    priced = cost(usage, RATES[model], ttl) if status == 200 else None
    return {'kind': 'messages', 'started_unix': started, 'model': model, 'effort': effort,
            'http_status': status, 'usage': usage if status == 200 else {}, 'cost_usd': priced}


class CacheAttributionTests(unittest.TestCase):
    def test_pilot_second_trial_first_request_is_repriced_as_a_cold_write(self):
        # runs/bench-20260923-070044, Sonnet 5 trial #2: its first request read 7,902 tokens
        # that trial #1 had written; the second request read exactly the first one's prefix.
        rows = [row(1000.0, 7902, 9172), row(1003.0, 17074, 329, output=154)]
        result = cache_attribution(rows, RATES)
        self.assertEqual(result['cache_start'], {'first_read_tokens': 7902, 'carried_tokens': 7902, 'warm': True})
        self.assertEqual(result['carried_read_tokens'], 7902)
        measured = rows[0]['cost_usd'] + rows[1]['cost_usd']
        self.assertAlmostEqual(result['measured_cost_usd'], measured)
        self.assertAlmostEqual(result['cold_equivalent_cost_usd'] - measured, 7902 * (2.5 - .2) / 1e6)
        self.assertIsNone(result['cold_equivalent_unknown'])

    def test_a_cold_trial_costs_what_was_measured(self):
        rows = [row(0.0, 0, 17074), row(2.0, 17074, 400)]
        result = cache_attribution(rows, RATES)
        self.assertEqual(result['cache_start'], {'first_read_tokens': 0, 'carried_tokens': 0, 'warm': False})
        self.assertAlmostEqual(result['cold_equivalent_cost_usd'], result['measured_cost_usd'])

    def test_reads_after_the_cache_lifetime_are_carried(self):
        # A 330 s follow-up gap: the in-trial entry has expired, so anything read came from elsewhere.
        rows = [row(0.0, 0, 20000), row(331.0, 12000, 8400)]
        result = cache_attribution(rows, RATES)
        self.assertEqual(result['carried_read_tokens'], 12000)
        self.assertAlmostEqual(result['cold_equivalent_cost_usd'] - result['measured_cost_usd'], 12000 * 2.3 / 1e6)

    def test_reads_beyond_the_in_trial_prefix_are_carried(self):
        rows = [row(0.0, 0, 5000), row(1.0, 9000, 100)]
        self.assertEqual(cache_attribution(rows, RATES)['carried_read_tokens'], 4000)

    def test_models_and_efforts_keep_separate_entries(self):
        for second in (row(1.0, 5000, 10, model='claude-opus-5'), row(1.0, 5000, 10, effort='low')):
            result = cache_attribution([row(0.0, 0, 5000, effort=None), second], RATES)
            self.assertEqual(result['carried_read_tokens'], 5000)

    def test_one_hour_entries_stay_usable_and_reprice_at_the_one_hour_rate(self):
        rows = [row(0.0, 3000, 5000, ttl='1h'), row(1000.0, 8000, 10, ttl='1h')]
        result = cache_attribution(rows, RATES)
        self.assertEqual(result['carried_read_tokens'], 3000)
        self.assertAlmostEqual(result['cold_equivalent_cost_usd'] - result['measured_cost_usd'], 3000 * (4 - .2) / 1e6)

    def test_requests_are_ordered_by_start_time_not_log_order(self):
        rows = [row(5.0, 5000, 10), row(0.0, 0, 5000)]  # the proxy logs at completion
        self.assertEqual(cache_attribution(rows, RATES)['carried_read_tokens'], 0)

    def test_rows_without_the_write_breakdown_count_carried_tokens_but_not_dollars(self):
        # Rows logged before the proxy kept the 5m/1h split (e.g. the 3c pilot).
        rows = [row(0.0, 7902, 9172, split=False), row(3.0, 17074, 329, split=False)]
        result = cache_attribution(rows, RATES)
        self.assertEqual(result['carried_read_tokens'], 7902)
        self.assertIsNone(result['cold_equivalent_cost_usd'])
        self.assertEqual(result['cold_equivalent_unknown'], 'no_cache_write_breakdown')

    def test_an_unpriced_success_leaves_every_cost_unknown(self):
        rows = [row(0.0, 0, 5000), dict(row(1.0, 5000, 10), cost_usd=None)]
        result = cache_attribution(rows, RATES)
        self.assertIsNone(result['measured_cost_usd'])
        self.assertIsNone(result['cold_equivalent_cost_usd'])
        self.assertIsNone(result['cold_equivalent_if_rejected_free_usd'])
        self.assertEqual(result['cold_equivalent_unknown'], 'unpriced_request')

    def test_a_rejected_request_leaves_the_headline_unknown_but_gives_the_sensitivity_figure(self):
        # Compat Jev on Haiku: the system-role message is rejected, then the resend succeeds.
        rows = [row(0.0, 0, 5000), row(1.0, 0, 0, status=400), row(2.0, 5000, 10)]
        result = cache_attribution(rows, RATES)
        self.assertIsNone(result['measured_cost_usd'])
        self.assertIsNone(result['cold_equivalent_cost_usd'])
        self.assertEqual(result['cold_equivalent_unknown'], 'rejected_request')
        self.assertEqual(result['rejected_requests'], 1)
        self.assertAlmostEqual(result['cold_equivalent_if_rejected_free_usd'], rows[0]['cost_usd'] + rows[2]['cost_usd'])


def record(task, arm, trial=0, *, passed=True, cost_usd=.1, wall=60.0, stop='success', complete=True,
           scope='complete', if_free=None, rejected=0, routing=None):
    if_free = cost_usd if if_free is None else if_free
    out = {'task': task, 'arm': arm, 'trial': trial, 'passed': passed, 'wall_seconds': wall, 'complete': complete,
           'accounting': {'cost_usd': cost_usd, 'first_byte_seconds': [1.0, 2.0], 'cost_scope': scope,
                          'rejected_requests': rejected},
           'cache': {'cold_equivalent_cost_usd': cost_usd, 'measured_cost_usd': cost_usd,
                     'cold_equivalent_if_rejected_free_usd': if_free,
                     'cache_start': {'first_read_tokens': 0, 'carried_tokens': 0, 'warm': False}},
           'sessions': [{'stop': stop}], 'grade': {'reason': 'passed' if passed else 'hidden_tests_failed'}}
    if routing is not None:
        out['routing'] = routing
    return out


def routed(model, decisions=1, usage=({'input_tokens': 893, 'output_tokens': 100},)):
    return {'routed': True, 'router': 'typesafe', 'decisions': decisions, 'extra_decisions': decisions - 1, 'fail_open': [],
            'served_models': [model], 'router_usage': list(usage)}


class SummaryTests(unittest.TestCase):
    TASKS = [f't{i}' for i in range(12)]

    def test_clear_cost_difference_excludes_zero_and_identical_arms_do_not(self):
        records = ([record(t, 'cheap', cost_usd=.10 + i / 1000) for i, t in enumerate(self.TASKS)] +
                   [record(t, 'dear', cost_usd=.30 + i / 1000) for i, t in enumerate(self.TASKS)] +
                   [record(t, 'twin', cost_usd=.10 + i / 1000) for i, t in enumerate(self.TASKS)])
        summary = summarize(records, ['cheap', 'dear', 'twin'], seed=1, resamples=2000)
        pairs = {tuple(p['arms']): p for p in summary['paired']}
        dear = pairs[('cheap', 'dear')]['differences']['mean_cost_usd']
        self.assertAlmostEqual(dear['estimate'], -.2)
        self.assertLess(dear['ci95'][1], 0)
        self.assertTrue(dear['shows_difference'])
        twin = pairs[('cheap', 'twin')]['differences']['mean_cost_usd']
        self.assertLessEqual(twin['ci95'][0], 0)
        self.assertGreaterEqual(twin['ci95'][1], 0)
        self.assertFalse(twin['shows_difference'])
        self.assertEqual(twin['note'], 'no difference shown')
        self.assertEqual(summary['bootstrap'], {'seed': 1, 'resamples': 2000, 'unit': 'task', 'interval': '95% percentile'})

    def test_too_few_tasks_never_show_a_difference(self):
        # The 3c pilot: two tasks give a tight but meaningless interval.
        records = [record(t, 'a', wall=10 + i) for i, t in enumerate(self.TASKS[:2])] + \
                  [record(t, 'b', wall=50 + i) for i, t in enumerate(self.TASKS[:2])]
        wall = summarize(records, ['a', 'b'], seed=0, resamples=500)['paired'][0]['differences']['mean_wall_seconds']
        self.assertLess(wall['ci95'][1], 0)
        self.assertFalse(wall['shows_difference'])
        self.assertEqual(wall['note'], f'too few tasks (fewer than {bench_report.MIN_TASKS})')

    def test_bootstrap_is_reproducible_from_its_seed(self):
        records = [record(t, arm, passed=(i + len(arm)) % 3 > 0, cost_usd=.1 + i / 100)
                   for i, t in enumerate(self.TASKS) for arm in ('a', 'bb')]
        self.assertEqual(summarize(records, ['a', 'bb'], seed=7, resamples=500),
                         summarize(records, ['a', 'bb'], seed=7, resamples=500))

    def test_per_arm_metrics_and_counts(self):
        records = [record('t0', 'a', 0, cost_usd=.1, wall=10), record('t0', 'a', 1, passed=False, cost_usd=.3, wall=30, stop='turn_limit'),
                   record('t1', 'a', 0, cost_usd=.2, wall=20)]
        records[1]['cache']['cache_start']['warm'] = True
        arm = summarize(records, ['a'], seed=0, resamples=200)['arms'][0]
        self.assertEqual((arm['trials'], arm['passes']), (3, 2))
        self.assertAlmostEqual(arm['pass_rate'], 2 / 3)
        self.assertAlmostEqual(arm['mean_cost_usd'], .2)
        self.assertAlmostEqual(arm['cost_per_pass_usd'], .3)
        self.assertAlmostEqual(arm['mean_wall_seconds'], 20)
        self.assertEqual(arm['median_first_byte_seconds'], 1.5)
        self.assertEqual(arm['warm_starts'], 1)
        self.assertEqual(arm['stops'], {'success': 2, 'turn_limit': 1})
        self.assertEqual(arm['grade_reasons'], {'passed': 2, 'hidden_tests_failed': 1})
        self.assertEqual(len(arm['ci95']['pass_rate']), 2)

    def test_pairs_use_shared_tasks_and_unknown_costs_are_excluded_and_listed(self):
        records = [record('t0', 'a'), record('t1', 'a'), record('t2', 'a'),
                   record('t0', 'b'), record('t1', 'b', cost_usd=None)]  # b never ran t2
        summary = summarize(records, ['a', 'b'], seed=0, resamples=200)
        pair = summary['paired'][0]
        self.assertEqual((pair['tasks'], pair['dollar_tasks'], pair['excluded_unpriced_tasks']), (2, 1, 1))
        b = summary['arms'][1]
        self.assertEqual(b['unknown_cost'], ['t1/0'])
        self.assertEqual(b['cold_equivalent_unknown_trials'], 1)
        self.assertAlmostEqual(b['mean_cost_usd'], .1)  # never priced at zero

    def test_cost_per_pass_is_none_without_passes(self):
        arm = summarize([record('t0', 'a', passed=False)], ['a'], seed=0, resamples=50)['arms'][0]
        self.assertIsNone(arm['cost_per_pass_usd'])
        self.assertEqual(arm['pass_rate'], 0)

    def test_jev_arms_are_labeled_provider_only_with_routing_and_sensitivity(self):
        unrouted = {'routed': False, 'router': None, 'decisions': 0, 'extra_decisions': 0, 'fail_open': ['unrouted_sentinel'],
                    'served_models': ['claude-opus-5'], 'router_usage': []}
        records = [record('t0', 'jev', cost_usd=None, if_free=.05, rejected=1, scope='provider_only_router_unpriced',
                          routing=routed('claude-haiku-4-5-20251001', decisions=2)),
                   record('t1', 'jev', cost_usd=.2, scope='provider_only_router_unpriced', routing=routed('claude-sonnet-5')),
                   record('t2', 'jev', cost_usd=.3, passed=False, scope='provider_only_router_unpriced', routing=unrouted),
                   record('t0', 'opus'), record('t1', 'opus'), record('t2', 'opus')]
        summary = summarize(records, ['jev', 'opus'], seed=0, resamples=50)
        jev, opus = summary['arms']
        self.assertEqual((jev['cost_scope'], opus['cost_scope']), ('provider_only_router_unpriced', 'complete'))
        self.assertAlmostEqual(jev['mean_cost_usd'], .25)  # the rejected trial stays out of the headline
        self.assertAlmostEqual(jev['mean_cost_if_rejected_free_usd'], .55 / 3)
        self.assertAlmostEqual(jev['cost_per_pass_if_rejected_free_usd'], .55 / 2)
        self.assertEqual(jev['rejected_requests'], 1)
        self.assertEqual(jev['routing'], {'trials': 3, 'routed_trials': 2, 'fail_open_trials': 1,
                                          'routers': {'typesafe': 2, None: 1}, 'decisions': 3,
                                          'extra_decisions': 1,
                                          'served_models': {'claude-haiku-4-5-20251001': 1, 'claude-sonnet-5': 1,
                                                            'claude-opus-5': 1},
                                          'router_tokens': {'input_tokens': 1786, 'output_tokens': 200}})
        self.assertNotIn('routing', opus)
        pair = summary['paired'][0]
        self.assertIn('jev', pair['dollar_basis'])
        self.assertIn('lower bound', pair['dollar_basis'])
        fixed = summarize([record('t0', 'a'), record('t0', 'b')], ['a', 'b'], seed=0, resamples=20)['paired'][0]
        self.assertEqual(fixed['dollar_basis'], 'complete')

    def test_incomplete_trials_are_counted_not_analysed(self):
        records = [record('t0', 'a'), record('t1', 'a', complete=False, cost_usd=5.0)]
        arm = summarize(records, ['a'], seed=0, resamples=50)['arms'][0]
        self.assertEqual((arm['trials'], arm['incomplete_trials']), (1, 1))
        self.assertAlmostEqual(arm['mean_cost_usd'], .1)


class IncompleteCostTests(unittest.TestCase):
    def test_adapter_reported_incomplete_cost_has_no_dollar_figure(self):
        record = {'accounting': {'cost_complete': False},
                  'cache': {'cold_equivalent_cost_usd': .1, 'cold_equivalent_if_rejected_free_usd': .1}}
        self.assertIsNone(bench_report.cold_cost(record))
        self.assertIsNone(bench_report.cold_cost_if_rejected_free(record))


class RebuildTests(unittest.TestCase):
    def test_summary_rebuilds_from_a_run_directory_and_recomputes_cache_attribution(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run/'manifest.json').write_text(json.dumps({'seed': 3, 'arms': {'sonnet-5': {}}}))
            trial = run/'t0'/'sonnet-5'/'0'
            trial.mkdir(parents=True)
            old = record('t0', 'sonnet-5')
            del old['cache'], old['complete'], old['trial']  # a pilot-era record
            (trial/'trial.json').write_text(json.dumps(old))
            rows = [row(0.0, 7902, 9172), row(3.0, 17074, 329)]
            (trial/'observations.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
            manifest, records = bench_report.load_run(run, RATES)
            self.assertEqual(manifest['seed'], 3)
            self.assertEqual((records[0]['trial'], records[0]['complete']), (0, True))
            self.assertEqual(records[0]['cache']['carried_read_tokens'], 7902)
            out = run/'summary.rebuilt.json'
            bench_report.write_summary(run, RATES, out, resamples=50)
            self.assertEqual(json.loads(out.read_text())['arms'][0]['warm_starts'], 1)
            with self.assertRaises(FileExistsError):
                bench_report.write_summary(run, RATES, out, resamples=50)  # evidence is never overwritten


if __name__ == '__main__': unittest.main()
