import socket
import ssl
import unittest
from unittest.mock import patch
import urllib.error
from modelpilot.cache_probe import error_details, tls_context, redact_message


class TransportTests(unittest.TestCase):
    def test_certificate_failure_is_actionable_without_raw_exception(self):
        error = urllib.error.URLError(ssl.SSLCertVerificationError(1, 'PRIVATE'))
        result = error_details(error)
        self.assertEqual(result['error_category'], 'tls_certificate')
        self.assertIn('certifi', result['error_hint'])
        self.assertNotIn('PRIVATE', str(result))

    def test_dns_is_distinct_from_tls(self):
        result = error_details(urllib.error.URLError(socket.gaierror(8, 'PRIVATE')))
        self.assertEqual(result['error_category'], 'dns')
        self.assertNotIn('PRIVATE', str(result))

    def test_auth_failure_is_distinct(self):
        error = urllib.error.HTTPError('https://example.com', 401, 'PRIVATE', {}, None)
        self.assertEqual(error_details(error)['error_category'], 'http')
        self.assertIn('authentication', error_details(error)['error_hint'])

    def test_tls_verification_remains_required(self):
        context = tls_context()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_server_message_is_used_after_redaction(self):
        error = urllib.error.HTTPError('https://example.com', 400, 'Bad request', {}, None)
        try:
            error.safe_api_message = 'Your credit balance is too low.'
            self.assertEqual(error_details(error)['error_hint'], error.safe_api_message)
        finally:
            error.close()

    def test_message_redacts_key_and_bounds_output(self):
        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'secret-value'}):
            result = redact_message('secret-value sk-ant-example123\n' + 'x' * 2000)
        self.assertNotIn('secret-value', result)
        self.assertNotIn('sk-ant-', result)
        self.assertNotIn('\n', result)
        self.assertLessEqual(len(result), 1000)

    def test_connection_failure_reports_safe_reason_metadata(self):
        result = error_details(urllib.error.URLError(ConnectionRefusedError(61, 'PRIVATE proxy credential')))
        self.assertEqual(result['error_category'], 'connection')
        self.assertEqual(result['reason_type'], 'ConnectionRefusedError')
        self.assertEqual(result['errno'], 61)
        self.assertNotIn('PRIVATE', str(result))
