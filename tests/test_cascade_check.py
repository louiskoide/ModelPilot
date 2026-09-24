import json
from pathlib import Path
import tempfile
import unittest
from modelpilot.cascade_check import FALLBACK_MODEL, governed_fallback
from modelpilot.governor import Governor
from modelpilot.m4 import sha

RATES = {'claude-sonnet-4-6': dict(input=3, output=15, read=.3, write_5m=3.75, write_1h=6),
         'claude-opus-4-6': dict(input=5, output=25, read=.5, write_5m=6.25, write_1h=10)}
USAGE = {'input_tokens': 100, 'output_tokens': 10, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0}


class GovernedFallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.gov = Governor(Path(self.tmp.name)/'state.db', 'cascade', 1)
        self.sent = []

    def tearDown(self):
        self.gov.close()
        self.tmp.cleanup()

    def respond(self, text):
        def transport(request, config):
            self.sent.append(request)
            return {'model': request['model'], 'stop_reason': 'end_turn', 'usage': USAGE,
                    'content': [{'type': 'text', 'text': text}]}, 'req_1'
        return transport

    def draft(self, source, text):
        request = {'model': 'claude-sonnet-4-6', 'max_tokens': 192, 'messages': [{'role': 'user', 'content': source}]}
        response = {'model': 'claude-sonnet-4-6', 'stop_reason': 'end_turn', 'usage': USAGE,
                    'content': [{'type': 'text', 'text': text}]}
        return request, response, {'revision': 1, 'text': source, 'current_source_sha256': sha(source)}

    def test_absent_token_fallback_is_executed_once_and_defers(self):
        request, response, evidence = self.draft('There is no verification token in this source.', '{}')
        result = governed_fallback(self.gov, RATES, request, response, evidence, transport=self.respond('{}'))
        self.assertEqual((result['action'], result['fallback']['executed']), ('would_defer', True))
        self.assertEqual([r['model'] for r in self.sent], [FALLBACK_MODEL])
        self.assertAlmostEqual(result['fallback']['cost_usd'], (100*5+10*25)/1e6)

    def test_fallback_that_finds_the_real_span_is_accepted(self):
        source = 'TOKEN_X is the verification token.'
        request, response, evidence = self.draft(source, 'not json')
        answer = json.dumps({'answer': 'TOKEN_X', 'start': 0, 'end': 7})
        result = governed_fallback(self.gov, RATES, request, response, evidence, transport=self.respond(answer))
        self.assertEqual(result['action'], 'would_accept')


if __name__ == '__main__': unittest.main()
