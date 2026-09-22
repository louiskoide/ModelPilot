import unittest
from modelpilot.claude_check import validate_result


class ClaudeCheckTests(unittest.TestCase):
    def test_tool_claim_without_tool_event_is_not_pass(self):
        events = [{'type': 'result', 'subtype': 'success', 'result': 'MODEL_PILOT_OK'}]
        self.assertFalse(validate_result(events, True)['passed'])

    def test_tool_and_success_result_required(self):
        events = [{'type': 'assistant', 'message': {'content': [{'type':'tool_use', 'name':'Read'}]}},
                  {'type':'result','subtype':'success','result':'MODEL_PILOT_OK'}]
        self.assertTrue(validate_result(events, True)['passed'])
        events[-1]['is_error'] = True
        self.assertFalse(validate_result(events, True)['passed'])

    def test_budget_stop_is_not_pass(self):
        self.assertFalse(validate_result([{'type':'result','subtype':'error_max_budget_usd','result':'OK'}], False)['passed'])

    def test_acknowledgment_period_is_not_transport_failure(self):
        for answer in ('OK', 'OK.', ' OK.\n'):
            self.assertTrue(validate_result([{'type':'result','subtype':'success','result':answer}], False)['passed'])
        self.assertFalse(validate_result([{'type':'result','subtype':'success','result':'Not OK'}], False)['passed'])

class M2CheckTests(unittest.TestCase):
    def test_correct_answer_without_successful_tools_is_not_pass(self):
        from modelpilot.claude_check import validate_m2
        events=[{'type':'result','subtype':'success','result':'TOKEN'}]
        self.assertFalse(validate_m2(events,['TOKEN'],[('mcp__modelpilot__expand_output',{'handle':'h'})])['passed'])

    def test_tool_input_and_success_must_match(self):
        from modelpilot.claude_check import validate_m2
        events=[{'type':'assistant','message':{'content':[{'type':'tool_use','id':'1','name':'tool','input':{'task':'correct'}}]}},
                {'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'1','content':'TOKEN'}]}},
                {'type':'result','subtype':'success','result':'TOKEN'}]
        self.assertTrue(validate_m2(events,['TOKEN'],[('tool',{'task':'correct'})])['passed'])
        self.assertFalse(validate_m2(events,['TOKEN'],[('tool',{'task':'wrong'})])['passed'])
        events[1]['message']['content'][0]['is_error']=True
        self.assertFalse(validate_m2(events,['TOKEN'],[('tool',{'task':'correct'})])['passed'])

    def test_generated_server_starts_from_fixture_directory(self):
        import tempfile, json, subprocess
        from pathlib import Path
        from modelpilot.claude_check import m2_cases
        with tempfile.TemporaryDirectory() as directory:
            run=Path(directory); scratch=run/'fixture';scratch.mkdir()
            root=Path(__file__).resolve().parents[1]
            config,cases=m2_cases(root,run)
            server=json.loads(config.read_text())['mcpServers']['modelpilot']
            for _,prompt,expected,_ in cases:
                self.assertNotIn(expected[0],prompt)
            requests=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18'}},
                      {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'expand_output','arguments':cases[0][3][0][1]}}]
            result=subprocess.run([server['command']]+server['args'],cwd=scratch,input='\n'.join(json.dumps(x) for x in requests)+'\n',text=True,capture_output=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            replies=[json.loads(line) for line in result.stdout.splitlines()]
            self.assertIn(cases[0][2][0],replies[-1]['result']['content'][0]['text'])
