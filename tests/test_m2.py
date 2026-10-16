from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from modelpilot.m2 import State, assess, normalize_error


class DetectorTests(unittest.TestCase):
    def test_retry_language_alone_does_not_escalate(self):
        self.assertEqual(assess([{'retry_language':True}]*6)['recommendation'],'hold')

    def test_verified_progress_resets_repeated_errors(self):
        e={'error_hash':'same'}
        self.assertEqual(assess([e]*3)['recommendation'],'increase_effort')
        self.assertEqual(assess([e,e,{'progress':True},e,{'progress':True},e])['recommendation'],'hold')

    def test_oscillation_requires_same_file(self):
        events=[{'file':'a','content_hash':h} for h in ('a','b','a','b')]
        self.assertTrue(assess(events)['signals']['edit_oscillation'])
        events[-1]['file']='other'
        self.assertFalse(assess(events)['signals']['edit_oscillation'])

    def test_test_progress_and_success_are_not_stuck(self):
        for counts in ([3,2,1],[0,0,0]):
            self.assertEqual(assess([{'suite':'unit','failures':n} for n in counts])['recommendation'],'hold')
        self.assertTrue(assess([{'suite':'unit','failures':2}]*3)['signals']['stalled_tests'])

    def test_the_agents_own_test_runs_are_observed_apart_and_never_scored(self):
        result=assess([{'agent_suite':'python3 -m pytest -q','agent_failures':2}]*3)
        self.assertTrue(result['signals']['stalled_agent_tests'])
        self.assertEqual((result['score'],result['recommendation'],result['observed_only']),(0,'hold',['stalled_agent_tests']))
        self.assertFalse(result['signals']['stalled_tests'])  # never read as a host suite's result
        mixed=[{'suite':'unit','failures':2},{'agent_suite':'unit','agent_failures':2}]*2  # two series of two, not one of four
        self.assertFalse(any(assess(mixed)['signals'][s] for s in ('stalled_tests','stalled_agent_tests')))
        self.assertEqual(assess([{'suite':'unit','failures':2}]*3)['recommendation'],'increase_effort')  # a host suite's still counts

    def test_errors_hash_alike_across_temporary_paths_lines_addresses_and_timings(self):
        one=('Exit code 1\n  File "/private/var/folders/ab/T/pytest-7/w/test_x.py", line 6, in test_b\n'
             'AssertionError: <Node object at 0x7f3a2c>\nRan 2 tests in 0.004s\nFAILED (failures=1)')
        two=one.replace('pytest-7','pytest-8').replace('line 6','line 9').replace('0x7f3a2c','0x10b4e').replace('0.004s','1.2s')
        self.assertEqual(normalize_error(one),normalize_error(two))
        self.assertIn('"test_x.py", line #',normalize_error(one))
        self.assertEqual(normalize_error('src/pkg/core.py:41:7: E501'),'src/pkg/core.py:#: E501')
        self.assertNotEqual(normalize_error(one),normalize_error(one.replace('failures=1','failures=2')))  # counts stay
        self.assertNotEqual(normalize_error('a/test_x.py: 2 failed'),normalize_error('b/test_x.py: 2 failed'))  # relative paths stay


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)/'state.db'
        self.state=State(self.path)
        self.task=self.state.create('test','initial')['id']
        self.state.claim(self.task,1,'worker')

    def tearDown(self):
        self.state.close(); self.tmp.cleanup()

    def test_correction_blocks_old_and_unacknowledged_results(self):
        self.state.correct(self.task,1,'new')
        with self.assertRaises(ValueError): self.state.complete(self.task,1,'worker','stale')
        with self.assertRaises(ValueError): self.state.complete(self.task,2,'worker','unacked')
        self.state.acknowledge(self.task,2,'worker')
        self.assertEqual(self.state.complete(self.task,2,'worker','valid')['status'],'done')

    def test_cancel_blocks_completion(self):
        self.state.cancel(self.task,1)
        with self.assertRaises(ValueError): self.state.complete(self.task,1,'worker','late')

    def test_reclaim_invalidates_previous_lease_even_same_owner(self):
        with self.state.db:
            self.state.db.execute('UPDATE tasks SET lease_until=0 WHERE id=?',(self.task,))
        row=self.state.claim(self.task,1,'worker')
        self.assertEqual(row['revision'],2)
        with self.assertRaises(ValueError): self.state.complete(self.task,1,'worker','old')

    def test_concurrent_claim_only_one_wins(self):
        task=self.state.create('race','test')['id']
        def claim(owner):
            state=State(self.path)
            try:
                state.claim(task,1,owner); return True
            except ValueError: return False
            finally: state.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(claim,['a','b']))
        self.assertEqual(sum(outcomes),1)

    def test_escalation_confirmation_is_one_rung_and_needs_new_evidence(self):
        for _ in range(3): result=self.state.observe(self.task,1,'worker',{'error':'same'})
        self.assertEqual(result['recommendation'],'increase_effort')
        self.state.confirm_escalation(self.task,1,'worker',result['last_event'])
        with self.assertRaises(ValueError): self.state.confirm_escalation(self.task,1,'worker',result['last_event'])
        for _ in range(3): result=self.state.observe(self.task,1,'worker',{'error':'same'})
        self.assertEqual(result['recommendation'],'stronger_model')
        self.assertFalse(result['applied'])

    def test_output_unicode_pagination_roundtrip_and_path_rejection(self):
        text='é🐈\n'*3000
        output=self.state.store_output(text)
        self.assertTrue(output['truncated'])
        self.assertEqual(output,self.state.store_output(text))
        restored=''; offset=0
        while offset is not None:
            page=self.state.expand(output['handle'],offset,777)
            restored+=page['text']; offset=page['next_offset']
        self.assertEqual(restored,text)
        with self.assertRaises(ValueError): self.state.expand('../../etc/passwd')
        with self.assertRaises(ValueError): self.state.expand(output['handle'],limit=0)

    def test_errors_are_hashed_normalized_and_agent_fields_are_checked(self):
        for n in (1,2,3): result=self.state.observe(self.task,1,'worker',{'error':f'boom in /tmp/run{n}/x.py, line {n}'})
        self.assertEqual((result['signals']['repeated_error'],result['recommendation']),(True,'increase_effort'))
        for bad in ({'agent_failures':-1},{'agent_failures':'2'},{'agent_failures':True},{'agent_suite':3}):
            with self.assertRaises(ValueError): self.state.observe(self.task,1,'worker',bad)
        result=self.state.observe(self.task,1,'worker',{'agent_suite':'pytest -q','agent_failures':0})
        self.assertEqual(self.state.observations(self.task,1,'agent_failures')[0]['agent_suite'],'pytest -q')
        self.assertEqual(self.state.observations(self.task,1,'failures'),[])  # a host suite's readers never see it

    def test_wrong_owner_observation_does_not_pollute_detector(self):
        with self.assertRaises(ValueError): self.state.observe(self.task,1,'other',{'error':'bad'})
        self.assertEqual(self.state.recommend(self.task)['window_events'],0)


# Another process tries for a write lock over SQLite's shared-lock bytes (PENDING_BYTE + 2, 510 bytes): held while any
# connection here has the database open in WAL mode.
LOCK_PROBE = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR)
try:
    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 510, 0x40000000 + 2, os.SEEK_SET)
except OSError:
    print('held')
else:
    print('free')
"""


@unittest.skipIf(sys.platform == 'win32', 'POSIX advisory locks')
class LedgerLockTests(unittest.TestCase):
    def test_opening_the_ledger_again_keeps_this_processs_sqlite_locks(self):
        """The proxy and coordinator open the ledger once per request while hook processes write it. Opening it must
        not drop the locks of connections this process already has (October 14: an os.open/os.close of the file did,
        so a hook could checkpoint under them and real-client sessions ended 'database disk image is malformed')."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp)/'ledger.sqlite3'
            probe = lambda: subprocess.run([sys.executable, '-c', LOCK_PROBE, str(db)], capture_output=True, text=True,
                                           check=True).stdout.strip()
            first = State(db)
            try:
                if probe() != 'held':
                    self.skipTest('this SQLite holds no lock on an idle WAL database')
                second = State(db)
                try:
                    self.assertEqual(probe(), 'held')
                finally:
                    second.close()
            finally:
                first.close()
            self.assertEqual(db.stat().st_mode & 0o777, 0o600)  # a new ledger is private

