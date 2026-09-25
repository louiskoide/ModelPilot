"""Pinned Jev proxy -> accounting proxy -> scripted provider. Never contacts TypeSafe."""
import json,os,shutil,subprocess,tempfile,threading,time,unittest
from pathlib import Path
from modelpilot.fixtures import fixture_server
from modelpilot.proxy import ProxyServer
from modelpilot.bench_adapters import JevAdapter,ROOT
from modelpilot.jev_route_check import TOKEN_FIELDS

AVAILABLE=bool(shutil.which('node')) and all((ROOT/f'work/jev-router-{v}/src/proxy.mjs').exists() for v in ('baseline','compat'))

@unittest.skipUnless(AVAILABLE,'requires pinned Jev checkouts and Node')
class PinnedProxyTests(unittest.TestCase):
    def probe(self,variant,scenario='normal'):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'tmp').mkdir()
            adapter=JevAdapter(variant,ROOT/('work/jev-router-baseline' if variant=='stock' else 'work/jev-router-compat'),Path(shutil.which('node')),'fake-router')
            adapter.verify()
            rates=json.loads((ROOT/'configs/jev-rates.json').read_text())['rates']
            provider=fixture_server()
            proxy=ProxyServer(('127.0.0.1',0),f'http://127.0.0.1:{provider.server_port}',root/'observations.jsonl',rates)
            threads=[threading.Thread(target=s.serve_forever,daemon=True) for s in (provider,proxy)]
            for t in threads:t.start()
            try:
                env={'PATH':os.environ['PATH'],'HOME':tmp,'TMPDIR':str(root/'tmp'),'JEV_DEBUG':'1'}
                result=subprocess.run([str(adapter.node),str(ROOT/'tests/fixtures/jev_proxy_probe.mjs'),str(adapter.checkout),f'http://127.0.0.1:{proxy.server_port}',scenario],env=env,capture_output=True,text=True,timeout=30)
                self.assertEqual(result.returncode,0,result.stderr)
                (root/'client.stderr.txt').write_text(result.stderr)
                evidence=adapter.evidence(root)
                for _ in range(100):
                    rows=[json.loads(x) for x in (root/'observations.jsonl').read_text().splitlines()]
                    if len(rows)>=4:break
                    time.sleep(.01)
                messages=[r for r in rows if r['kind']=='messages']
                probe=json.loads(result.stdout)
                usage={client:sum(reply['body'].get('usage',{}).get(wire,0) for reply in probe['replies'] if reply['status']==200) for wire,client in TOKEN_FIELDS}
                return probe,evidence,messages,adapter.accounting(rows,{'modelUsage':{'jev-router':usage}})
            finally:
                for s in (proxy,provider):s.shutdown();s.server_close()
                for t in threads:t.join()

    def test_stock_silent_default_is_not_routing(self):
        probe,evidence,rows,cost=self.probe('stock')
        self.assertEqual(probe['calls'],0)
        self.assertFalse(evidence['routing_observed'])
        self.assertEqual({r['model'] for r in rows},{'claude-opus-5'})

    def test_compat_routes_once_per_turn_not_per_tool(self):
        probe,evidence,rows,cost=self.probe('compat')
        self.assertEqual((probe['afterContinuation'],probe['calls']),(1,2))
        self.assertTrue(evidence['routing_observed'])
        self.assertEqual(evidence['decision_count'],2)
        self.assertEqual({r['model'] for r in rows},{'claude-sonnet-5'})
        self.assertTrue(all(r['cost_usd'] is not None for r in rows))
        self.assertIsNone(cost['cost_usd'])
        self.assertTrue(cost['tokens_match'])
        self.assertAlmostEqual(cost['provider_cost_usd'],3*(10*2+14750*.2+4*10)/1e6)

    def test_fallback_is_not_reported_as_verified_routing(self):
        probe,evidence,rows,cost=self.probe('compat','fallback')
        self.assertFalse(evidence['routing_observed'])
        self.assertIn('no-jev',evidence['fallback_markers'])
        self.assertIsNone(cost['cost_usd'])

    def test_rejected_request_leaves_provider_total_unknown(self):
        probe,evidence,rows,cost=self.probe('compat','reject')
        self.assertEqual(rows[-1]['http_status'],429)
        self.assertIsNone(cost['provider_cost_usd'])
        self.assertIsNone(cost['cost_usd'])
