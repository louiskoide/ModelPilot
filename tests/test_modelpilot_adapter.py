import unittest
from modelpilot.modelpilot_adapter import ModelPilotAdapter, switch_decision

RATES={'strong':dict(input=5,write_5m=6.25,read=.5,output=25),
       'cheap':dict(input=1,write_5m=1.25,read=.1,output=5)}
class AdmissionTests(unittest.TestCase):
    def test_warm_target_does_not_avoid_write_reservation(self):
        decision=switch_decision('strong','cheap',10000,0,1,1,RATES)
        self.assertEqual(decision['action'],'stay')
        self.assertEqual(decision['target_write_usd'],.0125)
        self.assertFalse(decision['applied'])

    def test_unknown_cost_or_insufficient_budget_defers(self):
        for budget in (None, .001):
            d=switch_decision('strong','cheap',10000,100,3,budget,RATES)
            self.assertEqual(d['action'],'defer')

    def test_economic_candidate_still_does_not_apply(self):
        d=switch_decision('strong','cheap',10000,1000,3,1,RATES)
        self.assertEqual(d['action'],'would_switch')
        self.assertFalse(d['applied'])

    def test_active_mode_refused(self):
        with self.assertRaises(ValueError): ModelPilotAdapter(mode='active')

class IntegrationTests(unittest.TestCase):
    def test_real_client_hooks_and_governor_reconcile_offline(self):
        import shutil,sys
        from tests.test_bench import OfflineTrialTests,RATES
        from modelpilot import bench
        if not shutil.which('claude'): self.skipTest('Claude CLI required')
        fixture=OfflineTrialTests('test_fixing_agent_passes_and_idle_agent_fails_with_exact_accounting')
        fixture.setUp()
        try:
            out=fixture.out/'governed'
            fixture.upstream.script=[{'tool':'Read','input':{'file_path':str(out/'workspace/pkg/__init__.py')}},
                                     {'text':'Done.'}]
            record=bench.run_trial(fixture.task,'modelpilot',out,shutil.which('claude'),
                  'sk-ant-offline-not-a-key',f'http://127.0.0.1:{fixture.upstream.server_port}',RATES,
                  python=sys.executable,adapter=ModelPilotAdapter(limit_usd=1e-6),max_turns=4)
            self.assertTrue(record['routing']['accounting_matches'],record)
            self.assertGreater(record['routing']['hook_events'],0)
            self.assertGreater(record['routing']['would_refuse'],0)
            self.assertFalse(record['routing']['applied'])
            self.assertFalse(record['routing']['benchmark_eligible'])
            self.assertTrue((out/'governor.sqlite3').exists())
        finally:
            fixture.tearDown()

class ReportTests(unittest.TestCase):
    def test_observer_trial_is_excluded_from_comparisons(self):
        from modelpilot.bench_report import summarize
        row=dict(task='x',arm='modelpilot',complete=True,passed=True,wall_seconds=1,
                 accounting={'cost_usd':.1},cache={'cold_equivalent_cost_usd':.1},
                 routing={'benchmark_eligible':False})
        result=summarize([row],['modelpilot'],resamples=10)
        self.assertEqual(len(result['excluded_ineligible_trials']),1)
        self.assertIsNone(result['arms'][0]['pass_rate'])
