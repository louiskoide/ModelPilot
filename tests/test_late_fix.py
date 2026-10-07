"""The late-fix harness (plan item 7's measured part): host-written bug reports, the shipped miss as the session's
start, the repair graded strictly, and the run's stop rules. $0: nothing reaches a provider."""
import http.client
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from modelpilot import bench, bench_tasks, late_fix
from modelpilot.fixtures import fixture_server
from tests import test_bench_tasks as synthetic
from tests import test_regrade as regrade_tests
from tests.test_bench import RATES

UNITTEST_OUTPUT = """test_one (m.T.test_one) ... ok
test_two (m.T.test_two) ... FAIL

======================================================================
FAIL: test_two (m.T.test_two) (n=1)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/abs/tree/_edge/m.py", line 9, in test_two
    self.assertEqual(f(1), 2)
AssertionError: 1 != 2

======================================================================
FAIL: test_two (m.T.test_two) (n=2)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/abs/tree/_edge/m.py", line 9, in test_two
    self.assertEqual(f(2), 3)
AssertionError: 2 != 3

======================================================================
ERROR: test_three (m.T.test_three)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/abs/tree/_edge/m.py", line 12, in test_three
    g()
NameError: name 'g' is not defined

----------------------------------------------------------------------
Ran 3 tests in 0.001s

FAILED (failures=2, errors=1)
"""
PYTEST_OUTPUT = """F.F                                                                      [100%]
=================================== FAILURES ===================================
______________________________ test_cases[a-1] _______________________________

x = 'a', n = 1

    def test_cases(x, n):
>       helper(x, n)

pkg/tests/test_m.py:5:
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _

    def helper(x, n):
>       assert len(x) == n + 1
E       AssertionError: assert 1 == 2

pkg/tests/test_m.py:9: AssertionError
______________________________ TestK.test_method _______________________________

    def test_method(self):
>       assert False
E       assert False

pkg/tests/test_m.py:14: AssertionError
=========================== short test summary info ============================
FAILED pkg/tests/test_m.py::test_cases[a-1] - AssertionError: assert 1 == 2
FAILED pkg/tests/test_m.py::TestK::test_method - assert False
2 failed, 1 passed in 0.02s
"""
PYTEST_MODULE = '''import itertools
import os

import pytest

from pkg import thing

GRAPH = thing.Graph()
GRAPH.name = "g"
GRAPH.add(1, 2)
UNUSED = 3


def make():
    return thing.Graph()


CASES = [pytest.param(make, id="m"), pytest.param(GRAPH, id="g")]


def test_other():
    assert os.sep


@pytest.mark.parametrize("case", CASES)
def test_target(case):
    assert itertools.count
    assert case
'''
UNITTEST_MODULE = '''import unittest
import pkg


class T(unittest.TestCase):
    """Doc."""
    LIMIT = 3

    def setUp(self):
        self.value = pkg.make()

    def check(self, x):
        return self.inner(x)

    def inner(self, x):
        return x < self.LIMIT

    def unused_helper(self):
        pass

    def test_a(self):
        self.assertTrue(self.check(self.value))

    def test_b(self):
        self.assertTrue(False)
'''


class ReportTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def test_the_runners_own_account_of_one_unittest_failure(self):
        block = late_fix.failure_block(UNITTEST_OUTPUT, 'test_two (m.T)', regrade_tests.regrade.EDGE_COMMAND)
        self.assertEqual(block.count('FAIL: test_two'), 2)  # every failing subtest
        self.assertIn('AssertionError: 2 != 3', block)
        self.assertNotIn('test_three', block)
        last = late_fix.failure_block(UNITTEST_OUTPUT, 'test_three (m.T)', regrade_tests.regrade.EDGE_COMMAND)
        self.assertTrue(last.endswith("NameError: name 'g' is not defined"))
        self.assertIsNone(late_fix.failure_block(UNITTEST_OUTPUT, 'test_one (m.T)', regrade_tests.regrade.EDGE_COMMAND))
        self.assertEqual(late_fix.test_location('test_three (m.T)', last, []), ('/abs/tree/_edge/m.py', ['T', 'test_three']))

    def test_the_runners_own_account_of_one_pytest_failure(self):
        command = ['{python}', '-m', 'pytest', '-q']
        block = late_fix.failure_block(PYTEST_OUTPUT, 'pkg/tests/test_m.py::test_cases[a-1]', command)
        self.assertTrue(block.startswith('___') and block.endswith('pkg/tests/test_m.py:9: AssertionError'))
        self.assertIn('_ _ _ _', block)  # the separator between its frames isn't another entry
        self.assertNotIn('test_method', block)
        method = late_fix.failure_block(PYTEST_OUTPUT, 'pkg/tests/test_m.py::TestK::test_method', command)
        self.assertTrue(method.endswith('pkg/tests/test_m.py:14: AssertionError'))
        self.assertNotIn('short test summary', method)
        self.assertEqual(late_fix.test_location('pkg/tests/test_m.py::TestK::test_method[x-1]', method, command),
                         ('pkg/tests/test_m.py', ['TestK', 'test_method']))

    def test_a_pytest_reproduction_carries_what_the_test_uses_and_nothing_else(self):
        (self.tmp/'t').mkdir()
        (self.tmp/'t'/'test_m.py').write_text(PYTEST_MODULE)
        code = late_fix.reproduction(self.tmp, ('t/test_m.py', ['test_target']))
        for needed in ('import itertools', 'import pytest', 'from pkg import thing', 'GRAPH = thing.Graph()',
                       'GRAPH.name = "g"', 'GRAPH.add(1, 2)', 'def make():', 'CASES = [', '@pytest.mark.parametrize',
                       'def test_target(case):'):
            self.assertIn(needed, code)
        for unused in ('import os', 'UNUSED', 'test_other'):
            self.assertNotIn(unused, code)
        self.assertLess(code.index('def make'), code.index('CASES'))  # file order
        compile(code, 'reproduction', 'exec')
        self.assertIsNone(late_fix.reproduction(self.tmp, ('t/test_m.py', ['test_missing'])))
        self.assertIsNone(late_fix.reproduction(self.tmp, ('../t/test_m.py', ['test_target'])))
        self.assertIsNone(late_fix.reproduction(self.tmp, None))

    def test_a_unittest_reproduction_keeps_the_class_fixtures_and_helpers_it_calls(self):
        (self.tmp/'test_u.py').write_text(UNITTEST_MODULE)
        code = late_fix.reproduction(self.tmp, ('test_u.py', ['T', 'test_a']))
        for needed in ('import unittest', 'import pkg', 'class T(unittest.TestCase):', 'LIMIT = 3', 'def setUp',
                       'def check', 'def inner', 'def test_a'):
            self.assertIn(needed, code)
        for unused in ('unused_helper', 'test_b'):
            self.assertNotIn(unused, code)
        compile(code, 'reproduction', 'exec')

    def test_the_report_is_host_written_with_relative_paths(self):
        tree = self.tmp/'graded'
        block = late_fix.relative(f'  File "{tree}/_edge/m.py", line 9\n  File "{late_fix.ROOT}/work/x.py"', tree)
        self.assertEqual(block, '  File "_edge/m.py", line 9\n  File "<host>/work/x.py"')
        text = late_fix.report('test_two (m.T)', block, 'def test_two(self):\n    pass\n')
        self.assertTrue(text.startswith("A test of the reporter's, `test_two`, fails against the latest commit."))
        self.assertIn('```python\ndef test_two', text)
        self.assertNotIn('```python', late_fix.report('p.py::test_x[1]', block, None))
        self.assertIn('`test_x[1]`', late_fix.report('p.py::test_x[1]', block, None))


class SyntheticMisses(unittest.TestCase):
    """The synthetic task with an edge suite, and saved fixes as a benchmark run keeps them."""
    def setUp(self):
        self.case = synthetic.BenchTaskTests('test_candidates_find_the_fix_commit')
        self.case.setUp()
        self.root = self.case.root
        self.task = dict(self.case.task, repo_path=str(self.case.repo))
        self.edge = self.root/'edge'
        (self.edge/'synthetic').mkdir(parents=True)
        (self.edge/'synthetic'/'test_edge.py').write_text(regrade_tests.EDGE)
        patch = mock.patch.object(bench, 'EDGE_ROOT', self.edge)
        patch.start()
        self.addCleanup(patch.stop)

    def tearDown(self):
        self.case.tearDown()

    def saved(self, arm, source):
        work = bench_tasks.workspace(self.task, self.case.repo, self.root/'ws'/arm)
        if source is not None:
            (work/'pkg'/'__init__.py').write_text(source)
        diff = subprocess.run(['git', 'diff', '--binary', 'HEAD'], cwd=work, capture_output=True, check=True).stdout
        out = self.root/'runs'/'bench-x'/'synthetic'/arm/'0'
        out.mkdir(parents=True)
        (out/'agent.diff').write_bytes(diff)
        record = {'task': 'synthetic', 'arm': arm, 'trial': 0, 'model': 'claude-sonnet-5', 'effort': 'low',
                  'passed': False, 'cache': {'cold_equivalent_cost_usd': .05}}
        return out, record


class PrepareTests(SyntheticMisses):
    def test_each_miss_is_regraded_and_gets_a_report_from_its_first_failing_test(self):
        found = [self.saved('partial', regrade_tests.LISTS_ONLY), self.saved('idle', None),
                 self.saved('fixed', synthetic.FIXED)]
        out = self.root/'late'
        plan = late_fix.prepare(out, found, {'synthetic': self.task}, python=sys.executable, jobs=2)
        items = {i['arm']: i for i in plan['items']}
        self.assertEqual(plan['counts'], {'not_reproduced': 1, 'ready': 2})
        self.assertEqual(plan['reference_preflight']['synthetic']['hidden_passed'], 2)
        partial, idle = items['partial'], items['idle']
        self.assertEqual((partial['reported_source'], partial['reported_test']), ('edge', 'test_string (test_edge.Edge)'))
        self.assertEqual(partial['failing_tests'], {'hidden': [], 'suite': [], 'edge': ['test_string (test_edge.Edge)']})
        self.assertEqual((idle['reported_source'], idle['reported_test']), ('hidden', 'test_many (tests.test_pkg.T)'))
        self.assertEqual(items['fixed']['status'], 'not_reproduced')
        self.assertNotIn('report', items['fixed'])
        self.assertEqual((partial['id'], partial['miss_cost_usd']), ('bench-x/synthetic/partial/0', .05))
        text = (out/partial['report']).read_text()
        self.assertIn('last("ab")', text)
        self.assertIn('AssertionError', text)
        self.assertNotIn('test_three', text)  # one test, not the suite
        self.assertNotIn(str(self.root), text)
        hidden = (out/idle['report']).read_text()
        self.assertIn('def test_many', hidden)
        self.assertNotIn('def test_one', hidden)
        for secret in (self.task['reference'], self.task['base']):
            self.assertNotIn(secret, text + hidden)
        self.assertFalse((out/'scratch').exists())
        with self.assertRaises(FileExistsError):  # never into an existing directory
            late_fix.prepare(out, found, {'synthetic': self.task}, python=sys.executable)


class TrialTests(SyntheticMisses):
    """LateFixTrial with the client replaced by a stub that edits the checkout and sends one request."""
    def setUp(self):
        super().setUp()
        self.upstream = fixture_server()
        thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)
        self.seen = []

    def stub(self, fix):
        def run_client(command, env, cwd, timeout, **_):
            log = subprocess.run(['git', 'log', '--format=%s'], cwd=cwd, capture_output=True, text=True).stdout
            self.seen.append({'prompt': command[command.index('-p') + 1], 'log': log.splitlines(),
                              'source': (Path(cwd)/'pkg'/'__init__.py').read_text()})
            if fix is not None:
                (Path(cwd)/'pkg'/'__init__.py').write_text(fix)
            host, port = env['ANTHROPIC_BASE_URL'].rsplit('/', 1)[1].split(':')
            conn = http.client.HTTPConnection(host, int(port), timeout=5)
            conn.request('POST', '/v1/messages', json.dumps({'model': 'claude-sonnet-5', 'max_tokens': 8, 'messages': []}),
                         {'Content-Type': 'application/json'})
            conn.getresponse().read()
            conn.close()
            final = {'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 1}
            return {'status': 'completed', 'returncode': 0, 'stdout': json.dumps(final) + '\n', 'stderr': ''}
        return mock.patch.object(bench, 'run_client', run_client)

    def late(self, name, fix):
        trial_dir, record = self.saved(name, regrade_tests.LISTS_ONLY)
        item = dict(record, id=f'bench-x/synthetic/{name}/0', trial_dir=str(trial_dir),
                    reported_test='test_string (test_edge.Edge)', reported_source='edge', report_sha256='r')
        trial = late_fix.LateFixTrial(item, 'REPORT TEXT', self.task, 'sonnet-5', self.root/'repairs'/name,
                                      '/fake/claude', 'k', f'http://127.0.0.1:{self.upstream.server_port}', RATES,
                                      python=sys.executable, expected_hidden_passed=2)
        with self.stub(fix):
            self.assertFalse(trial.step())
        trial.close()
        return trial.record

    def test_the_session_starts_at_the_shipped_miss_and_its_repair_is_graded_strictly(self):
        repaired = self.late('repaired', synthetic.FIXED)
        seen = self.seen[0]
        self.assertEqual(seen['prompt'], late_fix.PREAMBLE + 'REPORT TEXT')
        self.assertEqual(seen['log'], [self.task['instruction'], 'Task base'])  # the miss shipped on the base
        self.assertEqual(seen['source'], regrade_tests.LISTS_ONLY)
        self.assertTrue(repaired['passed'])
        late = repaired['late_fix']
        self.assertEqual((late['reported_test_fixed'], late['repair_scope']['source_lines']), (True, 2))
        self.assertRegex(late['shipped_commit'], r'^[0-9a-f]{40}$')
        diff = (self.root/'repairs'/'repaired'/'agent.diff').read_text()
        self.assertIn('-    return items[len(items) - 1]', diff)  # the repair alone, against the shipped commit
        saved = json.loads((self.root/'repairs'/'repaired'/'trial.json').read_text())
        self.assertEqual(saved['late_fix']['reported_test_fixed'], True)
        idle = self.late('idle', None)
        self.assertFalse(idle['passed'])
        self.assertEqual((idle['late_fix']['reported_test_fixed'], idle['late_fix']['repair_scope']['source_lines']),
                         (False, 0))

    def test_the_reported_test_reading(self):
        edge = {'reported_test': 'test_x (m.T)', 'reported_source': 'edge'}
        graded = {'grade': {'reason': 'passed', 'failing_tests': []}}
        self.assertTrue(late_fix.reported_fixed(dict(graded, edge={'tests_run': 2, 'failing_tests': []}), edge))
        self.assertFalse(late_fix.reported_fixed(dict(graded, edge={'tests_run': 2, 'failing_tests': ['test_x (m.T)']}), edge))
        self.assertIsNone(late_fix.reported_fixed(dict(graded, edge={'tests_run': 0, 'failing_tests': []}), edge))
        self.assertIsNone(late_fix.reported_fixed({'grade': {'reason': 'grader_timeout'}}, edge))
        hidden = {'reported_test': 'p/test_m.py::test_x[1]', 'reported_source': 'hidden'}
        failed = lambda *ids: {'grade': {'reason': 'hidden_tests_failed', 'failing_tests': list(ids)}}
        self.assertTrue(late_fix.reported_fixed(failed('p/test_m.py::test_y'), hidden))
        self.assertFalse(late_fix.reported_fixed(failed('p/test_m.py::test_x[1]'), hidden))
        self.assertFalse(late_fix.reported_fixed(failed('p/test_m.py'), hidden))  # its module didn't import
        self.assertIsNone(late_fix.reported_fixed({'grade': {'reason': 'hidden_tests_not_run', 'failing_tests': []}}, hidden))


class FakeTrial:
    """Stands in for LateFixTrial: one session at a fixed cost."""
    def __init__(self, item, cost=.1, repaired=True, rate_limited=False):
        self.item, self.cost, self.repaired, self.limited = item, cost, repaired, rate_limited
        self.finished = self.account_error = self.rate_limited = False
        self.record, self.closed = {}, False

    def step(self):
        self.finished, self.rate_limited = True, self.limited
        self.record = {'task': self.item['task'], 'passed': self.repaired, 'complete': not self.limited,
                       'wall_seconds': 10.0, 'client': {'stop': 'success'}, 'accounting': {'requests': 3},
                       'cache': {'cold_equivalent_cost_usd': self.cost}, 'grade': {'reason': 'passed'},
                       'late_fix': {'id': self.item['id'], 'reported_test': 't', 'reported_test_fixed': self.repaired}}
        return False

    def known_cost(self):
        return self.cost if self.finished else 0

    def close(self):
        self.closed = True


class RunTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name)/'late'
        self.out.mkdir()
        task = bench_tasks.load('parse-decimal-grouping')[0]
        self.tasks = {task['id']: task}
        items = [{'id': f'bench-x/{task["id"]}/arm/{n}', 'task': task['id'], 'status': 'ready',
                  'spec_sha256': bench_tasks.spec_hash(task)} for n in range(4)]
        items.append({'id': 'bench-x/other/arm/0', 'task': task['id'], 'status': 'not_reproduced'})
        (self.out/'plan.json').write_text(json.dumps({'items': items, 'reference_preflight': {}}))

    def run_with(self, make, **options):
        made = []

        def factory(item):
            made.append(make(item))
            return made[-1]
        summary = late_fix.run(self.out, '/fake/claude', None, 'http://unused', RATES, tasks=self.tasks,
                               trial_factory=factory, **options)
        return summary, made

    def test_every_ready_miss_runs_once_in_a_seeded_order_and_is_summarized(self):
        summary, made = self.run_with(FakeTrial, seed=3)
        manifest = json.loads((self.out/'repairs'/'manifest.json').read_text())
        self.assertEqual([t.item['id'] for t in made], manifest['order'])
        self.assertEqual(len(made), 4)
        self.assertNotEqual(manifest['order'], sorted(manifest['order']))  # seed 3 shuffles four items
        self.assertTrue(all(t.closed for t in made))
        self.assertEqual((summary['all']['complete'], summary['all']['repaired'], summary['all']['cost_usd']['total']),
                         (4, 4, .4))
        self.assertTrue(summary['complete'])
        self.assertEqual(json.loads((self.out/'summary.json').read_text())['all'], summary['all'])
        with self.assertRaises(FileExistsError):  # once per preparation
            self.run_with(FakeTrial)

    def test_the_limit_and_a_rate_limit_stop_the_run(self):
        summary, made = self.run_with(lambda item: FakeTrial(item, cost=.3), limit_usd=.5)
        self.assertEqual((len(made), summary['stopped'], len(summary['not_run'])), (2, 'limit', 2))
        self.assertFalse(summary['complete'])
        shutil.rmtree(self.out/'repairs')
        (self.out/'summary.json').unlink()
        summary, made = self.run_with(lambda item: FakeTrial(item, rate_limited=True))
        self.assertEqual((len(made), summary['stopped']), (1, 'rate_limited'))
        self.assertEqual(summary['all']['complete'], 0)  # a rate-limited session is no result

    def test_a_session_whose_effort_or_prompt_missed_the_wire_is_left_out(self):
        def unapplied(item):
            trial = FakeTrial(item)
            step = trial.step
            trial.step = lambda: (step(), trial.record.update(effort_check={'applied': False}))[0]
            return trial
        summary, made = self.run_with(unapplied)
        self.assertEqual((summary['all']['sessions'], summary['all']['complete']), (4, 0))
        self.assertEqual({r['ineligible_reason'] for r in summary['rows']}, {'effort_not_applied'})

    def test_a_task_spec_changed_since_preparing_stops_before_any_session(self):
        plan = json.loads((self.out/'plan.json').read_text())
        plan['items'][0]['spec_sha256'] = 'old'
        (self.out/'plan.json').write_text(json.dumps(plan))
        with self.assertRaises(ValueError):
            self.run_with(FakeTrial)
        self.assertFalse((self.out/'repairs').exists())


@unittest.skipUnless(shutil.which('claude'), 'Claude Code CLI not installed')
class OfflineLateFixTests(SyntheticMisses):
    """Real client, fake key, scripted loopback upstream, synthetic repository: $0, no provider traffic."""
    def test_a_real_client_gets_the_report_and_repairs_the_shipped_miss(self):
        upstream = fixture_server()
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            cli, version = bench.resolve_client(shutil.which('claude'))
            trial_dir, record = self.saved('partial', regrade_tests.LISTS_ONLY)
            found = late_fix.prepare(self.root/'late', [(trial_dir, record)], {'synthetic': self.task},
                                     python=sys.executable)['items'][0]
            text = (self.root/'late'/found['report']).read_text()
            work = self.root/'late'/'repairs'/found['id']/'workspace'
            upstream.keep_bodies = True
            upstream.script = [{'tool': 'Read', 'input': {'file_path': str(work/'pkg'/'__init__.py')}},
                               {'tool': 'Write', 'input': {'file_path': str(work/'pkg'/'__init__.py'),
                                                           'content': synthetic.FIXED}},
                               {'text': 'Fixed.'}]
            trial = late_fix.LateFixTrial(found, text, self.task, 'sonnet-5', self.root/'late'/'repairs'/found['id'],
                                          cli, 'sk-ant-offline-not-a-key', f'http://127.0.0.1:{upstream.server_port}',
                                          RATES, python=sys.executable, max_turns=8, client_version=version,
                                          expected_hidden_passed=2)
            try:
                trial.step()
            finally:
                trial.close()
        finally:
            upstream.shutdown()
            upstream.server_close()
            thread.join()
        record = trial.record
        self.assertEqual(record['sessions'][0]['stop'], 'success')
        self.assertTrue(record['passed'], record['grade'])
        self.assertTrue(record['late_fix']['reported_test_fixed'])
        self.assertTrue(record['accounting']['tokens_match'], record['accounting'])
        first = next(json.loads(b) for b in upstream.bodies if json.loads(b).get('tools'))
        sent = ''.join(b.get('text', '') for m in first['messages'] for b in m['content'] if isinstance(b, dict))
        self.assertIn(late_fix.PREAMBLE + text, sent)  # the report reached the model whole


if __name__ == '__main__':
    unittest.main()
