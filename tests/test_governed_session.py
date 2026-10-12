import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import unittest
from modelpilot.fixtures import fixture_server
from modelpilot.governed_session import REBUILD_STEPS, rebuild_status, run_rebuild_session, run_session

RATES = {'claude-sonnet-4-6': dict(input=3, output=15, read=.3, write_5m=3.75, write_1h=6)}
RATES_55 = {'claude-sonnet-5-5': dict(input=2, output=10, read=.1, write_5m=2.5, write_1h=4)}


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

    def test_a_turn_end_correction_then_a_rebuild_plan_acknowledged_by_observation(self):
        run = Path(self.tmp.name)/'run'
        work = run/'workspace'
        # Turn 1 reads, answers ALPHA (held by the fixture while the coordinator corrects) and, blocked, BRAVO; then the
        # follow-up, the compaction summary and the rebuilt turn's reply.
        self.upstream.script = [{'tool': 'Read', 'input': {'file_path': str(work/'tokens.txt')}}, {'text': 'DONE ALPHA'},
                                {'text': 'DONE BRAVO'}, {'text': 'READY'}, {'text': 'Summary: the token was BRAVO.'},
                                {'text': 'DONE'}]
        idled = []

        def idle(db, session, limit):  # the 300 s gap, without waiting it out: the last turn end moves back 400 s
            idled.append(True)
            ledger = sqlite3.connect(db)
            try:
                with ledger:
                    ledger.execute("UPDATE gov_decisions SET created=created-400 WHERE kind='client_stop'")
            finally:
                ledger.close()
        report = run_rebuild_session(run, shutil.which('claude'), 'sk-ant-offline-fixture-not-a-key',
                                     f'http://127.0.0.1:{self.upstream.server_port}', RATES_55,
                                     prompt='Read tokens.txt, then reply with only the token your instruction asks for.',
                                     instruction='Report the first token.',
                                     correction='CORRECTION_MARKER: report the second token instead.',
                                     follow_up='Reply READY.', final_prompt='Reply DONE.',
                                     files={'tokens.txt': 'First token: ALPHA\nSecond token: BRAVO\n'},
                                     expect=['BRAVO'], forbid=['ALPHA'], idle=idle)
        self.assertEqual(idled, [True])
        self.assertEqual([e['step'] for e in report['invocations']], ['task', 'follow_up', 'compact', 'rebuilt'])
        turn = report['turn_end']
        # Delivered by the blocked Stop, the only hook left once the tool result had gone out.
        self.assertEqual((turn['delivered_by'], turn['stop_blocks'], turn['stop_events_in_task_turn']), (['Stop'], 1, 2))
        self.assertTrue(turn['acknowledged'] and turn['obeyed'], turn)
        bodies = self.upstream.bodies
        answered = next(i for i, b in enumerate(bodies) if b'"tool_result"' in b)
        carrying = [i for i, b in enumerate(bodies) if b'CORRECTION_MARKER' in b]
        self.assertGreater(carrying[0], answered)
        self.assertIn(f"[ModelPilot ledger update, code {turn['channel_code']}]".encode(), bodies[carrying[0]])
        rebuild = report['rebuild']
        self.assertEqual((rebuild['plan']['trigger'], rebuild['planned_in'], rebuild['plan_kinds']),
                         ('idle', 'follow_up', ['compact', 'effort']))
        self.assertEqual(rebuild['compact_observed_in'], 'compact')
        self.assertEqual((rebuild['acknowledged_by'], rebuild['acknowledged_in']), ('observed', 'rebuilt'))
        self.assertEqual(rebuild['efforts_by_step'], {'task': ['low'], 'follow_up': ['low'], 'compact': ['low'],
                                                      'rebuilt': ['medium']})
        self.assertEqual(rebuild['compaction_requests'], 1)
        self.assertTrue(rebuild['first_request']['compact']['compaction'])
        self.assertEqual(rebuild['first_request']['rebuilt']['effort'], 'medium')
        self.assertEqual(report['proxy']['unsettled'], 0)
        self.assertEqual(report['governor']['still_unknown'], 0)
        self.assertTrue(report['governor_matches_proxy'], report)
        # The last invocation's client totals are cumulative: they match the wire over all four, compaction included.
        self.assertTrue(report['client_tokens_match'] and report['client_cost_matches'], report)
        self.assertEqual(report['hook_errors'], 0)
        self.assertFalse(report['applied'])
        self.assertEqual(report['status'], 'passed', json.dumps(report, indent=1))
        self.assertTrue((run/'summary.json').exists())


class RebuildStatusTests(unittest.TestCase):
    def test_a_correction_that_reaches_the_model_through_a_tool_hook_is_inconclusive(self):
        report = {'invocations': [{'step': s, 'returncode': 0} for s in REBUILD_STEPS], 'target_effort': 'medium',
                  'client': {s: {'result_subtype': 'success', 'is_error': False} for s in REBUILD_STEPS},
                  'proxy': {'requests': 7, 'unsettled': 0, 'http_statuses': [200] * 7, 'unpriced': 0},
                  'governor': {'still_unknown': 0, 'cost_complete': True}, 'governor_matches_proxy': True,
                  'client_tokens_match': True, 'hook_errors': 0, 'applied': False,
                  'turn_end': {'correction_issued': True, 'acknowledged': True, 'obeyed': True, 'delivered_by': ['Stop'],
                               'stop_blocks': 1, 'stop_events_in_task_turn': 2},
                  'rebuild': {'plan': {'trigger': 'idle'}, 'planned_in': 'follow_up', 'plan_kinds': ['compact', 'effort'],
                              'compact_observed_in': 'compact', 'acknowledged_by': 'observed', 'acknowledged_in': 'rebuilt',
                              'efforts_by_step': {'rebuilt': ['medium']}}}
        self.assertEqual(rebuild_status(report), 'passed')
        report['turn_end'].update(delivered_by=['PostToolUse'], stop_blocks=0, stop_events_in_task_turn=1)
        self.assertEqual(rebuild_status(report), 'inconclusive')
        report['rebuild']['acknowledged_by'] = None  # never seen running the plan: no observed rebuild
        self.assertEqual(rebuild_status(report), 'failed')


if __name__ == '__main__': unittest.main()
