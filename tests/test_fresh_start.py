"""Fresh-start cutoffs use generated state, fake identities, and no network."""
import copy
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import care
import inline_ui
import migrate
import service
from configuration import Runtime
from service import Bot
from storage import Store
from test_service import FakeClient

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / '.test-tmp'
TMP.mkdir(exist_ok=True)
CHAT, TOPIC, BOT_ID, USER_ID = -1000000000001, 42, 12345, 101


class FreshStartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TMP)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.json'
        self.store = Store(self.path)

    def bind(self, stamp, *, fresh_start=True, store=None):
        # Keep the supplied offset to test equivalent timestamps in different zones.
        now = datetime.fromisoformat(stamp)
        (store or self.store).bind(BOT_ID, CHAT, TOPIC, now, fresh_start=fresh_start)
        return now

    def tick(self, stamp, *, store=None):
        store = store or self.store
        with store.transaction() as state:
            text = care.tick(state, care.instant(stamp))
        return text, store.snapshot()

    def test_environment_requires_explicit_one_and_defaults_to_disabled(self):
        env = {'TELEGRAM_CHAT_ID': str(CHAT), 'TELEGRAM_TOPIC_ID': str(TOPIC),
               'TELEGRAM_ALLOWED_USER_IDS': str(USER_ID)}
        self.assertFalse(Runtime(CHAT, TOPIC, frozenset({USER_ID}), self.path).fresh_start)
        for value in (None, '', '0', 'true', 'yes', '01', '1'):
            with self.subTest(value=value):
                values = env if value is None else {**env, 'ACORN_FRESH_START': value}
                with patch.dict(os.environ, values, clear=True):
                    self.assertEqual(Runtime.from_env().fresh_start, value == '1')

    def test_service_startup_passes_flag_to_real_store_without_network(self):
        now = care.instant('2026-10-20T15:13:22+01:00')
        for flag in ('0', '1'):
            with self.subTest(flag=flag):
                path = self.path.parent / (flag + '.json')
                env = {'TELEGRAM_CHAT_ID': str(CHAT), 'TELEGRAM_TOPIC_ID': str(TOPIC),
                       'TELEGRAM_ALLOWED_USER_IDS': str(USER_ID),
                       'ACORN_STATE_PATH': str(path), 'ACORN_FRESH_START': flag}
                with (patch.dict(os.environ, env, clear=True),
                      patch('service.read_token', return_value='synthetic-test-token'),
                      patch('service.Client', return_value=FakeClient()),
                      patch('service.os.umask'), patch('service.signal.signal'),
                      patch('service.care.instant', return_value=now),
                      patch('service.health_server') as health,
                      patch('service.threading.Thread') as thread,
                      patch.object(Bot, 'poll_loop', lambda bot: bot.stop.set())):
                    thread.return_value.is_alive.return_value = False
                    self.assertEqual(service.main(), 1)
                    health.assert_called_once()
                meta = Store(path).snapshot()['standalone']
                self.assertEqual(meta.get('fresh_start_cutoff'), care.iso(now) if flag == '1' else None)

    def test_new_fresh_state_is_empty_and_preserves_entire_care_plan(self):
        config = copy.deepcopy(care.BOOT_CONFIG)
        config['food_transfer_policy'] = 'add_full'
        with patch.object(care, 'BOOT_CONFIG', config):
            expected = care.initial()
            self.bind('2026-11-02T15:13:22.123456+00:00')
        state = self.store.snapshot()
        self.assertEqual(state['standalone']['fresh_start_cutoff'], '2026-11-02T15:13:22.123456+00:00')
        self.assertEqual(state['standalone']['offset'], 0)
        self.assertEqual(state['standalone']['outbox'], {})
        self.assertEqual(state.get('carryovers', {}), {})
        self.assertEqual([entry['kind'] for entry in state['audit']], ['standalone_started'])
        for key, value in expected.items():
            if key != 'audit':
                self.assertEqual(state[key], value, key)

    def test_prior_days_and_elapsed_meals_are_absent_but_next_meal_is_normal(self):
        self.bind('2026-11-02T15:13:22+00:00')
        original = self.store.snapshot()
        for stamp in ('2026-11-02T15:13:22+00:00', '2026-11-02T18:59:59+00:00'):
            text, state = self.tick(stamp)
            self.assertEqual(text, 'NO_REPLY')
            self.assertEqual(state['checklists'], {})
            self.assertEqual(state['tasks'], {})
            self.assertEqual(state.get('carryovers', {}), {})
        text, state = self.tick('2026-11-02T19:00:00+00:00')
        cid = '2026-11-02_19:00'
        self.assertTrue(text.startswith('🐾 Acorn · ужин и вечерняя уборка\n'))
        self.assertEqual(set(state['checklists']), {cid})
        self.assertEqual(state['checklists'][cid]['stage']['id'], 'week_3')
        self.assertEqual(state['checklists'][cid]['announced_at'], '2026-11-02T19:00:00+00:00')
        self.assertEqual(state['tasks'][cid + '_wet']['grams'], 65)
        self.assertEqual(state['tasks'][cid + '_dry']['grams'], 16)
        for task in state['tasks'].values():
            self.assertEqual(task['due_at'], '2026-11-02T19:00:00+00:00')
            self.assertEqual(task['status'], 'pending')
            self.assertIsNone(task['done_at'])
            self.assertIsNone(task['skip_at'])
            self.assertNotIn('carried_grams', task)
        for key in ('config', 'schedule_history'):
            self.assertEqual(state[key], original[key])
        self.assertEqual(self.tick('2026-11-02T19:00:01+00:00')[0], 'NO_REPLY')

    def test_exact_cutoff_boundary_is_excluded_in_equivalent_timezones(self):
        cases = [
            ('2026-10-20T12:59:59.999999+00:00', True),
            ('2026-10-20T13:00:00+00:00', False),
            ('2026-10-20T13:00:00.000001+00:00', False),
            ('2026-10-20T14:00:00+01:00', False),
        ]
        for index, (stamp, included) in enumerate(cases):
            with self.subTest(cutoff=stamp):
                store = Store(self.path.parent / (str(index) + '.json'))
                self.bind(stamp, store=store)
                _, state = self.tick('2026-10-20T14:00:00+01:00', store=store)
                self.assertEqual(set(state['checklists']), {'2026-10-20_14:00'} if included else set())
                _, state = self.tick('2026-10-20T19:00:00+01:00', store=store)
                self.assertIn('2026-10-20_19:00', state['checklists'])

    def test_start_after_dinner_waits_for_next_morning_through_quiet_hours(self):
        self.bind('2026-10-21T23:30:00+01:00')
        text, state = self.tick('2026-10-22T06:59:59+01:00')
        self.assertEqual(text, 'NO_REPLY')
        self.assertEqual(state['tasks'], {})
        text, state = self.tick('2026-10-22T07:00:00+01:00')
        self.assertIn('доброе утро', text)
        self.assertEqual(set(state['checklists']), {'2026-10-22_07:00'})
        self.assertEqual(state['tasks']['2026-10-22_07:00_wet']['grams'], 49)

    def test_start_before_diet_does_not_move_first_meal_or_stage_dates(self):
        self.bind('2026-10-18T12:00:00+01:00')
        original = self.store.snapshot()['config']
        self.assertEqual(self.tick('2026-10-18T14:00:00+01:00')[0], 'NO_REPLY')
        _, state = self.tick('2026-10-19T07:00:00+01:00')
        self.assertEqual(set(state['checklists']), {'2026-10-19_07:00'})
        self.assertEqual(state['config'], original)

    def test_restart_keeps_cutoff_and_catches_up_all_later_meals_without_first_tick(self):
        for flag in (False, True):
            with self.subTest(restart_flag=flag):
                path = self.path.parent / (str(flag) + '.json')
                store = Store(path)
                self.bind('2026-10-20T15:13:22+01:00', store=store)
                before = store.snapshot()
                restarted = Store(path)
                self.bind('2026-10-22T08:00:00+01:00', store=restarted, fresh_start=flag)
                self.assertEqual(restarted.snapshot()['standalone'], before['standalone'])
                _, state = self.tick('2026-10-22T08:00:00+01:00', store=restarted)
                self.assertEqual(set(state['checklists']), {
                    '2026-10-20_19:00', '2026-10-21_07:00', '2026-10-21_14:00',
                    '2026-10-21_19:00', '2026-10-22_07:00'})
                self.assertEqual(state['standalone']['fresh_start_cutoff'],
                                 '2026-10-20T15:13:22+01:00')
                self.assertTrue(state['tasks']['2026-10-20_19:00_wet']['needs_clarification'])
                self.assertEqual(state.get('carryovers', {}), {})

    def test_restart_after_first_meal_catches_up_from_saved_tick(self):
        self.bind('2026-10-20T15:13:22+01:00')
        _, before = self.tick('2026-10-20T19:00:00+01:00')
        self.bind('2026-10-22T08:00:00+01:00', store=Store(self.path))
        self.assertEqual(self.store.snapshot()['last_tick'], before['last_tick'])
        self.assertEqual(self.store.snapshot()['standalone'], before['standalone'])
        _, state = self.tick('2026-10-22T08:00:00+01:00')
        self.assertEqual(set(state['checklists']), {
            '2026-10-20_19:00', '2026-10-21_07:00', '2026-10-21_14:00',
            '2026-10-21_19:00', '2026-10-22_07:00'})
        self.assertTrue(state['tasks']['2026-10-20_19:00_wet']['needs_clarification'])
        self.assertEqual(state['tasks']['2026-10-22_07:00_wet']['status'], 'pending')

    def test_restart_catch_up_compares_cutoff_across_seasonal_clock_change(self):
        self.bind('2026-10-24T22:00:00+01:00')
        self.bind('2026-10-25T14:00:00+00:00', store=Store(self.path))
        _, state = self.tick('2026-10-25T14:00:00+00:00')
        self.assertEqual(set(state['checklists']), {'2026-10-25_07:00', '2026-10-25_14:00'})
        self.assertEqual(state['tasks']['2026-10-25_07:00_wet']['due_at'], '2026-10-25T07:00:00+00:00')
        self.assertEqual(state['standalone']['fresh_start_cutoff'], '2026-10-24T22:00:00+01:00')

    def test_cutoff_uses_elapsed_time_inside_repeated_clock_hour(self):
        config = copy.deepcopy(care.BOOT_CONFIG)
        config['quiet_end'] = '01:00'
        config['slots'][0]['time'] = '01:30'
        with patch.object(care, 'BOOT_CONFIG', config):
            # 01:30 BST (the first occurrence) precedes 01:15 GMT (the second).
            self.bind('2026-10-25T01:15:00+00:00')
        text, state = self.tick('2026-10-25T02:00:00+00:00')
        self.assertEqual(text, 'NO_REPLY')
        self.assertEqual(state['tasks'], {})
        _, state = self.tick('2026-10-25T14:00:00+00:00')
        self.assertEqual(set(state['checklists']), {'2026-10-25_14:00'})

    def test_restart_preserves_real_tasks_confirmations_and_full_food_transfers(self):
        for initially_fresh in (False, True):
            for restart_flag in (False, True):
                with self.subTest(initially_fresh=initially_fresh, restart_flag=restart_flag):
                    path = self.path.parent / f'{initially_fresh}-{restart_flag}.json'
                    store = Store(path)
                    config = copy.deepcopy(care.BOOT_CONFIG)
                    config['food_transfer_policy'] = 'add_full'
                    with patch.object(care, 'BOOT_CONFIG', config):
                        self.bind('2026-10-19T06:00:00+01:00', store=store, fresh_start=initially_fresh)
                    runtime = Runtime(CHAT, TOPIC, frozenset({USER_ID}), path, fresh_start=initially_fresh)
                    now = care.instant('2026-10-19T07:00:00+01:00')
                    bot = Bot(runtime, FakeClient(), BOT_ID, 'example_care_bot', store, lambda: now)
                    bot.dispatch_once()
                    panel = next(iter(store.snapshot()['inline_panels'].values()))
                    source = '2026-10-19_07:00'
                    for uid, (kind, action) in enumerate((('wet', ''), ('dry', 'next:')), 20):
                        index = panel['task_ids'].index(source + '_' + kind)
                        cb = {'id': 'q' + str(uid),
                              'data': inline_ui.PREFIX + panel['nonce'] + ':' + action + str(index),
                              'from': {'id': USER_ID, 'is_bot': False},
                              'message': {'message_id': panel['pages'][-1]['message_id'],
                                          'chat': {'id': CHAT}, 'message_thread_id': TOPIC,
                                          'from': {'id': BOT_ID, 'is_bot': True}}}
                        bot.process_update({'update_id': uid, 'callback_query': cb})
                    before = store.snapshot()
                    self.assertEqual(before['tasks'][source + '_wet']['done_by'], 'telegram:101')
                    self.assertEqual(before['tasks'][source + '_dry']['skip_by'], 'telegram:101')
                    self.assertEqual(before['tasks'][source + '_water']['status'], 'pending')
                    target = '2026-10-19_14:00_dry'
                    self.assertEqual(before['carryovers'][target]['food_grams'], {source + '_dry': 10})
                    self.assertEqual(before['standalone']['offset'], 22)
                    self.assertEqual(set(before['inline_receipts']), {'q20', 'q21'})
                    restarted = Store(path)
                    self.bind('2026-10-19T13:00:00+01:00', store=restarted, fresh_start=restart_flag)
                    after = restarted.snapshot()
                    self.assertEqual(after['audit'][:-1], before['audit'])
                    for key in before.keys() - {'audit'}:
                        self.assertEqual(after[key], before[key], key)
                    _, state = self.tick('2026-10-19T14:00:00+01:00', store=restarted)
                    self.assertEqual(state['carryovers'], {})
                    self.assertEqual(state['tasks'][target]['grams'], 20)
                    self.assertEqual(state['tasks'][target]['planned_grams'], 10)
                    self.assertEqual(state['tasks'][target]['carried_grams'], 10)
                    self.assertEqual(state['tasks'][target]['carried_from'], [source + '_dry'])
                    for kind in ('wet', 'dry'):
                        self.assertEqual(state['tasks'][source + '_' + kind], before['tasks'][source + '_' + kind])
                    self.assertEqual(state['tasks'][source + '_water']['status'], 'pending')
                    self.assertEqual(state['tasks'][source + '_water']['next_repeat_at'],
                                     '2026-10-19T14:30:00+01:00')

    def test_existing_empty_migrated_standalone_state_does_not_get_cutoff(self):
        now = care.instant('2026-10-20T15:13:22+01:00')
        imported = migrate.migrate(care.initial(), USER_ID, now)
        self.assertIsNone(imported['standalone']['identity'])
        self.path.write_text(json.dumps(imported))
        self.bind(now.isoformat())
        self.assertNotIn('fresh_start_cutoff', self.store.snapshot()['standalone'])
        _, state = self.tick(now.isoformat())
        self.assertEqual(set(state['checklists']), {
            '2026-10-19_07:00', '2026-10-19_14:00', '2026-10-19_19:00',
            '2026-10-20_07:00', '2026-10-20_14:00'})

    def test_care_only_state_still_refuses_implicit_adoption_even_when_empty(self):
        for populated in (False, True):
            with self.subTest(populated=populated):
                state = care.initial()
                if populated:
                    care.tick(state, care.instant('2026-10-19T07:00:00+01:00'))
                self.path.write_text(json.dumps(state))
                before = self.path.read_bytes()
                with self.assertRaisesRegex(ValueError, 'Existing state requires explicit migration'):
                    self.bind('2026-10-20T15:13:22+01:00')
                self.assertEqual(self.path.read_bytes(), before)

    def test_flag_does_not_bypass_identity_refusal_or_replace_corrupt_state(self):
        self.bind('2026-10-20T15:13:22+01:00')
        before = self.path.read_bytes()
        for ids in ((999, CHAT, TOPIC), (BOT_ID, -2, TOPIC), (BOT_ID, CHAT, 43)):
            with self.subTest(ids=ids), self.assertRaisesRegex(ValueError, 'different bot, chat, or topic'):
                self.store.bind(*ids, care.instant('2026-10-21T16:00:00+01:00'), fresh_start=True)
            self.assertEqual(self.path.read_bytes(), before)
        self.path.write_text('{bad')
        with self.assertRaises(ValueError):
            self.bind('2026-10-21T16:00:00+01:00')
        self.assertEqual(self.path.read_text(), '{bad')

    def test_fresh_bind_requires_timezone_and_does_not_save_naive_cutoff(self):
        with self.assertRaises(ValueError):
            self.bind('2026-10-20T15:13:22')
        self.assertFalse(self.path.exists())

    def test_disabled_mode_keeps_historical_backfill_and_exact_meal_boundary(self):
        self.bind('2026-10-20T14:00:00+01:00', fresh_start=False)
        self.assertNotIn('fresh_start_cutoff', self.store.snapshot()['standalone'])
        _, state = self.tick('2026-10-20T14:00:00+01:00')
        self.assertEqual(set(state['checklists']), {
            '2026-10-19_07:00', '2026-10-19_14:00', '2026-10-19_19:00',
            '2026-10-20_07:00', '2026-10-20_14:00'})
        self.assertEqual(state['tasks']['2026-10-20_14:00_wet']['grams'], 49)
        self.assertTrue(state['tasks']['2026-10-19_07:00_wet']['needs_clarification'])
        self.assertEqual(state.get('carryovers', {}), {})


if __name__ == '__main__':
    unittest.main()
