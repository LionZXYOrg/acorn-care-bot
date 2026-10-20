"""Offline deployment contracts for the current gateway implementation.

Only the Linux/root subprocess cases open sockets. They copy four production
modules into a public temporary directory, drop all gateway/worker identities,
and forbid Internet sockets. No token, worker service, or real API is involved.
"""
import contextlib
import copy
import json
import os
from pathlib import Path
import queue
import selectors
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from gateway import Gateway, validate_config
from gateway_client import encode
from telegram_api import TelegramError
from test_gateway import (
    ACORN_TOPIC, BINS_TOPIC, BOT, CHAT, USER, FakeUpstream, callback,
    config_at, message,
)


ACORN_UID = 10001
GREENWICH_UID = 10002
GATEWAY_UID = 10003
SHARED_GID = 10001
FUTURE_UID = 10004
FUTURE_TOPIC = 44
DATE = 1700000000
COUNCIL_URL = 'https://www.royalgreenwich.gov.uk/bins-rubbish-and-recycling'
SOURCE = Path(__file__).resolve().parents[1]


def production_config_at(directory):
    """Use production worker identities, with paths redirected to disposable data."""
    config = config_at(directory)
    config['state_path'] = str(Path(directory) / 'gateway' / 'private' / 'gateway.sqlite3')
    for worker, wid, uid in zip(config['workers'], ('acorn', 'greenwich'),
                                (ACORN_UID, GREENWICH_UID)):
        worker['id'] = wid
        worker['uid'] = uid
        worker['socket_path'] = str(Path(directory) / wid / 'private' / 'telegram.sock')
    return config


class NativeReceiptUpstream(FakeUpstream):
    """The shared fake, extended with native message receipts and admin rights."""
    def __init__(self):
        super().__init__()
        self.permissions = {'can_pin_messages': True, 'can_delete_messages': True}
        self.receipt_topics = {}
        self.call_lock = threading.Lock()

    def call(self, method, payload=None):
        with self.call_lock:
            result = super().call(method, payload)
            if method == 'getChatMember':
                result.update(status='administrator', can_invite_users=True,
                              custom_title='not part of the gateway contract',
                              **self.permissions)
            elif method in {'sendMessage', 'sendRichMessage',
                            'editMessageText', 'editMessageReplyMarkup'}:
                mid = result['message_id']
                topic = payload.get('message_thread_id',
                                    self.receipt_topics.get(mid, ACORN_TOPIC))
                result.update(date=DATE, message_thread_id=topic)
                if 'text' in payload:
                    result['text'] = payload['text']
                if 'rich_message' in payload:
                    result['rich_message'] = copy.deepcopy(payload['rich_message'])
                if method in {'sendMessage', 'sendRichMessage'}:
                    self.receipt_topics[mid] = topic
                    self.mid += 1
            return result


class HoldingUpstream(NativeReceiptUpstream):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def call(self, method, payload=None):
        if method == 'sendMessage' and payload.get('text') == 'hold-acorn':
            self.entered.set()
            if not self.release.wait(10):
                raise RuntimeError('Offline test failed to release its fake upstream')
        return super().call(method, payload)


class GatewayRuntimeContractTests(unittest.TestCase):
    """Durability and API tests use only in-process fakes, never a root socket."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gw-contract-')
        self.addCleanup(self.temp.cleanup)
        self.config = production_config_at(self.temp.name)
        self.upstream = NativeReceiptUpstream()
        self.gw = self.new_gateway()

    def new_gateway(self, config=None, upstream=None):
        gateway = Gateway(config if config is not None else self.config,
                          upstream if upstream is not None else self.upstream)
        self.addCleanup(gateway.close)
        return gateway

    def restart(self, config=None):
        self.gw.close()
        self.gw = self.new_gateway(config)
        return self.gw

    def error(self, code, fn, *args, **kwargs):
        with self.assertRaises(TelegramError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertFalse(caught.exception.uncertain)
        return caught.exception

    def send(self, worker='acorn', text='offline message'):
        return self.gw.call(worker, 'sendMessage', {'chat_id': CHAT, 'text': text})

    def owners(self):
        with self.gw.state.mutex:
            return self.gw.state.db.execute(
                'SELECT chat,mid,topic,worker FROM owners ORDER BY mid').fetchall()

    def meta(self):
        with self.gw.state.mutex:
            return dict(self.gw.state.db.execute('SELECT key,value FROM meta'))

    def legacy(self, **changes):
        result = {'schema': 1, 'worker': 'acorn', 'bot_id': BOT,
                  'chat_id': CHAT, 'topic_id': ACORN_TOPIC,
                  'offset': 40, 'message_ids': [500, 501, 502]}
        result.update(changes)
        return result

    def snapshot(self, value):
        path = Path(self.temp.name) / 'legacy.json'
        # json.dumps deliberately also permits non-finite test inputs. The
        # production strict parser, not this fixture, must reject those inputs.
        path.write_text(json.dumps(value), encoding='utf-8')
        path.chmod(0o600)
        return path

    def assert_bad_binding(self, config):
        before = len(self.upstream.calls)
        with self.assertRaises(ValueError):
            candidate = Gateway(config, self.upstream)
            candidate.close()  # Release the lease even if a regression accepts it.
        self.assertEqual(len(self.upstream.calls), before,
                         'Durable worker binding must be checked before upstream calls')

    def test_production_workers_need_no_legacy_list_or_root_worker_identity(self):
        self.assertEqual([(w['id'], w['uid']) for w in self.config['workers']],
                         [('acorn', ACORN_UID), ('greenwich', GREENWICH_UID)])
        self.assertNotIn('legacy_path', self.config)
        for worker in self.config['workers']:
            self.assertNotIn('legacy_message_ids', worker)
        normalized = validate_config(self.config)
        self.assertEqual([w['legacy_message_ids'] for w in normalized['workers']], [[], []])
        self.assertEqual(self.owners(), [])
        self.assertEqual(self.gw.state.offset(), 0)
        for worker, topic in (('acorn', ACORN_TOPIC), ('greenwich', BINS_TOPIC)):
            with self.subTest(worker=worker):
                self.assertEqual(self.gw.call(worker, 'getMe')['id'], BOT)
                result = self.send(worker)
                self.assertEqual(result['message_thread_id'], topic)
                self.assertEqual(result['date'], DATE)
        self.gw.ingest([message(10, BINS_TOPIC)])
        self.assertEqual(self.gw.call('greenwich', 'getUpdates', {'offset': 0}), [])
        self.assertEqual(self.gw.state.offset(), 11)

    def test_future_module_does_not_reset_offsets_acks_queues_receipts_or_callbacks(self):
        acorn_mid = self.send()['message_id']
        greenwich_mid = self.send('greenwich')['message_id']
        self.gw.ingest([message(10), message(11), callback(12, mid=acorn_mid)])
        delivered = self.gw.call('acorn', 'getUpdates', {'offset': 11})
        self.assertEqual([item['update_id'] for item in delivered], [11, 12])
        owners = self.owners()
        original_meta = self.meta()
        self.assertEqual(original_meta['offset'], '13')
        self.assertEqual(original_meta['ack:acorn'], '11')

        expanded = copy.deepcopy(self.config)
        future = copy.deepcopy(expanded['workers'][1])
        future.update(id='future', uid=FUTURE_UID, topic_id=FUTURE_TOPIC,
                      receives_updates=True,
                      socket_path=str(Path(self.temp.name) / 'future' / 'private' / 'telegram.sock'))
        # Neither changing registry order nor adding a worker is a migration.
        expanded['workers'] = [future, *reversed(expanded['workers'])]
        self.restart(expanded)
        self.assertEqual(self.gw.state.offset(), 13)
        self.assertEqual(self.owners(), owners)
        for key, value in original_meta.items():
            self.assertEqual(self.meta()[key], value, key)
        self.assertEqual(self.meta()['ack:future'], '0')
        self.assertEqual(self.gw.call('future', 'getUpdates'), [])
        self.assertEqual(self.gw.call('acorn', 'getUpdates', {'offset': 11}), delivered)
        self.error(400, self.gw.call, 'acorn', 'getUpdates', {'offset': 0})
        self.assertTrue(self.gw.call('acorn', 'answerCallbackQuery',
                                    {'callback_query_id': 'callback-12'}))
        for worker, mid in (('acorn', acorn_mid), ('greenwich', greenwich_mid)):
            self.assertTrue(self.gw.call(worker, 'pinChatMessage',
                                        {'chat_id': CHAT, 'message_id': mid}))
            self.error(403, self.gw.call, 'future', 'deleteMessage',
                       {'chat_id': CHAT, 'message_id': mid})

        self.upstream.updates = [message(14, FUTURE_TOPIC)]
        self.gw.poll_once()
        self.assertEqual(self.upstream.calls[-1], ('getUpdates', {
            'offset': 13, 'timeout': 20, 'limit': 100,
            'allowed_updates': ['message', 'callback_query']}))
        self.assertEqual(self.gw.state.offset(), 15)
        self.assertEqual([u['update_id'] for u in self.gw.call('future', 'getUpdates')], [14])
        self.assertEqual(self.gw.call('acorn', 'getUpdates', {'offset': 11}), delivered)

    def test_changed_module_uid_chat_topic_or_name_is_rejected_without_state_damage(self):
        self.send()
        self.gw.ingest([message(8)])
        before_meta, before_owners = self.meta(), self.owners()
        self.gw.close()
        for key, value in (('uid', FUTURE_UID), ('chat_id', CHAT - 1),
                           ('topic_id', FUTURE_TOPIC), ('id', 'renamed-acorn')):
            with self.subTest(changed=key):
                changed = copy.deepcopy(self.config)
                changed['workers'][0][key] = value
                self.assert_bad_binding(changed)
        self.gw = self.new_gateway()
        self.assertEqual(self.meta(), before_meta)
        self.assertEqual(self.owners(), before_owners)
        self.assertEqual([u['update_id'] for u in self.gw.call('acorn', 'getUpdates')], [8])

    def test_retired_uid_or_route_cannot_be_reused_but_original_worker_can_return(self):
        self.send()
        self.gw.ingest([message(9)])
        original = copy.deepcopy(self.config)
        reduced = copy.deepcopy(original)
        reduced['workers'] = [reduced['workers'][1]]
        self.restart(reduced)
        self.assertIn('worker:acorn', self.meta())
        self.assertEqual(self.gw.state.offset(), 10)
        self.gw.close()
        for uid, topic in ((ACORN_UID, FUTURE_TOPIC), (FUTURE_UID, ACORN_TOPIC)):
            with self.subTest(uid=uid, topic=topic):
                attempted = copy.deepcopy(reduced)
                replacement = copy.deepcopy(original['workers'][0])
                replacement.update(id='replacement', uid=uid, topic_id=topic,
                                   socket_path=str(Path(self.temp.name) / 'replacement.sock'))
                attempted['workers'].append(replacement)
                self.assert_bad_binding(attempted)
        self.gw = self.new_gateway(original)
        self.assertEqual(self.gw.state.offset(), 10)
        self.assertEqual([u['update_id'] for u in self.gw.call('acorn', 'getUpdates')], [9])
        self.assertTrue(self.gw.call('acorn', 'deleteMessage',
                                    {'chat_id': CHAT, 'message_id': 700}))

    def test_legacy_import_checks_saved_bot_chat_and_topic_before_copying_anything(self):
        for key, value in (('bot_id', BOT + 1), ('chat_id', CHAT - 1),
                           ('topic_id', BINS_TOPIC), ('worker', 'greenwich'),
                           ('bot_id', True), ('schema', True)):
            with self.subTest(field=key, value=value):
                path = self.snapshot(self.legacy(**{key: value}))
                with self.assertRaises(ValueError):
                    self.gw.import_legacy(path)
                self.assertEqual(self.gw.state.offset(), 0)
                self.assertEqual(self.owners(), [])
                self.assertNotIn('legacy_imported', self.meta())
        # Failed validation must not burn the one valid import opportunity.
        self.gw.import_legacy(self.snapshot(self.legacy()))
        self.assertEqual(self.gw.state.offset(), 40)
        self.assertEqual(self.meta()['ack:acorn'], '40')
        self.assertEqual(self.meta()['ack:greenwich'], '0')
        self.assertEqual(self.owners(), [(CHAT, mid, ACORN_TOPIC, 'acorn')
                                         for mid in (500, 501, 502)])

    def test_legacy_import_requires_a_finite_explicit_receipt_set_and_integer_offset(self):
        invalid = [
            {'message_ids': [500, 500]}, {'message_ids': [0]},
            {'message_ids': [-1]}, {'message_ids': [True]},
            {'message_ids': [500.0]}, {'message_ids': [float('inf')]},
            {'message_ids': '*'}, {'offset': -1}, {'offset': True},
            {'offset': 40.0}, {'offset': float('nan')},
            {'offset': float('inf')}, {'unexpected_receipts': [999]},
        ]
        for changes in invalid:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.gw.import_legacy(self.snapshot(self.legacy(**changes)))
                self.assertEqual(self.gw.state.offset(), 0)
                self.assertEqual(self.owners(), [])
                self.assertNotIn('legacy_imported', self.meta())

    def test_imported_old_acorn_panel_supports_edit_and_delivered_callback_only(self):
        self.gw.import_legacy(self.snapshot(self.legacy()))
        result = self.gw.call('acorn', 'editMessageText', {
            'chat_id': CHAT, 'message_id': 500, 'text': '<b>Old Acorn panel</b>',
            'parse_mode': 'HTML'})
        self.assertEqual((result['message_id'], result['date']), (500, DATE))
        self.assertEqual(result['message_thread_id'], ACORN_TOPIC)
        for worker, mid in (('acorn', 499), ('acorn', 503), ('greenwich', 500)):
            self.error(403, self.gw.call, worker, 'editMessageText',
                       {'chat_id': CHAT, 'message_id': mid, 'text': 'not owned'})
        self.gw.ingest([callback(41, mid=500), callback(42, mid=999),
                        callback(43, mid=500, topic=BINS_TOPIC)])
        self.error(403, self.gw.call, 'acorn', 'answerCallbackQuery',
                   {'callback_query_id': 'callback-41'})
        delivered = self.gw.call('acorn', 'getUpdates', {'offset': 40})
        self.assertEqual([u['update_id'] for u in delivered], [41])
        self.assertEqual(delivered[0]['callback_query']['message']['message_id'], 500)
        self.assertTrue(self.gw.call('acorn', 'answerCallbackQuery', {
            'callback_query_id': 'callback-41', 'text': 'Saved', 'cache_time': 0}))
        self.error(403, self.gw.call, 'greenwich', 'answerCallbackQuery',
                   {'callback_query_id': 'callback-41'})
        for cid in ('callback-42', 'callback-43'):
            self.error(403, self.gw.call, 'acorn', 'answerCallbackQuery',
                       {'callback_query_id': cid})

    def test_import_is_once_only_and_forged_later_receipts_cannot_survive_restart(self):
        path = self.snapshot(self.legacy())
        self.gw.import_legacy(path)
        self.gw.ingest([callback(41, mid=500)])
        self.gw.call('acorn', 'getUpdates', {'offset': 40})
        self.gw.call('acorn', 'getUpdates', {'offset': 42})
        before_meta, before_owners = self.meta(), self.owners()

        self.snapshot(self.legacy(offset=9999, message_ids=[500, 501, 502, 999]))
        self.gw.import_legacy(path)
        self.assertEqual(self.meta(), before_meta)
        self.assertEqual(self.owners(), before_owners)
        restarted_config = copy.deepcopy(self.config)
        restarted_config['legacy_path'] = str(path)
        self.restart(restarted_config)
        self.gw.import_legacy(path)
        self.assertEqual(self.meta(), before_meta)
        self.assertEqual(self.owners(), before_owners)
        self.assertEqual(self.gw.state.offset(), 42)
        self.assertEqual(self.gw.call('acorn', 'getUpdates', {'offset': 42}), [])
        self.assertTrue(self.gw.call('acorn', 'answerCallbackQuery',
                                    {'callback_query_id': 'callback-41'}))
        result = self.gw.call('acorn', 'editMessageReplyMarkup', {
            'chat_id': CHAT, 'message_id': 500,
            'reply_markup': {'inline_keyboard': []}})
        self.assertEqual(result['message_id'], 500)
        self.error(403, self.gw.call, 'acorn', 'deleteMessage',
                   {'chat_id': CHAT, 'message_id': 999})
        # A sealed import must not even depend on the later file being valid JSON.
        path.write_text('later worker-controlled garbage', encoding='utf-8')
        self.restart(restarted_config)
        self.assertEqual(self.gw.state.offset(), 42)
        self.assertEqual(self.owners(), before_owners)

    def test_absent_legacy_snapshot_is_sealed_against_later_worker_additions(self):
        path = Path(self.temp.name) / 'legacy.json'
        self.gw.import_legacy(path)
        self.assertEqual(self.meta()['legacy_imported'], '1')
        self.snapshot(self.legacy(message_ids=[999]))
        changed = copy.deepcopy(self.config)
        changed['legacy_path'] = str(path)
        self.restart(changed)
        self.assertEqual(self.gw.state.offset(), 0)
        self.assertEqual(self.owners(), [])
        self.error(403, self.gw.call, 'acorn', 'deleteMessage',
                   {'chat_id': CHAT, 'message_id': 999})

    def test_legacy_offset_cannot_overwrite_an_already_polled_queue(self):
        self.gw.ingest([message(3)])
        with self.assertRaises(ValueError):
            self.gw.import_legacy(self.snapshot(self.legacy()))
        self.assertEqual(self.gw.state.offset(), 4)
        self.assertEqual([u['update_id'] for u in self.gw.call('acorn', 'getUpdates')], [3])
        self.assertEqual(self.owners(), [])
        self.assertNotIn('legacy_imported', self.meta())

    def test_get_chat_member_preserves_both_true_and_false_admin_permissions(self):
        for worker in ('acorn', 'greenwich'):
            for uid in (USER, BOT):
                for pin, delete in ((True, True), (True, False),
                                    (False, True), (False, False)):
                    with self.subTest(worker=worker, uid=uid, pin=pin, delete=delete):
                        self.upstream.permissions = {'can_pin_messages': pin,
                                                     'can_delete_messages': delete}
                        result = self.gw.call(worker, 'getChatMember',
                                              {'chat_id': CHAT, 'user_id': uid})
                        self.assertEqual(result, {
                            'user': {'id': uid, 'is_bot': uid == BOT},
                            'status': 'administrator', 'can_pin_messages': pin,
                            'can_delete_messages': delete})
                        self.assertIs(result['can_pin_messages'], pin)
                        self.assertIs(result['can_delete_messages'], delete)

    def test_nonboolean_admin_permissions_are_not_promoted_to_authority(self):
        for pin, delete in ((1, 0), ('true', 'false'), (None, [])):
            with self.subTest(pin=pin, delete=delete):
                self.upstream.permissions = {'can_pin_messages': pin,
                                             'can_delete_messages': delete}
                result = self.gw.call('acorn', 'getChatMember',
                                      {'chat_id': CHAT, 'user_id': BOT})
                self.assertNotIn('can_pin_messages', result)
                self.assertNotIn('can_delete_messages', result)

    def test_native_rich_message_is_forwarded_exactly_and_returns_a_native_receipt(self):
        html = ('<h1>Greenwich collections</h1><h2>This week</h2>'
                '<p>Check your bins &amp; recycling.</p><ul><li>Tuesday</li></ul>'
                '<footer><a href="' + COUNCIL_URL + '">Official council guidance</a></footer>')
        payload = {'chat_id': CHAT, 'rich_message': {'html': html},
                   'disable_notification': True, 'protect_content': False,
                   'link_preview_options': {'is_disabled': True}}
        original = copy.deepcopy(payload)
        self.upstream.calls.clear()
        result = self.gw.call('greenwich', 'sendRichMessage', payload)
        self.assertEqual(self.upstream.calls, [('sendRichMessage', {
            **original, 'message_thread_id': BINS_TOPIC})])
        self.assertEqual(payload, original)
        self.assertEqual(result['rich_message'], {'html': html})
        self.assertEqual(result['date'], DATE)
        self.assertEqual(result['from']['id'], BOT)
        self.assertEqual(result['message_thread_id'], BINS_TOPIC)
        self.assertNotIn('text', result)
        self.assertTrue(self.gw.call('greenwich', 'deleteMessage',
                                    {'chat_id': CHAT, 'message_id': result['message_id']}))
        self.error(403, self.gw.call, 'acorn', 'deleteMessage',
                   {'chat_id': CHAT, 'message_id': result['message_id']})

    def test_html_alert_and_disabled_link_preview_keep_the_council_link(self):
        html = '<b>Collection alert</b>\n<a href="' + COUNCIL_URL + '">Council details</a>'
        payload = {'chat_id': CHAT, 'text': html, 'parse_mode': 'HTML',
                   'disable_notification': False,
                   'link_preview_options': {'is_disabled': True}}
        original = copy.deepcopy(payload)
        self.upstream.calls.clear()
        result = self.gw.call('greenwich', 'sendMessage', payload)
        self.assertEqual(self.upstream.calls, [('sendMessage', {
            **original, 'message_thread_id': BINS_TOPIC,
            'disable_web_page_preview': True})])
        self.assertEqual(payload, original)
        self.assertEqual(result['text'], html)
        self.assertEqual(result['date'], DATE)
        self.assertNotIn('rich_message', self.upstream.calls[0][1])

    def test_foreign_or_malformed_html_urls_never_reach_either_send_method(self):
        urls = [
            'https://evil.invalid/',
            'http://www.royalgreenwich.gov.uk/',
            '//www.royalgreenwich.gov.uk/',
            'javascript:alert(1)',
            'https://www.royalgreenwich.gov.uk.evil.invalid/',
            'https://www.royalgreenwich.gov.uk@evil.invalid/',
            'https://evil.invalid@www.royalgreenwich.gov.uk/',
            'https://user:password@www.royalgreenwich.gov.uk/',
            'https://www.royalgreenwich.gov.uk:444/',
            'https://www.royalgreenwich.gov.uk:not-a-port/',
            'https://www.royalgreenwich.gov.uk:65536/',
            'https://[invalid-ipv6/',
            'https://www.royalgreenwich.gov.uk/a b',
            'https://www.royalgreenwich.gov.uk/&#10;injected',
            '',
        ]
        before = len(self.upstream.calls)
        for method in ('sendMessage', 'sendRichMessage'):
            for url in urls:
                with self.subTest(method=method, url=url):
                    html = '<a href="' + url + '">Council</a>'
                    body = ({'text': html, 'parse_mode': 'HTML'} if method == 'sendMessage'
                            else {'rich_message': {'html': html}})
                    self.error(400, self.gw.call, 'greenwich', method,
                               {'chat_id': CHAT, **body})
        self.assertEqual(len(self.upstream.calls), before)
        self.assertEqual(self.owners(), [])

    def test_html_link_attributes_and_unsafe_markup_are_rejected_before_upstream(self):
        invalid = [
            '<a>missing destination</a>', '<a href>missing value</a>',
            '<a href="' + COUNCIL_URL + '" href="https://evil.invalid/">duplicate</a>',
            '<a href="' + COUNCIL_URL + '" onclick="bad()">Council</a>',
            '<script>alert(1)</script>', '<img src="https://evil.invalid/">',
            '<!-- hidden --><b>Alert</b>',
        ]
        before = len(self.upstream.calls)
        for method in ('sendMessage', 'sendRichMessage'):
            for html in invalid:
                with self.subTest(method=method, html=html):
                    body = ({'text': html, 'parse_mode': 'HTML'} if method == 'sendMessage'
                            else {'rich_message': {'html': html}})
                    self.error(400, self.gw.call, 'greenwich', method,
                               {'chat_id': CHAT, **body})
        self.assertEqual(len(self.upstream.calls), before)

    def test_link_preview_policy_rejects_extra_fields_and_nonboolean_disable(self):
        before = len(self.upstream.calls)
        for options in ({'is_disabled': False}, {'is_disabled': 1}, {},
                        {'is_disabled': True, 'url': 'https://evil.invalid/'},
                        {'is_disabled': True, 'prefer_large_media': True}):
            with self.subTest(options=options):
                self.error(400, self.gw.call, 'greenwich', 'sendMessage', {
                    'chat_id': CHAT, 'text': '<b>Alert</b>', 'parse_mode': 'HTML',
                    'link_preview_options': options})
        self.assertEqual(len(self.upstream.calls), before)

    @staticmethod
    def start_call(fn, *args):
        result = queue.Queue()

        def run():
            try:
                result.put((True, fn(*args)))
            except BaseException as exc:
                result.put((False, exc))

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, result

    @contextlib.contextmanager
    def held_acorn_call(self):
        self.gw.close()
        upstream = HoldingUpstream()
        self.gw = self.new_gateway(upstream=upstream)
        threads = []
        held, result = self.start_call(self.send, 'acorn', 'hold-acorn')
        threads.append(held)
        try:
            self.assertTrue(upstream.entered.wait(2), 'Fake upstream was not entered')
            yield upstream, threads
        finally:
            upstream.release.set()
            for thread in threads:
                thread.join(3)
            self.assertTrue(all(not thread.is_alive() for thread in threads),
                            'An offline request outlived bounded cleanup')
        ok, value = result.get_nowait()
        self.assertTrue(ok, repr(value))

    def test_worker_api_concurrency_limit_does_not_block_the_other_worker(self):
        with self.held_acorn_call() as (upstream, threads):
            second, second_result = self.start_call(
                self.gw.call, 'acorn', 'getChatMember', {'chat_id': CHAT, 'user_id': BOT})
            threads.append(second)
            other, other_result = self.start_call(self.send, 'greenwich', 'free-greenwich')
            threads.append(other)
            other.join(2)
            self.assertFalse(other.is_alive(), 'Greenwich waited on Acorn\'s occupied API slot')
            self.assertFalse(upstream.release.is_set())
            self.assertTrue(threads[0].is_alive(), 'The Acorn API must still be held')
            ok, receipt = other_result.get_nowait()
            self.assertTrue(ok, repr(receipt))
            self.assertEqual(receipt['message_thread_id'], BINS_TOPIC)
            second.join(2)
            self.assertFalse(second.is_alive(), 'The own-worker concurrency rejection was unbounded')
            ok, exc = second_result.get_nowait()
            self.assertFalse(ok)
            self.assertIsInstance(exc, TelegramError)
            self.assertEqual((exc.code, exc.retry_after, exc.uncertain), (429, 1, False))

    def test_worker_rate_budget_does_not_spend_or_block_the_other_worker_budget(self):
        payload = {'chat_id': CHAT, 'user_id': BOT}
        self.upstream.calls.clear()
        for _ in range(60):
            self.assertEqual(self.gw.call('acorn', 'getChatMember', payload)['user']['id'], BOT)
        self.assertEqual(len(self.upstream.calls), 60)
        exc = self.error(429, self.gw.call, 'acorn', 'getChatMember', payload)
        self.assertEqual(exc.retry_after, 60)
        self.assertEqual(len(self.upstream.calls), 60)
        # Local identity/queue operations must not consume an upstream rate slot.
        self.assertEqual(self.gw.call('acorn', 'getMe')['id'], BOT)
        self.assertEqual(self.gw.call('acorn', 'getUpdates'), [])
        for _ in range(3):
            self.assertEqual(self.gw.call('greenwich', 'getChatMember', payload)['user']['id'], BOT)
        result = self.send('greenwich')
        self.assertEqual(result['message_thread_id'], BINS_TOPIC)
        self.assertEqual(len(self.upstream.calls), 64)
        self.error(429, self.gw.call, 'acorn', 'getChatMember', payload)
        self.assertEqual(len(self.upstream.calls), 64)


# These child programs are passed with -c. No extra helper source or project code
# is copied into the isolated directory. -I and a fresh environment prevent token,
# proxy, site, or PYTHONPATH inheritance. The audit hook forbids Internet sockets.
_OFFLINE_CHILD = r'''
import json
import os
import socket
import sys
sys.path.insert(0, os.getcwd())

def offline(event, args):
    if event == 'socket.__new__' and args[1] != socket.AF_UNIX:
        raise RuntimeError('Only offline Unix sockets are allowed')
    if event in {'socket.getaddrinfo', 'socket.gethostbyname', 'socket.gethostbyaddr'}:
        raise RuntimeError('DNS is forbidden in offline tests')

sys.addaudithook(offline)
'''

_GATEWAY_CHILD = _OFFLINE_CHILD + r'''
import signal
import threading
from gateway import Gateway

os.umask(0o077)
config = json.loads(sys.stdin.readline())
stop = threading.Event()
signal.signal(signal.SIGTERM, lambda *_: stop.set())
signal.signal(signal.SIGINT, lambda *_: stop.set())

class FakeUpstream:
    def call(self, method, payload=None):
        if method == 'getMe':
            return {'id': 12345, 'is_bot': True, 'username': 'offline_gateway_bot'}
        if method == 'getWebhookInfo':
            return {'url': ''}
        if method in {'sendMessage', 'editMessageText'}:
            topic = payload.get('message_thread_id', 42)
            result = {'message_id': payload.get('message_id', 700),
                      'chat': {'id': -100123}, 'message_thread_id': topic,
                      'from': {'id': 12345, 'is_bot': True},
                      'date': 1700000000, 'text': payload['text']}
            return result
        if method in {'deleteMessage', 'pinChatMessage'}:
            return True
        raise AssertionError('Unexpected fake upstream method')

gateway = Gateway(config, FakeUpstream())
try:
    gateway.start(health_port=None, poll=False)
    status = dict(line.split(':', 1) for line in open('/proc/self/status') if ':' in line)
    print(json.dumps({'ready': True, 'uid': os.geteuid(), 'gid': os.getegid(),
                      'groups': os.getgroups(), 'cap_eff': int(status['CapEff'].strip(), 16)}),
          flush=True)
    if not stop.wait(20):
        raise RuntimeError('Offline gateway exceeded its bounded lifetime')
finally:
    gateway.close()
'''

_WORKER_CHILD = _OFFLINE_CHILD + r'''
import gateway_client
from telegram_api import TelegramError

request = json.loads(sys.stdin.readline())
http_status = None
original_connection = gateway_client._Connection

class ObservedConnection(original_connection):
    def getresponse(self):
        global http_status
        response = super().getresponse()
        http_status = response.status
        return response

gateway_client._Connection = ObservedConnection
result = {'uid': os.geteuid(), 'gid': os.getegid(), 'groups': os.getgroups(),
          'can_read_state': os.access(request['state_path'], os.R_OK),
          'can_write_socket_dir': os.access(os.path.dirname(request['socket_path']), os.W_OK)}
try:
    receipt = gateway_client.UnixClient(request['socket_path'], timeout=2).call(
        request.get('method', 'getMe'), request.get('payload'))
    result.update(ok=True, result=receipt)
except TelegramError as exc:
    result.update(ok=False, code=exc.code, uncertain=exc.uncertain)
result['http_status'] = http_status
print(json.dumps(result), flush=True)
'''


@unittest.skipUnless(sys.platform.startswith('linux') and os.geteuid() == 0,
                     'Real UID/fsGroup subprocess contracts require Linux root')
class NonRootGatewayDeploymentTests(unittest.TestCase):
    def setUp(self):
        # A short, world-traversable /tmp path avoids /root traversal restrictions
        # and the Linux sockaddr_un length limit after adding private directories.
        self.temp = tempfile.TemporaryDirectory(prefix='gwc-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o755)
        self.code = self.root / 'code'
        self.code.mkdir(mode=0o755)
        for name in ('gateway.py', 'gateway_client.py', 'configuration.py', 'telegram_api.py'):
            target = self.code / name
            shutil.copyfile(SOURCE / name, target)
            target.chmod(0o444)
        self.assertEqual({p.name for p in self.code.iterdir()}, {
            'gateway.py', 'gateway_client.py', 'configuration.py', 'telegram_api.py'})
        # Simulate kubelet fsGroup ownership on existing volume roots, not chown
        # inside the gateway. All private child directories must be made by 10003.
        for name in ('gateway', 'acorn', 'greenwich'):
            mount = self.root / name
            mount.mkdir()
            os.chown(mount, 0, SHARED_GID)
            mount.chmod(0o2775)
        self.config = production_config_at(self.root)
        self.env = {'PATH': os.defpath, 'LANG': 'C.UTF-8'}

    def spawn(self, script, uid):
        return subprocess.Popen(
            [sys.executable, '-I', '-B', '-c', script], cwd=self.code,
            env=self.env, user=uid, group=SHARED_GID, extra_groups=[SHARED_GID],
            start_new_session=True, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    @staticmethod
    def stop_child(process):
        if process.poll() is None:
            process.terminate()
        try:
            return process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            return process.communicate(timeout=2)

    def start_gateway(self):
        process = self.spawn(_GATEWAY_CHILD, GATEWAY_UID)
        self.addCleanup(self.stop_child, process)
        process.stdin.write(encode(self.config) + b'\n')
        process.stdin.flush()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            ready = selector.select(timeout=8)
            if not ready:
                stdout, stderr = self.stop_child(process)
                self.fail('Non-root gateway did not start within 8s: ' +
                          repr((stdout, stderr)))
            line = process.stdout.readline()
        if not line:
            stdout, stderr = self.stop_child(process)
            self.fail('Non-root gateway exited before readiness: ' + repr((stdout, stderr)))
        identity = json.loads(line)
        self.assertEqual(identity, {'ready': True, 'uid': GATEWAY_UID,
                                    'gid': SHARED_GID, 'groups': [SHARED_GID],
                                    'cap_eff': 0})
        self.assertIsNone(process.poll())
        return process

    def worker_call(self, uid, socket_worker='acorn', method='getMe', payload=None):
        worker = next(w for w in self.config['workers'] if w['id'] == socket_worker)
        process = self.spawn(_WORKER_CHILD, uid)
        request = {'socket_path': worker['socket_path'],
                   'state_path': self.config['state_path'],
                   'method': method, 'payload': payload}
        try:
            stdout, stderr = process.communicate(encode(request) + b'\n', timeout=5)
        except subprocess.TimeoutExpired:
            self.stop_child(process)
            self.fail('Offline worker request exceeded its 5s deadline')
        finally:
            if process.poll() is None:
                self.stop_child(process)
        self.assertEqual(process.returncode, 0, stderr.decode('utf-8', errors='replace'))
        self.assertEqual(stderr, b'')
        result = json.loads(stdout)
        self.assertEqual((result['uid'], result['gid'], result['groups']),
                         (uid, SHARED_GID, [SHARED_GID]))
        self.assertFalse(result['can_read_state'])
        self.assertFalse(result['can_write_socket_dir'])
        return result

    def assert_deployment_modes(self):
        for name in ('gateway', 'acorn', 'greenwich'):
            mount = (self.root / name).stat()
            self.assertEqual((mount.st_uid, mount.st_gid, stat.S_IMODE(mount.st_mode)),
                             (0, SHARED_GID, 0o2775))
            private = (self.root / name / 'private').stat()
            self.assertEqual((private.st_uid, private.st_gid), (GATEWAY_UID, SHARED_GID))
            self.assertEqual(stat.S_IMODE(private.st_mode) & 0o777, 0o750)
            self.assertFalse(private.st_mode & 0o022)
        for worker in self.config['workers']:
            info = os.stat(worker['socket_path'])
            self.assertTrue(stat.S_ISSOCK(info.st_mode))
            self.assertEqual((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)),
                             (GATEWAY_UID, SHARED_GID, 0o660))
        # Also catch group-readable SQLite/WAL/lease files despite fsGroup access.
        state_files = list(Path(self.config['state_path']).parent.iterdir())
        self.assertIn(Path(self.config['state_path']), state_files)
        for path in state_files:
            info = path.stat()
            self.assertEqual((info.st_uid, info.st_gid), (GATEWAY_UID, SHARED_GID))
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o600, path.name)

    def assert_peer_identity_contract(self):
        for uid, worker in ((ACORN_UID, 'acorn'), (GREENWICH_UID, 'greenwich')):
            with self.subTest(uid=uid, socket=worker, authorized=True):
                result = self.worker_call(uid, worker)
                self.assertTrue(result['ok'])
                self.assertEqual(result['result']['id'], BOT)
                self.assertEqual(result['http_status'], 200)
        for uid, worker in ((GREENWICH_UID, 'acorn'), (ACORN_UID, 'greenwich')):
            with self.subTest(uid=uid, socket=worker, authorized=False):
                result = self.worker_call(uid, worker)
                self.assertFalse(result['ok'])
                self.assertEqual((result['http_status'], result['code'], result['uncertain']),
                                 (403, 403, False))

    def assert_graceful_stop(self, process):
        started = time.monotonic()
        stdout, stderr = self.stop_child(process)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(process.returncode, 0, repr((stdout, stderr)))
        self.assertEqual((stdout, stderr), (b'', b''))
        for worker in self.config['workers']:
            self.assertFalse(os.path.lexists(worker['socket_path']))

    def test_emptydir_fs_group_permissions_and_real_peer_uids_without_cap_chown(self):
        self.assertFalse(Path(self.config['state_path']).parent.exists())
        for worker in self.config['workers']:
            self.assertFalse(Path(worker['socket_path']).parent.exists())
        process = self.start_gateway()
        self.assert_deployment_modes()
        self.assert_peer_identity_contract()
        self.assert_graceful_stop(process)

    def test_pvc_fs_group_permissions_and_owned_receipt_survive_nonroot_restart(self):
        process = self.start_gateway()
        self.assert_deployment_modes()
        self.assert_peer_identity_contract()
        sent = self.worker_call(ACORN_UID, method='sendMessage',
                                payload={'chat_id': CHAT, 'text': 'offline durable panel'})
        self.assertTrue(sent['ok'])
        mid = sent['result']['message_id']
        self.assertEqual(sent['result']['date'], DATE)
        self.assert_graceful_stop(process)
        # Retain exactly the PVC/state and private directories across a new exec.
        state_inode = Path(self.config['state_path']).stat().st_ino
        process = self.start_gateway()
        self.assertEqual(Path(self.config['state_path']).stat().st_ino, state_inode)
        self.assert_deployment_modes()
        self.assert_peer_identity_contract()
        edited = self.worker_call(ACORN_UID, method='editMessageText', payload={
            'chat_id': CHAT, 'message_id': mid, 'text': 'offline panel after restart'})
        self.assertTrue(edited['ok'])
        self.assertEqual(edited['result']['message_id'], mid)
        self.assertEqual(edited['result']['text'], 'offline panel after restart')
        cross_module = self.worker_call(GREENWICH_UID, 'greenwich', 'deleteMessage',
                                        {'chat_id': CHAT, 'message_id': mid})
        self.assertFalse(cross_module['ok'])
        self.assertEqual(cross_module['code'], 403)
        self.assert_graceful_stop(process)


if __name__ == '__main__':
    unittest.main()
