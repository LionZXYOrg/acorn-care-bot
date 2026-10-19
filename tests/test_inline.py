import copy
import json
import pathlib
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import care
import inline_ui

NOW = care.instant('2026-10-19T07:01:00+01:00')
USER_ID = 101
BOT_ID = 12345


class InlineTests(unittest.TestCase):
    def setUp(self):
        care.BUTTON_BOT = '@example_care_bot'
        self.state = care.initial()
        care.tick(self.state, care.instant('2026-10-19T07:00:00+01:00'))
        self.cid = '2026-10-19_07:00'
        self.keys = self.state['checklists'][self.cid]['task_ids']
        text = care.render_checklist(self.state, self.cid)
        self.panel = inline_ui.prepare(self.state, [text], self.keys, NOW, primary=self.cid)
        self.panel['pages'][0]['message_id'] = 777
        self.panel['status'] = 'sent'
        self.panel['last_digest'] = inline_ui.digest(self.state, self.panel)

    def callback(self, index=0, query_id='q1'):
        return {'id': query_id, 'from': {'id': USER_ID, 'username': 'caregiver_a', 'is_bot': False},
                'message': {'message_id': 777, 'message_thread_id': 42,
                            'chat': {'id': -1000000000001},
                            'from': {'id': BOT_ID, 'is_bot': True}},
                'data': inline_ui.PREFIX + self.panel['nonce'] + ':' + str(index)}

    def accept(self, callback=None, at=NOW):
        return inline_ui.accept_callback(self.state, callback or self.callback(),
                                         {USER_ID}, BOT_ID, at, -1000000000001, 42)

    def test_buttons_are_attached_to_message(self):
        self.assertEqual(inline_ui.PREFIX, 'acorn:1:')
        keyboard = inline_ui.keyboard(self.state, self.panel)
        self.assertNotIn('keyboard', keyboard)
        self.assertEqual(len(keyboard['inline_keyboard']), 5)
        for row in keyboard['inline_keyboard']:
            self.assertLessEqual(len(row[0]['callback_data'].encode()), 64)
            self.assertNotIn('@example', row[0]['text'])

    def test_short_labels_keep_full_callback_identity(self):
        rows = inline_ui.keyboard(self.state, self.panel)['inline_keyboard']
        self.assertEqual([row[0]['text'] for row in rows], [
            '✅ Влажный ≈1/4 банки', '✅ Сухой 10 г',
            '✅ Лоток', '✅ Миски', '✅ Вода',
        ])
        for index, row in enumerate(rows):
            self.assertNotIn('19.10.2026', row[0]['text'])
            self.assertNotIn('07:00', row[0]['text'])
            self.assertEqual(row[0]['callback_data'],
                             inline_ui.PREFIX + self.panel['nonce'] + ':' + str(index))

    def test_identical_meal_labels_cannot_close_another_meal(self):
        at = care.instant('2026-10-19T14:01:00+01:00')
        care.tick(self.state, at)
        cid = '2026-10-19_14:00'
        panel = inline_ui.prepare(self.state, [care.render_checklist(self.state, cid)],
                                  self.state['checklists'][cid]['task_ids'], at, primary=cid)
        panel['pages'][0]['message_id'] = 778
        panel['status'] = 'sent'
        old_button = inline_ui.keyboard(self.state, self.panel)['inline_keyboard'][0][0]
        new_button = inline_ui.keyboard(self.state, panel)['inline_keyboard'][0][0]
        self.assertEqual(old_button['text'], new_button['text'])
        self.assertNotEqual(old_button['callback_data'], new_button['callback_data'])
        callback = self.callback()
        callback['message']['message_id'] = 778
        callback['data'] = new_button['callback_data']
        old_task = copy.deepcopy(self.state['tasks'][self.cid + '_wet'])
        answer = self.accept(callback, at=at)
        self.assertFalse(answer['show_alert'])
        self.assertEqual(self.state['tasks'][cid + '_wet']['status'], 'done')
        self.assertEqual(self.state['tasks'][self.cid + '_wet'], old_task)

    def test_click_saves_one_task_and_stops_its_repeats(self):
        self.accept()
        task = self.state['tasks'][self.keys[0]]
        self.assertEqual(task['status'], 'done')
        self.assertEqual(task['done_by'], 'telegram:101')
        self.assertIsNone(task['next_repeat_at'])
        self.assertEqual(sum(t['status'] == 'done' for t in self.state['tasks'].values()), 1)
        care.tick(self.state, care.instant('2026-10-19T07:30:00+01:00'))
        self.assertNotIn(self.keys[0], self.state['emissions'][self.state['last_emission_id']]['repeat_ids'])

    def test_original_text_shows_confirmation_and_preserves_transition_notice(self):
        self.accept()
        text = inline_ui.page_text(self.state, self.panel, self.panel['pages'][0])
        self.assertIn('✅ Влажный корм, ≈1/4 банки: telegram:101, 07:01.', text)
        self.assertNotIn('☐ Дать ≈1/4 банки влажного корма.', text)
        self.assertIn('Сегодня меняются порции', text)
        self.assertNotIn('Через 30 минут', text)
        self.assertNotIn('Пора покормить', text)
        self.assertEqual(len(inline_ui.keyboard(self.state, self.panel)['inline_keyboard']), 4)

    def test_duplicate_delivery_has_no_second_audit_or_portion(self):
        self.accept()
        before = copy.deepcopy(self.state)
        self.accept()
        self.assertEqual(before, self.state)

    def test_second_person_cannot_overwrite_first_completion(self):
        self.accept()
        cb = self.callback(query_id='q2')
        cb['from']['username'] = 'caregiver_b'
        self.accept(cb)
        self.assertEqual(self.state['tasks'][self.keys[0]]['done_by'], 'telegram:101')

    def test_skipped_task_stays_skipped(self):
        task = self.state['tasks'][self.keys[0]]
        task.update(status='skipped', skip_by='caregiver_a', skip_at=care.iso(NOW), next_repeat_at=None)
        self.accept()
        self.assertEqual(task['status'], 'skipped')
        self.assertIsNone(task['done_by'])

    def test_future_click_is_rejected(self):
        self.accept(at=care.instant('2026-10-19T06:59:59+01:00'))
        self.assertEqual(self.state['tasks'][self.keys[0]]['status'], 'pending')

    def test_old_food_click_does_not_complete_new_food(self):
        at = care.instant('2026-10-19T14:01:00+01:00')
        care.tick(self.state, at)
        self.accept(at=at)
        self.assertEqual(self.state['tasks'][self.keys[0]]['status'], 'done')
        self.assertEqual(self.state['tasks']['2026-10-19_14:00_wet']['status'], 'pending')

    def test_scope_and_identity_guards(self):
        mutations = [
            lambda c: c['from'].update(id=999),
            lambda c: c['from'].update(is_bot=True),
            lambda c: c['message']['chat'].update(id=-123),
            lambda c: c['message'].update(message_thread_id=211),
            lambda c: c['message'].update(message_id=778),
            lambda c: c['message']['from'].update(id=98765),
            lambda c: c.update(data=inline_ui.PREFIX + '0' * 16 + ':0'),
            lambda c: c.update(data=inline_ui.PREFIX + self.panel['nonce'] + ':99'),
            lambda c: c.update(data=inline_ui.PREFIX + self.panel['nonce'] + ':test'),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                callback = self.callback()
                mutate(callback)
                before = copy.deepcopy(self.state)
                self.assertTrue(self.accept(callback)['show_alert'])
                self.assertEqual(before, self.state)

    def test_retired_callback_prefix_does_not_change_care(self):
        callback = self.callback()
        prefix = '\u0065\u006b\u006f\u0072\u006e:1:'
        callback['data'] = prefix + self.panel['nonce'] + ':0'
        before = copy.deepcopy(self.state)
        self.assertIsNone(self.accept(callback))
        self.assertEqual(before, self.state)

    def test_approval_callbacks_are_not_intercepted(self):
        callback = self.callback()
        callback['data'] = 'approval:some-id:approve'
        before = copy.deepcopy(self.state)
        self.assertIsNone(self.accept(callback))
        self.assertEqual(before, self.state)

    def test_full_completion_removes_buttons_and_reminder_line(self):
        for index in range(5):
            self.accept(self.callback(index=index, query_id='q' + str(index)))
        self.assertEqual(inline_ui.keyboard(self.state, self.panel), {'inline_keyboard': []})
        text = inline_ui.page_text(self.state, self.panel, self.panel['pages'][0])
        self.assertIn('Всё выполнено', text)
        self.assertNotIn('Напомню', text)
        self.assertNotIn('Отмечайте выполненное', text)

    def test_text_confirmation_updates_the_same_panel(self):
        care.complete_button(self.state, care.button_text(self.state['tasks'][self.keys[2]]),
                             'caregiver_b', 'text1', NOW)
        self.assertNotEqual(self.panel['last_digest'], inline_ui.digest(self.state, self.panel))
        self.assertIn('✅ Лоток', inline_ui.page_text(self.state, self.panel, self.panel['pages'][0]))

    def test_multipart_bindings_keep_their_checklist_context(self):
        source = self.panel['pages'][0]['template'].splitlines()
        parts = ['\n'.join(source[:10]), '\n'.join(source[10:])]
        panel = inline_ui.prepare(self.state, parts, self.keys, NOW, primary=self.cid)
        self.assertEqual(sum(len(p['bindings']) for p in panel['pages']), 5)

    def test_test_callback_does_not_change_care_tasks(self):
        tasks = copy.deepcopy(self.state['tasks'])
        self.panel = inline_ui.prepare(self.state, ['Тест'], [], NOW, test=True)
        self.panel['pages'][0]['message_id'] = 777
        self.panel['status'] = 'sent'
        cb = self.callback()
        cb['data'] = inline_ui.PREFIX + self.panel['nonce'] + ':test'
        self.accept(cb)
        self.assertEqual(tasks, self.state['tasks'])
        self.assertTrue(self.panel['test_done'])
        self.assertIn('Кнопка работает', inline_ui.page_text(self.state, self.panel, self.panel['pages'][0]))

    def test_receipt_survives_reload(self):
        self.accept()
        loaded = json.loads(json.dumps(self.state))
        before = copy.deepcopy(loaded)
        inline_ui.accept_callback(loaded, self.callback(), {USER_ID}, BOT_ID, NOW, -1000000000001, 42)
        self.assertEqual(before, loaded)
