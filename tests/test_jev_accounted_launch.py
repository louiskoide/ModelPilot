import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from modelpilot.fixtures import fixture_server
from modelpilot.proxy import ProxyServer

ROOT = Path(__file__).resolve().parents[1]
JEV = ROOT/'work/jev-router-compat'
RATES = json.loads((ROOT/'configs/jev-rates.json').read_text())['rates'] if (ROOT/'configs/jev-rates.json').exists() else {}


@unittest.skipUnless(shutil.which('node') and (JEV/'src/proxy.mjs').exists(), 'needs node and work/jev-router-compat')
class AccountedLaunchSelfTest(unittest.TestCase):
    """Jev's real proxy -> ModelPilot ProxyServer -> loopback fixture; stub router, no Claude Code."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name)/'observations.jsonl'
        self.upstream = fixture_server()
        self.proxy = ProxyServer(('127.0.0.1', 0), f'http://127.0.0.1:{self.upstream.server_port}', self.log, RATES)
        self.threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (self.upstream, self.proxy)]
        for t in self.threads:
            t.start()

    def tearDown(self):
        for s in (self.proxy, self.upstream):
            s.shutdown()
            s.server_close()
        for t in self.threads:
            t.join()
        self.tmp.cleanup()

    def test_rewritten_request_is_measured_behind_jev(self):
        result = subprocess.run(['node', str(ROOT/'modelpilot/jev_accounted_launch.mjs'), '--self-test', str(JEV),
                                 f'http://127.0.0.1:{self.proxy.server_port}'], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        # Catalog discovery went through ModelPilot: the router saw the fixture's catalog, not static tiers.
        self.assertEqual(report['offered'], ['claude-sonnet-5'])
        self.assertEqual(report['status'], 200)
        for _ in range(200):
            if self.log.exists() and len(self.log.read_text().splitlines()) >= 2:
                break
            time.sleep(.005)
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual([r['kind'] for r in rows], ['models', 'messages'])
        messages = rows[1]
        self.assertEqual((messages['model'], messages['http_status'], messages['tool_count']), ('claude-sonnet-5', 200, 1))
        self.assertIsNotNone(messages['cost_usd'])
        self.assertFalse(messages['applied'])


if __name__ == '__main__': unittest.main()
