import json,threading,unittest
from tests import test_fixture_dispatch as base
from modelpilot.fixture_dispatch import FixtureDispatcher
from modelpilot.loopback_fixture import LoopbackFixture

class LoopbackTests(unittest.TestCase):
    setUp=base.DispatchTests.setUp
    tearDown=base.DispatchTests.tearDown
    def run_mode(self,mode):
        with LoopbackFixture(mode) as fixture:
            result=FixtureDispatcher(self.gov,base.RATES,fixture).dispatch(self.proposal,'owner',self.request)
            self.assertEqual(fixture.calls,1)
            self.assertNotIn('system',[m['role'] for m in fixture.last_request['messages']])
            return result
    def test_json_settles(self):self.assertEqual(self.run_mode('json')['status'],'fixture_confirmed')
    def test_fragmented_stream_settles(self):self.assertEqual(self.run_mode('stream')['cost_usd'],.00024)
    def test_truncated_stream_is_unknown(self):
        self.assertEqual(self.run_mode('truncated')['status'],'unknown_outcome')
        self.assertFalse(self.gov.policy()['cost_complete'])
    def test_provider_error_is_not_free(self):
        self.assertIsNone(self.run_mode('error')['cost_usd'])
    def test_cancellation_after_first_bytes_is_unknown(self):
        with LoopbackFixture('cancel') as fixture:
            result=FixtureDispatcher(self.gov,base.RATES,fixture).dispatch(self.proposal,'owner',self.request)
        self.assertEqual(result['error_type'],'InterruptedError')
        self.assertFalse(self.gov.policy()['cost_complete'])
    def test_arbitrary_endpoint_is_not_an_option(self):
        with self.assertRaises(ValueError):LoopbackFixture('https://api.anthropic.com')
