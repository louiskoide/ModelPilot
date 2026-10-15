"""modelpilot.client_matrix (October 15, overnight cloud run): the comparison helpers on fixtures, and the probes on the
pinned 2.1.284 client only (skipped without it): its hook payloads match the recording's fields, and a 429 at
CLAUDE_CODE_MAX_RETRIES=0 is sent once. Loopback fixtures, a fake key, an empty HOME: $0. Other versions run only
from the command line."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock
from modelpilot import client_matrix as cm

ROOT = Path(__file__).resolve().parents[1]
PINNED = ROOT/'work/claude-client/node_modules/.bin/claude'


def pinned():
    return PINNED.exists() and shutil.which('node') and cm.client_version(PINNED) == '2.1.284'


class HelperTests(unittest.TestCase):
    def test_a_shape_compared_with_itself_has_no_differences(self):
        reference = json.loads(cm.SHAPE_FIXTURE.read_text())
        for model, diff in cm.shape_diff(reference, reference).items():
            self.assertTrue(diff['captured'], model)
            self.assertEqual((diff['betas'], diff['top_level_keys']),
                             ({'added': [], 'removed': []}, {'added': [], 'removed': []}))
            self.assertTrue(diff['trailing_system_message'] and diff['message_roles_same'])

    def test_a_dropped_beta_and_a_missing_model_show(self):
        reference = json.loads(cm.SHAPE_FIXTURE.read_text())
        changed = json.loads(cm.SHAPE_FIXTURE.read_text())
        model = next(iter(changed['requests']))
        changed['requests'][model]['anthropic_beta'] = changed['requests'][model]['anthropic_beta'][1:] + ['new-beta']
        other = next(m for m in changed['requests'] if m != model)
        del changed['requests'][other]
        diff = cm.shape_diff(changed, reference)
        self.assertEqual(diff[model]['betas']['added'], ['new-beta'])
        self.assertEqual(len(diff[model]['betas']['removed']), 1)
        self.assertEqual(diff[other], {'captured': False})

    def test_key_paths_never_keep_values(self):
        paths = cm.key_paths({'a': 1, 'b': {'c': 'secret'}, 'd': [{'e': 2}], 'f': []})
        self.assertEqual(paths, {'a', 'b', 'b.c', 'd', 'd.[].e', 'f'})

    def test_cache_lifetimes(self):
        self.assertEqual(cm.cache_lifetimes({'x': [{'cache_control': {'type': 'ephemeral'}},
                                                   {'cache_control': {'type': 'ephemeral', 'ttl': '1h'}}]}), {'5m', '1h'})

    def test_a_missing_version_is_not_installed_without_install(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(cm, 'install', lambda *a: self.fail('installed')):
            self.assertEqual(cm.probe('9.9.9', root=Path(tmp)), {'version': '9.9.9', 'installed': False})
            self.assertEqual(cm.summary_row({'version': '9.9.9', 'installed': False}), {'version': '9.9.9', 'probed': False})


@unittest.skipUnless(pinned(), 'needs node and the pinned 2.1.284 client in work/claude-client')
class PinnedClientTests(unittest.TestCase):
    def test_hook_payloads_have_the_recorded_fields(self):
        hooks = cm.hooks_probe(PINNED)
        self.assertEqual(hooks['exit_code'], 0)
        for event, seen in hooks['events'].items():
            with self.subTest(event=event):
                self.assertTrue(seen['seen'])
                self.assertEqual(seen['removed'], [])

    def test_the_pinned_client_runs_bash_in_its_default_mode_without_side_requests(self):
        result = cm.permission_probe(PINNED)
        self.assertEqual((result['permission_mode'], result['bash_ran'], result['side_requests']), ('default', True, 0))

    def test_a_429_is_sent_once_at_no_retries(self):
        result = cm.failure_probe(PINNED, 'http_429')
        self.assertEqual((result['main_requests'], result['resent'], result['exit_code']), (1, False, 1))
        self.assertEqual(result['proxy_rows'], ['upstream_error'])


if __name__ == '__main__':
    unittest.main()
