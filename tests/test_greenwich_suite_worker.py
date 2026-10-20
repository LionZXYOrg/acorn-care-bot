"""Exercise the weekly scheduler and scoped gateway without live requests."""
import importlib.util
import os
from datetime import datetime
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, call, patch

from test_greenwich_runtime import bot, council, storage

SRC = Path(__file__).resolve().parents[1] / 'greenwich'
spec = importlib.util.spec_from_file_location('greenwich_suite_runner', SRC / 'runner.py')
runner = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'bot': bot, 'council': council, 'storage': storage}):
    spec.loader.exec_module(runner)

MONDAY = datetime.fromisoformat('2026-10-19T11:30:00+00:00')
BOT_ID = 999000
SOCKET = '/run/telegram/private/api.sock'


def bot_identity():
    return {'id': BOT_ID, 'is_bot': True}


def bot_membership():
    return {'user': bot_identity(), 'status': 'administrator', 'can_pin_messages': True}


class SchedulerTests(unittest.TestCase):
    def test_london_monday_window_and_dst(self):
        yes = ['2026-10-19T11:00:00+00:00', '2026-10-19T11:59:59+00:00',
               '2026-10-26T12:00:00+00:00', '2026-10-26T12:59:59+00:00']
        no = ['2026-10-19T10:59:59+00:00', '2026-10-19T12:00:00+00:00',
              '2026-10-26T11:59:59+00:00', '2026-10-26T13:00:00+00:00',
              '2026-10-20T11:30:00+00:00']
        for value in yes:
            self.assertIsNotNone(runner.due_key(datetime.fromisoformat(value)))
        for value in no:
            self.assertIsNone(runner.due_key(datetime.fromisoformat(value)))

    def test_claim_is_durable_before_send_and_restarts_never_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'attempts.json'
            stop = threading.Event()
            def attempt(event):
                self.assertIs(event, stop)
                self.assertEqual(storage.strict_json(path.read_text())['attempts'][runner.due_key(now)], 'claimed')
                return False
            run = Mock(side_effect=attempt)
            ready = Mock(return_value=True)
            now = MONDAY
            self.assertTrue(runner.step(now, path, stop, run, ready))
            saved = path.read_bytes()
            self.assertFalse(runner.step(now, path, stop, run, ready))
            self.assertEqual(path.read_bytes(), saved)
            run.assert_called_once_with(stop)
            now = datetime.fromisoformat('2026-10-26T12:30:00+00:00')
            self.assertTrue(runner.step(now, path, stop, run, ready))
            self.assertEqual(run.call_count, 2)

    def test_default_preflight_absent_gateway_then_recovery_runs_once_in_window(self):
        gateway = Mock()
        factory = Mock(return_value=gateway)
        with tempfile.TemporaryDirectory() as temp, \
                patch.dict(os.environ, {'TELEGRAM_GATEWAY_SOCKET': SOCKET}, clear=True), \
                patch.dict(sys.modules, {'gateway_client': types.SimpleNamespace(UnixClient=factory)}), \
                patch.object(bot, 'build_opener', side_effect=AssertionError('direct network')):
            path = Path(temp) / 'state' / 'attempts.json'
            stop = threading.Event()
            run = Mock(return_value=True)
            gateway.call.side_effect = FileNotFoundError('socket not yet created')
            self.assertFalse(runner.step(MONDAY, path, stop, run))
            self.assertFalse(path.parent.exists(), 'preflight must not create state or a lock file')
            run.assert_not_called()
            gateway.call.side_effect = lambda method, payload: (
                bot_identity() if method == 'getMe' else bot_membership())
            recovered = datetime.fromisoformat('2026-10-19T11:31:00+00:00')
            self.assertTrue(runner.step(recovered, path, stop, run))
            saved = path.read_bytes()
            restarted = datetime.fromisoformat('2026-10-19T11:59:59+00:00')
            self.assertFalse(runner.step(restarted, path, threading.Event(), run))
            run.assert_called_once_with(stop)
            self.assertEqual(storage.strict_json(path.read_text()),
                             {'schema': 1, 'attempts': {'2026-10-19': 'claimed'}})
            self.assertEqual(path.read_bytes(), saved)
            self.assertTrue(all(args.args[0] in {'getMe', 'getChatMember'}
                                for args in gateway.call.call_args_list))

    def test_unavailable_preflight_preserves_existing_history_without_new_claim(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'attempts.json'
            runner.claim(path, '2026-10-12')
            saved = path.read_bytes()
            run = Mock()
            self.assertFalse(runner.step(MONDAY, path, threading.Event(), run, Mock(return_value=False)))
            self.assertEqual(path.read_bytes(), saved)
            run.assert_not_called()

    def test_unsafe_or_failed_preflight_never_claims_or_executes(self):
        results = [None, False, 1, 'ready', [], {}, {'can_pin_messages': True}, Mock()]
        errors = [FileNotFoundError(), TimeoutError(), ValueError(),
                  bot.TelegramError('unconfirmed read', uncertain=True)]
        for result in results + errors:
            with self.subTest(result=type(result).__name__), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / 'state' / 'attempts.json'
                ready = Mock(side_effect=result) if isinstance(result, Exception) else Mock(return_value=result)
                run = Mock()
                with patch.object(runner, 'claim', side_effect=AssertionError('must not claim')):
                    self.assertFalse(runner.step(MONDAY, path, threading.Event(), run, ready))
                self.assertFalse(path.parent.exists())
                run.assert_not_called()

    def test_ambiguous_write_keeps_claim_even_after_gateway_recovers(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'attempts.json'
            stop = threading.Event()
            run = Mock(side_effect=bot.TelegramError('unconfirmed write', uncertain=True))
            ready = Mock(return_value=True)
            with self.assertRaises(bot.TelegramError):
                runner.step(MONDAY, path, stop, run, ready)
            saved = path.read_bytes()
            ready.return_value = False
            self.assertFalse(runner.step(MONDAY, path, threading.Event(), run, ready))
            ready.return_value = True
            self.assertFalse(runner.step(MONDAY, path, threading.Event(), run, ready))
            self.assertEqual(path.read_bytes(), saved)
            run.assert_called_once_with(stop)

    def test_corrupt_claim_file_prevents_send(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'attempts.json'
            path.write_text('{"schema":1,"attempts":[]}')
            run = Mock()
            with self.assertRaises(ValueError):
                runner.step(MONDAY, path, threading.Event(), run, Mock(return_value=True))
            run.assert_not_called()

    def test_other_days_do_not_check_gateway_or_create_state(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'attempts.json'
            ready, run = Mock(), Mock()
            self.assertFalse(runner.step(datetime.fromisoformat('2026-10-20T11:30:00+00:00'), path,
                                         threading.Event(), run, ready))
            ready.assert_not_called()
            run.assert_not_called()
            self.assertFalse(path.exists())

    def test_gateway_recovery_after_window_does_not_claim(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'attempts.json'
            ready, run = Mock(return_value=False), Mock()
            self.assertFalse(runner.step(MONDAY, path, threading.Event(), run, ready))
            ready.return_value = True
            self.assertFalse(runner.step(datetime.fromisoformat('2026-10-19T12:00:00+00:00'),
                                         path, threading.Event(), run, ready))
            ready.assert_called_once_with()
            run.assert_not_called()
            self.assertFalse(path.exists())

    def test_shutdown_before_or_during_preflight_does_not_claim(self):
        for already_stopped in (False, True):
            with self.subTest(already_stopped=already_stopped), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / 'attempts.json'
                stop = threading.Event()
                if already_stopped:
                    stop.set()
                def preflight():
                    stop.set()
                    return True
                ready, run = Mock(side_effect=preflight), Mock()
                self.assertFalse(runner.step(MONDAY, path, stop, run, ready))
                self.assertEqual(ready.call_count, 0 if already_stopped else 1)
                run.assert_not_called()
                self.assertFalse(path.exists())


class GatewayReadinessTests(unittest.TestCase):
    def setUp(self):
        self.me = bot_identity()
        self.member = bot_membership()
        self.gateway = Mock()
        self.gateway.call.side_effect = lambda method, payload: (
            self.me if method == 'getMe' else self.member)
        self.factory = Mock(return_value=self.gateway)
        patches = [patch.dict(os.environ, {'TELEGRAM_GATEWAY_SOCKET': SOCKET}, clear=True),
                   patch.dict(sys.modules, {'gateway_client': types.SimpleNamespace(UnixClient=self.factory)}),
                   patch.object(Path, 'open', side_effect=AssertionError('token read')),
                   patch.object(bot, 'build_opener', side_effect=AssertionError('direct network'))]
        for context in patches:
            context.start()
            self.addCleanup(context.stop)

    def test_ready_uses_only_bounded_scoped_identity_and_membership_reads(self):
        self.assertIs(runner.gateway_ready(), True)
        self.factory.assert_called_once_with(SOCKET, timeout=5)
        self.assertEqual(self.gateway.call.call_args_list,
                         [call('getMe', {}), call('getChatMember', {'chat_id': bot.CHAT_ID, 'user_id': BOT_ID})])

    def test_availability_uses_only_a_short_gateway_cached_identity_read(self):
        self.gateway.call.side_effect = [bot_identity(), AssertionError('no upstream health request')]
        self.assertIs(runner.gateway_available(), True)
        self.factory.assert_called_once_with(SOCKET, timeout=0.4)
        self.gateway.call.assert_called_once_with('getMe', {})

    def test_creator_is_ready_without_administrator_pin_field(self):
        self.member = {'user': bot_identity(), 'status': 'creator'}
        self.assertIs(runner.gateway_ready(), True)

    def test_missing_or_unsafe_socket_never_reads_a_token_or_uses_network(self):
        for value in (None, '', 'relative/api.sock', 'https://api.telegram.org', '/run/bad\0socket'):
            environment = {} if value is None else {'TELEGRAM_GATEWAY_SOCKET': value}
            with self.subTest(value=value), patch.object(runner.os, 'environ', environment):
                self.assertIs(runner.gateway_ready(), False)
                self.assertIs(runner.gateway_available(), False)
        self.factory.assert_not_called()
        self.gateway.call.assert_not_called()

    def test_malformed_identity_never_checks_permissions(self):
        invalid = [None, [], True, {}, {'id': BOT_ID},
                   *({'id': value, 'is_bot': True} for value in (True, 0, -1, str(BOT_ID), float(BOT_ID))),
                   *({'id': BOT_ID, 'is_bot': value} for value in (False, 1, 'true', None))]
        for value in invalid:
            with self.subTest(identity=value):
                self.me = value
                self.gateway.call.reset_mock()
                self.assertIs(runner.gateway_ready(), False)
                self.gateway.call.assert_called_once_with('getMe', {})
                self.assertIs(runner.gateway_available(), False)

    def test_malformed_or_unsafe_membership_is_rejected(self):
        invalid = [None, [], True, {}, {'user': None},
                   *({'user': {**bot_identity(), 'id': value}, 'status': 'creator'}
                     for value in (True, BOT_ID + 1, str(BOT_ID), float(BOT_ID))),
                   *({'user': {'id': BOT_ID, 'is_bot': value}, 'status': 'creator'}
                     for value in (False, None, 1, 'true')),
                   {'user': {'id': BOT_ID}, 'status': 'creator'},
                   *({**bot_membership(), 'status': status}
                     for status in (None, 'member', 'restricted', 'left', 'kicked')),
                   {'user': bot_identity(), 'status': 'administrator'},
                   *({**bot_membership(), 'can_pin_messages': value}
                     for value in (False, None, 1, 'true'))]
        for value in invalid:
            with self.subTest(membership=value):
                self.member = value
                self.assertIs(runner.gateway_ready(), False)

    def test_boolean_membership_id_cannot_impersonate_bot_one(self):
        self.me = {'id': 1, 'is_bot': True}
        self.member = {'user': {'id': True, 'is_bot': True}, 'status': 'creator'}
        self.assertIs(runner.gateway_ready(), False)

    def test_unavailable_or_uncertain_read_is_not_readiness(self):
        for error in (FileNotFoundError(), TimeoutError(), ValueError(),
                      bot.TelegramError('private gateway detail', uncertain=True)):
            for failure_at in ('connect', 'identity', 'membership'):
                with self.subTest(error=type(error).__name__, failure_at=failure_at):
                    self.factory.side_effect = error if failure_at == 'connect' else None
                    self.gateway.call.side_effect = ([bot_identity(), error]
                                                     if failure_at == 'membership' else error)
                    self.assertIs(runner.gateway_ready(), False)


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.heartbeat = Mock()
        self.heartbeat.read_text.return_value = '999'
        for context in (patch.object(runner, 'HEARTBEAT', self.heartbeat),
                        patch.object(runner.time, 'time', return_value=1000)):
            context.start()
            self.addCleanup(context.stop)

    def test_fresh_heartbeat_requires_explicit_gateway_readiness(self):
        for value, expected in ((True, 0), (False, 1), (None, 1), (1, 1), ({'ready': True}, 1)):
            with self.subTest(readiness=value):
                ready = Mock(return_value=value)
                self.assertEqual(runner.health(ready), expected)
                ready.assert_called_once_with()
        self.assertEqual(runner.health(Mock(side_effect=OSError('private detail'))), 1)

    def test_stale_future_or_invalid_heartbeat_is_unhealthy_without_gateway_calls(self):
        for value in ('910', '1001', '', 'bad', 'nan', 'inf', '-inf'):
            with self.subTest(heartbeat=value):
                self.heartbeat.read_text.return_value = value
                ready = Mock(return_value=True)
                self.assertEqual(runner.health(ready), 1)
                ready.assert_not_called()
        self.heartbeat.read_text.side_effect = FileNotFoundError()
        self.assertEqual(runner.health(Mock(return_value=True)), 1)

    def test_health_entrypoint_checks_gateway_without_claiming_or_running(self):
        gateway = Mock()
        module = types.SimpleNamespace(UnixClient=Mock(return_value=gateway))
        with patch.object(sys, 'argv', ['runner.py', 'health']), \
                patch.dict(os.environ, {'TELEGRAM_GATEWAY_SOCKET': SOCKET}, clear=True), \
                patch.dict(sys.modules, {'gateway_client': module}), \
                patch.object(runner, 'claim', side_effect=AssertionError('health must not claim')), \
                patch.object(runner, 'execute', side_effect=AssertionError('health must not run')), \
                patch.object(Path, 'open', side_effect=AssertionError('token read')), \
                patch.object(bot, 'build_opener', side_effect=AssertionError('direct network')):
            gateway.call.side_effect = FileNotFoundError()
            self.assertEqual(runner.main(), 1)
            gateway.call.side_effect = lambda method, payload: (
                bot_identity() if method == 'getMe' else bot_membership())
            self.assertEqual(runner.main(), 0)
            gateway.call.side_effect = TimeoutError()
            self.assertEqual(runner.main(), 1)
        self.assertEqual(gateway.call.call_args_list, [call('getMe', {})] * 3)
        self.heartbeat.write_text.assert_not_called()


class GatewayTransportTests(unittest.TestCase):
    def test_socket_identity_never_reads_a_token_and_keeps_local_scope_checks(self):
        fake = Mock()
        fake.call.return_value = {'id': 999000, 'is_bot': True}
        module = types.SimpleNamespace(UnixClient=Mock(return_value=fake))
        with patch.dict(os.environ, {'TELEGRAM_GATEWAY_SOCKET': '/run/telegram/api.sock'}), \
                patch.dict(sys.modules, {'gateway_client': module,
                                         'telegram_api': types.SimpleNamespace(TelegramError=RuntimeError)}), \
                patch.object(Path, 'open', side_effect=AssertionError('token read')):
            client = bot.Telegram()
            self.assertEqual(client._bot_id, 999000)
            self.assertFalse(hasattr(client, '_token'))
            for method, payload in [('getUpdates', {}), ('sendMessage', {'chat_id': bot.CHAT_ID,
                                        'message_thread_id': 210, 'parse_mode': 'HTML', 'text': 'wrong topic'})]:
                with self.assertRaises(bot.BotError):
                    client.call(method, payload)
        self.assertEqual(fake.call.call_count, 1)

    def test_gateway_failure_remains_uncertain_without_direct_fallback(self):
        class GatewayError(Exception):
            uncertain = True
        fake = Mock()
        fake.call.side_effect = GatewayError()
        modules = {'gateway_client': types.SimpleNamespace(UnixClient=Mock(return_value=fake)),
                   'telegram_api': types.SimpleNamespace(TelegramError=GatewayError)}
        with patch.dict(os.environ, {'TELEGRAM_GATEWAY_SOCKET': '/run/telegram/api.sock'}), \
                patch.dict(sys.modules, modules), patch.object(Path, 'open', side_effect=AssertionError('token read')):
            with self.assertRaises(bot.TelegramError) as caught:
                bot.Telegram()
        self.assertTrue(caught.exception.uncertain)
        self.assertNotIn('token', str(caught.exception).lower())


if __name__ == '__main__':
    unittest.main()
