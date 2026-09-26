import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from modelpilot.fixtures import fixture_server
from modelpilot.jev_route_check import DECISION, load_decisions
from modelpilot.proxy import ProxyServer

ROOT = Path(__file__).resolve().parents[1]
JEV = ROOT/'work/jev-router-compat'
LAUNCHER = ROOT/'modelpilot/jev_accounted_launch.mjs'
RATES = json.loads((ROOT/'configs/jev-rates.json').read_text())['rates'] if (ROOT/'configs/jev-rates.json').exists() else {}


@unittest.skipUnless(shutil.which('node') and (JEV/'src/proxy.mjs').exists(), 'needs node and work/jev-router-compat')
class AccountedLaunchSelfTest(unittest.TestCase):
    """Jev's real proxy -> ModelPilot ProxyServer -> loopback fixture; stub router, no Claude Code."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name)/'observations.jsonl'
        self.upstream = fixture_server()
        self.proxy = ProxyServer(('127.0.0.1', 0), f'http://127.0.0.1:{self.upstream.server_port}', self.log, RATES)
        self.threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (self.upstream, self.proxy)]
        for t in self.threads:
            t.start()

    def tearDown(self):
        for s in (self.proxy, self.upstream):
            s.shutdown()
            s.server_close()
        for t in self.threads:
            t.join()
        self.tmp.cleanup()

    def test_rewritten_request_is_measured_behind_jev(self):
        result = subprocess.run(['node', str(ROOT/'modelpilot/jev_accounted_launch.mjs'), '--self-test', str(JEV),
                                 f'http://127.0.0.1:{self.proxy.server_port}'], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        # Catalog discovery went through ModelPilot: the router saw the fixture's catalog, not static tiers.
        self.assertEqual(report['offered'], ['claude-sonnet-5'])
        self.assertEqual(report['status'], 200)
        for _ in range(200):
            if self.log.exists() and len(self.log.read_text().splitlines()) >= 2:
                break
            time.sleep(.005)
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual([r['kind'] for r in rows], ['models', 'messages'])
        messages = rows[1]
        self.assertEqual((messages['model'], messages['http_status'], messages['tool_count']), ('claude-sonnet-5', 200, 1))
        self.assertIsNotNone(messages['cost_usd'])
        self.assertFalse(messages['applied'])



@unittest.skipUnless(shutil.which('node') and (JEV/'src/proxy.mjs').exists(), 'needs node and work/jev-router-compat')
class ServeModeTests(unittest.TestCase):
    """--serve: Jev's real proxy for a whole trial, in front of ModelPilot's proxy; the harness starts Claude Code."""
    setUp = AccountedLaunchSelfTest.setUp
    tearDown = AccountedLaunchSelfTest.tearDown

    def serve(self, *extra, key=None):
        self.home = Path(self.tmp.name)
        env = {'PATH': os.environ['PATH'], 'HOME': str(self.home), 'TMPDIR': str(self.home), 'JEV_DEBUG': '1'}
        if key:
            env['JEV_API_KEY'] = key
        return subprocess.Popen(['node', str(LAUNCHER), '--serve', str(JEV), f'http://127.0.0.1:{self.proxy.server_port}', *extra],
                                env=env, cwd=JEV, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def send(self, port, method, path, body=None):
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        conn.request(method, path, json.dumps(body) if body else None,
                     {'Content-Type': 'application/json', 'x-api-key': 'sk-ant-offline', 'anthropic-version': '2023-06-01'})
        response = conn.getresponse()
        response.read()
        conn.close()
        return response.status

    def test_serve_prints_the_client_environment_and_routes_until_stdin_closes(self):
        proc = self.serve('--stub-route', 'claude-sonnet-5')
        try:
            info = json.loads(proc.stdout.readline())
            self.assertEqual(info['env']['ANTHROPIC_BASE_URL'], f"http://127.0.0.1:{info['port']}")
            self.assertEqual(info['env']['ANTHROPIC_MODEL'], 'jev-router')
            self.assertEqual(info['env']['CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY'], '1')
            self.assertEqual((info['add_dir'], info['router']), (str(JEV.resolve()), 'stub'))
            self.assertEqual(self.send(info['port'], 'GET', '/v1/models?limit=100'), 200)
            turn = {'model': 'jev-router', 'max_tokens': 8, 'tools': [{'name': 'Read', 'input_schema': {'type': 'object'}}],
                    'messages': [{'role': 'user', 'content': 'Fix the bug'}, {'role': 'system', 'content': '# Environment'}]}
            self.assertEqual(self.send(info['port'], 'POST', '/v1/messages', turn), 200)
        finally:
            proc.stdin.close()
            self.assertEqual(proc.wait(timeout=10), 0)  # a closed stdin (the harness gone) ends the router
        stderr = proc.stderr.read().decode()
        proc.stdout.close()
        proc.stderr.close()
        self.assertTrue(DECISION.findall(stderr), stderr)
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        messages = [r for r in rows if r['kind'] == 'messages']
        self.assertEqual([(r['model'], r['http_status']) for r in messages], [('claude-sonnet-5', 200)])
        decisions = load_decisions(self.home)
        self.assertEqual(len(decisions), 1)
        self.assertTrue(decisions[0]['jev']['request']['stub'])
        self.assertEqual(decisions[0]['jev']['request']['state']['session']['current_model'], 'claude-opus-5')

    def test_serve_refuses_to_run_unrouted_without_a_key(self):
        proc = self.serve()
        out, err = proc.communicate(timeout=30)
        self.assertEqual((proc.returncode, out), (2, b''))
        self.assertIn(b'refusing', err)

    def test_serve_stops_on_sigterm(self):
        proc = self.serve(key='ts-offline-not-a-key')
        try:
            self.assertIn('port', json.loads(proc.stdout.readline()))
            proc.terminate()
            self.assertEqual(proc.wait(timeout=10), 0)
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                stream.close()

if __name__ == '__main__': unittest.main()


@unittest.skipUnless(shutil.which('node'), 'needs node')
class ExplicitClientTests(unittest.TestCase):
    """--claude-bin runs that exact client, never the first claude on PATH; stub Jev modules."""
    def launch(self, *before, client=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'src').mkdir()
            (root/'src/proxy.mjs').write_text('export async function startProxy(){return {port:1234,close(){}};}')
            (root/'src/config.mjs').write_text('export const AUTO_MODEL="jev-router";')
            selected = root/'selected-client'
            selected.write_text('#!/bin/sh\nprintf "EXACT:%s:%s" "$ANTHROPIC_MODEL" "$1"\n')
            selected.chmod(0o700)
            args = [a.replace('{client}', str(selected)) for a in before]
            env = {'PATH': '/usr/bin:/bin', 'JEV_API_KEY': 'fake', 'HOME': tmp}
            return subprocess.run([shutil.which('node'), str(LAUNCHER), str(root), 'http://127.0.0.1:1234', *args,
                                   '--', 'hello'], env=env, capture_output=True, text=True, timeout=10)

    def test_explicit_binary_wins_over_path(self):
        result = self.launch('--claude-bin', '{client}')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'EXACT:jev-router:hello')

    def test_relative_or_missing_binary_is_refused(self):
        for path in ('relative/claude', '/nonexistent/claude'):
            result = self.launch('--claude-bin', path)
            self.assertEqual(result.returncode, 2, result.stderr)
