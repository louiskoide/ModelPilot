import http.client,json,tempfile,threading,unittest
from pathlib import Path
from modelpilot.proxy import ProxyServer
from modelpilot.fixtures import fixture_server

class ShutdownTests(unittest.TestCase):
    def test_close_waits_for_inflight_metadata_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider=fixture_server();proxy=ProxyServer(('127.0.0.1',0),f'http://127.0.0.1:{provider.server_port}',Path(tmp)/'log',{})
            threads=[threading.Thread(target=s.serve_forever,daemon=True) for s in (provider,proxy)]
            for t in threads:t.start()
            entered,release,closed=threading.Event(),threading.Event(),threading.Event()
            record=proxy.record
            def delayed(row):
                entered.set();release.wait(5);record(row)
            proxy.record=delayed
            conn=http.client.HTTPConnection('127.0.0.1',proxy.server_port,timeout=3)
            conn.request('POST','/v1/messages',json.dumps({'model':'fixture','messages':[],'max_tokens':1}),{'Content-Type':'application/json'})
            response=conn.getresponse()
            self.assertTrue(entered.wait(3))
            proxy.shutdown()
            def close():proxy.server_close();closed.set()
            closer=threading.Thread(target=close);closer.start()
            try:self.assertFalse(closed.wait(.1),'log closed while request still owns it')
            finally:
                release.set();closer.join(5);conn.close()
                provider.shutdown();provider.server_close()
                for t in threads:t.join()
            self.assertTrue(closed.is_set())
            self.assertEqual(len((Path(tmp)/'log').read_text().splitlines()),1)
