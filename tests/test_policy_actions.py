import copy,tempfile,unittest
from pathlib import Path
from unittest import mock
from modelpilot import policy_actions
from modelpilot.m2 import State
from modelpilot.policy_actions import transform_request, escalation_proposal, prepare_action

H='claude-haiku-4-5-20251001'; S='claude-sonnet-5'; O='claude-opus-5-5'  # the ladder's top rung
# Probe summaries whose verified_transitions fill THINKING_HISTORY_VERIFIED (docs/thinking-history-probe.md).
EVIDENCE={'thinking-probe-transitions-20260926-131953':('349303b64a924350bf911fe189ad95c9635ee58aca6a1d82f5615f0c955b7c32',{(S,S)}),
          'thinking-probe-top-rung-20260926-153723':('08ba4941f6d2b2c1a6b0780b1359675fcf200db623c78f4dfae359d25d5d642d',{(S,O),(O,S),(O,O)})}
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
        with mock.patch.object(policy_actions,'THINKING_HISTORY_VERIFIED',frozenset()):
            with self.assertRaises(ValueError):transform_request(p,S,'medium')
        out=transform_request(p,O,'high')
        self.assertEqual(out['messages'][0],p['messages'][0])
    def with_thinking(self):
        p=self.request(); p['messages'].insert(0,{'role':'assistant','content':[{'type':'thinking','thinking':'private','signature':'sig'}]})
        return p
    def test_probe_override_builds_and_keeps_thinking_blocks_unchanged(self):
        p=self.with_thinking(); old=copy.deepcopy(p)
        out=transform_request(p,S,'medium',allow_thinking_history=True)
        self.assertEqual(p,old)
        self.assertEqual(out['messages'][0],old['messages'][0])
        self.assertEqual((out['model'],out['output_config']['effort']),(S,'medium'))
    def test_verified_pairs_come_from_the_probe_evidence(self):
        # Every ladder rung and correction reset between Sonnet 5 and Opus 5.5; never Haiku (its targets are
        # refused by the transform for mid-history system messages) and never Opus 5 (it refused every request).
        self.assertEqual(policy_actions.THINKING_HISTORY_VERIFIED,frozenset(set().union(*(p for _,p in EVIDENCE.values()))))
        import hashlib,json
        from modelpilot.thinking_probe import verified_transitions
        for run,(digest,pairs) in EVIDENCE.items():
            summary=Path(__file__).resolve().parents[1]/'runs'/run/'summary.json'
            if summary.exists():  # raw evidence is ignored by Git; check it wherever it was copied
                self.assertEqual(hashlib.sha256(summary.read_bytes()).hexdigest(),digest,run)
                self.assertEqual({tuple(p) for p in verified_transitions(json.loads(summary.read_text()))},pairs,run)
    def test_verified_changes_keep_thinking_history_unchanged(self):
        p=self.with_thinking(); p['model']=S; p['output_config']={'effort':'medium'}
        for model,effort in [(S,'high'),(O,'medium')]:
            out=transform_request(p,model,effort)
            self.assertEqual((out['model'],out['output_config']['effort']),(model,effort))
            self.assertEqual(out['messages'][0],p['messages'][0])
        p['model']=O
        self.assertEqual(transform_request(p,S,'medium')['messages'][0],p['messages'][0])  # correction reset
        with self.assertRaises(ValueError):transform_request(p,H,None)
    def test_only_verified_pairs_allow_thinking_history(self):
        with mock.patch.object(policy_actions,'THINKING_HISTORY_VERIFIED',frozenset({(O,S)})):
            self.assertEqual(transform_request(self.with_thinking(),S,'medium')['model'],S)
            for model,effort in [(O,'low'),(H,None)]:
                with self.assertRaises(ValueError):transform_request(self.with_thinking(),model,effort)
    def test_top_rung_is_opus_5_5_and_keeps_client_fields(self):
        self.assertEqual(policy_actions.MODELS,(H,S,O))
        with self.assertRaises(ValueError):transform_request(self.request(),'claude-opus-5','medium')  # no longer a tier
        p=self.request(); p['model']=S; p['output_config']={'effort':'high'}
        out=transform_request(p,O,'medium')
        self.assertEqual((out['model'],out['output_config']['effort'],out['thinking']),(O,'medium',{'type':'adaptive'}))
        self.assertEqual(out['messages'],p['messages'])  # Opus 5.5 takes mid-conversation system messages
        self.assertEqual(out['context_management'],p['context_management'])
    def test_unknown_model_and_invalid_haiku_effort_refused(self):
        for model,effort in [('unknown',None),(H,'high'),(S,'extreme')]:
            with self.assertRaises(ValueError):transform_request(self.request(),model,effort)
    def test_non_trailing_system_message_is_not_silently_moved(self):
        p=self.request(); p['messages'].append({'role':'user','content':'later'})
        with self.assertRaises(ValueError):transform_request(p,H,None)
    def test_sonnet_keeps_system_messages_as_the_client_sends_them(self):
        p=self.request(); p['messages'].append({'role':'user','content':'later'})
        self.assertEqual(transform_request(p,S,'low')['messages'],p['messages'])

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

    def test_sonnet_high_escalates_to_opus_5_5(self):
        p=escalation_proposal(self.state,self.task,1,'worker',S,'high')
        # The detector says increase_effort; at high effort the ladder moves up a tier instead.
        self.assertEqual((p['action'],p['target_model'],p['target_effort']),('increase_effort',O,'medium'))

    def test_prepare_action_never_overrides_the_thinking_gate(self):
        # With the model rung unverified, thinking history refuses it: the gate has no bypass here.
        p={'model':S,'max_tokens':32,'output_config':{'effort':'high'},
           'messages':[{'role':'user','content':'test'},
                       {'role':'assistant','content':[{'type':'thinking','thinking':'x','signature':'s'}]},
                       {'role':'user','content':'more'}]}
        rates={S:dict(input=2,write_5m=2.5,write_1h=4,read=.2,output=10)}
        proposal=escalation_proposal(self.state,self.task,1,'worker',S,'high')
        self.assertEqual((proposal['target_model'],proposal['target_effort']),(O,'medium'))
        with mock.patch.object(policy_actions,'THINKING_HISTORY_VERIFIED',frozenset()):
            with self.assertRaises(ValueError):prepare_action(self.state,proposal,'worker',p,1,rates)
        self.assertEqual(self.state.get(self.task)['level'],0)

    def test_tampered_proposal_cannot_skip_to_stronger_model(self):
        p=self.proposal();p['target_model']=O
        with self.assertRaises(ValueError):prepare_action(self.state,p,'worker',{},1,{})
