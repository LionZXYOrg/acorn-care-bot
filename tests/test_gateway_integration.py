"""Select a scoped socket without reading a Telegram token."""
import os
import sys
import types
import unittest
from unittest.mock import Mock, patch

import service


class GatewaySelectionTests(unittest.TestCase):
    def test_gateway_selection_never_reads_a_real_token(self):
        factory = Mock(return_value=object())
        module = types.SimpleNamespace(UnixClient=factory)
        with patch.dict(os.environ, {'TELEGRAM_GATEWAY_SOCKET': '/run/telegram/api.sock'}), \
                patch.dict(sys.modules, {'gateway_client': module}), \
                patch.object(service, 'read_token', side_effect=AssertionError('token read')):
            result = service.configured_client()
        self.assertIs(result, factory.return_value)
        factory.assert_called_once_with('/run/telegram/api.sock')

    def test_invalid_socket_does_not_fall_back_to_token(self):
        for value in ('', 'relative.sock'):
            with self.subTest(value=value), patch.dict(os.environ, {'TELEGRAM_GATEWAY_SOCKET': value}), \
                    patch.object(service, 'read_token', side_effect=AssertionError('token read')):
                with self.assertRaises(ValueError):
                    service.configured_client()

    def test_standalone_transport_stays_available_when_socket_is_not_configured(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(service, 'read_token', return_value='synthetic'), \
                patch.object(service, 'Client') as client:
            self.assertIs(service.configured_client(), client.return_value)
        client.assert_called_once_with('synthetic')


if __name__ == '__main__':
    unittest.main()
