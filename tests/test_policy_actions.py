import copy,tempfile,unittest
from pathlib import Path
from modelpilot.m2 import State
from modelpilot.policy_actions import transform_request, escalation_proposal, prepare_action

H='claude-haiku-4-5-20251001'; S='claude-sonnet-5'; O='claude-opus-5'
class TransformTests(unittest.TestCase):
    def request(self):
        return dict(model=O,max_tokens=32,thinking={'type':'adaptive'},output_config={'effort':'high','format':{'type':'json_schema'}},
                    context_management={'edits':[{'type':'clear_thinking_20251015'},{'type':'clear_tool_uses_20250919'}]},
                    messages=[{'role':'user','content':[{'type':'tool_result','tool_use_id':'x','content':'result'}]},
                              {'role':'system','content':'environment'}])
    def test_haiku_removes_only_unsupported_options_preserves_tools(self):
        p=self.request(); old=copy.deepcopy(p)
        out=transform_request(p,H,None)
        self.assertEqual(p,old)
        self.assertNotIn('thinking',out)
        self.assertNotIn('effort',out['output_config'])
        self.assertIn('format',out['output_config'])
        self.assertEqual(out['messages'][0]['content'][0]['tool_use_id'],'x')
        self.assertIn('environment',out['messages'][0]['content'][-1]['text'])
        self.assertEqual(len(out['context_management']['edits']),1)
    def test_thinking_history_blocks_cross_model_switch(self):
        p=self.request(); p['messages'].insert(0,{'role':'assistant','content':[{'type':'thinking','thinking':'private','signature':'sig'}]})
        with self.assertRaises(ValueError):transform_request(p,S,'medium')
        out=transform_request(p,O,'high')
        self.assertEqual(out['messages'][0],p['messages'][0])
    def test_unknown_model_and_invalid_haiku_effort_refused(self):
        for model,effort in [('unknown',None),(H,'high'),(S,'extreme')]:
            with self.assertRaises(ValueError):transform_request(self.request(),model,effort)
    def test_non_trailing_system_message_is_not_silently_moved(self):
        p=self.request(); p['messages'].append({'role':'user','content':'later'})
        with self.assertRaises(ValueError):transform_request(p,S,'low')

class EscalationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.state=State(Path(self.tmp.name)/'s.db')
        self.task=self.state.create('t','fix')['id'];self.state.claim(self.task,1,'worker')
        for _ in range(3):self.state.observe(self.task,1,'worker',{'error':'same'})
    def tearDown(self):self.state.close();self.tmp.cleanup()
    def proposal(self):return escalation_proposal(self.state,self.task,1,'worker',S,'medium')
    def test_one_rung_preview_does_not_consume_window(self):
        a=self.proposal();b=self.proposal()
        self.assertEqual(a,b);self.assertEqual(a['target_effort'],'high')
        self.assertEqual(self.state.get(self.task)['level'],0)
        self.assertFalse(a['applied'])
    def test_correction_and_wrong_owner_block(self):
        with self.assertRaises(ValueError):escalation_proposal(self.state,self.task,1,'other',S,'medium')
        self.state.correct(self.task,1,'different')
        with self.assertRaises(ValueError):self.proposal()
    def test_new_observation_invalidates_prepared_action(self):
        a=self.proposal();self.state.observe(self.task,1,'worker',{'progress':True})
        with self.assertRaises(ValueError):prepare_action(self.state,a,'worker',{'model':S,'max_tokens':1,'messages':[],'output_config':{'effort':'medium'}},1,{})
    def test_budget_unknown_defers_and_does_not_reset_detector(self):
        p={'model':S,'max_tokens':1,'messages':[],'output_config':{'effort':'medium'}}
        d=prepare_action(self.state,self.proposal(),'worker',p,None,{})
        self.assertEqual(d['action'],'defer')
        self.assertEqual(self.state.get(self.task)['level'],0)

    def test_prepared_request_reserves_write_and_keeps_ladder_unchanged(self):
        p={'model':S,'max_tokens':32,'messages':[{'role':'user','content':'test'}],
           'output_config':{'effort':'medium'}}
        rates={S:dict(input=2,write_5m=2.5,write_1h=4,read=.2,output=10)}
        proposal=self.proposal()
        ready=prepare_action(self.state,proposal,'worker',p,1,rates)
        self.assertEqual(ready['action'],'prepared_offline')
        self.assertEqual(ready['request']['output_config']['effort'],'high')
        self.assertGreater(ready['reserve_usd'],0)
        self.assertEqual(self.state.get(self.task)['level'],0)
        denied=prepare_action(self.state,proposal,'worker',p,0,rates)
        self.assertEqual(denied['reason'],'insufficient_write_reservation')

    def test_tampered_proposal_cannot_skip_to_stronger_model(self):
        p=self.proposal();p['target_model']=O
        with self.assertRaises(ValueError):prepare_action(self.state,p,'worker',{},1,{})
