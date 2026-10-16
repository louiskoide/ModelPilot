"""Turn-start risk predictor: what the host gives Haiku (the base commit's source only), the requests, the live step
with scripted transports, and the pre-registered score. $0: nothing is sent."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from modelpilot import bench, risk_predictor as rp

USAGE = {'input_tokens': 9000, 'output_tokens': 1500, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0}


def reply(text):
    return {'model': 'claude-haiku-5-5', 'stop_reason': 'end_turn', 'usage': USAGE,
            'content': [{'type': 'thinking', 'thinking': ''}, {'type': 'text', 'text': text}]}


def rated(risk):
    return reply('```json\n' + json.dumps({'risk': risk, 'traps': ['a second code path'], 'reason': 'r'}) + '\n```')


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


class ToyRepo:
    """A repository whose base commit has a source file naming the task's function, an unrelated one and a test; the
    next commit (the 'reference') changes the source and adds a file, neither of which the context may show."""
    def __init__(self, root):
        self.path = Path(root)/'toy'
        self.path.mkdir()
        git(self.path, 'init', '-q')
        git(self.path, 'config', 'user.email', 't@example.invalid')
        git(self.path, 'config', 'user.name', 't')
        self.write('pkg/core.py', 'def frob_value(x):\n    return x\n')
        self.write('pkg/other.py', 'def unrelated():\n    return 1\n')
        self.write('tests/test_core.py', 'from pkg.core import frob_value\n')
        self.base = self.commit('base')
        self.write('pkg/core.py', 'def frob_value(x):\n    return REFERENCE_MARKER\n')
        self.write('pkg/new.py', 'from .core import frob_value\n')
        self.reference = self.commit('reference')
        self.task = {'id': 'toy-task', 'repo': 'toy', 'url': 'https://example.invalid/toy', 'base': self.base,
                     'reference': self.reference, 'hidden_tests': ['tests/test_hidden.py'],
                     'instruction': 'Make pkg.core.frob_value(x) double x. frob_value(2) == 4.'}

    def write(self, path, text):
        target = self.path/path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def commit(self, message):
        git(self.path, 'add', '-A')
        git(self.path, 'commit', '-q', '-m', message)
        return git(self.path, 'rev-parse', 'HEAD')


class RiskPredictorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.toy = ToyRepo(self.root)
        self.rates = bench.rates()

    def tearDown(self):
        self.tmp.cleanup()

    def test_code_names_are_the_instructions_code_not_its_prose(self):
        text = ('Give tomli.TOMLDecodeError the attributes msg and doc. Calling loads(s, parse_float=float) with '
                '`key_parts` set must raise ValueError; isCamel too. Ok if not.')
        self.assertEqual(rp.code_names(text),
                         ['TOMLDecodeError', 'isCamel', 'key_parts', 'loads', 'parse_float', 'tomli'])

    def test_the_context_is_the_base_commits_source_never_the_fix_or_tests(self):
        text, record = rp.context(self.toy.task, self.toy.path)
        self.assertIn('def frob_value(x):\n    return x', text)
        self.assertEqual([f['path'] for f in record['files']], ['pkg/core.py'])
        for unseen in ('REFERENCE_MARKER', 'pkg/new.py', 'tests/test_core.py', 'unrelated'):
            self.assertNotIn(unseen, text)
        payload = rp.request(self.toy.task, text)
        self.assertEqual((payload['model'], payload['output_config'], payload['system']),
                         ('claude-haiku-5-5', {'effort': 'low'}, rp.SYSTEM))
        sent = json.dumps(payload)
        self.assertIn(self.toy.task['instruction'], sent)
        for secret in (self.toy.reference, self.toy.base, 'test_hidden'):
            self.assertNotIn(secret, sent)

    def test_a_one_file_repository_still_gets_its_file(self):
        repo = self.root/'one'
        repo.mkdir()
        git(repo, 'init', '-q')
        (repo/'parse.py').write_text('class Parser:\n    pass\n')
        git(repo, 'add', '-A')
        git(repo, '-c', 'user.email=t@example.invalid', '-c', 'user.name=t', 'commit', '-q', '-m', 'base')
        task = dict(self.toy.task, base=git(repo, 'rev-parse', 'HEAD'), instruction='Parser() must accept x.')
        self.assertEqual([f['path'] for f in rp.context(task, repo)[1]['files']], ['parse.py'])

    def test_a_file_too_large_is_cut_to_excerpts_around_the_names_within_the_budget(self):
        filler = ''.join(f'def f{i}():\n    return {i}\n' for i in range(400))
        self.toy.write('pkg/core.py', filler + 'def frob_value(x):\n    return x\n' + filler)
        task = dict(self.toy.task, base=self.toy.commit('big'))
        text, record = rp.context(task, self.toy.path, max_bytes=3000)
        core = next(f for f in record['files'] if f['path'] == 'pkg/core.py')  # the commit also has pkg/new.py
        self.assertEqual((core['whole'], record['bytes'] <= 3000), (False, True))
        self.assertIn('def frob_value(x):', text)
        self.assertNotIn('def f100():', text)

    def test_a_risk_is_a_number_from_0_to_100_or_the_reply_is_unusable(self):
        self.assertEqual(rp.parse_risk('```json\n{"risk": 35, "traps": ["a", "b"], "reason": "x"}\n```')['risk'], 35)
        self.assertEqual(rp.parse_risk('Here: {"risk": 7.5}')['risk'], 7.5)
        for text in ('no json', '{"risk": 101}', '{"risk": -1}', '{"risk": true}', '{"risk": "40"}', '[1, 2]'):
            self.assertIsNone(rp.parse_risk(text), text)

    def test_without_live_only_the_prompts_and_plan_are_written(self):
        sent = []
        out = self.root/'plan'
        plan = rp.rate(out, [(self.toy.task, 'dev')], self.rates, transport=lambda p: sent.append(p),
                       repo_of=lambda spec: self.toy.path)
        self.assertEqual((sent, plan['results'], plan['live'], plan['samples']), ([], {}, False, 3))
        self.assertTrue((out/'prompts'/'toy-task.json').exists())
        self.assertGreater(plan['max_cost_usd'], plan['expected_cost_usd'])
        with self.assertRaises(FileExistsError):  # never into an existing directory
            rp.rate(out, [(self.toy.task, 'dev')], self.rates, repo_of=lambda spec: self.toy.path)

    def test_live_sends_each_sample_once_and_records_errors_without_retrying(self):
        calls = []

        def transport(payload):
            calls.append(payload)
            if len(calls) == 2:
                raise TimeoutError('slow')
            return (rated(40) if len(calls) == 1 else reply('no json')), f'req_{len(calls)}'
        plan = rp.rate(self.root/'live', [(self.toy.task, 'dev')], self.rates, live=True, transport=transport,
                       repo_of=lambda spec: self.toy.path)
        statuses = {k: v['status'] for k, v in plan['results'].items()}
        self.assertEqual((len(calls), statuses),
                         (3, {'toy-task#1': 'rated', 'toy-task#2': 'error', 'toy-task#3': 'unusable_reply'}))
        self.assertAlmostEqual(plan['results']['toy-task#1']['cost_usd'], (9000 * .1 + 1500 * .5) / 1e6)
        self.assertFalse(plan['cost_complete'])  # the timeout's cost is unknown
        capped = rp.rate(self.root/'capped', [(self.toy.task, 'dev')], self.rates, live=True, limit_usd=.0001,
                         transport=lambda p: self.fail('sent past the limit'), repo_of=lambda spec: self.toy.path)
        self.assertEqual({v['status'] for v in capped['results'].values()}, {'not_sent'})

    def write_plan(self, out, risks, part='dev'):
        out.mkdir()
        results = {f'{t}#{i}': ({'status': 'rated', 'risk': r} if r is not None else {'status': 'error'})
                   for t, rs in risks.items() for i, r in enumerate(rs, 1)}
        (out/'plan.json').write_text(json.dumps({'tasks': {t: {'part': part} for t in risks}, 'results': results,
                                                 'spent_usd': 0.012 * len(risks), 'cost_complete': True}))
        return out

    @staticmethod
    def label(m, y_o=None, cheap=0.07, opus=0.26, part='dev'):
        return {'part': part, 'M': m, 'Y_S': False, 'Y_O': m if y_o is None else y_o, 'trials': {},
                'mean_usd': {'cheap': cheap, 'sonnet': 0.09, 'opus': opus}}

    def test_the_top_third_is_flagged_and_a_pass_needs_p_under_005_and_half_the_misses(self):
        # 12 tasks, 3 misses all rated highest: flagged = top 4 (ties at the cut flagged), 3 of 3 caught.
        risks = {f't{i}': [90 - i, 90 - i, None] for i in range(12)}
        label = {f't{i}': self.label(i < 3) for i in range(12)}
        report = rp.score(self.write_plan(self.root/'a', risks), label)
        self.assertEqual((report['flagged'], report['caught'], report['verdict']),
                         (['t0', 't1', 't2', 't3'], 3, 'pass'))
        self.assertAlmostEqual(report['p'], rp.tail(12, 3, 4, 3), places=5)
        self.assertEqual(report['answers']['t0'], 2)  # the median of the usable answers only
        routing = report['routing_for_an_opus_user']
        self.assertEqual((routing['Y_O_left_on_low_concise'], routing['Y_O']), ([], 3))
        self.assertAlmostEqual(routing['routed_usd'], round((4 * .26 + 8 * .07) / 12 + .012, 4), places=4)
        # The same misses rated lowest: none caught, fail; a task with no usable answer is never flagged.
        risks = {f't{i}': [10 + i] for i in range(12)}
        risks['t11'] = [None]
        report = rp.score(self.write_plan(self.root/'b', risks), label)
        self.assertEqual((report['caught'], report['verdict'], report['unscored']), (0, 'fail', ['t11']))
        self.assertNotIn('t11', report['flagged'])
        # Fewer than 3 misses: inconclusive, whatever is caught.
        label2 = {f't{i}': self.label(i < 2) for i in range(12)}
        report = rp.score(self.write_plan(self.root/'c', {f't{i}': [90 - i] for i in range(12)}), label2)
        self.assertEqual(report['verdict'], 'inconclusive')

    def test_the_pre_registered_tasks_are_the_dev_and_item_8_tasks_never_the_final_split(self):
        chosen = rp.tasks()
        final = set(json.loads((rp.ROOT/'bench'/'splits.json').read_text())['final'])
        ids = {spec['id'] for spec, _ in chosen}
        self.assertEqual((len(chosen), sum(p == 'dev' for _, p in chosen), sum(p == 'test' for _, p in chosen)),
                         (66, 29, 37))
        self.assertFalse(ids & final)
        self.assertNotIn('nx-generalized-petersen', ids)


if __name__ == '__main__':
    unittest.main()
