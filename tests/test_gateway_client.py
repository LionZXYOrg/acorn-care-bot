"""Unix client tests: no Telegram, credentials, DNS, or remote network access."""
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest

from gateway_client import MAX_BODY, MAX_RESPONSE, UnixClient, encode, strict_loads
from telegram_api import TelegramError


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gw-client-')
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / 'api.sock')

    def serve_once(self, body, *, status=200, drop=False):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(self.path)
        listener.listen(1)
        listener.settimeout(2)
        self.addCleanup(listener.close)
        received = []

        def serve():
            try:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(2)
                    data = b''
                    while b'\r\n\r\n' not in data:
                        data += connection.recv(8192)
                    header, content = data.split(b'\r\n\r\n', 1)
                    length = int(next(line.split(b':', 1)[1] for line in header.split(b'\r\n')[1:]
                                      if line.lower().startswith(b'content-length:')))
                    while len(content) < length:
                        content += connection.recv(8192)
                    received.append(strict_loads(content))
                    if drop:
                        return
                    connection.sendall(('HTTP/1.1 %d Response\r\nContent-Length: %d\r\nConnection: close\r\n\r\n'
                                        % (status, len(body))).encode() + body)
            except (OSError, ValueError):
                pass
            finally:
                listener.close()
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.addCleanup(lambda: thread.join(3))
        return received, thread

    def call_error(self, body, *, status=200):
        received, thread = self.serve_once(body, status=status)
        with self.assertRaises(TelegramError) as caught:
            UnixClient(self.path, timeout=1).call('sendMessage', {'text': 'private'})
        thread.join(2)
        self.assertEqual(len(received), 1)
        return caught.exception

    def test_call_interface_and_single_connection(self):
        received, thread = self.serve_once(encode({'ok': True, 'result': {'message_id': 700}}))
        result = UnixClient(self.path).call('sendMessage', {'text': 'hello'})
        thread.join(2)
        self.assertEqual(result, {'message_id': 700})
        self.assertEqual(received, [{'method': 'sendMessage', 'payload': {'text': 'hello'}}])

    def test_none_payload_becomes_empty_object(self):
        received, thread = self.serve_once(encode({'ok': True, 'result': {'url': ''}}))
        self.assertEqual(UnixClient(self.path).call('getWebhookInfo'), {'url': ''})
        thread.join(2)
        self.assertEqual(received[0]['payload'], {})

    def test_safe_envelope_preserves_error_fields(self):
        for code, retry, uncertain, not_modified in ((429, 15, False, False),
                                                    (503, 0, True, False),
                                                    (400, 0, False, True)):
            with self.subTest(code=code):
                if os.path.exists(self.path):
                    os.unlink(self.path)
                error = self.call_error(encode({'ok': False, 'error': {
                    'code': code, 'retry_after': retry, 'uncertain': uncertain,
                    'not_modified': not_modified}}))
                self.assertEqual((error.code, error.retry_after, error.uncertain, error.not_modified),
                                 (code, retry, uncertain, not_modified))

    def test_lost_response_after_send_is_uncertain_and_not_retried(self):
        received, thread = self.serve_once(b'', drop=True)
        with self.assertRaises(TelegramError) as caught:
            UnixClient(self.path, timeout=1).call('sendMessage', {'text': 'one message only'})
        thread.join(2)
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(len(received), 1)

    def test_connection_failure_is_safe_and_uncertain(self):
        with self.assertRaises(TelegramError) as caught:
            UnixClient(self.path, timeout=0.1).call('sendMessage', {'text': 'private'})
        self.assertTrue(caught.exception.uncertain)
        self.assertNotIn(self.path, str(caught.exception))
        self.assertNotIn('private', str(caught.exception))

    def test_malformed_result_is_uncertain(self):
        bodies = [b'not json secret', b'{"ok":true,"ok":true,"result":1}',
                  b'{"ok":true,"result":NaN}', b'{"ok":true,"result":1e999}',
                  b'{"ok":true}', b'{"ok":1,"result":1}', b'[]',
                  encode({'ok': False, 'error': {'code': 400}}),
                  encode({'ok': False, 'error': {'code': 429, 'retry_after': 1,
                                                'uncertain': 'false', 'not_modified': False}})]
        for body in bodies:
            with self.subTest(body=body):
                if os.path.exists(self.path):
                    os.unlink(self.path)
                self.assertTrue(self.call_error(body).uncertain)

    def test_http_error_cannot_masquerade_as_success(self):
        error = self.call_error(encode({'ok': True, 'result': 700}), status=503)
        self.assertTrue(error.uncertain)

    def test_oversize_response_is_uncertain(self):
        self.assertTrue(self.call_error(b'x' * (MAX_RESPONSE + 1)).uncertain)

    def test_local_bad_payloads_are_definite_and_do_not_connect(self):
        for payload in ([], {'x': float('nan')}, {'x': float('inf')}, {'text': 'x' * MAX_BODY}):
            with self.subTest(payload_type=type(payload)):
                with self.assertRaises(TelegramError) as caught:
                    UnixClient(self.path).call('sendMessage', payload)
                self.assertEqual(caught.exception.code, 400)
                self.assertFalse(caught.exception.uncertain)


if __name__ == '__main__':
    unittest.main()
