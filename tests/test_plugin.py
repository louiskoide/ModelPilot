"""The Claude Code plugin (plugin/, marketplace at .claude-plugin/): its files offline, and a real client at $0."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from modelpilot import plugin_check as pc

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT/'plugin'
CONCISE = (ROOT/'bench/prompts/concise.md').read_text()


def load(path):
    return json.loads(path.read_text())


def frontmatter(path):
    _, head, body = path.read_text().split('---\n', 2)
    fields = dict(line.split(': ', 1) for line in head.strip().splitlines())
    return fields, body


def hook(payload):
    return subprocess.run(['bash', str(PLUGIN/'hooks/concise.sh')], input=json.dumps(payload), capture_output=True,
                          text=True, timeout=30).stdout


class PluginFilesTests(unittest.TestCase):
    def test_the_marketplace_lists_the_plugin_by_its_own_name(self):
        market = load(ROOT/'.claude-plugin/marketplace.json')
        [entry] = market['plugins']
        self.assertEqual((ROOT/entry['source']).resolve(), PLUGIN)
        self.assertEqual(entry['name'], load(PLUGIN/'.claude-plugin/plugin.json')['name'])

    def test_the_default_agent_is_the_plugins_lean_agent(self):
        name = load(PLUGIN/'.claude-plugin/plugin.json')['name']
        fields, _ = frontmatter(PLUGIN/'agents/lean.md')
        self.assertEqual(load(PLUGIN/'settings.json'), {'agent': f"{name}:{fields['name']}"})

    def test_the_agent_sets_the_measured_setting_and_keeps_claude_codes_prompt(self):
        fields, body = frontmatter(PLUGIN/'agents/lean.md')
        self.assertEqual((fields['model'], fields['effort']), ('claude-sonnet-5-5', 'low'))
        self.assertEqual(body.strip(), '', 'a non-empty body replaces Claude Code\'s system prompt')

    def test_the_hook_carries_the_benchmarks_concise_text(self):
        out = load(PLUGIN/'hooks/concise.json')['hookSpecificOutput']
        self.assertEqual(out, {'hookEventName': 'SessionStart', 'additionalContext': CONCISE})
        [entry] = load(PLUGIN/'hooks/hooks.json')['hooks']['SessionStart']
        self.assertIn('${CLAUDE_PLUGIN_ROOT}/hooks/concise.sh', entry['hooks'][0]['command'])

    @unittest.skipUnless(shutil.which('bash'), 'bash not installed')
    def test_the_hook_skips_other_agents_and_models(self):
        text = (PLUGIN/'hooks/concise.json').read_text()
        self.assertEqual(hook({'agent_type': 'modelpilot:lean', 'source': 'startup'}), text)
        self.assertEqual(hook({'agent_type': 'modelpilot:lean', 'model': 'claude-sonnet-5-5'}), text)
        self.assertEqual(hook({'source': 'compact'}), text)  # nothing shows another setting
        self.assertEqual(hook({'agent_type': 'reviewer', 'source': 'startup'}), '')
        self.assertEqual(hook({'agent_type': 'modelpilot:lean', 'model': 'claude-opus-5-5'}), '')


@unittest.skipUnless(pc.PINNED.exists(), 'pinned Claude Code client not installed in work/claude-client')
class RealClientPluginTests(unittest.TestCase):
    """Real client, fake key, owned fixture upstream: $0 and no provider traffic."""
    @classmethod
    def setUpClass(cls):
        cls.report = pc.check(pc.PINNED)
        cls.found = cls.report['findings']

    def test_the_plugin_sets_the_model_and_model_still_overrides_it(self):
        self.assertEqual(self.found['models']['control'], ['claude-opus-5-5'])
        self.assertEqual(self.found['models']['plugin'], ['claude-sonnet-5-5'])
        self.assertEqual(self.found['models']['plugin_model_opus'], ['claude-opus-5-5'])

    def test_claude_codes_system_prompt_is_unchanged_and_the_concise_text_arrives_once(self):
        self.assertTrue(self.found['system_prompt_unchanged'])
        self.assertTrue(self.found['concise_once_in_messages'])
        self.assertTrue(self.found['concise_never_in_system'])
        self.assertFalse(self.found['concise_without_plugin'])

    def test_low_effort_needs_the_users_saved_level(self):
        self.assertEqual(self.found['efforts']['plugin'], ['medium'],
                         "the agent's effort now reaches the main thread: update plugin/README.md's setup step")
        self.assertEqual(self.found['efforts']['plugin_saved_low'], ['low'])

    def test_strict_validation_passes(self):
        for name, result in self.report['validate'].items():
            self.assertEqual(result['returncode'], 0, f"{name}: {result['output']}")


if __name__ == '__main__':
    unittest.main()
