"""Offline security, transaction and lifecycle tests for the shared gateway."""
import contextlib
import copy
import io
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch

import gateway
from gateway import Gateway, LimitedUpstream, QueueFull, WORKER_METHODS, validate_config
from gateway_client import UnixClient, encode, strict_loads
from telegram_api import TelegramError

CHAT, ACORN_TOPIC, BINS_TOPIC, BOT, USER = -100123, 42, 43, 12345, 101


def config_at(directory, *, two=True):
    def worker(wid, topic, uid):
        return {'id': wid, 'uid': uid, 'socket_path': str(Path(directory) / (wid + '.sock')),
                'chat_id': CHAT, 'topic_id': topic, 'allowed_user_ids': [USER],
                'receives_updates': wid == 'acorn', 'methods': sorted(WORKER_METHODS)}
    workers = [worker('acorn', ACORN_TOPIC, os.getuid())]
    if two:
        workers.append(worker('bins', BINS_TOPIC, os.getuid() + 1))
    return {'state_path': str(Path(directory) / 'gateway.sqlite3'), 'workers': workers}


def message(uid, topic=ACORN_TOPIC, sender=USER):
    return {'update_id': uid, 'message': {'message_id': uid + 100,
            'chat': {'id': CHAT}, 'message_thread_id': topic,
            'from': {'id': sender, 'is_bot': False}, 'text': '/status', 'date': 1700000000}}


def callback(uid, mid=700, topic=ACORN_TOPIC, sender=USER):
    return {'update_id': uid, 'callback_query': {'id': 'callback-' + str(uid), 'data': 'ac:abc:0',
            'from': {'id': sender, 'is_bot': False}, 'message': {
                'message_id': mid, 'chat': {'id': CHAT}, 'message_thread_id': topic,
                'from': {'id': BOT, 'is_bot': True}}}}


class FakeUpstream:
    def __init__(self):
        self.calls = []
        self.updates = []
        self.error = None
        self.send_result = None
        self.mid = 700
        self.on_poll = None

    def call(self, method, payload=None):
        self.calls.append((method, copy.deepcopy(payload)))
        if method == 'getMe':
            return {'id': BOT, 'is_bot': True, 'username': 'example_care_bot', 'first_name': 'Example'}
        if method == 'getWebhookInfo':
            return {'url': ''}
        if method == 'getUpdates':
            if self.on_poll:
                self.on_poll(payload)
            return copy.deepcopy(self.updates)
        if self.error:
            raise self.error
        if method == 'getChatMember':
            return {'status': 'member', 'user': {'id': payload['user_id'], 'is_bot': payload['user_id'] == BOT}}
        if method in {'sendMessage', 'sendRichMessage', 'editMessageText', 'editMessageReplyMarkup'}:
            if method in {'sendMessage', 'sendRichMessage'} and self.send_result is not None:
                return self.send_result
            mid = self.mid if method in {'sendMessage', 'sendRichMessage'} else payload['message_id']
            topic = payload.get('message_thread_id', ACORN_TOPIC if mid != 701 else BINS_TOPIC)
            return {'message_id': mid, 'chat': {'id': CHAT}, 'message_thread_id': topic,
                    'from': {'id': BOT, 'is_bot': True}}
        return True


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gateway-')
        self.addCleanup(self.temp.cleanup)
        self.config = config_at(self.temp.name)
        self.upstream = FakeUpstream()
        self.gw = self.new_gateway()

    def new_gateway(self):
        gw = Gateway(self.config, self.upstream)
        self.addCleanup(gw.close)
        return gw

    def error(self, code, fn, *args, **kwargs):
        with self.assertRaises(TelegramError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def send(self, wid='acorn'):
        topic = ACORN_TOPIC if wid == 'acorn' else BINS_TOPIC
        return self.gw.call(wid, 'sendMessage', {'chat_id': CHAT, 'message_thread_id': topic, 'text': 'hello'})

    def test_identity_and_webhook_are_local_safe_results(self):
        self.upstream.calls.clear()
        self.assertEqual(self.gw.call('acorn', 'getMe')['id'], BOT)
        self.assertEqual(self.gw.call('bins', 'getWebhookInfo'), {'url': ''})
        self.assertEqual(self.upstream.calls, [])
        self.error(400, self.gw.call, 'acorn', 'getMe', {'token': 'hidden'})

    def test_explicit_method_allowlist_and_no_global_operations(self):
        for method in ('setWebhook', 'deleteWebhook', 'getFile', 'sendPhoto', 'setMyCommands',
                       'forwardMessage', 'copyMessage', 'unpinAllChatMessages', 'bindLegacyMessage'):
            self.error(403, self.gw.call, 'acorn', method, {})
        self.gw.workers['bins']['methods'].remove('sendMessage')
        self.error(403, self.send, 'bins')

    def test_route_sanitizes_nested_content_and_never_delivers_edits(self):
        update = message(10)
        update['message']['reply_to_message'] = message(999, BINS_TOPIC)['message']
        edits = {'update_id': 11, 'edited_message': message(11)['message']}
        wrong_chat = message(13)
        wrong_chat['message']['chat']['id'] = -999
        wrong_type = message(14)
        wrong_type['message']['message_thread_id'] = True
        forwarded = message(15)
        forwarded['message']['forward_origin'] = {}
        self.gw.ingest([update, edits, message(12, BINS_TOPIC), wrong_chat, wrong_type,
                        forwarded, message(16, sender=999)])
        result = self.gw.call('acorn', 'getUpdates', {'offset': 0})
        self.assertEqual([u['update_id'] for u in result], [10])
        self.assertNotIn('reply_to_message', result[0]['message'])
        self.assertEqual(self.gw.call('bins', 'getUpdates', {'offset': 0}), [])
        self.assertEqual(self.gw.state.offset(), 17)

    def test_nontext_addressed_event_is_retained_without_media(self):
        update = message(1)
        del update['message']['text']
        update['message']['photo'] = [{'file_id': 'not-exposed'}]
        self.gw.ingest([update])
        result = self.gw.call('acorn', 'getUpdates')
        self.assertEqual(len(result), 1)
        self.assertNotIn('photo', result[0]['message'])

    def test_callback_needs_known_message_sender_topic_and_delivery(self):
        self.send()
        self.gw.ingest([callback(1), callback(2, mid=999), callback(3, topic=BINS_TOPIC),
                        callback(4, sender=999)])
        self.error(403, self.gw.call, 'acorn', 'answerCallbackQuery', {'callback_query_id': 'callback-1'})
        self.assertEqual([u['update_id'] for u in self.gw.call('acorn', 'getUpdates')], [1])
        self.assertTrue(self.gw.call('acorn', 'answerCallbackQuery', {'callback_query_id': 'callback-1', 'text': 'ok'}))
        for wid, cid in (('bins', 'callback-1'), ('acorn', 'callback-2'), ('acorn', 'callback-4')):
            self.error(403, self.gw.call, wid, 'answerCallbackQuery', {'callback_query_id': cid})
        self.error(400, self.gw.call, 'acorn', 'answerCallbackQuery', {'callback_query_id': 'callback-1', 'url': 'https://evil.invalid'})
        with self.gw.state.transaction() as db:
            db.execute('UPDATE callbacks SET expires=0')
        self.error(403, self.gw.call, 'acorn', 'answerCallbackQuery', {'callback_query_id': 'callback-1'})

    def test_callback_auth_survives_ack_and_restart(self):
        self.send()
        self.gw.ingest([callback(5)])
        self.gw.call('acorn', 'getUpdates')
        self.gw.call('acorn', 'getUpdates', {'offset': 6})
        self.gw.close()
        self.gw = self.new_gateway()
        self.assertTrue(self.gw.call('acorn', 'answerCallbackQuery', {'callback_query_id': 'callback-5'}))

    def test_ownership_is_separate_from_received_updates(self):
        self.gw.ingest([message(1)])
        self.gw.call('acorn', 'getUpdates')
        self.error(403, self.gw.call, 'acorn', 'editMessageText', {'chat_id': CHAT, 'message_id': 101, 'text': 'bad'})
        self.error(403, self.gw.call, 'acorn', 'deleteMessage', {'chat_id': CHAT, 'message_id': 101})

    def test_cross_module_edits_pins_unpins_deletes_are_denied(self):
        self.send()
        self.upstream.mid = 701
        self.send('bins')
        for method in ('editMessageText', 'editMessageReplyMarkup', 'pinChatMessage', 'unpinChatMessage', 'deleteMessage'):
            extras = {'text': 'edit'} if method == 'editMessageText' else {}
            if method == 'editMessageReplyMarkup':
                extras = {'reply_markup': {'inline_keyboard': []}}
            for wid, mid in (('acorn', 701), ('bins', 700), ('acorn', 999)):
                self.error(403, self.gw.call, wid, method, {'chat_id': CHAT, 'message_id': mid, **extras})
            self.assertTrue(self.gw.call('acorn', method, {'chat_id': CHAT, 'message_id': 700, **extras}))
        self.error(400, self.gw.call, 'acorn', 'unpinChatMessage', {'chat_id': CHAT})
        pins = [p for m, p in self.upstream.calls if m == 'pinChatMessage']
        self.assertEqual(pins, [{'chat_id': CHAT, 'message_id': 700, 'disable_notification': True}])

    def test_ownership_survives_restart(self):
        self.send()
        self.gw.close()
        self.gw = self.new_gateway()
        self.assertTrue(self.gw.call('acorn', 'deleteMessage', {'chat_id': CHAT, 'message_id': 700}))

    def test_legacy_finite_admin_list_is_acorn_only(self):
        self.gw.close()
        self.config['workers'][0]['legacy_message_ids'] = [500, 501]
        self.config['state_path'] = str(Path(self.temp.name) / 'legacy-first.sqlite3')
        self.gw = self.new_gateway()
        self.assertTrue(self.gw.call('acorn', 'editMessageText', {'chat_id': CHAT, 'message_id': 500, 'text': 'old panel'}))
        self.error(403, self.gw.call, 'acorn', 'deleteMessage', {'chat_id': CHAT, 'message_id': 499})
        self.error(403, self.gw.call, 'bins', 'deleteMessage', {'chat_id': CHAT, 'message_id': 500})
        invalid = copy.deepcopy(self.config)
        invalid['workers'][1]['legacy_message_ids'] = [500]
        with self.assertRaises(ValueError):
            validate_config(invalid)

    def test_registry_identity_change_is_not_silently_adopted(self):
        self.gw.close()
        for key, value in (('uid', os.getuid() + 2), ('topic_id', 99), ('chat_id', -999), ('id', 'other')):
            invalid = copy.deepcopy(self.config)
            invalid['workers'][0][key] = value
            with self.assertRaises(ValueError):
                Gateway(invalid, self.upstream)

    def test_second_process_lease_precedes_any_upstream_call(self):
        before = len(self.upstream.calls)
        with self.assertRaises(OSError):
            Gateway(self.config, self.upstream)
        self.assertEqual(len(self.upstream.calls), before)

    def test_upstream_offset_and_queue_commit_before_next_poll(self):
        self.upstream.updates = [message(10), message(11)]
        self.gw.poll_once()
        self.gw.close()
        self.gw = self.new_gateway()
        def check_saved(payload):
            self.assertEqual(payload['offset'], 12)
            with contextlib.closing(sqlite3.connect(self.config['state_path'])) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM updates').fetchone()[0], 2)
                self.assertEqual(db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()[0], '12')
        self.upstream.on_poll = check_saved
        self.gw.poll_once()
        self.assertEqual([u['update_id'] for u in self.gw.call('acorn', 'getUpdates')], [10, 11])

    def test_replay_prune_and_persisted_worker_ack(self):
        self.gw.ingest([message(1), message(2)])
        self.assertEqual(len(self.gw.call('acorn', 'getUpdates')), 2)
        self.assertEqual(len(self.gw.call('acorn', 'getUpdates')), 2)
        self.gw.close()
        self.gw = self.new_gateway()
        self.assertEqual([u['update_id'] for u in self.gw.call('acorn', 'getUpdates', {'offset': 2})], [2])
        self.gw.close()
        self.gw = self.new_gateway()
        self.assertEqual([u['update_id'] for u in self.gw.call('acorn', 'getUpdates', {'offset': 2})], [2])
        self.error(400, self.gw.call, 'acorn', 'getUpdates', {'offset': 1})
        self.error(400, self.gw.call, 'acorn', 'getUpdates', {'offset': 999999})

    def test_only_own_queue_is_acknowledged(self):
        self.gw.workers['bins']['receives_updates'] = True
        self.gw.ingest([message(1), message(2, BINS_TOPIC)])
        self.gw.call('acorn', 'getUpdates', {'offset': 3})
        self.assertEqual([u['update_id'] for u in self.gw.call('bins', 'getUpdates')], [2])
        self.assertFalse(any(m == 'getUpdates' for m, _ in self.upstream.calls))

    def test_queue_caps_rollback_entire_batch_and_never_drop(self):
        self.gw.workers['acorn']['queue_max_updates'] = 1
        self.gw.ingest([message(1)])
        self.upstream.updates = [message(2), message(3)]
        with self.assertRaises(QueueFull):
            self.gw.poll_once()
        self.assertTrue(self.gw.blocked)
        self.assertEqual(self.gw.state.offset(), 2)
        self.assertEqual([u['update_id'] for u in self.gw.call('acorn', 'getUpdates')], [1])
        self.gw.call('acorn', 'getUpdates', {'offset': 2})
        # Even an empty queue cannot fit this whole batch: refuse, never truncate it.
        with self.assertRaises(QueueFull):
            self.gw.poll_once()
        self.assertEqual(self.upstream.calls[-1][1]['offset'], 2)
        self.gw.workers['acorn']['queue_max_updates'] = 3
        self.gw.poll_once()
        self.assertEqual([u['update_id'] for u in self.gw.call('acorn', 'getUpdates', {'offset': 2})], [2, 3])

    def test_byte_cap_and_storage_failure_do_not_advance_offset(self):
        self.gw.workers['acorn']['queue_max_bytes'] = 1
        with self.assertRaises(QueueFull):
            self.gw.ingest([message(1)])
        self.assertEqual(self.gw.state.offset(), 0)
        self.gw.workers['acorn']['queue_max_bytes'] = 10000
        with self.gw.state.transaction() as db:
            db.execute("CREATE TRIGGER refuse_updates BEFORE INSERT ON updates BEGIN SELECT RAISE(FAIL, 'full'); END")
        with self.assertRaises(sqlite3.Error):
            self.gw.ingest([message(1)])
        self.assertEqual(self.gw.state.offset(), 0)

    def test_malformed_batch_is_atomic(self):
        for second in ({'update_id': True}, {'update_id': 1}, {'update_id': -1}):
            with self.assertRaises(TelegramError):
                self.gw.ingest([message(1), second])
            self.assertEqual(self.gw.state.offset(), 0)
            self.assertEqual(self.gw.call('acorn', 'getUpdates'), [])

    def test_unknown_send_outcome_is_never_retried(self):
        for exc in (TelegramError(uncertain=True), TelegramError(503, uncertain=True), RuntimeError('secret URL')):
            self.upstream.error = exc
            before = len(self.upstream.calls)
            result = self.error(0 if not isinstance(exc, TelegramError) else exc.code, self.send)
            self.assertTrue(result.uncertain)
            self.assertEqual(len(self.upstream.calls), before + 1)
            with self.gw.state.mutex:
                self.assertEqual(self.gw.state.db.execute('SELECT COUNT(*) FROM owners').fetchone()[0], 0)

    def test_confirmed_send_but_failed_registry_is_uncertain(self):
        with patch.object(gateway.State, 'register', side_effect=sqlite3.OperationalError('full')):
            exc = self.error(0, self.send)
        self.assertTrue(exc.uncertain)
        self.assertEqual(sum(m == 'sendMessage' for m, _ in self.upstream.calls), 1)

    def test_mismatched_send_confirmation_never_authorizes_an_id(self):
        for result in ({'message_id': 700}, {'message_id': 700, 'chat': {'id': CHAT},
                       'message_thread_id': BINS_TOPIC, 'from': {'id': BOT, 'is_bot': True}}):
            self.upstream.send_result = result
            self.assertTrue(self.error(0, self.send).uncertain)
        self.error(403, self.gw.call, 'acorn', 'deleteMessage', {'chat_id': CHAT, 'message_id': 700})

    def test_send_field_keyboard_and_length_allowlists(self):
        base = {'chat_id': CHAT, 'text': 'safe'}
        bad = [ {'reply_to_message_id': 123}, {'reply_parameters': {'message_id': 1}},
                {'message_thread_id': BINS_TOPIC}, {'chat_id': str(CHAT)}, {'text': '😀' * 2049},
                {'entities': []}, {'protect_content': 1},
                {'reply_markup': {'inline_keyboard': [[{'text': 'link', 'url': 'https://evil.invalid'}]]}},
                {'reply_markup': {'inline_keyboard': [[{'text': 'x', 'callback_data': 'x', 'web_app': {}}]]}},
                {'reply_markup': {'inline_keyboard': [[{'text': 'x', 'callback_data': '😀' * 17}]]}},
                {'reply_markup': {'keyboard': [['not inline']]}}, {'parse_mode': 'Markdown'}]
        before = len(self.upstream.calls)
        for extras in bad:
            with self.assertRaises(TelegramError):
                self.gw.call('acorn', 'sendMessage', {**base, **extras})
        self.assertEqual(len(self.upstream.calls), before)
        self.gw.call('acorn', 'sendMessage', {**base, 'reply_markup': {
            'inline_keyboard': [[{'text': 'ok', 'callback_data': 'a' * 64}]]}})

    def test_native_rich_html_is_forwarded_without_losing_headings_or_links(self):
        self.upstream.mid = 701
        body = '<h1>Bins</h1><p><a href="https://www.royalgreenwich.gov.uk/">Council</a></p>'
        result = self.gw.call('bins', 'sendRichMessage', {'chat_id': CHAT, 'rich_message': {'html': body}})
        self.assertEqual(result['message_id'], 701)
        method, payload = self.upstream.calls[-1]
        self.assertEqual(method, 'sendRichMessage')
        self.assertEqual(payload['rich_message'], {'html': body})
        self.assertEqual(payload['message_thread_id'], BINS_TOPIC)
        self.assertNotIn('text', payload)
        for html in ('<a href="https://evil.invalid">x</a>', '<b onclick="x">x</b>', '<img src="x">'):
            self.error(400, self.gw.call, 'bins', 'sendRichMessage', {'chat_id': CHAT, 'rich_message': {'html': html}})

    def test_member_queries_are_chat_and_user_scoped(self):
        for uid in (USER, BOT):
            self.assertEqual(self.gw.call('acorn', 'getChatMember', {'chat_id': CHAT, 'user_id': uid})['user']['id'], uid)
        for p in ({'chat_id': -999, 'user_id': USER}, {'chat_id': CHAT, 'user_id': 999},
                  {'chat_id': CHAT, 'user_id': str(USER)}):
            self.error(403, self.gw.call, 'acorn', 'getChatMember', p)

    def test_errors_preserve_all_telegram_semantics(self):
        for error in (TelegramError(429, retry_after=17), TelegramError(400, not_modified=True),
                      TelegramError(503, uncertain=True)):
            self.upstream.error = error
            caught = self.error(error.code, self.send)
            self.assertEqual((caught.retry_after, caught.uncertain, caught.not_modified),
                             (error.retry_after, error.uncertain, error.not_modified))


class UnixGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gw-unix-')
        self.addCleanup(self.temp.cleanup)
        self.config = config_at(self.temp.name, two=False)
        self.upstream = FakeUpstream()
        self.gw = Gateway(self.config, self.upstream)
        self.addCleanup(self.gw.close)
        self.gw.start(health_host='127.0.0.1', health_port=0, poll=False)
        self.path = self.config['workers'][0]['socket_path']
        self.client = UnixClient(self.path, timeout=2)

    def raw(self, body, headers=None):
        headers = headers or ('Content-Length: %d\r\nContent-Type: application/json\r\n' % len(body)).encode()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(2)
            sock.connect(self.path)
            sock.sendall(b'POST /call HTTP/1.1\r\n' + headers + b'\r\n' + body)
            result = b''
            while True:
                part = sock.recv(8192)
                if not part:
                    break
                result += part
        return strict_loads(result.split(b'\r\n\r\n')[1])

    def test_real_current_uid_peer_and_no_json_identity_override(self):
        self.assertEqual(self.client.call('getMe')['id'], BOT)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o660)
        self.assertEqual(os.stat(self.path).st_uid, os.getuid())
        body = encode({'method': 'getMe', 'payload': {}, 'worker_id': 'bins'})
        self.assertEqual(self.raw(body)['error']['code'], 400)
        # Same actual AF_UNIX peer, deliberately mismatching the configured UID.
        self.gw.servers[0].worker['uid'] = os.getuid() + 1
        with self.assertRaises(TelegramError) as caught:
            self.client.call('getMe')
        self.assertEqual(caught.exception.code, 403)

    def test_strict_json_and_http_size_limits(self):
        for raw in (b'{"method":"sendMessage","method":"getMe","payload":{}}',
                    b'{"method":"getMe","payload":{"x":NaN}}',
                    b'{"method":"getMe","payload":{"x":Infinity}}',
                    b'{"method":"getMe","payload":{"x":1e999}}',
                    b'{"method":"getMe","payload":{"a":1,"a":2}}'):
            self.assertEqual(self.raw(raw)['error']['code'], 400)
        for headers in (b'Content-Length: 65537\r\n', b'Transfer-Encoding: chunked\r\n',
                        b'Content-Length: 0\r\nContent-Length: 0\r\n'):
            self.assertEqual(self.raw(b'', headers)['error']['code'], 400)

    def test_real_socket_unknown_send_is_not_replayed_and_error_is_safe(self):
        self.upstream.error = RuntimeError('https://api.telegram.org/botPRIVATE/path payload')
        with self.assertRaises(TelegramError) as caught:
            self.client.call('sendMessage', {'chat_id': CHAT, 'text': 'private'})
        self.assertTrue(caught.exception.uncertain)
        self.assertNotIn('PRIVATE', str(caught.exception))
        self.assertEqual(sum(m == 'sendMessage' for m, _ in self.upstream.calls), 1)

    def test_health_listener_never_exposes_worker_api(self):
        import http.client
        port = self.gw.servers[-1].server_address[1]
        for method, path, expected in (('GET', '/livez', 200), ('GET', '/readyz', 503),
                                       ('POST', '/call', 404), ('GET', '/getMe', 404)):
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
            try:
                conn.request(method, path)
                response = conn.getresponse()
                self.assertEqual(response.status, expected)
                self.assertNotIn(b'12345', response.read())
            finally:
                conn.close()

    def test_long_poll_wakes_on_ingest_and_shutdown_is_bounded(self):
        result = []
        thread = threading.Thread(target=lambda: result.extend(self.client.call('getUpdates', {'timeout': 1})))
        thread.start()
        time.sleep(0.05)
        self.gw.ingest([message(1)])
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0]['update_id'], 1)
        # A slow, incomplete HTTP request must not delay shutdown by its read timeout.
        slow = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        slow.connect(self.path)
        slow.sendall(b'POST /call HTTP/1.1\r\n')
        time.sleep(0.05)
        started = time.monotonic()
        self.gw.close()
        slow.close()
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(os.path.exists(self.path))

    def test_stale_socket_restarts_but_regular_file_is_not_replaced(self):
        self.gw.close()
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(self.path)
        stale.close()
        self.gw = Gateway(self.config, self.upstream)
        self.addCleanup(self.gw.close)
        self.gw.start(health_port=None, poll=False)
        self.assertEqual(UnixClient(self.path).call('getMe')['id'], BOT)
        self.gw.close()
        Path(self.path).write_text('do not replace')
        other = Gateway(self.config, self.upstream)
        self.addCleanup(other.close)
        with self.assertRaises(ValueError):
            other.start(health_port=None, poll=False)
        self.assertEqual(Path(self.path).read_text(), 'do not replace')


class UpstreamTests(unittest.TestCase):
    def call_with(self, raw, status=200):
        opener = unittest.mock.Mock()
        if status == 200:
            opener.open.side_effect = lambda *args, **kwargs: io.BytesIO(raw)
        else:
            opener.open.side_effect = urllib.error.HTTPError('https://invalid.example/PRIVATE', status, 'private', {}, io.BytesIO(raw))
        return LimitedUpstream('12345:OFFLINE_TEST_NOT_A_REAL_TOKEN', opener), opener

    def test_extended_operations_and_native_rich_are_explicit(self):
        upstream, opener = self.call_with(b'{"ok":true,"result":true}')
        for method in ('pinChatMessage', 'unpinChatMessage', 'deleteMessage', 'editMessageReplyMarkup', 'sendRichMessage'):
            self.assertTrue(upstream.call(method, {}))
        for method in ('deleteWebhook', 'setMyCommands'):
            with self.assertRaises(ValueError):
                upstream.call(method)
        self.assertEqual(opener.open.call_count, 5)

    def test_upstream_json_is_strict_and_sends_never_retry(self):
        for raw in (b'{"ok":true,"ok":true,"result":7}', b'{"ok":true,"result":NaN}', b'private invalid response'):
            upstream, opener = self.call_with(raw)
            with self.assertRaises(TelegramError) as caught:
                upstream.call('sendMessage', {'text': 'private'})
            self.assertTrue(caught.exception.uncertain)
            self.assertEqual(opener.open.call_count, 1)
            self.assertNotIn('private', str(caught.exception))

    def test_upstream_error_semantics_and_transport_uncertainty(self):
        for raw, status, code, retry, uncertain, modified in (
            (b'{"ok":false,"error_code":429,"parameters":{"retry_after":13}}', 429, 429, 13, False, False),
            (b'{"ok":false,"error_code":400,"description":"Bad Request: message is not modified"}', 400, 400, 0, False, True),
            (b'{"ok":false,"error_code":503}', 503, 503, 0, True, False),
            (b'{"ok":false,"error_code":429}', 503, 429, 0, True, False)):
            upstream, _ = self.call_with(raw, status)
            with self.assertRaises(TelegramError) as caught:
                upstream.call('sendMessage')
            e = caught.exception
            self.assertEqual((e.code, e.retry_after, e.uncertain, e.not_modified), (code, retry, uncertain, modified))
        upstream, opener = self.call_with(b'')
        opener.open.side_effect = TimeoutError('private token')
        with self.assertRaises(TelegramError) as caught:
            upstream.call('sendMessage')
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(opener.open.call_count, 1)

    def test_configuration_rejects_ambiguous_workers_and_unknown_fields(self):
        with tempfile.TemporaryDirectory(prefix='gw-config-') as directory:
            base = config_at(directory)
            for key in ('uid', 'id', 'socket_path', 'topic_id'):
                value = copy.deepcopy(base)
                value['workers'][1][key] = value['workers'][0][key]
                with self.assertRaises(ValueError):
                    validate_config(value)
            for key, val in (('methods', ['setWebhook']), ('receives_updates', 1),
                             ('uid', True), ('legacy_message_ids', list(range(1, 10002)))):
                value = copy.deepcopy(base)
                value['workers'][0][key] = val
                with self.assertRaises(ValueError):
                    validate_config(value)


if __name__ == '__main__':
    unittest.main()
