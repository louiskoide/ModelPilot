from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from modelpilot.m2 import State, assess


class DetectorTests(unittest.TestCase):
    def test_retry_language_alone_does_not_escalate(self):
        self.assertEqual(assess([{'retry_language':True}]*6)['recommendation'],'hold')

    def test_verified_progress_resets_repeated_errors(self):
        e={'error_hash':'same'}
        self.assertEqual(assess([e]*3)['recommendation'],'increase_effort')
        self.assertEqual(assess([e,e,{'progress':True},e,{'progress':True},e])['recommendation'],'hold')

    def test_oscillation_requires_same_file(self):
        events=[{'file':'a','content_hash':h} for h in ('a','b','a','b')]
        self.assertTrue(assess(events)['signals']['edit_oscillation'])
        events[-1]['file']='other'
        self.assertFalse(assess(events)['signals']['edit_oscillation'])

    def test_test_progress_and_success_are_not_stuck(self):
        for counts in ([3,2,1],[0,0,0]):
            self.assertEqual(assess([{'suite':'unit','failures':n} for n in counts])['recommendation'],'hold')
        self.assertTrue(assess([{'suite':'unit','failures':2}]*3)['signals']['stalled_tests'])


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)/'state.db'
        self.state=State(self.path)
        self.task=self.state.create('test','initial')['id']
        self.state.claim(self.task,1,'worker')

    def tearDown(self):
        self.state.close(); self.tmp.cleanup()

    def test_correction_blocks_old_and_unacknowledged_results(self):
        self.state.correct(self.task,1,'new')
        with self.assertRaises(ValueError): self.state.complete(self.task,1,'worker','stale')
        with self.assertRaises(ValueError): self.state.complete(self.task,2,'worker','unacked')
        self.state.acknowledge(self.task,2,'worker')
        self.assertEqual(self.state.complete(self.task,2,'worker','valid')['status'],'done')

    def test_cancel_blocks_completion(self):
        self.state.cancel(self.task,1)
        with self.assertRaises(ValueError): self.state.complete(self.task,1,'worker','late')

    def test_reclaim_invalidates_previous_lease_even_same_owner(self):
        with self.state.db:
            self.state.db.execute('UPDATE tasks SET lease_until=0 WHERE id=?',(self.task,))
        row=self.state.claim(self.task,1,'worker')
        self.assertEqual(row['revision'],2)
        with self.assertRaises(ValueError): self.state.complete(self.task,1,'worker','old')

    def test_concurrent_claim_only_one_wins(self):
        task=self.state.create('race','test')['id']
        def claim(owner):
            state=State(self.path)
            try:
                state.claim(task,1,owner); return True
            except ValueError: return False
            finally: state.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(claim,['a','b']))
        self.assertEqual(sum(outcomes),1)

    def test_escalation_confirmation_is_one_rung_and_needs_new_evidence(self):
        for _ in range(3): result=self.state.observe(self.task,1,'worker',{'error':'same'})
        self.assertEqual(result['recommendation'],'increase_effort')
        self.state.confirm_escalation(self.task,1,'worker',result['last_event'])
        with self.assertRaises(ValueError): self.state.confirm_escalation(self.task,1,'worker',result['last_event'])
        for _ in range(3): result=self.state.observe(self.task,1,'worker',{'error':'same'})
        self.assertEqual(result['recommendation'],'stronger_model')
        self.assertFalse(result['applied'])

    def test_output_unicode_pagination_roundtrip_and_path_rejection(self):
        text='é🐈\n'*3000
        output=self.state.store_output(text)
        self.assertTrue(output['truncated'])
        self.assertEqual(output,self.state.store_output(text))
        restored=''; offset=0
        while offset is not None:
            page=self.state.expand(output['handle'],offset,777)
            restored+=page['text']; offset=page['next_offset']
        self.assertEqual(restored,text)
        with self.assertRaises(ValueError): self.state.expand('../../etc/passwd')
        with self.assertRaises(ValueError): self.state.expand(output['handle'],limit=0)

    def test_wrong_owner_observation_does_not_pollute_detector(self):
        with self.assertRaises(ValueError): self.state.observe(self.task,1,'other',{'error':'bad'})
        self.assertEqual(self.state.recommend(self.task)['window_events'],0)
