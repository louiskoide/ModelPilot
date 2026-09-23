import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from modelpilot.fixtures import fixture_server
from modelpilot.governed_session import run_session

RATES = {'claude-sonnet-4-6': dict(input=3, output=15, read=.3, write_5m=3.75, write_1h=6)}


@unittest.skipUnless(shutil.which('claude'), 'Claude Code CLI not installed')
class OfflineGovernedSessionTests(unittest.TestCase):
    """Real Claude Code client, fake key, loopback fixture upstream: $0 and no provider traffic."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.upstream = fixture_server()
        self.upstream.keep_bodies = True
        self.upstream.delay = .5  # leaves the coordinator time to correct between tool steps
        self.thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.upstream.shutdown()
        self.upstream.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def test_correction_reaches_the_model_and_every_request_settles(self):
        run = Path(self.tmp.name)/'run'
        work = run/'workspace'
        self.upstream.script = [{'tool': 'Read', 'input': {'file_path': str(work/'a.txt')}},
                                {'tool': 'Write', 'input': {'file_path': str(work/'notes.txt'), 'content': 'draft\n'}},
                                {'tool': 'Bash', 'input': {'command': 'exit 3', 'description': 'fail on purpose'}},
                                {'tool': 'Read', 'input': {'file_path': str(work/'b.txt')}},
                                {'text': 'DONE'}]
        report = run_session(run, shutil.which('claude'), 'sk-ant-offline-fixture-not-a-key',
                             f'http://127.0.0.1:{self.upstream.server_port}', RATES,
                             prompt='Read a.txt and follow it.', instruction='Report the first token.',
                             correction='CORRECTION_MARKER: report the second token instead.',
                             files={'a.txt': 'ALPHA. Next read b.txt.\n', 'b.txt': 'BRAVO\n'},
                             tools='Read,Write,Bash', limit_usd=5)
        bodies = [json.loads(b) for b in self.upstream.bodies]
        carrying = [i for i, b in enumerate(self.upstream.bodies) if b'CORRECTION_MARKER' in b]
        self.assertTrue(carrying, 'correction never reached a model request')
        self.assertEqual(carrying[-1], len(bodies)-1)  # still in context for the final answer
        self.assertTrue(report['correction']['delivered'])
        self.assertTrue(report['correction']['acknowledged'])
        self.assertEqual(report['proxy']['requests'], len(bodies))
        self.assertEqual(report['proxy']['unsettled'], 0)
        self.assertEqual(report['governor']['still_unknown'], 0)
        self.assertAlmostEqual(report['governor']['spent_usd'], report['proxy']['known_cost_usd'])
        self.assertTrue(report['accounting_matches'], report)
        self.assertGreaterEqual(report['observations'], 2)  # the write and the failed command
        self.assertFalse(report['applied'])
        self.assertEqual(report['status'], 'passed', report)
        self.assertTrue((run/'summary.json').exists())


if __name__ == '__main__': unittest.main()
