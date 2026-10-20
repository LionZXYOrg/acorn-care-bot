"""Offline regressions for ownership races, shutdown receipts and rollback drain."""
import contextlib
import copy
import http.client
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest.mock import Mock, patch

import gateway
from gateway import Gateway, LimitedUpstream, QueueFull, WORKER_METHODS
from gateway_client import UnixClient, encode
from telegram_api import TelegramError

CHAT, TOPIC, OTHER_TOPIC, BOT, USER = -100123, 42, 43, 12345, 101


def message(uid):
    return {'update_id': uid, 'message': {
        'message_id': uid + 100, 'chat': {'id': CHAT}, 'message_thread_id': TOPIC,
        'from': {'id': USER, 'is_bot': False}, 'text': '/status'}}


def callback(uid, mid=700):
    return {'update_id': uid, 'callback_query': {
        'id': 'click-' + str(uid), 'data': 'ac:abc:0',
        'from': {'id': USER, 'is_bot': False}, 'message': {
            'message_id': mid, 'chat': {'id': CHAT}, 'message_thread_id': TOPIC,
            'from': {'id': BOT, 'is_bot': True}}}}


class DelayedUpstream:
    def __init__(self):
        self.calls = []
        self.updates = []
        self.mid = 700
        self.block_send = self.block_poll = False
        self.send_entered, self.send_release = threading.Event(), threading.Event()
        self.poll_entered, self.poll_release = threading.Event(), threading.Event()
        self.send_error = None

    def call(self, method, payload=None):
        self.calls.append((method, copy.deepcopy(payload)))
        if method == 'getMe':
            return {'id': BOT, 'is_bot': True, 'username': 'offline_test_bot'}
        if method == 'getWebhookInfo':
            return {'url': ''}
        if method == 'getUpdates':
            self.poll_entered.set()
            if self.block_poll and not self.poll_release.wait(3):
                raise TimeoutError('offline poll deadline')
            return copy.deepcopy(self.updates)
        if method in {'sendMessage', 'sendRichMessage'}:
            self.send_entered.set()
            if self.block_send and not self.send_release.wait(3):
                raise TimeoutError('offline send deadline')
            if self.send_error:
                raise self.send_error
            return {'message_id': self.mid, 'chat': {'id': CHAT},
                    'message_thread_id': payload['message_thread_id'],
                    'from': {'id': BOT, 'is_bot': True}}
        return True


class BackgroundCall:
    def __init__(self, fn):
        self.result = self.error = None
        self.done = threading.Event()

        def run():
            try:
                self.result = fn()
            except BaseException as exc:
                self.error = exc
            finally:
                self.done.set()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def finish(self):
        if not self.done.wait(3):
            raise AssertionError('Offline operation did not finish')
        self.thread.join(1)
        if self.error:
            raise self.error
        return self.result


class GatewayRaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gw-races-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {'state_path': str(self.root / 'state.sqlite3'), 'workers': [
            {'id': wid, 'uid': os.geteuid() + index,
             'socket_path': str(self.root / 'sockets' / wid / 'api.sock'),
             'chat_id': CHAT, 'topic_id': topic, 'allowed_user_ids': [USER],
             'receives_updates': True, 'methods': sorted(WORKER_METHODS)}
            for index, (wid, topic) in enumerate((('acorn', TOPIC), ('bins', OTHER_TOPIC)))]}
        self.upstream = DelayedUpstream()
        self.gw = Gateway(self.config, self.upstream)
        self.addCleanup(self.gw.close)
        self.addCleanup(self.upstream.send_release.set)
        self.addCleanup(self.upstream.poll_release.set)
        control = self.root / 'control'
        control.mkdir(mode=0o700)
        self.pause = control / 'pause-polling'

    def send(self, method='sendMessage', worker='acorn'):
        payload = {'chat_id': CHAT}
        payload.update({'rich_message': {'html': '<h1>Offline</h1>'}} if method == 'sendRichMessage'
                       else {'text': 'offline', 'reply_markup': {'inline_keyboard': [
                           [{'text': 'Click', 'callback_data': 'ac:abc:0'}]]}})
        return self.gw.call(worker, method, payload)

    def start(self, *, poll=False):
        self.gw.start(health_host='127.0.0.1', health_port=0, poll=poll)
        return UnixClient(self.config['workers'][0]['socket_path'], timeout=3)

    def health(self, path, method='GET'):
        port = self.gw.servers[-1].server_address[1]
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def wait_for(self, condition):
        deadline = time.monotonic() + 2
        while not condition():
            if time.monotonic() >= deadline:
                self.fail('Offline condition did not become true')
            time.sleep(0.005)

    def test_fast_buttons_rollback_batch_until_durable_receipt_then_deliver_once(self):
        original_api = self.gw._worker_api
        original_transaction = self.gw.state.transaction
        commit_counts = []

        def checked_api(w, method, payload):
            self.assertGreater(self.gw.inflight_sends[w['id']], 0)
            return original_api(w, method, payload)

        @contextlib.contextmanager
        def checked_transaction():
            with original_transaction() as db:
                yield db
            # The registration COMMIT has completed, but the send is still pending.
            if threading.current_thread() is not threading.main_thread():
                commit_counts.append(self.gw.inflight_sends['acorn'])

        with patch.object(self.gw, '_worker_api', side_effect=checked_api), \
                patch.object(self.gw.state, 'transaction', checked_transaction):
            for index, method in enumerate(('sendMessage', 'sendRichMessage')):
                with self.subTest(method=method):
                    uid = 10 + index * 10
                    before = self.gw.state.offset()
                    self.upstream.mid = 700 + index
                    self.upstream.block_send = True
                    self.upstream.send_entered.clear()
                    self.upstream.send_release.clear()
                    sending = BackgroundCall(lambda: self.send(method))
                    self.assertTrue(self.upstream.send_entered.wait(1))
                    self.upstream.updates = [message(uid), callback(uid + 1, self.upstream.mid)]
                    with self.assertRaises(QueueFull):
                        self.gw.poll_once()
                    self.assertEqual(self.gw.state.offset(), before)
                    with self.gw.state.mutex:
                        self.assertEqual(self.gw.state.db.execute('SELECT COUNT(*) FROM updates').fetchone()[0], 0)
                        self.assertEqual(self.gw.state.db.execute('SELECT COUNT(*) FROM callbacks').fetchone()[0], index)
                        self.assertFalse(gateway.State.owns(self.gw.state.db, self.gw.workers['acorn'], self.upstream.mid))
                    self.upstream.send_release.set()
                    self.assertEqual(sending.finish()['message_id'], self.upstream.mid)
                    self.assertEqual(self.gw.inflight_sends['acorn'], 0)
                    self.gw.poll_once()
                    self.gw.poll_once()  # Replay of the same batch cannot enqueue duplicates.
                    result = self.gw.call('acorn', 'getUpdates', {'offset': before})
                    self.assertEqual([item['update_id'] for item in result], [uid, uid + 1])
                    self.assertEqual(self.gw.call('acorn', 'getUpdates', {'offset': uid + 2}), [])
                    polls = [p['offset'] for m, p in self.upstream.calls if m == 'getUpdates']
                    self.assertEqual(polls[-3:], [before, before, uid + 2])
        self.assertEqual(commit_counts, [1, 1])
        with contextlib.closing(sqlite3.connect(self.config['state_path'])) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM owners').fetchone()[0], 2)

    def test_forged_callbacks_do_not_stall_without_a_matching_pending_send(self):
        self.upstream.block_send = True
        sending = BackgroundCall(lambda: self.send(worker='bins'))
        self.assertTrue(self.upstream.send_entered.wait(1))
        self.gw.ingest([callback(1)])  # Other worker has a pending send, not Acorn.
        self.assertEqual(self.gw.state.offset(), 2)
        self.upstream.send_release.set()
        sending.finish()
        self.upstream.send_entered.clear()
        self.upstream.send_release.clear()
        sending = BackgroundCall(self.send)
        self.assertTrue(self.upstream.send_entered.wait(1))
        bad_sender, bad_bot, bad_data = callback(2), callback(3), callback(4)
        bad_sender['callback_query']['from']['id'] = 999
        bad_bot['callback_query']['message']['from']['id'] = BOT + 1
        bad_data['callback_query']['data'] = ''
        wrong_owner = callback(5)  # bins owns this ID, even though this topic says Acorn.
        self.gw.ingest([bad_sender, bad_bot, bad_data, wrong_owner])
        self.assertEqual(self.gw.state.offset(), 6)
        self.upstream.mid = 701
        self.upstream.send_release.set()
        sending.finish()
        self.gw.ingest([callback(6, mid=999)])  # No pending send: discard, do not block.
        self.assertEqual(self.gw.state.offset(), 7)
        self.assertEqual(self.gw.call('acorn', 'getUpdates'), [])

    def test_failed_send_clears_pending_without_granting_callback_ownership(self):
        self.upstream.block_send = True
        self.upstream.send_error = TelegramError(uncertain=True)
        sending = BackgroundCall(self.send)
        self.assertTrue(self.upstream.send_entered.wait(1))
        with self.assertRaises(QueueFull):
            self.gw.ingest([callback(1)])
        self.upstream.send_release.set()
        with self.assertRaises(TelegramError):
            sending.finish()
        self.assertEqual(self.gw.inflight_sends['acorn'], 0)
        self.gw.ingest([callback(1)])
        self.assertEqual(self.gw.state.offset(), 2)
        self.assertEqual(self.gw.call('acorn', 'getUpdates'), [])
        with self.assertRaises(TelegramError) as caught:
            self.gw.call('acorn', 'answerCallbackQuery', {'callback_query_id': 'click-1'})
        self.assertEqual(caught.exception.code, 403)

    def test_shutdown_preserves_real_unix_send_receipt_until_response_finishes(self):
        client = self.start()
        self.upstream.block_send = True
        sending = BackgroundCall(lambda: client.call('sendMessage', {'chat_id': CHAT, 'text': 'offline'}))
        self.assertTrue(self.upstream.send_entered.wait(1))
        started = time.monotonic()
        closing = BackgroundCall(self.gw.close)
        self.assertTrue(self.gw.stop.wait(1))
        self.assertFalse(sending.done.wait(0.1))
        self.assertFalse(closing.done.is_set())
        with self.gw.servers[0].active_lock:
            self.assertTrue(self.gw.servers[0].admitted)
            self.assertTrue(all(sock.fileno() >= 0 for sock in self.gw.servers[0].admitted))
        self.upstream.send_release.set()
        self.assertEqual(sending.finish()['message_id'], 700)
        closing.finish()
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(sum(m == 'sendMessage' for m, _ in self.upstream.calls), 1)
        with contextlib.closing(sqlite3.connect(self.config['state_path'])) as db:
            self.assertEqual(db.execute('SELECT mid FROM owners').fetchall(), [(700,)])

    def test_shutdown_force_closes_only_overdue_and_keeps_lease_until_handler_exits(self):
        client = self.start()
        self.upstream.block_send = True
        sending = BackgroundCall(lambda: client.call('sendMessage', {'chat_id': CHAT, 'text': 'offline'}))
        self.assertTrue(self.upstream.send_entered.wait(1))
        started = time.monotonic()
        with patch.object(gateway, 'NETWORK_TIMEOUT', 0.1), patch.object(gateway, 'SHUTDOWN_GRACE', 0.1):
            with self.assertRaises(RuntimeError):
                self.gw.close()
        self.assertLess(time.monotonic() - started, 1)
        with self.assertRaises(TelegramError) as caught:
            sending.finish()
        self.assertTrue(caught.exception.uncertain)
        self.assertIsNotNone(self.gw.state.db)
        self.assertIsNotNone(self.gw.state.lease)
        self.upstream.send_release.set()
        self.wait_for(lambda: not any(server.active for server in self.gw.servers))
        self.gw.close()
        self.assertIsNone(self.gw.state.db)

    def test_shutdown_wakes_worker_long_poll_and_stop_rejects_new_requests(self):
        client = self.start()
        polling = BackgroundCall(lambda: client.call('getUpdates', {'timeout': 30}))
        self.wait_for(self.gw.worker_polls['acorn'].locked)
        self.gw.stop.set()
        with self.assertRaises(TelegramError):
            client.call('getMe')
        started = time.monotonic()
        self.gw.close()
        self.assertEqual(polling.finish(), [])
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(any(m == 'getUpdates' for m, _ in self.upstream.calls))

    def test_pause_prevents_polls_but_keeps_heartbeat_worker_api_and_queue_drain(self):
        self.assertIsNone(self.gw.pause_path)
        self.assertFalse(self.gw.drained())
        self.gw.pause_path = self.pause
        self.pause.touch()
        self.gw.ingest([message(1)])
        self.gw.heartbeat = time.monotonic() - 200
        self.gw.last_contact = time.monotonic()
        client = self.start(poll=True)
        self.wait_for(self.gw.live)
        self.assertFalse(self.upstream.poll_entered.wait(0.3))
        self.gw.poll_once()  # The check also protects direct polls and drain races.
        self.assertFalse(self.upstream.poll_entered.is_set())
        self.assertEqual(self.health('/livez'), (200, b'ok\n'))
        self.assertEqual(self.health('/readyz'), (503, b'not ready\n'))
        self.assertEqual(self.health('/drainz'), (503, b'not ready\n'))
        self.assertEqual(client.call('getMe')['id'], BOT)
        self.assertEqual(client.call('sendMessage', {'chat_id': CHAT, 'text': 'offline'})['message_id'], 700)
        self.assertEqual([u['update_id'] for u in client.call('getUpdates')], [1])
        self.assertFalse(self.gw.drained())  # Delivery is not a durable worker ack.
        with contextlib.closing(sqlite3.connect(self.config['state_path'])) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM updates').fetchone()[0], 1)
        self.assertEqual(client.call('getUpdates', {'offset': 2}), [])
        self.assertTrue(self.gw.drained())
        self.assertEqual(self.health('/drainz'), (200, b'ok\n'))
        self.assertFalse(self.upstream.poll_entered.is_set())
        self.pause.unlink()
        self.assertFalse(self.gw.drained())
        self.assertTrue(self.upstream.poll_entered.wait(1))

    def test_pause_waits_for_inflight_poll_and_all_worker_queues(self):
        self.gw.pause_path = self.pause
        self.upstream.block_poll = True
        other = message(2)
        other['message']['message_thread_id'] = OTHER_TOPIC
        self.upstream.updates = [message(1), other]
        client = self.start(poll=True)
        self.assertTrue(self.upstream.poll_entered.wait(1))
        self.pause.touch()
        self.assertFalse(self.gw.drained())
        self.assertEqual(self.health('/drainz'), (503, b'not ready\n'))
        self.upstream.poll_release.set()
        self.wait_for(lambda: not self.gw.poll_lock.locked())
        self.assertEqual(client.call('getUpdates', {'offset': 3}), [])
        self.assertFalse(self.gw.drained())  # bins still has a persistent queued update.
        self.assertEqual(self.gw.call('bins', 'getUpdates', {'offset': 3}), [])
        self.assertTrue(self.gw.drained())
        self.assertEqual(sum(m == 'getUpdates' for m, _ in self.upstream.calls), 1)

    def test_health_has_no_pause_controls_or_private_information(self):
        self.start()
        for method, path in (('POST', '/drainz'), ('POST', '/pause'), ('GET', '/pause'),
                             ('GET', '/resume'), ('POST', '/call'), ('GET', '/getMe')):
            with self.subTest(method=method, path=path):
                self.assertEqual(self.health(path, method), (404, b'not found\n'))
        self.assertEqual(self.health('/drainz'), (503, b'not ready\n'))
        self.assertIsNone(self.gw.pause_path)

    def test_main_pause_path_rejects_worker_mount_symlink_and_writable_file(self):
        workers = copy.deepcopy(self.config['workers'])
        for index, w in enumerate(workers):
            w['uid'] = os.geteuid() + index + 10
        self.assertEqual(gateway.trusted_pause_path(str(self.pause), workers), self.pause)
        with self.assertRaises(ValueError):
            gateway.trusted_pause_path(str(self.pause), self.config['workers'])
        worker_dir = Path(workers[0]['socket_path']).parent
        worker_dir.mkdir(parents=True, mode=0o750)
        with self.assertRaises(ValueError):
            gateway.trusted_pause_path(str(worker_dir / 'pause'), workers)
        self.pause.symlink_to(self.root / 'missing')
        with self.assertRaises(ValueError):
            gateway.trusted_pause_path(str(self.pause), workers)
        self.pause.unlink()
        self.pause.touch(mode=0o666)
        self.pause.chmod(0o666)
        with self.assertRaises(ValueError):
            gateway.trusted_pause_path(str(self.pause), workers)

    def test_main_sets_pause_path_from_environment_or_gateway_owned_default(self):
        config_path = self.root / 'config.json'
        config_path.write_bytes(encode(self.config))
        for override in (None, str(self.pause)):
            with self.subTest(override=override), patch.dict(os.environ, {}, clear=True):
                if override:
                    os.environ['GATEWAY_PAUSE_PATH'] = override
                fake_gateway = Mock()
                fake_gateway.stop.is_set.return_value = True
                with patch('sys.argv', ['gateway.py', '--config', str(config_path)]), \
                        patch.object(gateway.os, 'umask'), patch.object(gateway.signal, 'signal'), \
                        patch.object(gateway, 'trusted_pause_path', return_value=self.pause) as trusted, \
                        patch.object(gateway, 'Gateway', return_value=fake_gateway) as factory, \
                        patch.object(gateway, 'LimitedUpstream'), \
                        patch.object(gateway, 'read_token', return_value='OFFLINE_ONLY'):
                    self.assertEqual(gateway.main(), 1)
                trusted.assert_called_once_with(override or str(Path(self.config['state_path']).with_name('pause-polling')), self.config['workers'])
                self.assertEqual(fake_gateway.pause_path, self.pause)
                fake_gateway.close.assert_called_once_with()


class FailureEnvelopeTests(unittest.TestCase):
    def failure(self, status, data):
        raw = encode(data)
        opener = Mock()
        if status == 200:
            opener.open.side_effect = lambda *args, **kwargs: io.BytesIO(raw)
        else:
            def rejected(*args, **kwargs):
                raise urllib.error.HTTPError('https://invalid.example/PRIVATE', status, 'PRIVATE', {}, io.BytesIO(raw))
            opener.open.side_effect = rejected
        upstream = LimitedUpstream('OFFLINE_ONLY', opener)
        with self.assertRaises(TelegramError) as caught:
            upstream.call('sendMessage', {'text': 'PRIVATE'})
        self.assertEqual(opener.open.call_count, 1)
        self.assertNotIn('PRIVATE', str(caught.exception))
        self.assertNotIn('OFFLINE_ONLY', str(caught.exception))
        return caught.exception

    def test_malformed_or_conflicting_failures_are_uncertain_and_cannot_authorize_retry(self):
        cases = [
            (429, {'ok': True, 'result': {'message_id': 700}}),
            (429, {'result': {'message_id': 700}}),
            (429, {'error_code': 429}),
            (429, {'ok': 'false', 'error_code': 429}),
            (429, {'ok': 0, 'error_code': 429}),
            (429, {'ok': False}),
            (429, {'ok': False, 'error_code': '429'}),
            (429, {'ok': False, 'error_code': True}),
            (429, {'ok': False, 'error_code': 429.0}),
            (429, {'ok': False, 'error_code': 429, 'result': True}),
            (429, {'ok': False, 'error_code': 429, 'parameters': []}),
            (429, {'ok': False, 'error_code': 429, 'parameters': {'retry_after': True}}),
            (429, {'ok': False, 'error_code': 429, 'description': {'PRIVATE': 'body'}}),
            (429, {'ok': False, 'error_code': 400}),
            (500, {'ok': False, 'error_code': 429, 'parameters': {'retry_after': 13}}),
            (503, {'ok': False, 'error_code': 429, 'parameters': {'retry_after': 13}}),
            (200, {'error_code': 429}),
            (200, {'ok': False, 'error_code': 200}),
        ]
        for status, data in cases:
            with self.subTest(status=status, data=data):
                error = self.failure(status, data)
                self.assertTrue(error.uncertain)
                self.assertEqual(error.retry_after, 0)
                self.assertFalse(error.not_modified)

    def test_valid_explicit_429_and_other_telegram_error_semantics_are_preserved(self):
        for status in (200, 429):
            with self.subTest(status=status):
                error = self.failure(status, {'ok': False, 'error_code': 429,
                                              'parameters': {'retry_after': 13}})
                self.assertEqual((error.code, error.retry_after, error.uncertain), (429, 13, False))
        error = self.failure(429, {'ok': False, 'error_code': 429})
        self.assertEqual((error.code, error.retry_after, error.uncertain), (429, 0, False))
        error = self.failure(400, {'ok': False, 'error_code': 400,
                                   'description': 'Bad Request: message is not modified'})
        self.assertEqual((error.code, error.uncertain, error.not_modified), (400, False, True))
        error = self.failure(503, {'ok': False, 'error_code': 503})
        self.assertEqual((error.code, error.retry_after, error.uncertain), (503, 0, True))


if __name__ == '__main__':
    unittest.main()
