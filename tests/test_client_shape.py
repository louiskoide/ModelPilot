import json
from pathlib import Path
import unittest
from modelpilot import thinking_probe as tp

S, O, H = 'claude-sonnet-5', 'claude-opus-5', 'claude-haiku-4-5-20251001'
ROOT = Path(__file__).resolve().parents[1]
PINNED = ROOT/'work/claude-client/node_modules/.bin/claude'


def client_bodies():
    main = {'model': S, 'max_tokens': 32000, 'stream': True, 'metadata': {'user_id': 'SECRET-user'},
            'system': [{'type': 'text', 'text': 'SECRET system'},
                       {'type': 'text', 'text': 'SECRET more', 'cache_control': {'type': 'ephemeral'}}],
            'tools': [{'name': 'Bash', 'description': 'SECRET', 'input_schema': {'type': 'object'}}],
            'thinking': {'type': 'adaptive'}, 'output_config': {'effort': 'medium'},
            'context_management': {'edits': [{'type': 'clear_thinking_20251015', 'keep': 'all'}]},
            'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'SECRET prompt'}]},
                         {'role': 'system', 'content': 'SECRET reminder'},
                         {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'toolu_SECRET', 'name': 'Bash',
                                                            'input': {'command': 'SECRET'}}]},
                         {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'toolu_SECRET',
                                                       'content': 'SECRET', 'cache_control': {'type': 'ephemeral'}}]},
                         {'role': 'system', 'content': [{'type': 'text', 'text': 'SECRET'}]}]}
    first = dict(main, messages=main['messages'][:2])
    side = {'model': H, 'max_tokens': 512, 'messages': [{'role': 'user', 'content': 'SECRET title'}]}
    return [first, side, main], ['beta-a,beta-b', None, 'beta-a,beta-b']


class SummarizeShapeTests(unittest.TestCase):
    def test_summary_keeps_structure_and_drops_text_and_ids(self):
        bodies, betas = client_bodies()
        shape = tp.summarize_shape(bodies, betas, '2.1.282 (Claude Code)')
        self.assertNotIn('SECRET', json.dumps(shape))
        self.assertEqual(shape['client_version'], '2.1.282 (Claude Code)')
        self.assertEqual(set(shape['requests']), {S})  # side requests without tools are not the main loop
        r = shape['requests'][S]
        self.assertEqual(r['thinking'], {'type': 'adaptive'})
        self.assertEqual(r['effort'], 'medium')
        self.assertEqual(r['output_config_keys'], ['effort'])
        self.assertEqual(r['context_management'], {'edits': [{'type': 'clear_thinking_20251015', 'keep': 'all'}]})
        self.assertEqual(r['anthropic_beta'], ['beta-a', 'beta-b'])
        self.assertEqual(r['tool_names'], ['Bash'])
        self.assertEqual(r['system_blocks'], [{'type': 'text'}, {'type': 'text', 'cache_control': {'type': 'ephemeral'}}])
        self.assertEqual([m['role'] for m in r['messages']], ['user', 'system', 'assistant', 'user', 'system'])
        self.assertEqual(r['messages'][3], {'role': 'user', 'blocks': ['tool_result'], 'cache_control': [0]})
        self.assertEqual(r['messages'][1]['blocks'], 'str')
        self.assertTrue(r['system_after_prompt'])
        self.assertTrue(r['system_after_tool_result'])
        self.assertIn('metadata', r['top_level_keys'])

    def test_summary_uses_the_longest_main_loop_request(self):
        bodies, betas = client_bodies()
        r = tp.summarize_shape(bodies, betas, 'v')['requests'][S]
        self.assertEqual(len(r['messages']), 5)

    def test_missing_system_messages_are_reported_false(self):
        bodies, betas = client_bodies()
        bodies[2]['messages'] = [m for m in bodies[2]['messages'] if m['role'] != 'system']
        r = tp.summarize_shape(bodies[2:], betas[2:], 'v')['requests'][S]
        self.assertFalse(r['system_after_prompt'])
        self.assertFalse(r['system_after_tool_result'])


@unittest.skipUnless(PINNED.exists(), 'pinned Claude Code client not installed in work/claude-client')
class RealClientShapeTests(unittest.TestCase):
    """Real client, fake key, owned fixture upstream: $0 and no provider traffic."""
    def test_committed_fixture_matches_a_fresh_capture(self):
        committed = json.loads(tp.SHAPE_FIXTURE.read_text())
        fresh = tp.capture_shape(PINNED)
        self.assertEqual(fresh['client_version'], committed['client_version'])
        self.assertEqual(fresh['requests'], committed['requests'])
