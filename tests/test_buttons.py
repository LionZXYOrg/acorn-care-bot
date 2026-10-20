import argparse
import copy
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import care


NOW = care.instant('2026-10-19T07:00:05+01:00')


class ButtonTests(unittest.TestCase):
    def setUp(self):
        care.BUTTON_BOT = '@example_care_bot'
        care.BUTTON_TEST = '✅ Тест кнопки @example_care_bot'
        self.s = care.initial()
        care.tick(self.s, care.instant('2026-10-19T07:00:00+01:00'))
        self.cid = '2026-10-19_07:00'
        self.eid = self.s['last_emission_id']
        self.keys = self.s['checklists'][self.cid]['task_ids']

    def test_keyboard_has_five_distinct_text_buttons(self):
        rows = care.reply_keyboard(self.s, self.keys)['keyboard']
        self.assertEqual(len(rows), 5)
        labels = [row[0]['text'] for row in rows]
        self.assertEqual(len(set(labels)), 5)
        for label in labels:
            self.assertIn('19.10.2026 07:00', label)
            self.assertIn('@example_care_bot', label)
        self.assertTrue(all(set(row[0]) == {'text'} for row in rows))

    def test_keyboard_excludes_closed_and_deferred(self):
        self.s['tasks'][self.keys[0]]['status'] = 'done'
        self.s['tasks'][self.keys[1]]['status'] = 'deferred'
        labels = str(care.reply_keyboard(self.s, self.keys))
        self.assertNotIn('Влажный', labels)
        self.assertNotIn('Сухой', labels)
        self.assertIn('Лоток', labels)

    def test_no_tasks_removes_keyboard(self):
        self.assertEqual(care.reply_keyboard(self.s, []), {'remove_keyboard': True})

    def test_real_button_completes_exactly_one_task(self):
        task = self.s['tasks'][self.keys[0]]
        result = care.complete_button(self.s, care.button_text(task), 'caregiver_b', 'm1', NOW)
        self.assertIn('✅ Влажный корм, ≈1/4 банки: @caregiver_b', result)
        self.assertEqual(sum(t['status'] == 'done' for t in self.s['tasks'].values()), 1)
        self.assertIsNone(task['next_repeat_at'])

    def test_second_click_does_not_overwrite_first_actor(self):
        task = self.s['tasks'][self.keys[0]]
        label = care.button_text(task)
        care.complete_button(self.s, label, 'caregiver_a', 'm1', NOW)
        result = care.complete_button(self.s, label, 'caregiver_b', 'm2', NOW)
        self.assertIn('уже закрыты', result)
        self.assertEqual(task['done_by'], 'caregiver_a')

    def test_skipped_task_stays_skipped(self):
        task = self.s['tasks'][self.keys[0]]
        task.update(status='skipped', skip_by='caregiver_a', skip_at=care.iso(NOW))
        care.complete_button(self.s, care.button_text(task), 'caregiver_b', 'm1', NOW)
        self.assertEqual(task['status'], 'skipped')
        self.assertIsNone(task['done_by'])

    def test_test_button_does_not_touch_care_record(self):
        previous = copy.deepcopy(self.s)
        result = care.complete_button(self.s, care.BUTTON_TEST, 'caregiver_b', 'm1', NOW)
        self.assertIn('Кнопка работает', result)
        self.assertEqual(previous, self.s)

    def test_unknown_button_cannot_create_task(self):
        previous = copy.deepcopy(self.s)
        with self.assertRaises(ValueError):
            care.complete_button(self.s, '✅ Любое готово @example_care_bot', 'u', 'm', NOW)
        self.assertEqual(previous, self.s)

    def test_changed_grams_are_rejected(self):
        text = care.button_text(self.s['tasks'][self.keys[0]], legacy=True).replace('49 г', '98 г')
        with self.assertRaises(ValueError):
            care.complete_button(self.s, text, 'u', 'm', NOW)

    def test_future_task_is_rejected(self):
        task = self.s['tasks'][self.keys[0]]
        with self.assertRaises(ValueError):
            care.complete_button(self.s, care.button_text(task), 'u', 'm',
                                 care.instant('2026-10-19T06:59:00+01:00'))
        self.assertEqual(task['status'], 'pending')

    def test_old_food_button_only_confirms_old_portion(self):
        care.tick(self.s, care.instant('2026-10-19T14:00:00+01:00'))
        task = self.s['tasks'][self.keys[0]]
        care.complete_button(self.s, care.button_text(task), 'u', 'm',
                             care.instant('2026-10-19T14:05:00+01:00'))
        self.assertEqual(task['status'], 'done')
        self.assertEqual(self.s['tasks']['2026-10-19_14:00_wet']['status'], 'pending')
        self.assertEqual(self.s['tasks']['2026-10-19_14:00_wet']['grams'], 49)
