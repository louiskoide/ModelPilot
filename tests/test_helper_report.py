"""helper_report: the plugin's helper-mode log and Claude Code's transcripts, priced offline."""
import json
from pathlib import Path
import tempfile
import unittest
from modelpilot import helper_report as hr, switch_policy as sp, switch_quote

RATES = {'claude-sonnet-5-5': {'input': 3, 'output': 15, 'write_5m': 3.75, 'write_1h': 6, 'read': 0.3},
         'claude-opus-5-5': {'input': 5, 'output': 25, 'write_5m': 6.25, 'write_1h': 10, 'read': 0.5}}


def usage(inp, write, read, out):
    return {'input_tokens': inp, 'cache_creation_input_tokens': write, 'cache_read_input_tokens': read,
            'output_tokens': out, 'cache_creation': {'ephemeral_5m_input_tokens': write, 'ephemeral_1h_input_tokens': 0}}


def dollars(model, inp, write, read, out):
    r = RATES[model]
    return (inp*r['input'] + write*r['write_5m'] + read*r['read'] + out*r['output']) / 1e6


def assistant(req, model, content, use):
    return {'type': 'assistant', 'requestId': req, 'timestamp': '2026-10-21T10:00:00.000Z',
            'message': {'id': 'msg_' + req, 'model': model, 'content': content, 'usage': use}}


def user(content, origin=None):
    return dict({'type': 'user', 'message': {'role': 'user', 'content': content}}, **({'origin': origin} if origin else {}))


def write(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(e) + '\n' for e in entries))


class HelperReportTests(unittest.TestCase):
    def test_a_helper_tree_is_priced_beside_the_rewrite_and_a_lost_transcript_stays_unknown(self):
        S, O = 'claude-sonnet-5-5', 'claude-opus-5-5'
        with tempfile.TemporaryDirectory() as tmp:
            main = Path(tmp)/'s-1.jsonl'
            write(main, [
                user('fix the bug'),
                assistant('r1', S, [{'type': 'text', 'text': 'Handing it off.'}], usage(10, 1000, 9000, 20)),
                assistant('r1', S, [{'type': 'tool_use', 'id': 'toolu_1', 'name': 'Agent'}], usage(10, 1000, 9000, 200)),
                user([{'type': 'tool_result', 'tool_use_id': 'toolu_1', 'content': 'launched'}]),
                assistant('r2', S, [{'type': 'text', 'text': 'Started.'}], usage(5, 100, 10000, 10)),
                user('<task-notification>\n<task-id>a1</task-id>', {'kind': 'task-notification'}),
                assistant('r3', S, [{'type': 'text', 'text': 'Fixed.'}], usage(5, 300, 10000, 50)),
                user('thanks'),
                assistant('r4', S, [{'type': 'text', 'text': 'Welcome.'}], usage(5, 50, 10300, 5))])
            write(Path(tmp)/'s-1/subagents/agent-a1.jsonl', [
                assistant('h1', O, [], usage(3, 8000, 0, 400)), assistant('h2', O, [], usage(4, 2000, 8000, 300))])
            write(Path(tmp)/'s-1/subagents/agent-a2.jsonl', [assistant('h3', O, [], usage(3, 7000, 0, 100))])
            log = Path(tmp)/'helper-log.jsonl'
            base = {'session': 's-1', 'helper': 'modelpilot:helper-high', 'model': 'opus', 'transcript': str(main)}
            write(log, [{'session': 's-1', 'event': 'note', 'model': 'opus', 'effort': 'high'},
                        dict(base, event='launch', agent='a1', caller='', caller_effort='low', tool_use_id='toolu_1'),
                        dict(base, event='launch', agent='a2', caller='a1', caller_effort='high', tool_use_id='toolu_9'),
                        dict(base, event='finish', agent='a2'), dict(base, event='finish', agent='a1'),
                        dict(base, event='launch', agent='a9', caller='', caller_effort='low', tool_use_id='toolu_8')])
            out = hr.report(log, RATES)
        self.assertEqual({k: out[k] for k in ('sessions', 'notes', 'launches', 'nested_launches', 'finished', 'priced')},
                         {'sessions': 1, 'notes': 1, 'launches': 2, 'nested_launches': 1, 'finished': 1, 'priced': 1})
        row, lost = out['helpers']
        startup = dollars(O, 3, 8000, 0, 0) + dollars(O, 3, 7000, 0, 0)
        side = dollars(S, 0, 0, 0, 200) + dollars(S, 5, 300, 10000, 50)  # the brief, then the relay turn only
        rewrite = switch_quote.quote((S, 'low'), (O, 'high'), 10010, cfg=sp.load(), rates=RATES)['rewrite_usd']
        self.assertEqual((row['session_setting'], row['helper_setting']), ([S, 'low'], [O, 'high']))
        self.assertEqual((row['nested_helpers'], row['helper_requests'], row['session_prompt_tokens']), (1, 3, 10010))
        self.assertAlmostEqual(row['helper_usd'], dollars(O, 3, 8000, 0, 400) + dollars(O, 4, 2000, 8000, 300) +
                               dollars(O, 3, 7000, 0, 100), delta=1e-6)
        self.assertAlmostEqual(row['startup_usd'], startup, delta=1e-6)
        self.assertAlmostEqual(row['session_side_usd'], side, delta=1e-6)
        self.assertAlmostEqual(row['rewrite_usd'], rewrite, delta=1e-6)
        self.assertAlmostEqual(row['est_saving_usd'], rewrite - startup - side, delta=1e-6)
        self.assertEqual(out['totals_over_priced']['est_saving_usd'], row['est_saving_usd'])
        self.assertEqual((lost['missing_transcripts'], lost['helper_usd'], lost['est_saving_usd'], lost['finished']),
                         (['a9'], None, None, False))

    def test_no_log_is_an_empty_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = hr.report(Path(tmp)/'none.jsonl', RATES)
        self.assertEqual((out['launches'], out['helpers'], out['totals_over_priced']['est_saving_usd']), (0, [], 0))


if __name__ == '__main__':
    unittest.main()
