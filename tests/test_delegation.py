"""Consults and handoff notes: the brief, the side requests, delivery that keeps the cache, and their pricing. $0."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from modelpilot import delegation, switch_policy
from modelpilot.active_policy import normalized
from modelpilot.governed_session import OWNER
from modelpilot.governor import Governor
from tests.test_policy_session import O, RATES, S

# Claude Code 2.1.284's tool loop (tests/fixtures/claude-2.1.284-shape.json): the prompt, its system note carrying the
# effort, a tool call and its result, and a trailing system note that holds the cache breakpoint.
LOOP = [{'role': 'user', 'content': [{'type': 'text', 'text': 'fix it'}]},
        {'role': 'system', 'content': 'note', 'output_config': {'effort': 'medium'}},
        {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 't1', 'name': 'Bash', 'input': {}}]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'ok'}]},
        {'role': 'system', 'content': [{'type': 'text', 'text': 'reminder', 'cache_control': {'type': 'ephemeral'}}]}]
NEXT = LOOP[:4] + [{'role': 'system', 'content': 'reminder'},  # resent as a plain string, as 2.1.284 does
                   {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 't2', 'name': 'Bash', 'input': {}}]},
                   {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't2', 'content': 'ok'}]},
                   {'role': 'system', 'content': [{'type': 'text', 'text': 'reminder',
                                                   'cache_control': {'type': 'ephemeral'}}]}]


# An edit and a test run in one Bash call, as the probe's agents wrote them.
EDIT_AND_TEST = ("cat >> tests/test_x.py <<'EOF'\nimport pytest\n\n\n@pytest.mark.parametrize('n', [1, 2])\ndef test_n(n):\n"
                 "    assert n\nEOF\npython3 -m pytest -q tests 2>&1 | tail -3")


def bash(id, command, output, error=False):
    """A Bash call and the result the client returned for it."""
    result = {'type': 'tool_result', 'tool_use_id': id, 'content': output}
    return [{'role': 'assistant', 'content': [{'type': 'tool_use', 'id': id, 'name': 'Bash', 'input': {'command': command}}]},
            {'role': 'user', 'content': [dict(result, is_error=True) if error else result]}]


def delegating(consult=True, note=True, **consult_settings):
    cfg = switch_policy.load()
    cfg['calibration']['enabled'] = False
    return switch_policy.with_overrides(cfg, {'delegation': {'consult': dict({'enabled': consult}, **consult_settings),
                                                             'handoff_note': {'enabled': note}}})


class DeliveryTests(unittest.TestCase):
    def test_a_delivery_joins_claude_codes_trailing_note_and_stays_on_it(self):
        where = delegation.frontier(LOOP)
        self.assertEqual(where, ('merge', 4))
        first = delegation.apply({'messages': LOOP}, [(where, 'advice')])['messages']
        self.assertEqual([b['text'] for b in first[4]['content']], ['reminder', 'advice'])
        # The client's breakpoint moves to the end of the message, so the delivery is cached with it.
        self.assertEqual(['cache_control' in b for b in first[4]['content']], [False, True])
        later = delegation.apply({'messages': NEXT}, [(where, 'advice')])['messages']
        # From the API's side the history only grows: the earlier request is a prefix of the later one.
        self.assertEqual([normalized(m) for m in later[:len(first)]], [normalized(m) for m in first])
        self.assertEqual(len(later), len(NEXT))

    def test_after_a_user_message_a_delivery_is_a_new_system_message(self):
        messages = LOOP[:4]
        where = delegation.frontier(messages)
        self.assertEqual(where, ('insert', 4))
        out = delegation.apply({'messages': messages}, [(where, 'advice')])['messages']
        self.assertEqual(out[4], {'role': 'system', 'content': 'advice'})
        later = delegation.apply({'messages': messages + NEXT[5:7]}, [(where, 'advice')])['messages']
        self.assertEqual([m['role'] for m in later[3:]], ['user', 'system', 'assistant', 'user'])  # then the reply

    def test_nowhere_to_deliver_after_an_assistant_turn(self):
        self.assertIsNone(delegation.frontier(LOOP[:3]))
        self.assertIsNone(delegation.deliver({'messages': LOOP[:3]}, None, 'advice'))

    def test_a_history_that_changed_under_a_delivery_is_refused(self):
        self.assertIsNone(delegation.apply({'messages': LOOP[:4]}, [(('merge', 3), 'advice')]))  # a tool result now
        self.assertIsNone(delegation.apply({'messages': LOOP[:2]}, [(('insert', 4), 'advice')]))  # past the end

    def test_effort_messages_and_deliveries_share_the_clients_indexes(self):
        effort = {'role': 'system', 'content': [], 'output_config': {'effort': 'high'}}
        out = delegation.apply({'messages': LOOP}, [(('merge', 4), 'advice')], [(2, effort)])['messages']
        self.assertEqual([m['role'] for m in out], ['user', 'system', 'system', 'assistant', 'user', 'system'])
        self.assertEqual(out[2], effort)
        self.assertEqual(out[5]['content'][-1]['text'], 'advice')


class RequestTests(unittest.TestCase):
    def test_the_note_request_reads_the_cached_prefix_and_asks_for_text(self):
        request = {'model': S, 'stream': True, 'max_tokens': 128000, 'tools': [{'name': 'Bash'}],
                   'output_config': {'effort': 'medium'}, 'thinking': {'type': 'adaptive'}, 'messages': LOOP}
        spec = switch_policy.load()['delegation']['handoff_note']
        body = delegation.note_request(request, spec)
        self.assertEqual((body['stream'], body['max_tokens']), (False, spec['max_tokens']))
        self.assertEqual((body['tools'], body['model'], body['thinking']), (request['tools'], S, request['thinking']))
        self.assertNotIn('tool_choice', body)  # a tool_choice change would rewrite the messages cache
        self.assertEqual([normalized(m) for m in body['messages'][:4]], [normalized(m) for m in LOOP[:4]])
        self.assertIn('Do not call any tool', body['messages'][4]['content'][-1]['text'])
        self.assertNotIn('reasoning process', body['messages'][4]['content'][-1]['text'].replace('not your reasoning process', ''))
        self.assertEqual(request['messages'], LOOP)  # the client's request is untouched

    def test_the_consult_is_one_message_at_its_own_setting(self):
        body = delegation.consult_request('brief', (O, 'high'), switch_policy.load()['delegation']['consult'])
        self.assertEqual((body['model'], body['output_config'], body['stream'], body['messages']),
                         (O, {'effort': 'high'}, False, [{'role': 'user', 'content': 'brief'}]))
        self.assertNotIn('tools', body)

    def test_only_visible_text_is_advice(self):
        self.assertEqual(delegation.reply_text({'content': [{'type': 'thinking', 'thinking': ''},
                                                            {'type': 'text', 'text': ' Look at x. '}]}, 100), 'Look at x.')
        self.assertIsNone(delegation.reply_text({'stop_reason': 'refusal', 'content': [{'type': 'text', 'text': 'no'}]}, 100))
        self.assertIsNone(delegation.reply_text({'content': [{'type': 'tool_use', 'name': 'Bash', 'input': {}}]}, 100))
        self.assertLessEqual(len(delegation.reply_text({'content': [{'type': 'text', 'text': 'x' * 500}]}, 100).encode()), 100)


class BriefTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.work = root/'workspace'
        self.work.mkdir()
        git = lambda *a: subprocess.run(['git', *a], cwd=self.work, check=True, capture_output=True)
        git('init', '-q')
        (self.work/'mod.py').write_text('def f(x):\n    return x\n')
        git('add', '.')
        git('-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm', 'base')
        self.base = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=self.work, capture_output=True, text=True).stdout.strip()
        (self.work/'mod.py').write_text('def f(x):\n    return x + 1\n')
        (self.work/'test_new.py').write_text('def test_f():\n    pass\n')
        self.gov = Governor(root/'ledger.sqlite3', 's', 1)
        self.task = self.gov.state.create('t', 'Make f add one.')['id']
        self.gov.state.claim(self.task, 1, OWNER, seconds=3600)

    def tearDown(self):
        self.gov.close()
        self.tmp.cleanup()

    def test_the_brief_holds_host_facts_only(self):
        handle = self.gov.state.store_output('collected 3 items\n' + 'line\n' * 2000 + '3 passed')['handle']
        self.gov.note('bench_tool', {'tool': 'run_tests', 'exit_code': 0, 'tests_run': 3, 'tests_passed': 3,
                                     'failing_tests': [], 'handle': handle}, self.task)
        spec = switch_policy.load()['delegation']['consult']
        text = delegation.brief(self.gov, self.task, delegation.WHY['tests_pass'], (S, 'medium'),
                                {'requests': 4, 'spent_usd': .05, 'forecast_usd': .09}, self.work, self.base, spec)
        self.assertIn('Make f add one.', text)
        self.assertIn('3 of 3 tests passed', text)
        self.assertIn('3 passed', text)  # the output's tail
        self.assertIn('+    return x + 1', text)
        self.assertIn('--- new file: test_new.py', text)
        self.assertIn('every failure measured so far ended with the agent reporting success', text)
        self.assertLessEqual(len(text.encode()), spec['brief_max_bytes'])
        self.assertLess(text.count('line\n'), 2000)  # the output is cut to test_output_bytes
        # Read-only: the workspace's index is untouched by the host's git calls.
        self.assertEqual(subprocess.run(['git', 'status', '--porcelain'], cwd=self.work, capture_output=True,
                                        text=True).stdout.split('\n')[0], ' M mod.py')

    def test_a_brief_is_cut_to_its_limit_and_says_when_tests_never_ran(self):
        (self.work/'big.py').write_text('x = 1\n' * 20000)
        spec = dict(switch_policy.load()['delegation']['consult'], brief_max_bytes=3000)
        text = delegation.brief(self.gov, self.task, 'why', (S, 'medium'), None, self.work, self.base, spec)
        self.assertLessEqual(len(text.encode()), 3000)
        self.assertIn('has not run the host test tool', text)
        self.assertIn("None of the agent's Bash calls ran a test runner.", text)
        self.assertIn('cut by the host', text)

    def test_the_brief_carries_the_agents_own_test_runs(self):
        """bench-20261005-153506: every agent tested through Bash before its finish, and every brief said no tests ran."""
        spec = switch_policy.load()['delegation']['consult']
        messages = LOOP[:1] + bash('b1', EDIT_AND_TEST, '1 failed, 18 passed in 0.61s') + bash(
            'b2', 'python3 -W error -m pytest -q tests 2>&1 | tail -4', '....\n20 passed in 0.61s')
        text = delegation.brief(self.gov, self.task, delegation.WHY['agent_finish'], (S, 'medium'), None, self.work,
                                self.base, spec, messages)
        self.assertIn('has not run the host test tool', text)  # run_tests is still its own section
        own = text[text.index("## The agent's own test runs (Bash)"):text.index('## Current changes')]
        self.assertIn('2 Bash calls ran tests, oldest first', own)
        self.assertIn('$ python3 -m pytest -q tests 2>&1 | tail -3\n  -> 1 failed, 18 passed in 0.61s', own)
        self.assertIn('Newest:\n$ python3 -W error -m pytest -q tests 2>&1 | tail -4\noutput (tail):\n....\n20 passed', own)
        self.assertNotIn('parametrize', own)  # the here-document's body is the edit, which the diff shows

    def test_only_calls_that_run_a_test_runner_count(self):
        messages = (bash('g', 'grep -rn pytest pyproject.toml; pip install pytest', 'pytest>=7') +
                    bash('h', "cat > tests/test_x.py <<'EOF'\nimport pytest\n\n\ndef test_x():\n    pytest.skip()\nEOF", '') +
                    [{'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'r', 'name': 'Read',
                                                        'input': {'file_path': 'pytest.ini'}}]}])
        self.assertIsNone(delegation.agent_test_runs(messages, 4000))
        for line in ('python3 -m pytest -q', 'cd .. && PYTHONPATH=src pytest tests', 'timeout 300 python -m pytest x',
                     'uv run pytest', '/opt/bin/python3.12 -m unittest discover -s tests', 'tox -e py312', 'make test'):
            self.assertTrue(delegation.runs_tests(line), line)
        for line in ('grep -n pytest setup.cfg', 'python3 -c "import pytest"', 'cat tox.ini', 'make', 'echo unittest'):
            self.assertFalse(delegation.runs_tests(line), line)
        # A here-string isn't a here-document: the lines after it are still commands.
        self.assertEqual(delegation.shell_lines('cat <<<EOF\npytest -q'), ['cat <<<EOF', 'pytest -q'])

    def test_each_runs_outcome_is_read_from_its_summary(self):
        """Plan item 2: steps read these. A run without a summary, or that ran no test, gives no outcome."""
        cases = {'1 failed, 18 passed in 0.61s': 1, '....\n97 passed, 1 skipped in 0.51s': 0, '2 errors in 0.2s': 2,
                 '\x1b[31m3 failed\x1b[0m, 2 passed in 1.0s': 3,
                 'Ran 5 tests in 0.004s\n\nFAILED (failures=1, errors=1)': 2, 'Ran 5 tests in 0.004s\n\nOK': 0,
                 'Ran 5 tests in 0.004s\n\nOK (skipped=1)': 0,
                 'ERROR: usage: pytest [options]\n  rootdir: /w': None, '5 deselected in 0.10s': None,
                 'Ran 0 tests in 0.000s\n\nOK': None, 'Ran 5 tests in 0.004s': None, '': None}
        for output, expected in cases.items():
            self.assertEqual(delegation.failures(output), expected, output)
        messages = (bash('a', 'python3 -m pytest -q', '1 failed, 4 passed in 0.1s') +
                    bash('g', 'grep -rn pytest setup.cfg', '5 passed in 0.1s') +  # not a test run
                    bash('u', 'python3 -m pytest -q', 'ERROR: file or directory not found') +  # no outcome
                    bash('b', 'cd src && python -m unittest discover', 'Ran 3 tests in 0.01s\n\nOK'))
        self.assertEqual(delegation.agent_test_results(messages), [{'id': 'a', 'failures': 1}, {'id': 'b', 'failures': 0}])

    def test_many_runs_keep_the_newest_and_drop_the_oldest(self):
        messages = []
        for i in range(40):
            messages += bash(f'b{i}', f'python3 -m pytest -q tests/test_{i}.py', f'{i} failed, 1 passed', error=True)
        messages += bash('last', 'python3 -m pytest -q', [{'type': 'text', 'text': 'E' * 9000 + '\n3 failed, 9 passed'}])
        text = delegation.agent_test_runs(messages, 2000)
        self.assertLessEqual(len(text.encode()), 2000)
        self.assertIn('41 Bash calls ran tests, oldest first (', text)
        self.assertIn('earlier not shown', text)
        self.assertNotIn('test_0.py', text)
        self.assertIn('$ python3 -m pytest -q tests/test_39.py\n  -> 39 failed, 1 passed (the client reported the call as '
                      'failed)', text)
        self.assertTrue(text.endswith('\n3 failed, 9 passed'))  # the newest output's tail, from list content


class PricingTests(unittest.TestCase):
    """The gate weighs a consult against staying and switching (switch_policy.decide)."""
    def advice(self, model, effort, model_p=.9, effort_p=.85):
        models, efforts = ['claude-haiku-4-5-20251001', S, O], ['low', 'medium', 'high', 'xhigh', 'max']
        spread = lambda labels, top, p: {x: p if x == top else (1 - p) / (len(labels) - 1) for x in labels}
        return {'model': {'choice': model, 'confidence': model_p, 'probabilities': spread(models, model, model_p)},
                'effort': {'choice': effort, 'confidence': effort_p, 'probabilities': spread(efforts, effort, effort_p)}}

    def profile(self, cfg, tokens, **extra):
        prof = switch_policy.profile(cfg, {'messages': [{'role': 'user', 'content': 'x' * int(tokens * 2.8)}]}, True)
        prof.update(history=True, **extra)
        return prof

    def test_delegation_is_off_in_the_shipped_config(self):
        cfg = switch_policy.load()
        self.assertFalse(cfg['delegation']['consult']['enabled'])
        self.assertFalse(cfg['delegation']['handoff_note']['enabled'])
        prof = self.profile(cfg, 40000)
        decision = switch_policy.decide(cfg, RATES, self.advice(O, 'high'), (S, 'medium'), prof, 'stuck_evidence')
        self.assertFalse(any(c['setting'].startswith('consult:') for c in decision['candidates']))

    def test_deep_into_a_task_a_consult_beats_a_switch(self):
        cfg = delegating(note=False)
        prof = self.profile(cfg, 60000)
        decision = switch_policy.decide(cfg, RATES, self.advice(O, 'high'), (S, 'medium'), prof, 'stuck_evidence')
        consult = next(c for c in decision['candidates'] if c['setting'].startswith('consult:'))
        switch = next(c for c in decision['candidates'] if c['setting'] == f'{O}/medium')
        self.assertEqual(consult['setting'], f'consult:{O}/high')  # Jev's effort: a separate request runs at any
        self.assertGreater(switch['switch_usd'], .25)  # 60K tokens rewritten at Opus prices
        self.assertLess(consult['consult_usd'], .1)
        self.assertEqual((decision['action'], decision['consult'], decision['target']), ('consult', [O, 'high'], [S, 'medium']))

    def test_early_a_switch_still_beats_a_consult(self):
        cfg = delegating(note=False)
        decision = switch_policy.decide(cfg, RATES, self.advice(O, 'high'), (S, 'medium'), self.profile(cfg, 3000),
                                        'stuck_evidence')
        self.assertEqual((decision['action'], decision['target']), ('jump', [O, 'medium']))

    def test_calibrated_a_consult_at_a_step_doesnt_pay(self):
        cfg = switch_policy.with_overrides(switch_policy.load(), {'delegation': {'consult': {'enabled': True}}})
        prof = self.profile(cfg, 30000)
        decision = switch_policy.decide(cfg, RATES, self.advice(O, 'high', .6), (S, 'medium'), prof, 'step')
        consult = next(c for c in decision['candidates'] if c['setting'].startswith('consult:'))
        stay = decision['candidates'][0]
        self.assertLess(consult['p_ok'] - stay['p_ok'], .05)
        self.assertEqual(decision['action'], 'stay')

    def test_a_forced_consult_asks_the_strongest_model_when_jev_says_stay(self):
        cfg = delegating(force=['tests_pass'])
        decision = switch_policy.decide(cfg, RATES, self.advice(S, 'medium'), (S, 'medium'),
                                        self.profile(cfg, 20000, force_consult=True), 'step')
        self.assertEqual((decision['action'], decision['reason'], decision['consult']), ('consult', 'forced', [O, 'medium']))

    def test_consults_stop_at_the_per_revision_cap(self):
        cfg = delegating(note=False)
        decision = switch_policy.decide(cfg, RATES, self.advice(O, 'high'), (S, 'medium'),
                                        self.profile(cfg, 60000, consults_left=0), 'stuck_evidence')
        self.assertFalse(any(c['setting'].startswith('consult:') for c in decision['candidates']))
        self.assertEqual(decision['action'], 'jump')

    def test_a_handoff_note_is_priced_on_model_moves_with_earlier_work_only(self):
        cfg = delegating(consult=False)
        prof = self.profile(cfg, 20000)
        decision = switch_policy.decide(cfg, RATES, self.advice(O, 'medium'), (S, 'medium'), prof, 'stuck_evidence')
        opus = next(c for c in decision['candidates'] if c['setting'] == f'{O}/medium')
        self.assertGreater(opus['note_usd'], 0)
        self.assertAlmostEqual(opus['switch_usd'] - opus['note_usd'],
                               switch_policy.switch_cost(cfg, RATES, (S, 'medium'), (O, 'medium'), prof, reuse=True))
        self.assertEqual(switch_policy.note_cost(cfg, RATES, (S, 'medium'), (S, 'high'), prof), 0)  # same model
        self.assertEqual(switch_policy.note_cost(cfg, RATES, (S, 'medium'), (O, 'medium'), dict(prof, history=False)), 0)

    def test_overrides_are_checked(self):
        cfg = switch_policy.load()
        with self.assertRaises(ValueError):
            switch_policy.with_overrides(cfg, {'delegation': {'consult': {'enabled': True, 'force': ['always']}}})
        with self.assertRaises(ValueError):
            switch_policy.with_overrides(cfg, {'delegation': {'consult': {'enabeld': True}}})
        self.assertFalse(cfg['delegation']['consult']['enabled'])  # the loaded config is never changed


if __name__ == '__main__':
    unittest.main()
