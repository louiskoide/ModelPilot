import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from modelpilot.m2 import State


class MCPTests(unittest.TestCase):
    def test_stdio_initialize_expand_and_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'state.db'; state=State(path)
            handle=state.store_output('full tool output')['handle']; state.close()
            requests=[
                {'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18'}},
                {'jsonrpc':'2.0','method':'notifications/initialized'},
                {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'expand_output','arguments':{'handle':handle}}},
                {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'expand_output','arguments':{'handle':handle,'limit':True}}},
                {'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'cancel','arguments':{}}}]
            result=subprocess.run([sys.executable,'-m','modelpilot.m2_mcp','--db',str(path)],input='\n'.join(json.dumps(x) for x in requests)+'\n',text=True,capture_output=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            replies=[json.loads(x) for x in result.stdout.splitlines()]
            self.assertEqual(len(replies),4)
            self.assertEqual(json.loads(replies[1]['result']['content'][0]['text'])['text'],'full tool output')
            self.assertIn('error',replies[2]); self.assertIn('error',replies[3])
