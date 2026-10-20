"""Repeat settings use synthetic care state and no network access."""
import argparse
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import care
import inline_ui
from configuration import Runtime, validate_care_config
from service import Bot
from storage import Store
from test_service import FakeClient, NOW, CHAT, TOPIC, BOT_ID


class RepeatControlTests(unittest.TestCase):
    def setUp(self):
        self.state = care.initial()
        self.state['config']['automatic_repeats'] = False

    def tick(self, stamp):
        return care.tick(self.state, care.instant(stamp))

    def test_optional_flag_accepts_only_boolean_values(self):
        config = copy.deepcopy(care.BOOT_CONFIG)
        config.pop('automatic_repeats', None)
        validate_care_config(config)
        for value in (True, False):
            validate_care_config({**config, 'automatic_repeats': value})
        for value in (None, 0, 1, 'false', [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_care_config({**config, 'automatic_repeats': value})

    def test_each_meal_is_announced_once_without_automatic_repeats(self):
        plan = copy.deepcopy(self.state['config'])
        for meal in ('07:00', '14:00', '19:00'):
            text = self.tick('2026-10-19T' + meal + ':00+01:00')
            self.assertIn('🐾 Acorn', text)
            self.assertIn('Автоматические повторы выключены.', text)
            self.assertNotIn('Пока не подтверждены:', text)
            self.assertNotIn('Через 30 минут', text)
            self.assertEqual(self.state['emissions'][self.state['last_emission_id']]['repeat_ids'], [])
            hour, minute = meal.split(':')
            self.assertEqual(self.tick('2026-10-19T' + hour + ':30:00+01:00'), 'NO_REPLY')
        self.assertEqual(len(self.state['checklists']), 3)
        self.assertEqual(self.state['config'], plan)
        self.assertTrue(all(t['status'] not in care.CLOSED for t in self.state['tasks'].values()))
        self.assertTrue(all(t['next_repeat_at'] is None for t in self.state['tasks'].values()))
        self.assertEqual(self.tick('2026-10-19T22:30:00+01:00'), 'NO_REPLY')
        self.assertEqual(self.tick('2026-10-19T23:00:00+01:00'), 'NO_REPLY')
        morning = self.tick('2026-10-20T07:00:00+01:00')
        self.assertIn('🐾 Acorn', morning)
        self.assertNotIn('после тихих часов', morning)
        self.assertIn('Предыдущее кормление не подтверждено', morning)

    def test_disabling_repeats_preserves_existing_open_task_records(self):
        self.state['config'].pop('automatic_repeats')
        self.tick('2026-10-19T07:00:00+01:00')
        before = copy.deepcopy(self.state['tasks'])
        self.state['config']['automatic_repeats'] = False
        self.assertEqual(self.tick('2026-10-19T07:30:00+01:00'), 'NO_REPLY')
        self.assertEqual(self.state['tasks'], before)
        self.assertEqual(self.tick('2026-10-19T08:00:00+01:00'), 'NO_REPLY')

    def test_explicit_snooze_sends_one_reminder_then_stops(self):
        self.tick('2026-10-19T07:00:00+01:00')
        args = argparse.Namespace(checklist='2026-10-19_07:00', types=['wet'],
                                  action='snooze', actor='caregiver', minutes=60,
                                  message_id='synthetic', original_message_id=None)
        care.update(self.state, args, care.instant('2026-10-19T07:11:00+01:00'))
        self.assertEqual(self.tick('2026-10-19T07:30:00+01:00'), 'NO_REPLY')
        self.assertEqual(self.tick('2026-10-19T08:10:59+01:00'), 'NO_REPLY')
        text = self.tick('2026-10-19T08:11:00+01:00')
        self.assertIn('Влажный корм', text)
        self.assertNotIn('Сухой корм', text)
        eid = self.state['last_emission_id']
        self.assertEqual(self.state['emissions'][eid]['manual_repeat_ids'], ['2026-10-19_07:00_wet'])
        refreshed = care.refresh(self.state, eid, care.instant('2026-10-19T08:11:01+01:00'))
        self.assertIn('Влажный корм', refreshed)
        self.assertEqual(care.emission_task_ids(self.state, eid), ['2026-10-19_07:00_wet'])
        self.assertEqual(self.tick('2026-10-19T08:41:00+01:00'), 'NO_REPLY')
        self.assertIsNone(self.state['tasks']['2026-10-19_07:00_wet']['next_repeat_at'])

    def test_refresh_suppresses_prepared_automatic_repeat_and_buttons(self):
        self.state['config']['automatic_repeats'] = True
        self.tick('2026-10-19T07:00:00+01:00')
        self.tick('2026-10-19T07:30:00+01:00')
        eid = self.state['last_emission_id']
        self.state['config']['automatic_repeats'] = False
        self.assertEqual(care.refresh(self.state, eid, care.instant('2026-10-19T07:31:00+01:00')), 'NO_REPLY')
        self.assertEqual(care.emission_task_ids(self.state, eid), [])

    def test_existing_inline_footer_stops_promising_automatic_repeats(self):
        self.state['config']['automatic_repeats'] = True
        text = self.tick('2026-10-19T07:00:00+01:00')
        panel = inline_ui.prepare(self.state, [text], list(self.state['tasks']), NOW)
        tasks = copy.deepcopy(self.state['tasks'])
        self.state['config']['automatic_repeats'] = False
        rendered = '\n'.join(inline_ui.page_text(self.state, panel, page) for page in panel['pages'])
        self.assertIn('Автоматические повторы выключены.', rendered)
        self.assertNotIn('Через 30 минут', rendered)
        self.assertNotIn('Напомню только', rendered)
        self.assertEqual(self.state['tasks'], tasks)

    def test_health_hold_keeps_meal_checklists_without_automatic_repeats(self):
        care.health_hold(self.state, 'caregiver', 'Synthetic health hold', NOW)
        hold = copy.deepcopy(self.state['progression_hold'])
        self.tick('2026-10-19T07:00:00+01:00')
        self.assertEqual(self.tick('2026-10-19T07:30:00+01:00'), 'NO_REPLY')
        text = self.tick('2026-10-19T14:00:00+01:00')
        self.assertIn('смена этапов приостановлена', text)
        self.assertIn('Дать ≈1/4 банки', text)
        self.assertEqual(self.state['progression_hold'], hold)


class RepeatPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.temp) / 'state.json'
        self.store = Store(self.path)
        self.store.bind(BOT_ID, CHAT, TOPIC, NOW, fresh_start=True)
        self.runtime = Runtime(CHAT, TOPIC, frozenset({101}), self.path)
        self.client = FakeClient()
        self.now = care.instant('2026-10-20T07:00:00+01:00')
        self.bot = Bot(self.runtime, self.client, BOT_ID, 'example_care_bot', self.store, lambda: self.now)
        self.bot.dispatch_once()

    def disable(self):
        with self.store.transaction() as state:
            state['config']['automatic_repeats'] = False

    def prepare(self, stamp):
        self.now = care.instant(stamp)
        with self.store.transaction() as state:
            text = care.tick(state, self.now)
            self.assertNotEqual(text, 'NO_REPLY')
            eid = state['last_emission_id']
            panel = self.bot.prepare_reminder(state, eid, text, self.now)
            key = 'reminder:' + eid
            state['standalone']['outbox'][key] = {
                'status': 'pending', 'panel': panel['nonce'], 'emission': eid,
                'message_ids': [], 'retry_at': 0}
        return key

    def test_explicit_startup_flag_only_changes_repeat_setting(self):
        with self.store.transaction() as state:
            state['config']['repeat_minutes'] = 45
            state['carryovers'] = {'synthetic': {'preserve': True}}
            state['progression_hold'] = {'stage': 'weeks_1_2', 'since': care.iso(NOW)}
        before = self.store.snapshot()
        config = {**copy.deepcopy(care.BOOT_CONFIG), 'automatic_repeats': False}
        with patch.object(care, 'BOOT_CONFIG', config):
            self.store.bind(BOT_ID, CHAT, TOPIC, self.now, fresh_start=True)
        saved = self.store.snapshot()
        expected_config = {**before['config'], 'automatic_repeats': False}
        self.assertEqual(saved['config'], expected_config)
        for key in ('tasks', 'checklists', 'carryovers', 'progression_hold', 'schedule_history', 'standalone'):
            self.assertEqual(saved[key], before[key], key)
        self.assertTrue(any(entry['kind'] == 'automatic_repeats_changed' for entry in saved['audit']))

    def test_omitted_startup_flag_preserves_disabled_setting(self):
        self.disable()
        config = copy.deepcopy(care.BOOT_CONFIG)
        config.pop('automatic_repeats', None)
        with patch.object(care, 'BOOT_CONFIG', config):
            self.store.bind(BOT_ID, CHAT, TOPIC, self.now)
        self.assertIs(self.store.snapshot()['config']['automatic_repeats'], False)

    def test_pending_automatic_only_job_is_suppressed_without_send(self):
        key = self.prepare('2026-10-20T07:30:00+01:00')
        tasks = self.store.snapshot()['tasks']
        count = len(self.client.calls)
        self.disable()
        self.bot.deliver(key, self.now)
        self.assertEqual(self.store.snapshot()['standalone']['outbox'][key]['status'], 'suppressed')
        self.assertEqual(len(self.client.calls), count)
        self.assertEqual(self.store.snapshot()['tasks'], tasks)

    def test_partly_delivered_automatic_job_does_not_resume(self):
        key = self.prepare('2026-10-20T07:30:00+01:00')
        with self.store.transaction() as state:
            state['standalone']['outbox'][key]['message_ids'] = [777]
        self.disable()
        count = len(self.client.calls)
        self.bot.deliver(key, self.now)
        self.assertEqual(len(self.client.calls), count)
        self.assertEqual(self.store.snapshot()['standalone']['outbox'][key]['status'], 'suppressed')

    def test_unsent_mixed_job_keeps_primary_without_old_repeat_buttons(self):
        key = self.prepare('2026-10-20T14:00:00+01:00')
        self.disable()
        self.bot.deliver(key, self.now)
        saved = self.store.snapshot()
        job = saved['standalone']['outbox'][key]
        self.assertEqual(job['status'], 'sent')
        panel = saved['inline_panels'][job['panel']]
        self.assertEqual(set(panel['task_ids']), {'2026-10-20_14:00_wet', '2026-10-20_14:00_dry'})
        self.assertNotIn('Пока не подтверждены:', self.client.calls[-1][1]['text'])
        self.assertIn('Предыдущее кормление не подтверждено', self.client.calls[-1][1]['text'])

    def test_partial_mixed_job_stops_for_review_instead_of_sending_repeats(self):
        key = self.prepare('2026-10-20T14:00:00+01:00')
        with self.store.transaction() as state:
            state['standalone']['outbox'][key]['message_ids'] = [777]
        self.disable()
        count = len(self.client.calls)
        self.bot.deliver(key, self.now)
        self.assertEqual(len(self.client.calls), count)
        self.assertEqual(self.store.snapshot()['standalone']['outbox'][key]['status'], 'needs_review')


if __name__ == '__main__':
    unittest.main()
