"""Jev as ModelPilot's advisor (jev_advisor.mjs through advisor.JevAdvisor): offline, no key, nothing sent."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest import mock
from modelpilot import advisor as advisor_module
from modelpilot.advisor import JevAdvisor

ROOT = Path(__file__).resolve().parents[1]
JEV = ROOT/'work/jev-router-compat'
READY = bool(shutil.which('node')) and (JEV/'node_modules/@typesafe-ai/sdk').exists()
H, S, O = 'claude-haiku-4-5-20251001', 'claude-sonnet-5-5', 'claude-opus-5-5'
CATALOG = [{'id': O, 'display_name': 'Claude Opus 5.5', 'created_at': '2026-09-01T00:00:00Z', 'max_input_tokens': 1000000},
           {'id': S, 'display_name': 'Claude Sonnet 5.5', 'created_at': '2026-09-20T00:00:00Z'},
           {'id': H, 'display_name': 'Claude Haiku 4.5'}]
EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max']
PREFIX = 'CORRECTION CHANNEL paragraph\n\nModelPilot tools note\n\n'
TASK = 'Fix the SECRET-TASK parser bug'
TURN = {'model': S, 'tools': [{'name': 'Bash'}],
        'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': PREFIX + TASK}]},
                     {'role': 'system', 'content': [{'type': 'text', 'text': '# Environment'}]}]}
CONTINUE = dict(TURN, messages=TURN['messages'] + [
    {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 't1', 'name': 'Bash', 'input': {}}]},
    {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'x'}]},
    {'role': 'system', 'content': [{'type': 'text', 'text': '# Environment'}]}])


def jev_model_question(catalog):
    """Jev's own question for these models, computed by Jev's code independently of the bridge."""
    script = ("const [p, c] = await Promise.all([import(process.argv[1] + '/src/proxy.mjs'), import(process.argv[1] + "
              "'/src/config.mjs')]); const models = p.claudeModels(JSON.parse(process.argv[2])).filter((m) => "
              "c.availableTiers().includes(m.tier)); console.log(JSON.stringify(c.questionForModels(models)));")
    out = subprocess.run(['node', '--input-type=module', '-e', script, str(JEV), json.dumps(catalog)],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@unittest.skipUnless(READY, 'needs node and the compat Jev checkout with its dependencies')
class BridgeTests(unittest.TestCase):
    def ask(self, body=TURN, **kwargs):
        advisor = JevAdvisor(JEV, stub=kwargs.pop('stub', {}))
        return advisor.ask(body, S, CATALOG, EFFORTS, strip_prefix=PREFIX, **kwargs)

    def test_the_model_question_is_jevs_own_and_one_effort_question_is_added(self):
        out = self.ask(dry=True)
        self.assertEqual(out['questions'], ['task_complexity', 'reasoning_required', 'tool_complexity', 'model', 'effort'])
        self.assertEqual(out['model_question'], jev_model_question(CATALOG))
        self.assertEqual(out['effort_labels'], EFFORTS)
        self.assertEqual(out['models'], [O, S, H])

    def test_the_prompt_is_jevs_extraction_without_modelpilots_prefix_and_never_returned(self):
        import hashlib
        for body in (TURN, CONTINUE):  # a continuation uses the latest user turn
            out = self.ask(body, dry=True)
            self.assertEqual(out['prompt_sha256'], hashlib.sha256(TASK.encode()).hexdigest())
            self.assertNotIn('SECRET-TASK', json.dumps(out['questions']))
        stubbed = self.ask(stub={'model': {'choice': O, 'confidence': .8, 'probabilities': {O: .8, S: .15, H: .05}}})
        self.assertNotIn('SECRET-TASK', json.dumps(stubbed))
        self.assertEqual((stubbed['stub'], stubbed['model']['choice'], stubbed.get('effort')), (True, O, None))

    def test_evidence_goes_to_the_session_state_not_the_prompt(self):
        import hashlib
        out = self.ask(CONTINUE, dry=True, evidence='ModelPilot progress check: stalled.')
        self.assertIn('progress', out['session_keys'])
        self.assertEqual(out['prompt_sha256'], hashlib.sha256(TASK.encode()).hexdigest())

    def test_a_request_without_a_user_turn_or_models_is_an_error_not_advice(self):
        self.assertEqual(self.ask(dict(TURN, messages=[]))['error'], 'no_prompt')
        advisor = JevAdvisor(JEV, stub={})
        self.assertEqual(advisor.ask(TURN, S, [{'id': 'gpt-x'}], EFFORTS)['error'], 'no_models')  # not Jev's static tiers


class AdvisorProcessTests(unittest.TestCase):
    def test_the_bridge_gets_the_typesafe_key_and_never_the_anthropic_key(self):
        done = subprocess.CompletedProcess([], 0, stdout='{"model": null}\n', stderr='')
        with mock.patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'sk-ant-secret'}), \
                mock.patch.object(advisor_module.subprocess, 'run', return_value=done) as run:
            JevAdvisor(JEV, key='ts-key').ask(TURN, S, CATALOG, EFFORTS)
        env = run.call_args.kwargs['env']
        self.assertEqual(env['JEV_API_KEY'], 'ts-key')
        self.assertNotIn('ANTHROPIC_API_KEY', env)
        self.assertNotIn('sk-ant-secret', json.dumps(env))
        self.assertEqual(run.call_args.args[0][2:], [])  # live mode: no --dry or --stub

    def test_failures_come_back_as_errors(self):
        with mock.patch.object(advisor_module.subprocess, 'run', side_effect=subprocess.TimeoutExpired('node', 1)):
            self.assertEqual(JevAdvisor(JEV, key='k').ask(TURN, S, CATALOG, EFFORTS), {'error': 'advisor_TimeoutExpired'})
        bad = subprocess.CompletedProcess([], 1, stdout='', stderr='boom')
        with mock.patch.object(advisor_module.subprocess, 'run', return_value=bad):
            self.assertEqual(JevAdvisor(JEV, key='k').ask(TURN, S, CATALOG, EFFORTS), {'error': 'advisor_exit_1'})

    def test_a_live_advisor_needs_a_key_and_the_pinned_checkout(self):
        with self.assertRaises(ValueError):
            JevAdvisor(JEV)
        self.assertIsNotNone(JevAdvisor(ROOT/'work/no-such-checkout', key='k').problem())


if __name__ == '__main__':
    unittest.main()
