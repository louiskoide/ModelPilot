import unittest
from unittest.mock import patch
from modelpilot.jev_check import validate_result, clean_env

class JevCheckTests(unittest.TestCase):
    def test_null_or_invented_choice_is_not_a_pass(self):
        self.assertFalse(validate_result({'result': None}))
        self.assertFalse(validate_result({'offered_models': ['sonnet'], 'result': {'choice': 'invented'}}))

    def test_verified_exchange(self):
        data = {'offered_models': ['sonnet'], 'result': {'choice': 'sonnet', 'confidence': .9, 'response': {'answers': {}}, 'request': {'state': {}}}}
        self.assertTrue(validate_result(data))
        data['result']['confidence'] = float('nan')
        self.assertFalse(validate_result(data))
        data['result']['confidence'] = True
        self.assertFalse(validate_result(data))

    def test_no_provider_or_user_configuration_inheritance(self):
        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'secret', 'JEV_DUMP': '/private', 'NODE_OPTIONS': '--inspect', 'HOME': '/real'}):
            env = clean_env('jev-test', '/isolated', '/tmp/isolated')
        self.assertNotIn('ANTHROPIC_API_KEY', env)
        self.assertNotIn('JEV_DUMP', env)
        self.assertNotIn('NODE_OPTIONS', env)
        self.assertEqual(env['HOME'], '/isolated')
