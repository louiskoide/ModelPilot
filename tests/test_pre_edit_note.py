"""Option C's pre-edit note probe: the cut, the rendered transcript, the request, the live write with a scripted
transport, and the pre-registered reading. $0: nothing is sent."""
import json
from pathlib import Path
import tempfile
import unittest
from modelpilot import bench, bench_tasks, pre_edit_note as pen

TASK = bench_tasks.load('tomli-decode-error-attrs')[0]
SUITE_REPLY = '```python\nimport unittest\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        pass\n```\n'


def reply(text, model=pen.MODEL):
    return {'model': model, 'stop_reason': 'end_turn',
            'usage': {'input_tokens': 4000, 'output_tokens': 2000, 'cache_creation_input_tokens': 0,
                      'cache_read_input_tokens': 0},
            'content': [{'type': 'thinking', 'thinking': ''}, {'type': 'text', 'text': text}]}


def assistant(mid, *blocks, model=pen.AGENT_MODEL):
    return [{'type': 'assistant', 'message': {'id': mid, 'model': model, 'content': [block]}} for block in blocks]


def tool(tid, name, **data):
    return {'type': 'tool_use', 'id': tid, 'name': name, 'input': data}


def result(tid, text):
    return {'type': 'user', 'message': {'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': tid, 'content': text}]}}


def trial(directory, events):
    directory.mkdir(parents=True)
    (directory/'client.stdout.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events))
    return directory


EVENTS = (assistant('m1', {'type': 'text', 'text': 'Looking around.'},
                    tool('t1', 'Bash', command='grep -n TOMLDecodeError src/tomli/_parser.py 2>/dev/null'))
          + [result('t1', '53:class TOMLDecodeError(ValueError):')]
          + assistant('m2', tool('t2', 'Read', file_path='src/tomli/_parser.py'))
          + [result('t2', 'x' * (pen.RESULT_CHARS + 10))]
          + assistant('m3', {'type': 'text', 'text': 'Now the change.'},
                      tool('t3', 'Bash', command="python3 - <<'E'\np='a.py'\ns=open(p).read()\nopen(p,'w').write(s)\nE"))
          + [result('t3', 'SECRET AFTER THE CUT')])


class PreEditNoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runs = self.root/'runs'
        self.rates = bench.rates()

    def tearDown(self):
        self.tmp.cleanup()

    def item(self, key, task=TASK['id'], strict_pass=False, events=EVENTS):
        trial(self.runs/'bench-x'/task/'sonnet-5.5'/key, events)
        return {'id': f'bench-x__{task}__sonnet-5.5__{key}', 'run': 'bench-x', 'task': task, 'arm': 'sonnet-5.5',
                'trial': key, 'strict_pass': strict_pass, 'cut_at': 2, 'cut_by': 'Bash'}

    def test_the_cut_is_the_first_edit_or_bash_write_and_reads_are_not_writes(self):
        messages, _ = pen.conversation(trial(self.root/'t', EVENTS))
        self.assertEqual(pen.cut(messages), (2, 'Bash'))
        edit = assistant('e1', tool('a', 'Edit', file_path='x.py')) + assistant('e2', tool('b', 'Bash', command='sed -i s/a/b/ x.py'))
        self.assertEqual(pen.cut(pen.conversation(trial(self.root/'e', edit))[0]), (0, 'Edit'))
        reads = assistant('r1', tool('a', 'Bash', command='cat x.py | head -5; ls tests 2>&1; grep -c a x.py > /dev/null'))
        self.assertEqual(pen.cut(pen.conversation(trial(self.root/'r', reads))[0]), (None, None))
        for command in ('cat > notes.txt <<E', 'tee out.py', 'git apply fix.diff', 'rm x.py'):
            self.assertTrue(pen.bash_write(command), command)

    def test_the_transcript_stops_before_the_cut_and_long_results_are_marked(self):
        messages, results = pen.conversation(trial(self.root/'t', EVENTS))
        text, cuts = pen.render(messages, results, 2)
        self.assertIn('[agent]\nLooking around.', text)
        self.assertIn('53:class TOMLDecodeError', text)
        self.assertIn('[... 10 more characters]', text)
        self.assertEqual(cuts, 1)
        for later in ('Now the change.', "open(p,'w')", 'SECRET AFTER THE CUT'):
            self.assertNotIn(later, text)

    def test_the_request_holds_the_task_and_transcript_and_nothing_the_grader_has(self):
        payload = pen.request(TASK, 'TRANSCRIPT')
        self.assertEqual((payload['model'], payload['output_config']), ('claude-opus-5-5', {'effort': 'medium'}))
        text = json.dumps(payload)
        self.assertNotIn('cache_control', text)
        self.assertNotIn('thinking', payload)  # Opus 5.5 always thinks; disabling it is a 400
        self.assertIn(TASK['instruction'][:80], text)
        self.assertIn('TRANSCRIPT', text)
        for secret in (TASK['reference'], TASK['base'], 'test_edge', 'hidden', 'strict'):
            self.assertNotIn(secret, text)

    def test_misses_go_first_with_the_tasks_in_turn(self):
        items = [{'id': f'{t}{n}{p}', 'task': t, 'strict_pass': p} for t in pen.TASKS for n in range(2) for p in (0, 1)]
        order = pen.send_order(items)
        self.assertEqual([i['strict_pass'] for i in order], [0]*6 + [1]*6)
        self.assertEqual([i['task'] for i in order[:3]], list(pen.TASKS))

    def test_without_live_nothing_is_sent_and_the_plan_holds_the_prompts(self):
        sent = []
        plan = pen.write([self.item('0')], self.root/'out', self.rates, transport=sent.append, runs=self.runs)
        self.assertEqual((sent, plan['results'], plan['live'], plan['notes']), ([], {}, False, 1))
        self.assertTrue((self.root/'out'/'prompts'/f"{plan['population'][0]['id']}.json").exists())
        self.assertGreater(plan['max_cost_usd'], plan['estimate_usd'])
        with self.assertRaises(FileExistsError):  # never overwrite evidence
            pen.write([], self.root/'out', self.rates, runs=self.runs)

    def test_live_saves_suites_records_errors_unretried_and_stops_at_the_limit(self):
        items = [self.item('0'), self.item('1'), self.item('2', strict_pass=True)]
        calls = []

        def transport(payload):
            calls.append(payload)
            if len(calls) == 2:
                raise TimeoutError('slow')
            return reply('Note.\n' + SUITE_REPLY), 'req_1'
        cap = pen.spec_tests.max_cost(pen.request(TASK, ''), self.rates)
        plan = pen.write(items, self.root/'out', self.rates, live=True, limit_usd=cap*2.5, jobs=1,
                         transport=transport, runs=self.runs)
        statuses = [plan['results'][i['id']]['status'] for i in items]
        self.assertEqual(statuses, ['written', 'error', 'not_sent'])
        self.assertEqual(len(calls), 2)
        self.assertTrue((self.root/'out'/'suites'/items[0]['id']/TASK['id']/pen.SUITE).exists())
        self.assertFalse(plan['cost_complete'])  # the error's cost is unknown, never $0

    def test_readings(self):
        item = {'id': 'a', 'task': TASK['id'], 'strict_pass': False}
        check = {'reference': {'tests_run': 3, 'failing_tests': [], 'exit_code': 0},
                 'base': {'tests_run': 3, 'failing_tests': ['t2'], 'exit_code': 1}}
        own = {'tests_run': 3, 'failing_tests': ['t2'], 'exit_code': 1}
        self.assertIsNone(pen.reading(item, None, None, None))
        self.assertIsNone(pen.reading(item, {'status': 'error'}, None, None))
        self.assertEqual(pen.reading(item, {'status': 'no_code_block'}, None, None)['caught'], False)
        self.assertEqual(pen.reading(item, {'status': 'written'}, check, own)['caught'], True)
        wrong = dict(check, reference={'tests_run': 3, 'failing_tests': ['t2'], 'exit_code': 1})
        read = pen.reading(item, {'status': 'written'}, wrong, own)
        self.assertEqual((read['caught'], read['misleading']), (False, True))  # only a check the reference passes counts
        self.assertIsNone(pen.reading(item, {'status': 'written'}, check, {'error': 'diff_did_not_apply'}))

    def test_the_pre_registered_rule(self):
        def population(caught):  # 4 misses and 1 pass a task; caught: misses caught per task
            items, readings = [], {}
            for task in pen.TASKS:
                for n in range(5):
                    key = f'{task}{n}'
                    items.append({'id': key, 'task': task, 'strict_pass': n == 4})
                    readings[key] = {'caught': n < caught[task], 'misleading': False}
            return items, readings
        items, readings = population(dict(zip(pen.TASKS, (3, 2, 3))))
        self.assertEqual(pen.verdict(items, readings)['verdict'], 'pass')
        items, readings = population(dict(zip(pen.TASKS, (3, 1, 3))))
        self.assertEqual(pen.verdict(items, readings)['verdict'], 'fail')  # tomli 1/4 is under its 50% bar
        items, readings = population(dict(zip(pen.TASKS, (4, 4, 4))))
        for key in list(readings)[:4]:
            readings[key]['misleading'] = True
        self.assertEqual(pen.verdict(items, readings)['verdict'], 'fail')  # 4 of 15 notes misleading, over 25%
        items, readings = population(dict(zip(pen.TASKS, (4, 4, 4))))
        for key in list(readings)[:4]:
            readings[key] = None
        self.assertEqual(pen.verdict(items, readings)['verdict'], 'void')  # 4 of 15 without a reading, over 20%


if __name__ == '__main__':
    unittest.main()
