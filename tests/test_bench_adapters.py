import unittest
from pathlib import Path
from modelpilot.bench_adapters import JevAdapter

class JevAdapterTests(unittest.TestCase):
    def test_launcher_keeps_session_limits_and_uses_exact_client(self):
        adapter = JevAdapter('compat', Path('/jev'), Path('/node'), 'router-secret')
        command = adapter.command(['/exact/claude', '-p', 'task', '--model', 'claude-opus-5',
                                   '--resume', 'session', '--max-budget-usd', '0.004'], 'http://127.0.0.1:1234')
        self.assertNotIn('--model', command)
        self.assertEqual(command[-4:], ['--resume','session','--max-budget-usd','0.004'])
        self.assertIn('/exact/claude', command)
        self.assertNotIn('router-secret', command)

    def test_unknown_router_cost_never_becomes_zero(self):
        a = JevAdapter('stock', Path('/jev'), Path('/node'), 'key')
        evidence = a.accounting([{'kind':'messages','http_status':200,'cost_usd':.01,
                                 'usage': {'input_tokens':1}}], {})
        self.assertIsNone(evidence['cost_usd'])
        self.assertIsNone(evidence['router_cost_usd'])
        self.assertFalse(evidence['cost_complete'])
        self.assertEqual(evidence['known_cost_usd'], .01)

    def test_missing_key_and_unknown_variant_refused(self):
        for variant,key in [('compat',''),('unknown','key')]:
            with self.assertRaises(ValueError): JevAdapter(variant,Path('/jev'),Path('/node'),key)

    def test_environment_does_not_pin_model(self):
        a = JevAdapter('compat',Path('/jev'),Path('/node'),'secret')
        env=a.environment({'ANTHROPIC_MODEL':'opus','OTHER':'keep'})
        self.assertNotIn('ANTHROPIC_MODEL',env)
        self.assertEqual(env['JEV_API_KEY'],'secret')
        self.assertEqual(env['CLAUDE_CODE_MAX_RETRIES'],'0')

class LauncherTests(unittest.TestCase):
    def test_explicit_binary_wins_over_path(self):
        import tempfile, subprocess, shutil, os, json
        from modelpilot.bench_adapters import ROOT
        node=shutil.which('node')
        if not node: self.skipTest('node required')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'src').mkdir()
            (root/'src/proxy.mjs').write_text('export async function startProxy(){return {port:1234,close(){}};}')
            (root/'src/config.mjs').write_text('export const AUTO_MODEL="jev-router";')
            client=root/'selected-client'
            client.write_text('#!/bin/sh\nprintf "EXACT:%s:%s" "$ANTHROPIC_MODEL" "$1"\n')
            client.chmod(0o700)
            command=JevAdapter('stock',root,Path(node),'test-key').command([str(client),'hello'],'http://127.0.0.1:1234')
            env={'PATH':'/usr/bin:/bin','JEV_API_KEY':'fake','HOME':tmp}
            result=subprocess.run(command,env=env,capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stdout,'EXACT:jev-router:hello')

    def test_routing_evidence_survives_cleanup_without_secret(self):
        import tempfile,json
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'tmp/jev-claude').mkdir(parents=True)
            d={'model':'haiku','jev':{'response':{'secret':'test-secret'}}}
            (root/'tmp/jev-claude/session.json').write_text(json.dumps(d))
            (root/'client.stderr.txt').write_text('[jev] abc 4ms p=0.9 opus -> haiku (simple)\n')
            report=JevAdapter('compat',Path('/jev'),Path('/node'),'test-secret').evidence(root)
            self.assertTrue(report['routing_observed'])
            self.assertNotIn('test-secret',(root/'jev-decisions.json').read_text())

class CostReportTests(unittest.TestCase):
    def test_router_unknown_excludes_provider_only_cold_cost(self):
        from modelpilot.bench_report import cold_cost
        record={'accounting':{'cost_complete':False,'router_cost_usd':None},
                'cache':{'cold_equivalent_cost_usd':.1}}
        self.assertIsNone(cold_cost(record))

class TrialAdapterTests(unittest.TestCase):
    def test_trial_persists_adapter_evidence_and_unknown_total(self):
        import shutil,sys
        from unittest.mock import patch
        from tests.test_bench import OfflineTrialTests,RATES
        from modelpilot import bench
        if not shutil.which('claude'): self.skipTest('Claude CLI required')
        fixture=OfflineTrialTests('test_fixing_agent_passes_and_idle_agent_fails_with_exact_accounting')
        fixture.setUp()
        try:
            fixture.upstream.script=[{'text':'Done.'}]
            adapter=JevAdapter('compat',Path('/synthetic'),Path('/node'),'fake-router-key')
            def fake_router(command,url):
                result=list(command); result[result.index('--model')+1]='claude-sonnet-5'
                return result
            with patch.object(adapter,'verify'), patch.object(adapter,'command',side_effect=fake_router):
                record=bench.run_trial(fixture.task,'jev-compat',fixture.out/'adapter',shutil.which('claude'),
                    'sk-ant-offline-not-a-key',f'http://127.0.0.1:{fixture.upstream.server_port}',RATES,
                    python=sys.executable,adapter=adapter,max_turns=2)
            self.assertIsNone(record['accounting']['cost_usd'])
            self.assertFalse(record['routing']['routing_observed'])
            self.assertTrue((fixture.out/'adapter/jev-decisions.json').exists())
        finally:
            fixture.tearDown()
