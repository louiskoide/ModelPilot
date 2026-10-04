import json
from pathlib import Path
import shutil
import sqlite3
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
        code = report['correction']['channel_code']
        self.assertIn(f'code {code}'.encode(), self.upstream.bodies[0])  # declared in the user's prompt
        self.assertIn(f'[ModelPilot ledger update, code {code}]'.encode(), self.upstream.bodies[-1])
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

    def test_correction_during_the_last_model_call_is_delivered_by_blocking_the_stop(self):
        run = Path(self.tmp.name)/'run'
        work = run/'workspace'
        self.upstream.script = [{'tool': 'Read', 'input': {'file_path': str(work/'a.txt')}},
                                {'text': 'DONE ALPHA'}, {'text': 'DONE BRAVO'}]
        # Issued while the fixture holds the turn's last reply: after the last tool hook, before Stop.
        last_call = lambda: any(b'"tool_result"' in body for body in self.upstream.bodies)
        report = run_session(run, shutil.which('claude'), 'sk-ant-offline-fixture-not-a-key',
                             f'http://127.0.0.1:{self.upstream.server_port}', RATES,
                             prompt='Read a.txt and report its token.', instruction='Report the first token.',
                             correction='CORRECTION_MARKER: report the second token instead.',
                             files={'a.txt': 'ALPHA\n'}, tools='Read', limit_usd=5, correct_when=last_call)
        code = report['correction']['channel_code']
        bodies = self.upstream.bodies
        answered = next(i for i, b in enumerate(bodies) if b'"tool_result"' in b)
        carrying = [i for i, b in enumerate(bodies) if b'CORRECTION_MARKER' in b]
        self.assertTrue(carrying, 'the correction never reached a model request')
        self.assertGreater(carrying[0], answered)  # not delivered by a tool hook: it arrived after the last one
        self.assertIn(f'[ModelPilot ledger update, code {code}]'.encode(), bodies[carrying[0]])
        self.assertEqual(report['hook_events'].count('Stop'), 2)  # blocked once, then the turn ended
        db = sqlite3.connect(run/'ledger.sqlite3')
        try:
            kinds = [(kind, json.loads(payload).get('event')) for kind, payload in
                     db.execute("SELECT kind,payload FROM gov_decisions WHERE kind IN ('deliver_correction','stop_block') ORDER BY seq")]
        finally:
            db.close()
        self.assertEqual(kinds, [('deliver_correction', 'Stop'), ('stop_block', None)])
        self.assertTrue(report['correction']['delivered'])
        self.assertTrue(report['correction']['acknowledged'])
        self.assertEqual(report['proxy']['unsettled'], 0)
        self.assertTrue(report['accounting_matches'], report)
        self.assertFalse(report['applied'])
        self.assertEqual(report['status'], 'passed', report)


if __name__ == '__main__': unittest.main()
