import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import care
import inline_ui
import migrate
from configuration import Runtime, read_token, load_care_config
from service import Bot, chunks, health_server, identity
from storage import Store
from telegram_api import Client, NoRedirect, TelegramError

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / '.test-tmp'
TMP.mkdir(exist_ok=True)
NOW = care.instant('2026-10-19T07:00:00+01:00')
CHAT, TOPIC, BOT_ID = -1000000000001, 42, 12345


class FakeClient:
    def __init__(self):
        self.calls = []
        self.send_error = None
        self.edit_error = None
        self.before_ack = None
        self.updates = []
        self.webhook = ''
        self.next_id = 700

    def call(self, method, payload=None):
        self.calls.append((method, copy.deepcopy(payload)))
        if method == 'getMe':
            return {'id': BOT_ID, 'is_bot': True, 'username': 'example_care_bot'}
        if method == 'getWebhookInfo':
            return {'url': self.webhook}
        if method == 'getUpdates':
            return self.updates
        if method == 'answerCallbackQuery' and self.before_ack:
            self.before_ack()
        if method == 'editMessageText' and self.edit_error:
            raise self.edit_error
        if method == 'sendMessage':
            if self.send_error:
                raise self.send_error
            self.next_id += 1
            return {'message_id': self.next_id, 'chat': {'id': CHAT}, 'message_thread_id': TOPIC,
                    'from': {'id': BOT_ID, 'is_bot': True}}
        return True


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TMP)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.json'
        self.runtime = Runtime(CHAT, TOPIC, frozenset({101, 102}), self.path, '127.0.0.1', 8080)
        self.store = Store(self.path)
        self.store.acquire_lease()
        self.addCleanup(self.store.close)
        self.store.bind(BOT_ID, CHAT, TOPIC, NOW)
        self.client = FakeClient()
        self.now = NOW
        self.bot = Bot(self.runtime, self.client, BOT_ID, 'example_care_bot', self.store, lambda: self.now)
        self.bot.dispatch_once()
        self.panel = next(iter(self.store.snapshot()['inline_panels'].values()))
        self.key = self.panel['task_ids'][0]

    def cb(self, uid=10, index=0):
        return {'update_id': uid, 'callback_query': {
            'id': 'q' + str(uid), 'data': inline_ui.PREFIX + self.panel['nonce'] + ':' + str(index),
            'from': {'id': 101, 'is_bot': False, 'username': 'untrusted_display_name'},
            'message': {'message_id': self.panel['pages'][-1]['message_id'],
                        'chat': {'id': CHAT}, 'message_thread_id': TOPIC,
                        'from': {'id': BOT_ID, 'is_bot': True}}}}

    def msg(self, text, uid=10):
        return {'update_id': uid, 'message': {
            'message_id': uid + 500, 'chat': {'id': CHAT}, 'message_thread_id': TOPIC,
            'from': {'id': 101, 'is_bot': False}, 'text': text}}

    def sends(self):
        return [p for m, p in self.client.calls if m == 'sendMessage']

    def test_callback_username_is_display_only_and_keeps_numeric_audit(self):
        self.bot.process_update(self.cb())
        self.bot.sync_panels(NOW)
        saved = self.store.snapshot()
        self.assertEqual(saved['tasks'][self.key]['done_by'], 'telegram:101')
        self.assertEqual(saved['audit'][-1]['actor'], 'telegram:101')
        self.assertEqual(saved['audit'][-1]['telegram_user_id'], '101')
        edits = [payload for method, payload in self.client.calls if method == 'editMessageText']
        self.assertIn('@untrusted_display_name', edits[-1]['text'])
        self.assertNotIn('telegram:101', edits[-1]['text'])

    def test_command_and_legacy_button_use_username_without_changing_identity(self):
        for uid, use_button in ((10, False), (11, True)):
            key = self.panel['task_ids'][uid - 10]
            text = (care.button_text(self.store.snapshot()['tasks'][key], legacy=True)
                    if use_button else '/done ' + key)
            update = self.msg(text, uid)
            update['message']['from']['username'] = 'caregiver_a'
            self.bot.process_update(update)
            saved = self.store.snapshot()
            reply = ''.join(saved['standalone']['outbox']['update:' + str(uid)]['texts'])
            self.assertIn('@caregiver_a', reply)
            self.assertNotIn('telegram:101', reply)
            self.assertEqual(saved['tasks'][key]['done_by'], 'telegram:101')

    def test_skip_and_move_show_username_and_preserve_transfer(self):
        for uid, action in ((10, 'next:0'), (11, 'next:2')):
            update = self.cb(uid)
            update['callback_query']['data'] = inline_ui.PREFIX + self.panel['nonce'] + ':' + action
            self.bot.process_update(update)
        self.bot.process_update(self.msg('/skip ' + self.panel['task_ids'][1], 12))
        # The no-username command refresh removes the handle. Telegram name is the fallback.
        update = self.msg('/status', 13)
        update['message']['from']['username'] = 'caregiver_a'
        self.bot.process_update(update)
        self.bot.sync_panels(NOW)
        state = self.store.snapshot()
        text = inline_ui.page_text(state, state['inline_panels'][self.panel['nonce']], self.panel['pages'][0])
        self.assertIn('Пропущено:', text)
        self.assertIn('@caregiver_a', text)
        self.assertNotIn('telegram:101', text)
        self.assertTrue(state['carryovers'])
        self.assertEqual(state['tasks'][self.key]['skip_by'], 'telegram:101')

    def test_startup_profile_lookup_updates_old_panels_without_changing_care(self):
        self.bot.process_update(self.cb())
        before = self.store.snapshot()
        original = self.client.call
        def call(method, payload=None):
            if method == 'getChatMember':
                return {'user': {'id': payload['user_id'], 'is_bot': False,
                                 'username': 'caregiver_' + str(payload['user_id'])}}
            return original(method, payload)
        with patch.object(self.client, 'call', side_effect=call):
            self.bot.refresh_caregivers()
        after = self.store.snapshot()
        self.assertEqual({k: v for k, v in after.items() if k != 'caregivers'},
                         {k: v for k, v in before.items() if k != 'caregivers'})
        self.assertEqual(care.display_actor(after, 'telegram:101'), '@caregiver_101')
        self.bot.sync_panels(NOW)
        edits = [payload for method, payload in self.client.calls if method == 'editMessageText']
        self.assertIn('@caregiver_101', edits[-1]['text'])
        self.assertNotIn('telegram:101', edits[-1]['text'])

    def test_failed_or_mismatched_member_lookup_preserves_cached_names_and_state(self):
        self.bot.process_update(self.cb())
        before = self.store.snapshot()
        def call(method, payload=None):
            if payload['user_id'] == 101:
                raise TelegramError(403)
            return {'user': {'id': 101, 'is_bot': False, 'username': 'wrong_user'}}
        with patch.object(self.client, 'call', side_effect=call):
            self.bot.refresh_caregivers()
        self.assertEqual(before, self.store.snapshot())

    def test_spoofed_username_cannot_replace_the_author_profile(self):
        self.bot.process_update(self.cb())
        before = self.store.snapshot()
        update = self.cb(11)
        update['callback_query']['from'].update(id=999, username='untrusted_display_name')
        self.bot.process_update(update)
        after = self.store.snapshot()
        self.assertEqual(before['caregivers'], after['caregivers'])
        self.assertEqual(before['tasks'], after['tasks'])

    def test_commit_precedes_ack_and_survives_reload(self):
        def assert_saved():
            saved = self.store.snapshot()
            self.assertEqual(saved['tasks'][self.key]['status'], 'done')
            self.assertEqual(saved['tasks'][self.key]['grams'], 49)
            self.assertEqual(saved['tasks'][self.key]['done_by'], 'telegram:101')
            self.assertEqual(saved['standalone']['offset'], 11)
            self.assertIn('q10', saved['inline_receipts'])
        self.client.before_ack = assert_saved
        self.bot.process_update(self.cb())
        assert_saved()
        before = self.store.snapshot()
        self.bot.process_update(self.cb())
        self.assertEqual(before, self.store.snapshot())
        self.now = care.instant('2026-10-19T07:30:00+01:00')
        self.bot.dispatch_once()
        latest = self.store.snapshot()
        repeats = latest['emissions'][latest['last_emission_id']]['repeat_ids']
        self.assertNotIn(self.key, repeats)
        self.assertEqual(len(repeats), 4)

    def test_ack_failure_and_edit_failure_do_not_undo_care(self):
        self.bot.process_update(self.cb())
        self.client.edit_error = TelegramError(503, uncertain=True)
        self.bot.dispatch_once()
        saved = self.store.snapshot()
        self.assertEqual(saved['tasks'][self.key]['status'], 'done')
        self.assertIn('edit_retry_at', saved['inline_panels'][self.panel['nonce']])
        self.client.edit_error = None
        with self.store.transaction() as state:
            state['inline_panels'][self.panel['nonce']]['edit_retry_at'] = 0
        self.bot.dispatch_once()
        saved = self.store.snapshot()
        panel = saved['inline_panels'][self.panel['nonce']]
        self.assertEqual(panel['last_digest'], inline_ui.digest(saved, panel))

    def test_save_failure_never_acknowledges(self):
        before = self.path.read_bytes()
        self.client.calls.clear()
        with patch.object(care, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.bot.process_update(self.cb())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.client.calls, [])

    def test_callback_scope_and_numeric_identity(self):
        mutations = [
            lambda c: c['from'].update(id=999),
            lambda c: c['from'].update(id='101'),
            lambda c: c['from'].update(is_bot=True),
            lambda c: c['message']['chat'].update(id=-2),
            lambda c: c['message'].update(message_thread_id=43),
            lambda c: c['message'].update(message_id=9999),
            lambda c: c['message']['from'].update(id=9876),
            lambda c: c['message']['from'].update(is_bot=False),
            lambda c: c.update(data=inline_ui.PREFIX + '0' * 16 + ':0'),
        ]
        before = self.store.snapshot()['tasks']
        for uid, mutation in enumerate(mutations, 20):
            update = self.cb(uid)
            mutation(update['callback_query'])
            self.bot.process_update(update)
            self.assertEqual(before, self.store.snapshot()['tasks'])
        self.assertEqual(self.store.offset(), 29)

    def test_future_task_rejected_by_callback_and_commands(self):
        self.now = care.instant('2026-10-19T06:59:59+01:00')
        self.bot.process_update(self.cb())
        for uid, command in enumerate(('done', 'skip', 'snooze'), 11):
            tail = ' 30' if command == 'snooze' else ''
            self.bot.process_update(self.msg('/' + command + ' ' + self.key + tail, uid))
        self.assertEqual(self.store.snapshot()['tasks'][self.key]['status'], 'pending')

    def test_exact_legacy_fallback_and_original_grams_and_provenance(self):
        label = care.button_text(self.store.snapshot()['tasks'][self.key], legacy=True)
        self.bot.process_update(self.msg(label))
        saved = self.store.snapshot()
        self.assertEqual(sum(t['status'] == 'done' for t in saved['tasks'].values()), 1)
        self.assertEqual(saved['tasks'][self.key]['grams'], 49)
        self.assertEqual(saved['tasks'][self.key]['done_by'], 'telegram:101')
        later = self.msg('/done ' + self.key, 11)
        later['message']['from']['id'] = 102
        self.bot.process_update(later)
        self.assertEqual(self.store.snapshot()['tasks'][self.key], saved['tasks'][self.key])

    def test_ambiguous_changed_grams_and_wrong_bot_text_never_complete(self):
        label = care.button_text(self.store.snapshot()['tasks'][self.key], legacy=True)
        for uid, text in enumerate(('done', 'готово', 'сделано', label.replace('49 г', '98 г'),
                                     label.replace('@example_care_bot', '@other_care_bot'),
                                     '/done ' + self.key + ' extra', '/done@other_care_bot ' + self.key), 10):
            self.bot.process_update(self.msg(text, uid))
        self.assertTrue(all(t['status'] == 'pending' for t in self.store.snapshot()['tasks'].values()))

    def test_ignore_bot_posts_and_wrong_chat_topic_or_sender(self):
        mutations = [lambda m: m['from'].update(is_bot=True),
                     lambda m: m['from'].update(id=999),
                     lambda m: m['chat'].update(id=-2),
                     lambda m: m.update(message_thread_id=43),
                     lambda m: m.update(sender_chat={'id': CHAT}),
                     lambda m: m.update(via_bot={'id': BOT_ID}),
                     lambda m: m.update(forward_origin={'type': 'user'})]
        before = self.store.snapshot()['tasks']
        for uid, mutate in enumerate(mutations, 10):
            update = self.msg('/done ' + self.key, uid)
            mutate(update['message'])
            self.bot.process_update(update)
        self.assertEqual(before, self.store.snapshot()['tasks'])
        self.assertEqual(self.store.offset(), 17)

    def test_status_health_hold_skip_and_snooze(self):
        self.bot.process_update(self.msg('/status@example_care_bot'))
        self.bot.process_update(self.msg('/health_hold upset stomach', 11))
        self.bot.process_update(self.msg('/snooze ' + self.key + ' 60', 12))
        dry = self.key.removesuffix('wet') + 'dry'
        self.bot.process_update(self.msg('/skip ' + dry, 13))
        state = self.store.snapshot()
        self.assertEqual(state['progression_hold']['actor'], 'telegram:101')
        self.assertEqual(state['tasks'][self.key]['status'], 'deferred')
        self.assertEqual(state['tasks'][dry]['skip_by'], 'telegram:101')
        self.assertEqual(state['tasks'][self.key]['grams'], 49)
        status = ''.join(state['standalone']['outbox']['update:10']['texts'])
        self.assertTrue(status.startswith('Acorn: '))
        self.assertIn('/done YYYY', status)

    def test_unknown_send_outcome_is_never_replayed(self):
        self.bot.process_update(self.msg('/status'))
        self.client.send_error = TelegramError(uncertain=True)
        self.bot.dispatch_once()
        self.assertEqual(self.store.snapshot()['standalone']['outbox']['update:10']['status'], 'needs_review')
        count = len(self.sends())
        self.client.send_error = None
        self.bot.dispatch_once()
        self.assertEqual(len(self.sends()), count)
        self.store.bind(BOT_ID, CHAT, TOPIC, NOW)
        self.bot.dispatch_once()
        self.assertEqual(len(self.sends()), count)

    def test_definite_429_is_retried_but_not_in_a_busy_loop(self):
        self.bot.process_update(self.msg('/status'))
        self.client.send_error = TelegramError(429, retry_after=10)
        self.bot.dispatch_once()
        count = len(self.sends())
        self.assertEqual(self.store.snapshot()['standalone']['outbox']['update:10']['status'], 'pending')
        self.bot.dispatch_once()
        self.assertEqual(len(self.sends()), count)
        self.client.send_error = None
        with self.store.transaction() as state:
            state['standalone']['outbox']['update:10']['retry_at'] = 0
        self.bot.dispatch_once()
        self.assertGreater(len(self.sends()), count)
        self.assertEqual(self.store.snapshot()['standalone']['outbox']['update:10']['status'], 'sent')

    def test_interrupted_sending_requires_review_and_bot_change_is_refused(self):
        with self.store.transaction() as state:
            state['standalone']['outbox']['interrupted'] = {'status': 'sending', 'panel': None}
        self.store.bind(BOT_ID, CHAT, TOPIC, NOW)
        self.assertEqual(self.store.snapshot()['standalone']['outbox']['interrupted']['status'], 'needs_review')
        before = self.path.read_bytes()
        for identity_args in ((999, CHAT, TOPIC), (BOT_ID, -2, TOPIC), (BOT_ID, CHAT, 43)):
            with self.assertRaises(ValueError):
                self.store.bind(*identity_args, NOW)
            self.assertEqual(self.path.read_bytes(), before)

    def test_one_poller_requests_only_new_bot_updates_and_saves_offset(self):
        self.client.updates = [self.msg('/done ' + self.key, 30), {'update_id': 31, 'channel_post': {}}]
        self.bot.poll_once()
        calls = [p for m, p in self.client.calls if m == 'getUpdates']
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['timeout'], 30)
        self.assertEqual(calls[0]['offset'], 0)
        self.assertEqual(self.store.offset(), 32)
        self.client.updates = []
        self.bot.poll_once()
        self.assertEqual(self.client.calls[-1][1]['offset'], 32)

    def test_competing_poller_stops_service(self):
        with patch.object(self.client, 'call', side_effect=TelegramError(409)):
            self.bot.poll_loop()
        self.assertTrue(self.bot.stop.is_set())

    def test_mount_is_not_reset_and_exclusive_process_lease(self):
        before = self.store.snapshot()['config']
        self.bot.process_update(self.cb())
        with patch.object(care, 'BOOT_CONFIG', {'not': 'used'}):
            self.store.bind(BOT_ID, CHAT, TOPIC, NOW)
        self.assertEqual(self.store.snapshot()['config'], before)
        self.assertEqual(self.store.snapshot()['tasks'][self.key]['status'], 'done')
        other = Store(self.path)
        with self.assertRaises(ValueError):
            other.acquire_lease()

    def test_bare_existing_state_and_corrupt_json_are_not_replaced(self):
        path = self.path.parent / 'other.json'
        path.write_text(json.dumps(care.initial()))
        store = Store(path)
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            store.bind(BOT_ID, CHAT, TOPIC, NOW)
        self.assertEqual(path.read_bytes(), before)
        path.write_text('{bad')
        with self.assertRaises(ValueError):
            store.bind(BOT_ID, CHAT, TOPIC, NOW)
        self.assertEqual(path.read_text(), '{bad')

    def test_liveness_readiness_http_without_state_or_identity_details(self):
        runtime = Runtime(CHAT, TOPIC, frozenset({101}), self.path, '127.0.0.1', 0)
        bot = Bot(runtime, self.client, BOT_ID, 'example_care_bot', self.store)
        server = health_server(bot)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = 'http://127.0.0.1:' + str(server.server_port)
            with urllib.request.urlopen(base + '/livez') as response:
                self.assertEqual(response.read(), b'ok\n')
            with self.assertRaises(urllib.error.HTTPError) as failure:
                urllib.request.urlopen(base + '/readyz')
            self.assertEqual(failure.exception.code, 503)
            bot.last_poll = bot.last_worker = time.monotonic()
            with urllib.request.urlopen(base + '/readyz') as response:
                self.assertEqual(response.read(), b'ok\n')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class ConfigurationAndTransportTests(unittest.TestCase):
    def test_acorn_environment_sets_state_and_health_listener(self):
        env = {'TELEGRAM_CHAT_ID': str(CHAT), 'TELEGRAM_TOPIC_ID': str(TOPIC),
               'TELEGRAM_ALLOWED_USER_IDS': '101, 102',
               'ACORN_STATE_PATH': '/data/acorn-state.json',
               'ACORN_HTTP_HOST': '127.0.0.1', 'ACORN_HTTP_PORT': '9090'}
        with patch.dict(os.environ, env, clear=True):
            runtime = Runtime.from_env()
        self.assertEqual(runtime.state_path, Path('/data/acorn-state.json'))
        self.assertEqual(runtime.http_host, '127.0.0.1')
        self.assertEqual(runtime.http_port, 9090)
        self.assertEqual(runtime.allowed_ids, {101, 102})
        self.assertEqual((runtime.chat_id, runtime.topic_id), (CHAT, TOPIC))

    def test_numeric_ids_are_explicit_and_required(self):
        env = {'TELEGRAM_CHAT_ID': str(CHAT), 'TELEGRAM_TOPIC_ID': str(TOPIC),
               'TELEGRAM_ALLOWED_USER_IDS': '101, 102'}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(Runtime.from_env().allowed_ids, {101, 102})
        for value in ('', '@caregiver', '101,*', '0', '-1', '101.0'):
            with patch.dict(os.environ, {**env, 'TELEGRAM_ALLOWED_USER_IDS': value}, clear=True):
                with self.assertRaises(ValueError):
                    Runtime.from_env()
        for missing in ('TELEGRAM_CHAT_ID', 'TELEGRAM_TOPIC_ID', 'TELEGRAM_ALLOWED_USER_IDS'):
            with patch.dict(os.environ, {k: v for k, v in env.items() if k != missing}, clear=True):
                with self.assertRaises(ValueError):
                    Runtime.from_env()

    def test_missing_token_fails_and_file_has_precedence(self):
        with tempfile.TemporaryDirectory(dir=TMP) as directory:
            path = Path(directory) / 'new-bot-token'
            synthetic = '999999:synthetic_test_value_only'
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(ValueError):
                    read_token()
            with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': synthetic,
                                         'TELEGRAM_BOT_TOKEN_FILE': str(path)}, clear=True):
                with self.assertRaises(ValueError):
                    read_token()
                path.write_text(synthetic + '\n')
                self.assertEqual(read_token(), synthetic)
            state = Path(directory) / 'absent-state.json'
            env = {k: v for k, v in os.environ.items() if not k.startswith('TELEGRAM_')}
            env.update(ACORN_STATE_PATH=str(state), PYTHONDONTWRITEBYTECODE='1')
            process = subprocess.run([sys.executable, '-m', 'service'], cwd=ROOT, env=env, capture_output=True)
            self.assertEqual(process.returncode, 1)
            self.assertFalse(state.exists())
            self.assertNotIn(synthetic.encode(), process.stderr)

    def test_config_and_state_environment_resolve_before_care_globals(self):
        with tempfile.TemporaryDirectory(dir=TMP) as directory:
            config = load_care_config(ROOT / 'config.example.json')
            config['wet_can_grams'] = 100
            config['timezone'] = 'UTC'
            path = Path(directory) / 'custom.json'
            path.write_text(json.dumps(config))
            state = Path(directory) / 'custom-state.json'
            env = {**os.environ, 'ACORN_CONFIG_PATH': str(path), 'ACORN_STATE_PATH': str(state),
                   'PYTHONDONTWRITEBYTECODE': '1'}
            result = subprocess.run([sys.executable, '-c',
                'import care; print(care.STATE, care.TZ, care.DEFAULT_WET_CAN_GRAMS, care.initial()["config"]["wet_can_grams"])'],
                cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(state) + ' UTC 100 100')
            self.assertFalse(state.exists())

    def test_webhook_is_rejected_not_deleted(self):
        client = FakeClient()
        client.webhook = 'https://example.invalid/webhook'
        with self.assertRaises(ValueError):
            identity(client, threading.Event())
        self.assertEqual([m for m, _ in client.calls], ['getMe', 'getWebhookInfo'])

    def test_bootstrap_retries_transient_read_errors(self):
        client = FakeClient()
        calls = []
        original = client.call
        def flaky(method, payload=None):
            calls.append(method)
            if len(calls) == 1:
                raise TelegramError(503, uncertain=True)
            return original(method, payload)
        client.call = flaky
        stop = threading.Event()
        with patch.object(stop, 'wait', return_value=False):
            self.assertEqual(identity(client, stop)['id'], BOT_ID)
        self.assertEqual(calls, ['getMe', 'getMe', 'getWebhookInfo'])

    def test_real_transport_supports_read_only_member_lookup(self):
        class Opener:
            def open(self, request, timeout):
                self.request = request
                return io.BytesIO(b'{"ok":true,"result":{"user":{"id":101,"is_bot":false,"username":"caregiver_a"}}}')
        opener = Opener()
        result = Client('synthetic', opener).call('getChatMember', {'chat_id': CHAT, 'user_id': 101})
        self.assertEqual(result['user']['username'], 'caregiver_a')
        self.assertTrue(opener.request.full_url.endswith('/getChatMember'))
        self.assertEqual(json.loads(opener.request.data), {'chat_id': CHAT, 'user_id': 101})
        self.assertEqual(opener.request.method, 'POST')

    def test_transport_handles_fake_http_and_redacts_all_exception_context(self):
        token = '999999:synthetic_test_value_only'
        class Opener:
            def open(self, request, timeout):
                self.request = request
                return io.BytesIO(b'{"ok":true,"result":{"id":12345}}')
        opener = Opener()
        self.assertEqual(Client(token, opener).call('getMe'), {'id': 12345})
        self.assertEqual(opener.request.method, 'POST')
        self.assertEqual(json.loads(opener.request.data), {})
        with patch.object(opener, 'open', side_effect=OSError('https://api.telegram.org/bot' + token)):
            with self.assertRaises(TelegramError) as error:
                Client(token, opener).call('sendMessage', {'text': 'private'})
        self.assertNotIn(token, str(error.exception))
        self.assertTrue(error.exception.uncertain)
        self.assertTrue(error.exception.__suppress_context__)
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.invalid'))

    def test_transport_429_and_malformed_payload(self):
        class Opener:
            def open(self, request, timeout):
                raise urllib.error.HTTPError('redacted', 429, '', {}, io.BytesIO(
                    b'{"ok":false,"error_code":429,"parameters":{"retry_after":9}}'))
        with self.assertRaises(TelegramError) as error:
            Client('synthetic', Opener()).call('sendMessage')
        self.assertEqual(error.exception.retry_after, 9)
        self.assertFalse(error.exception.uncertain)
        with patch.object(Opener, 'open', return_value=io.BytesIO(b'not json')):
            with self.assertRaises(TelegramError) as failure:
                Client('synthetic', Opener()).call('sendMessage')
        self.assertTrue(failure.exception.uncertain)

    def test_utf16_chunks_are_lossless_and_fit(self):
        text = ('🐾 line\n' * 2000) + ('🐾' * 4000)
        parts = chunks(text)
        self.assertEqual(''.join(parts), text)
        self.assertTrue(all(len(p.encode('utf-16-le')) // 2 <= 3500 for p in parts))

    def test_explicit_migration_preserves_every_care_record_and_discards_transport(self):
        state = care.initial()
        care.tick(state, NOW)
        cid = next(iter(state['checklists']))
        key = state['checklists'][cid]['task_ids'][0]
        care.complete_button(state, care.button_text(state['tasks'][key]), 'original_actor', 'historic', NOW)
        state['progression_hold'] = {'stage': 'weeks_1_2', 'since': care.iso(NOW), 'actor': 'original_actor'}
        state['inline_panels'] = {'old': {'message_id': 777}}
        state['inline_receipts'] = {'old': {}}
        state['checklists'][cid]['original_message_id'] = '777'
        original = copy.deepcopy(state)
        imported = migrate.migrate(state, 101, NOW)
        self.assertEqual(state, original)
        self.assertEqual(imported['tasks'], original['tasks'])
        self.assertEqual(imported['config'], original['config'])
        self.assertEqual(imported['progression_hold'], original['progression_hold'])
        self.assertEqual(imported['audit'][:-1], original['audit'])
        self.assertEqual(imported['tasks'][key]['done_by'], 'original_actor')
        self.assertEqual(imported['tasks'][key]['grams'], 49)
        self.assertEqual(imported['standalone']['offset'], 0)
        self.assertIsNone(imported['standalone']['identity'])
        self.assertIsNone(imported['checklists'][cid]['original_message_id'])
        for field in ('inline_panels', 'inline_receipts', 'emissions', 'last_emission_id'):
            self.assertNotIn(field, imported)


if __name__ == '__main__':
    unittest.main()
