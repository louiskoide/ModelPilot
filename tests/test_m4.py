import math
import unittest
from modelpilot.m4 import cascade,run_scenarios,shadow_plan,sha

RATES={'write_5m':3.75,'write_1h':6,'read':.3,'output':15}

class CascadeTests(unittest.TestCase):
    def test_decision_matrix(self):
        self.assertTrue(all(c['passed'] for c in run_scenarios()))

    def test_model_confidence_does_not_override_bad_evidence(self):
        r=cascade('read',{'confidence':1,'answer':'made up'}, {'revision':1},1,1)
        self.assertEqual(r['action'],'would_escalate')

    def test_boolean_exit_code_rejected(self):
        r=cascade('test_report',{'exit_code':False,'passed':True,'run_id':'x'},
                  {'revision':1,'completed':True,'exit_code':0,'run_id':'x'},1,1)
        self.assertEqual(r['action'],'would_escalate')

    def test_source_hash_alone_is_not_enough(self):
        text='hello'
        r=cascade('read',{'source_sha256':sha(text),'start':0,'end':5,'answer':'world'},
                  {'revision':1,'text':text,'current_source_sha256':sha(text)},1,1)
        self.assertEqual(r['action'],'would_escalate')

class ShadowTests(unittest.TestCase):
    def test_no_refresh_still_prepays_write_plus_use_read(self):
        p=shadow_plan(1000,RATES,1)
        self.assertAlmostEqual(p['expected_shadow_usd'],.00405)
        self.assertAlmostEqual(p['expected_on_demand_usd'],.00375)
        self.assertFalse(p['would_consider_warming'])

    def test_unused_shadow_and_refreshes_are_charged(self):
        p=shadow_plan(1000,RATES,0,refreshes=2)
        self.assertEqual(p['expected_on_demand_usd'],0)
        self.assertAlmostEqual(p['expected_shadow_usd'],.00435)

    def test_latency_value_never_activates_warming(self):
        p=shadow_plan(1000,RATES,1,latency_value_usd=.1)
        self.assertTrue(p['would_consider_warming']);self.assertFalse(p['warming_enabled']);self.assertFalse(p['applied'])
        self.assertFalse(shadow_plan(1000,RATES,1,latency_value_usd=.1,configuration_matches=False)['would_consider_warming'])

    def test_hour_ttl_and_one_token_warmups(self):
        p=shadow_plan(1000,RATES,1,ttl='1h',refreshes=1,refresh_output_tokens=1)
        self.assertAlmostEqual(p['expected_shadow_usd'],.006+2*.0003+2*.000015)

    def test_invalid_economics_rejected(self):
        for value in (-1,math.nan,math.inf,True):
            with self.assertRaises(ValueError):shadow_plan(value,RATES,1)
        with self.assertRaises(ValueError):shadow_plan(1000,RATES,1.1)

class DraftTests(unittest.TestCase):
    def test_truncated_or_malformed_draft_escalates(self):
        from modelpilot.cascade_check import verify_draft
        for response in ({'stop_reason':'max_tokens'}, {'stop_reason':'end_turn','content':[{'type':'text','text':'not json'}]}):
            self.assertEqual(verify_draft(response,{'text':'TOKEN_a','revision':1})['action'],'would_escalate')

    def test_source_supported_draft_accepts_without_execution(self):
        from modelpilot.cascade_check import verify_draft
        text='TOKEN_a'
        result=verify_draft({'stop_reason':'end_turn','content':[{'type':'text','text':'{"answer":"TOKEN_a","start":0,"end":7}'}]},
                            {'text':text,'revision':1,'current_source_sha256':sha(text)})
        self.assertEqual(result['action'],'would_accept');self.assertFalse(result['applied'])
