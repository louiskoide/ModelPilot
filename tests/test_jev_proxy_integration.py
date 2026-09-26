"""Pinned Jev proxy -> accounting proxy -> scripted provider. Never contacts TypeSafe."""
import json,os,shutil,subprocess,tempfile,threading,time,unittest
from pathlib import Path
from modelpilot.fixtures import fixture_server
from modelpilot.proxy import ProxyServer
from modelpilot import bench
from modelpilot.bench_jev import ROOT,JevRouter,routing
from modelpilot.jev_route_check import TOKEN_FIELDS,load_decisions

AVAILABLE=bool(shutil.which('node')) and all((ROOT/f'work/jev-router-{v}/src/proxy.mjs').exists() for v in ('baseline','compat'))

@unittest.skipUnless(AVAILABLE,'requires pinned Jev checkouts and Node')
class PinnedProxyTests(unittest.TestCase):
    def probe(self,variant,scenario='normal'):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'tmp').mkdir()
            router=JevRouter(bench.ARMS['jev-'+variant])
            router.verify()
            rates=json.loads((ROOT/'configs/jev-rates.json').read_text())['rates']
            provider=fixture_server()
            proxy=ProxyServer(('127.0.0.1',0),f'http://127.0.0.1:{provider.server_port}',root/'observations.jsonl',rates)
            threads=[threading.Thread(target=s.serve_forever,daemon=True) for s in (provider,proxy)]
            for t in threads:t.start()
            try:
                env={'PATH':os.environ['PATH'],'HOME':tmp,'TMPDIR':str(root/'tmp'),'JEV_DEBUG':'1'}
                result=subprocess.run([shutil.which('node'),str(ROOT/'tests/fixtures/jev_proxy_probe.mjs'),str(router.checkout),f'http://127.0.0.1:{proxy.server_port}',scenario],env=env,capture_output=True,text=True,timeout=30)
                self.assertEqual(result.returncode,0,result.stderr)
                for _ in range(100):
                    rows=[json.loads(x) for x in (root/'observations.jsonl').read_text().splitlines()]
                    if len(rows)>=4:break
                    time.sleep(.01)
                messages=[r for r in rows if r['kind']=='messages']
                session=[{'started_unix':0,'ended_unix':time.time()+60,'rows':[0,len(rows)]}]
                evidence=routing(load_decisions(root/'tmp'),result.stderr,rows,session)
                probe=json.loads(result.stdout)
                usage={client:sum(reply['body'].get('usage',{}).get(wire,0) for reply in probe['replies'] if reply['status']==200) for wire,client in TOKEN_FIELDS}
                return probe,evidence,messages,bench.accounting(rows,{'modelUsage':{'jev-router':usage}},jev=True)
            finally:
                for s in (proxy,provider):s.shutdown();s.server_close()
                for t in threads:t.join()

    def test_stock_silent_default_is_not_routing(self):
        probe,evidence,rows,cost=self.probe('stock')
        self.assertEqual(probe['calls'],0)
        self.assertFalse(evidence['routed'])
        self.assertEqual({r['model'] for r in rows},{'claude-opus-5'})

    def test_compat_routes_once_per_turn_not_per_tool(self):
        probe,evidence,rows,cost=self.probe('compat')
        self.assertEqual((probe['afterContinuation'],probe['calls']),(1,2))
        self.assertTrue(evidence['routed'])
        self.assertEqual(evidence['decisions'],2)
        self.assertEqual({r['model'] for r in rows},{'claude-sonnet-5'})
        self.assertTrue(all(r['cost_usd'] is not None for r in rows))
        self.assertTrue(cost['tokens_match'])
        # Provider cost only: the router is unpriced, so this is a labeled lower bound.
        self.assertEqual((cost['cost_scope'],cost['router_cost_usd'],cost['client_cost_matches']),
                         ('provider_only_router_unpriced',None,None))
        self.assertAlmostEqual(cost['cost_usd'],3*(10*2+14750*.2+4*10)/1e6)

    def test_fallback_is_not_reported_as_verified_routing(self):
        probe,evidence,rows,cost=self.probe('compat','fallback')
        self.assertFalse(evidence['routed'])
        self.assertIn('no-jev',evidence['fail_open'])
        self.assertIsNone(cost['router_cost_usd'])

    def test_rejected_request_leaves_provider_total_unknown(self):
        probe,evidence,rows,cost=self.probe('compat','reject')
        self.assertEqual(rows[-1]['http_status'],429)
        self.assertEqual(cost['rejected_requests'],1)
        self.assertIsNone(cost['cost_usd'])
