import copy
from pathlib import Path
import sys
import tempfile
import unittest
from modelpilot.m2 import State
from modelpilot.workers import Worker, WorkerPool, WorkerRejected

RATES={'test-model':{'input':3,'output':15,'read':.3,'write_5m':3.75,'write_1h':6}}


def reply(text='verified'):
    return {'model':'test-model','stop_reason':'end_turn','content':[{'type':'text','text':text}],
            'usage':{'input_tokens':10,'output_tokens':2,'cache_creation_input_tokens':0,'cache_read_input_tokens':0}},None


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.db=self.root/'state.db';self.state=State(self.db)
        (self.root/'file.txt').write_text('needle = value\n')
        self.requests=[]
        def transport(p,c):
            self.requests.append(copy.deepcopy(p));return reply()
        self.settings=dict(root=self.root,db=self.db,model='test-model',rates=RATES,transport=transport)

    def tearDown(self):
        self.state.close();self.tmp.cleanup()

    def task(self):
        return self.state.create('search','Find needle')['id']

    def dispatch(self,worker,task=None):
        return worker.dispatch(task or self.task(),1,['file.txt'],query='needle')

    def test_persistent_role_history_and_other_role_isolation(self):
        pool=WorkerPool(**self.settings)
        self.dispatch(pool.workers['file_search']);r=self.dispatch(pool.workers['file_search'])
        self.assertEqual(r['prior_messages'],2)
        self.assertEqual(len(self.requests[1]['messages']),3)
        self.assertEqual(pool.workers['test_runner'].history,[])
        self.assertNotIn('cache_control',self.requests[1]['messages'][0]['content'][0])

    def test_correction_during_api_call_rejects_result_and_preserves_instruction(self):
        task=self.task()
        def transport(p,c):
            self.state.correct(task,1,'new correction');return reply('stale')
        worker=Worker('file_search',**dict(self.settings,transport=transport))
        with self.assertRaises(ValueError): self.dispatch(worker,task)
        row=self.state.get(task)
        self.assertEqual(row['status'],'pending');self.assertEqual(row['instruction'],'new correction')
        self.assertIsNone(row['result']);self.assertEqual(worker.history,[])

    def test_cancellation_during_call_is_not_reopened(self):
        task=self.task()
        def transport(p,c):
            self.state.cancel(task,1);return reply()
        worker=Worker('file_search',**dict(self.settings,transport=transport))
        with self.assertRaises(ValueError):self.dispatch(worker,task)
        self.assertEqual(self.state.get(task)['status'],'cancelled')

    def test_changed_file_rejects_result(self):
        def transport(p,c):
            (self.root/'file.txt').write_text('changed');return reply()
        worker=Worker('file_search',**dict(self.settings,transport=transport))
        with self.assertRaises(WorkerRejected):self.dispatch(worker)

    def test_outside_path_rejected_before_api_call(self):
        worker=Worker('file_search',**self.settings)
        with self.assertRaises(WorkerRejected):worker.dispatch(self.task(),1,['../outside'],query='x')
        self.assertEqual(len(self.requests),0)

    def test_incomplete_response_is_not_accepted(self):
        def transport(p,c):
            data,rid=reply();data['stop_reason']='max_tokens';return data,rid
        worker=Worker('file_search',**dict(self.settings,transport=transport))
        task=self.task()
        with self.assertRaises(WorkerRejected):self.dispatch(worker,task)
        self.assertEqual(self.state.get(task)['status'],'pending')

    def test_call_limit_blocks_additional_requests(self):
        worker=Worker('file_search',**self.settings,max_calls=1)
        self.dispatch(worker)
        with self.assertRaises(WorkerRejected):self.dispatch(worker)
        self.assertEqual(len(self.requests),1)

    def test_context_reset_only_between_tasks(self):
        worker=Worker('file_search',**self.settings,max_context_bytes=2000)
        worker.history=[{'role':'user','content':[{'type':'text','text':'x'*3000}]}]
        result=self.dispatch(worker)
        self.assertEqual(result['prior_messages'],0);self.assertEqual(result['context_resets'],1)

    def test_fixed_test_command_records_failure_without_claiming_test_success(self):
        worker=Worker('test_runner',**self.settings,test_command=[sys.executable,'-c','print("FAILED fixture");raise SystemExit(1)'])
        result=self.dispatch(worker)
        self.assertEqual(result['operation']['exit_code'],1)
        self.assertIn('FAILED',result['evidence']['digest'])
        # Task completion means the test-run report was produced, not that tests passed.
        self.assertEqual(self.state.get(result['task'])['status'],'done')

    def test_unknown_cost_blocks_followup(self):
        def transport(p,c):
            data,rid=reply();data['usage']={};return data,rid
        worker=Worker('file_search',**dict(self.settings,transport=transport))
        self.dispatch(worker)
        with self.assertRaises(WorkerRejected):self.dispatch(worker)
