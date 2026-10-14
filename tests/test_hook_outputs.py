"""Which hook outputs the pinned 2.1.284 client puts in front of the model and which it keeps for the user (October 14
cloud batch, for docs/governor.md's "Idle plans: options", DRAFT). The real client in -p mode against the loopback fixture, a
fake key, an empty HOME, hooks that print fixed JSON. $0. Skips without node or the pinned client first on PATH.

What -p mode can show is the client's stdout (stream-json) and what reached the fixture; how the interactive UI
displays a systemMessage isn't observable here."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from modelpilot.fixtures import fixture_server


def client_version():
    claude = shutil.which('claude')
    if not claude or not shutil.which('node'):
        return None
    with tempfile.TemporaryDirectory() as home:
        out = subprocess.run([claude, '--version'], env={'HOME': home, 'PATH': os.environ.get('PATH', '')},
                             capture_output=True, text=True, timeout=60)
    return out.stdout.split()[0] if out.returncode == 0 and out.stdout.strip() else None


VERSION = client_version()

HOOK = '''
import json, sys
event = sys.argv[1]
sys.stdin.read()
outputs = json.load(open(sys.argv[2]))
if event in outputs:
    sys.stdout.write(json.dumps(outputs[event]))
'''


@unittest.skipUnless(VERSION == '2.1.284', 'needs node and the pinned 2.1.284 client first on PATH')
class HookOutputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.upstream = fixture_server()
        self.upstream.keep_bodies = True
        self.thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.thread.start()
        (self.root/'hook.py').write_text(HOOK)
        for name in ('home', 'tmp', 'config', 'work'):
            (self.root/name).mkdir()

    def tearDown(self):
        self.upstream.shutdown()
        self.upstream.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def session(self, outputs, prompt='Reply with OK.', resume=None, session_id=None):
        """One -p invocation with hooks printing `outputs` ({event: JSON}); (stdout events, stdout text, stderr)."""
        (self.root/'outputs.json').write_text(json.dumps(outputs))
        hooks = {event: [{'hooks': [{'type': 'command', 'command':
                                     f'{sys.executable} {self.root/"hook.py"} {event} {self.root/"outputs.json"}'}]}]
                 for event in ('SessionStart', 'UserPromptSubmit', 'Stop')}
        (self.root/'settings.json').write_text(json.dumps({'hooks': hooks}))
        claude = shutil.which('claude')
        env = {'PATH': os.pathsep.join([str(Path(claude).parent), str(Path(shutil.which('node')).parent), '/usr/bin', '/bin']),
               'HOME': str(self.root/'home'), 'TMPDIR': str(self.root/'tmp'), 'CLAUDE_CONFIG_DIR': str(self.root/'config'),
               'ANTHROPIC_API_KEY': 'sk-ant-offline-fixture-not-a-key', 'DISABLE_AUTOUPDATER': '1',
               'CLAUDE_CODE_MAX_RETRIES': '0', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1',
               'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{self.upstream.server_port}'}
        command = [claude, '-p', prompt, '--model', 'claude-sonnet-4-6', '--output-format', 'stream-json', '--verbose',
                   '--max-turns', '1', '--setting-sources', '', '--settings', str(self.root/'settings.json'),
                   '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--tools', '']
        command += ['--resume', resume] if resume else ['--session-id', session_id] if session_id else ['--no-session-persistence']
        result = subprocess.run(command, env=env, cwd=self.root/'work', capture_output=True, text=True, timeout=120,
                                stdin=subprocess.DEVNULL)
        events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
        return events, result.stdout, result.stderr

    def sent(self):
        return b'\n'.join(self.upstream.bodies)

    def test_additional_context_reaches_the_model_and_system_message_does_not(self):
        events, stdout, stderr = self.session({
            'UserPromptSubmit': {'systemMessage': 'SYS_PROMPT_MARKER', 'hookSpecificOutput': {
                'hookEventName': 'UserPromptSubmit', 'additionalContext': 'CTX_PROMPT_MARKER'}},
            'Stop': {'systemMessage': 'SYS_STOP_MARKER'}})
        self.assertIn(b'CTX_PROMPT_MARKER', self.sent())
        self.assertNotIn(b'SYS_PROMPT_MARKER', self.sent())
        self.assertNotIn(b'SYS_STOP_MARKER', self.sent())
        self.assertEqual(events[-1].get('subtype'), 'success')
        # Where -p mode shows a systemMessage (recorded October 14, pinned so a client change shows): an informational
        # system event, "<Event> says: <message>", at level notice; nothing on stderr.
        shown = {e['content']: e.get('level') for e in events if e.get('type') == 'system' and e.get('subtype') == 'informational'}
        self.assertEqual(shown, {'UserPromptSubmit says: SYS_PROMPT_MARKER': 'notice', 'Stop says: SYS_STOP_MARKER': 'notice'})
        self.assertNotIn('MARKER', stderr)

    def test_a_stop_system_message_never_reaches_a_later_turn(self):
        session = '8a1c1d1e-0000-4000-8000-000000000001'
        self.session({'Stop': {'systemMessage': 'SYS_STOP_MARKER'}}, session_id=session)
        before = len(self.upstream.bodies)
        events, _, _ = self.session({}, prompt='And again.', resume=session)
        later = b'\n'.join(self.upstream.bodies[before:])
        self.assertTrue(later, 'the resumed turn sent nothing')
        self.assertNotIn(b'SYS_STOP_MARKER', later)
        self.assertEqual(events[-1].get('subtype'), 'success')

    def test_a_blocked_prompt_sends_no_request_and_its_reason_goes_to_the_user(self):
        events, stdout, stderr = self.session({'UserPromptSubmit': {'decision': 'block', 'reason': 'BLOCK_REASON_MARKER'}})
        messages = [r for r in self.upstream.received if r.get('path', '').split('?')[0] == '/v1/messages']
        self.assertEqual(messages, [])  # nothing reached the model: no rewrite at the old setting
        self.assertNotIn(b'BLOCK_REASON_MARKER', self.sent())
        # The user sees the reason and the prompt that wasn't sent (a warning); -p mode ends the session at once, a
        # success with no turn and $0 (recorded October 14).
        warning, = [e for e in events if e.get('type') == 'system' and e.get('subtype') == 'informational']
        self.assertEqual(warning['content'], 'UserPromptSubmit operation blocked by hook:\nBLOCK_REASON_MARKER\n\n'
                                             'Original prompt: Reply with OK.')
        self.assertEqual((warning['level'], warning.get('prevent_continuation')), ('warning', True))
        result = events[-1]
        self.assertEqual((result['subtype'], result['num_turns'], result['total_cost_usd']), ('success', 0, 0))


if __name__ == '__main__':
    unittest.main()
