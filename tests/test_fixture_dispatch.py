import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from modelpilot.governor import Governor
from modelpilot.policy_actions import escalation_proposal
from modelpilot.fixture_dispatch import FixtureDispatcher,StrictFixture

S='claude-sonnet-5'
RATES={S:dict(input=2,write_5m=2.5,write_1h=4,read=.2,output=10)}
class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.gov=Governor(Path(self.tmp.name)/'state.db','s',1)
        self.task=self.gov.state.create('t','fix')['id'];self.gov.state.claim(self.task,1,'owner')
        for _ in range(3):self.gov.state.observe(self.task,1,'owner',{'error':'same'})
        self.proposal=escalation_proposal(self.gov.state,self.task,1,'owner',S,'medium')
        self.request=dict(model=S,max_tokens=32,output_config={'effort':'medium'},messages=[{'role':'user','content':'hello'},{'role':'system','content':'context'}])
        self.fixture=StrictFixture();self.dispatch=FixtureDispatcher(self.gov,RATES,self.fixture)
    def tearDown(self):self.gov.close();self.tmp.cleanup()
    def send(self):return self.dispatch.dispatch(self.proposal,'owner',self.request)
    def test_success_settles_and_advances_once(self):
        result=self.send()
        self.assertEqual(result['status'],'fixture_confirmed')
        self.assertFalse(result['applied'])
        self.assertEqual(self.gov.state.get(self.task)['level'],1)
        self.assertAlmostEqual(self.gov.policy()['spent_usd'],.00024)
        with self.assertRaises(ValueError):self.send()
        self.assertEqual(self.fixture.calls,1)
    def test_stale_before_admission_never_sends(self):
        self.gov.state.correct(self.task,1,'new')
        with self.assertRaises(ValueError):self.send()
        self.assertEqual(self.fixture.calls,0)
    def test_failure_unknown_cost_stops_and_preserves_window(self):
        with patch.object(self.fixture,'reply',side_effect=TimeoutError('private')):
            result=self.send()
        self.assertEqual(result['status'],'unknown_outcome')
        self.assertFalse(self.gov.policy()['cost_complete'])
        self.assertEqual(self.gov.state.get(self.task)['level'],0)
        self.assertNotIn('private',str(result))
        self.assertEqual(self.send()['status'],'deferred')
    def test_correction_while_in_flight_bills_but_does_not_confirm(self):
        reply=self.fixture.reply
        def corrected(request):
            self.gov.state.correct(self.task,1,'corrected')
            return reply(request)
        with patch.object(self.fixture,'reply',side_effect=corrected):result=self.send()
        self.assertEqual(result['status'],'stale_after_send')
        self.assertGreater(self.gov.policy()['spent_usd'],0)
        self.assertEqual(self.gov.state.get(self.task)['level'],0)
    def test_nonfixture_transport_refused(self):
        with self.assertRaises(ValueError):FixtureDispatcher(self.gov,RATES,lambda p:None)
    def test_atomic_fence_rolls_back_reservation(self):
        def reject():raise ValueError('stale')
        with self.assertRaises(ValueError):self.gov.admit('x',.1,fence=reject)
        self.assertEqual(self.gov.policy()['reserved_usd'],0)

    def test_change_between_preparation_and_admission_is_fenced(self):
        admit=self.gov.admit
        def changed(*args,**kwargs):
            self.gov.state.observe(self.task,1,'owner',{'progress':True})
            return admit(*args,**kwargs)
        with patch.object(self.gov,'admit',side_effect=changed):
            with self.assertRaises(ValueError):self.send()
        self.assertEqual(self.fixture.calls,0)
        self.assertEqual(self.gov.policy()['reserved_usd'],0)

    def test_unexpected_served_model_stays_unknown(self):
        with patch.object(self.fixture,'reply',return_value={'model':'other','usage':{}}):
            result=self.send()
        self.assertEqual(result['status'],'unknown_outcome')
        self.assertFalse(self.gov.policy()['cost_complete'])
        self.assertEqual(self.gov.state.get(self.task)['level'],0)

    def test_concurrent_same_window_sends_only_once(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        barrier=threading.Barrier(2)
        db=Path(self.tmp.name)/'state.db'
        def worker():
            gov=Governor(db,'s',1)
            try:
                barrier.wait(timeout=5)
                try:return FixtureDispatcher(gov,RATES,self.fixture).dispatch(self.proposal,'owner',self.request)
                except ValueError:return {'status':'refused'}
            finally:gov.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:worker(),range(2)))
        self.assertEqual(sum(r['status']=='fixture_confirmed' for r in results),1)
        self.assertEqual(self.fixture.calls,1)
        self.assertEqual(self.gov.state.get(self.task)['level'],1)
