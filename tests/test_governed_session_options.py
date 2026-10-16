"""governed_session's two options (October 15, overnight cloud run), both off by default: --suite hands a declared test
suite to the hooks (MODELPILOT_SUITE) and the plan shows it; --subscription logs the client in with a Claude
subscription token, which never appears in argv, logs or the plan, and prices 1h cache writes API-key equivalent with
the as-sent figure kept. Plans are checked without --live; the sessions run the real pinned client against the
loopback fixture with a fake key or token ($0) and skip without it."""
import contextlib
import getpass
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock
from modelpilot import governed_session as gs
from modelpilot.fixtures import fixture_server
from modelpilot.hooks import SUITE
from modelpilot.suite_adapter import load_spec

TOKEN = 'sk-ant-oat01-offline-fixture-not-a-token-0815'
RATES = {'claude-sonnet-4-6': dict(input=3, output=15, read=.3, write_5m=3.75, write_1h=6)}
SUITE_SCRIPT = '''
open('.mp-junit.xml', 'w').write('<testsuites><testsuite name="s" tests="2" failures="0" errors="0" skipped="0"/></testsuites>')
'''


def spec():
    return load_spec({'name': 'unit', 'command': [sys.executable, 'run_suite.py'], 'junit': '.mp-junit.xml',
                      'timeout_seconds': 20, 'watch': ['*.txt']})


def row(cost, hour=100, five=0):
    usage = {'input_tokens': 10, 'output_tokens': 20, 'cache_read_input_tokens': 0,
             'cache_creation_input_tokens': hour + five,
             'cache_creation': {'ephemeral_5m_input_tokens': five, 'ephemeral_1h_input_tokens': hour}}
    return {'kind': 'messages', 'model': 'claude-sonnet-4-6', 'usage': usage, 'cost_usd': cost, 'started_unix': 1}


class OptionUnitTests(unittest.TestCase):
    def test_a_suite_reaches_the_hooks_and_the_stop_hook_outlives_it(self):
        env, timeout = gs.suite_options(spec())
        self.assertEqual(json.loads(env[SUITE])['name'], 'unit')
        self.assertEqual(timeout, 20 + gs.SUITE_HOOK_MARGIN_SECONDS)
        self.assertEqual(gs.suite_options(None), ({}, None))
        hooks = gs.hook_settings(stop_timeout=timeout)['hooks']
        self.assertEqual(hooks['Stop'][0]['hooks'][0]['timeout'], 50)
        self.assertNotIn('timeout', gs.hook_settings()['hooks']['Stop'][0]['hooks'][0])  # unchanged without a suite

    def test_subscription_login_replaces_the_api_key(self):
        env = gs.subscription_login({'ANTHROPIC_API_KEY': '', 'HOME': '/h'}, TOKEN)
        self.assertEqual(env, {'CLAUDE_CODE_OAUTH_TOKEN': TOKEN, 'HOME': '/h'})

    def test_the_token_comes_from_the_environment_or_a_hidden_prompt_and_a_bad_one_stops(self):
        with mock.patch.dict(os.environ, {'CLAUDE_CODE_OAUTH_TOKEN': TOKEN}):
            self.assertEqual(gs.subscription_token(prompt=lambda _: self.fail('prompted')), TOKEN)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(gs.subscription_token(prompt=lambda _: f' {TOKEN} '), TOKEN)
            for bad in ('', 'two words'):
                with self.assertRaises(SystemExit) as stop:
                    gs.subscription_token(prompt=lambda _: bad)
                self.assertIn('No requests sent', str(stop.exception))

    def test_1h_writes_are_priced_api_key_equivalent_and_as_sent_is_kept(self):
        as_sent = (10 * 3 + 20 * 15 + 100 * 6) / 1e6
        equivalent = (10 * 3 + 20 * 15 + 100 * 3.75) / 1e6
        pricing = gs.subscription_pricing([row(as_sent)], RATES)
        self.assertAlmostEqual(pricing['as_sent_usd'], as_sent)
        self.assertAlmostEqual(pricing['api_key_equivalent_usd'], equivalent)
        self.assertEqual(pricing['repriced_1h_write_tokens'], 100)
        unknown = gs.subscription_pricing([row(as_sent), row(None)], RATES)
        self.assertEqual((unknown['as_sent_usd'], unknown['api_key_equivalent_usd']), (None, None))

    def test_redaction_covers_the_key_and_the_token(self):
        self.assertEqual(gs.redacted(f'a {TOKEN} b sk-ant-key-0123456789', TOKEN, 'sk-ant-key-0123456789', None),
                         'a [REDACTED] b [REDACTED]')


class PlanTests(unittest.TestCase):
    """Without --live: a plan, nothing asked for, nothing sent."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fake = self.root/'claude'
        self.fake.write_text('#!/bin/sh\necho "2.1.284 (Claude Code)"\n')
        self.fake.chmod(0o755)
        (self.root/'suite.json').write_text(json.dumps(dict(spec())))

    def tearDown(self):
        self.tmp.cleanup()

    def main(self, *argv, env=None, answer=None):
        """One main() run; answer: what a hidden prompt would get (None: being asked fails the test)."""
        out = io.StringIO()
        ask = (lambda *a: answer) if answer is not None else (lambda *a: self.fail('asked for a credential'))
        with mock.patch.object(sys, 'argv', ['governed_session', '--claude', str(self.fake), *argv]), \
                mock.patch.dict(os.environ, env or {}, clear=True), \
                mock.patch.object(getpass, 'getpass', ask), \
                contextlib.redirect_stdout(out):
            gs.main()
        return out.getvalue()

    def test_the_plan_shows_the_suite_and_the_subscription_and_never_the_token(self):
        for scenario in ('correction', 'turn-end-rebuild'):
            with self.subTest(scenario=scenario):
                text = self.main('--scenario', scenario, '--suite', str(self.root/'suite.json'), '--subscription',
                                 env={'CLAUDE_CODE_OAUTH_TOKEN': TOKEN, 'PATH': os.environ.get('PATH', '')})
                self.assertIn("Declared suite 'unit'", text)
                self.assertIn('Subscription:', text)
                self.assertIn('Add --live', text)
                self.assertNotIn(TOKEN, text)
        rebuild = self.main('--scenario', 'turn-end-rebuild', '--subscription', env={'PATH': os.environ.get('PATH', '')})
        self.assertIn('3635 s', rebuild)  # the hour's idle boundary
        plain = self.main(env={'PATH': os.environ.get('PATH', '')})
        self.assertIn('No declared suite. API key', plain)

    def test_a_bad_suite_or_a_missing_token_stops_before_anything_is_sent(self):
        (self.root/'bad.json').write_text('{"name": "x", "command": "pytest"}')
        with self.assertRaises(SystemExit) as stop:
            self.main('--suite', str(self.root/'bad.json'))
        self.assertIn('Nothing sent', str(stop.exception))
        with mock.patch.object(gs, 'run_session', lambda *a, **k: self.fail('a session started')):
            with self.assertRaises(SystemExit) as stop:
                self.main('--live', '--subscription', answer='')
        self.assertIn('No requests sent', str(stop.exception))


def client_version():
    claude = shutil.which('claude')
    if not claude or not shutil.which('node'):
        return None
    import subprocess
    with tempfile.TemporaryDirectory() as home:
        out = subprocess.run([claude, '--version'], env={'HOME': home, 'PATH': os.environ.get('PATH', '')},
                             capture_output=True, text=True, timeout=60)
    return out.stdout.split()[0] if out.returncode == 0 and out.stdout.strip() else None


@unittest.skipUnless(client_version() == '2.1.284', 'needs node and the pinned 2.1.284 client first on PATH')
class OfflineSessionTests(unittest.TestCase):
    """The real client, a fake key or token, the loopback fixture: $0."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.upstream = fixture_server()
        self.upstream.delay = .3
        self.thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.upstream.shutdown()
        self.upstream.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def session(self, run, key='sk-ant-offline-fixture-not-a-key', **options):
        work = run/'workspace'
        self.upstream.script = [{'tool': 'Read', 'input': {'file_path': str(work/'a.txt')}},
                                {'tool': 'Read', 'input': {'file_path': str(work/'b.txt')}}, {'text': 'DONE BRAVO'}]
        return gs.run_session(run, shutil.which('claude'), key, f'http://127.0.0.1:{self.upstream.server_port}', RATES,
                              prompt='Read a.txt and follow it.', instruction='Report the first token.',
                              correction='CORRECTION_MARKER: report the second token instead.',
                              files={'a.txt': 'ALPHA. Next read b.txt.\n', 'b.txt': 'BRAVO\n', 'run_suite.py': SUITE_SCRIPT},
                              tools='Read', limit_usd=5, expect=['BRAVO'], **options)

    def test_a_declared_suite_runs_at_the_turns_end_and_lands_as_an_observation(self):
        run = Path(self.tmp.name)/'suite-run'
        report = self.session(run, suite=spec())
        self.assertEqual(report['status'], 'passed', report)
        self.assertEqual(report['suite']['name'], 'unit')
        self.assertEqual([(r['outcome'], r['tests']) for r in report['suite']['results']], [('pass', 2)])
        settings = json.loads((run/'settings.json').read_text())
        self.assertEqual(settings['hooks']['Stop'][0]['hooks'][0]['timeout'], 50)
        self.assertIsNone(self.session(Path(self.tmp.name)/'plain')['suite'])  # off by default

    def test_a_subscription_session_logs_in_with_its_token_and_never_records_it(self):
        run = Path(self.tmp.name)/'subscription-run'
        report = self.session(run, key=None, oauth_token=TOKEN)
        sent = [r for r in self.upstream.received if 'sha256' in r]
        self.assertTrue(sent)
        self.assertTrue(all(r['bearer'] and r['key'] is None for r in sent))  # no API key reached the upstream
        self.assertEqual(report['auth'], 'subscription')
        self.assertEqual(report['status'], 'passed', report)
        pricing = report['pricing']
        self.assertAlmostEqual(pricing['as_sent_usd'], report['proxy']['known_cost_usd'])
        self.assertIsNotNone(pricing['api_key_equivalent_usd'])
        for path in run.rglob('*'):
            if path.is_file():
                self.assertNotIn(TOKEN.encode(), path.read_bytes(), path)


if __name__ == '__main__':
    unittest.main()
