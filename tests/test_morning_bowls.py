"""Morning-only bowls use generated records and fake Telegram requests."""
import argparse
import copy
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import care
import inline_ui
from configuration import Runtime, load_care_config, validate_care_config
from service import Bot
from storage import Store
from test_service import BOT_ID, CHAT, TOPIC, FakeClient

ROOT = Path(__file__).resolve().parents[1]
NOW = care.instant('2026-10-20T15:00:00+01:00')


def legacy_config():
    config = copy.deepcopy(care.BOOT_CONFIG)
    config.pop('bowls_morning_only', None)
    config['slots'][2]['cleaning_tasks'] = ['litter', 'bowls']
    return config


def close(state, cid, kind, action='done'):
    args = argparse.Namespace(checklist=cid, types=[kind], action=action,
                              actor='telegram:101', minutes=None,
                              message_id='synthetic-confirmation', original_message_id='777')
    day, hhmm = cid.split('_')
    care.update(state, args, care.wall(date.fromisoformat(day), hhmm))


class MorningBowlsConfigurationTests(unittest.TestCase):
    def test_optional_flag_accepts_only_booleans(self):
        config = legacy_config()
        validate_care_config(config)
        for value in (False, True):
            validate_care_config({**config, 'bowls_morning_only': value})
        for value in (None, 0, 1, 'true', 'false', [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'morning-only bowls flag'):
                validate_care_config({**config, 'bowls_morning_only': value})

    def test_example_opts_in_and_has_exact_routine_cleaning_scope(self):
        config = load_care_config(ROOT / 'config.example.json')
        self.assertIs(config['bowls_morning_only'], True)
        with patch.object(care, 'BOOT_CONFIG', config):
            state = care.initial()
        for index, slot in enumerate(config['slots']):
            cid = care.ensure_checklist(state, date(2026, 10, 20), slot, index)
            kinds = {state['tasks'][key]['type'] for key in state['checklists'][cid]['task_ids']}
            expected = ({'litter', 'bowls', 'water'}, set(), {'litter'})[index]
            self.assertEqual(kinds & care.CLEANING.keys(), expected)
        self.assertEqual(sum(t['grams'] for t in state['tasks'].values() if t['type'] == 'wet'), 195)
        self.assertEqual(sum(t['grams'] for t in state['tasks'].values() if t['type'] == 'dry'), 30)


class MorningBowlsStartupTests(unittest.TestCase):
    def setUp(self):
        tmp = ROOT / '.test-tmp'
        tmp.mkdir(exist_ok=True)
        directory = self.enterContext(tempfile.TemporaryDirectory(dir=tmp))
        self.path = Path(directory) / 'state.json'
        self.store = Store(self.path)
        with patch.object(care, 'BOOT_CONFIG', legacy_config()):
            self.state = care.initial()
        self.state['standalone'] = {
            'schema': 1, 'identity': {'bot_id': BOT_ID, 'chat_id': CHAT, 'topic_id': TOPIC},
            'offset': 456, 'fresh_start_cutoff': '2026-10-19T06:00:00+01:00',
            'outbox': {'synthetic-reply': {'status': 'pending', 'texts': ['Saved reply'],
                                         'panel': None, 'message_ids': [], 'retry_at': 0}}}

    def save(self):
        care.save(self.path, self.state)
        return self.store.snapshot()

    def bind(self, now=NOW, flag=True):
        boot = copy.deepcopy(care.BOOT_CONFIG)
        if flag is None:
            boot.pop('bowls_morning_only', None)
        else:
            boot['bowls_morning_only'] = flag
        with patch.object(care, 'BOOT_CONFIG', boot):
            Store(self.path).bind(BOT_ID, CHAT, TOPIC, now, fresh_start=True)
        return self.store.snapshot()

    def checklist(self, day, index, *, slot=None):
        return care.ensure_checklist(self.state, date.fromisoformat(day),
                                     slot or self.state['config']['slots'][index], index)

    def migration_audits(self, state):
        return [entry for entry in state['audit'] if entry['kind'] == 'bowls_morning_only_applied']

    def test_startup_changes_only_flag_and_bowls_in_current_pending_and_future_plans(self):
        config = self.state['config']
        config['quiet_end'] = '08:00'
        config['quiet_start'] = '22:30'
        config['slots'][0]['time'] = '09:30'
        config['repeat_minutes'] = 45
        config['food_transfer_policy'] = 'add_full'
        config['stages'][1]['start'] = '2026-11-04'
        config['stages'][0].update(wet=[50, 50, 95], dry=[11, 11, 11], daily_dry=33)
        self.state['schedule_history'][0]['slots'] = copy.deepcopy(config['slots'])
        self.state['schedule_history'].append({'effective': '2026-10-20', 'slots': copy.deepcopy(config['slots'])})
        future_slots = copy.deepcopy(config['slots'])
        future_slots[0]['time'] = '07:00'
        future_slots[2]['time'] = '20:00'
        self.state['pending_config'] = {'effective': '2026-10-22', 'slots': copy.deepcopy(future_slots), 'quiet_end': '07:00'}
        self.state['schedule_history'].append({'effective': '2026-10-22', 'slots': future_slots})
        self.state['progression_hold'] = {'stage': 'weeks_1_2', 'since': '2026-10-19T12:00:00+01:00', 'actor': 'telegram:101'}
        self.state['reminder_pause'] = {'until': 'indefinite', 'actor': 'telegram:101'}
        self.state['proposals'] = {'saved': {'status': 'pending', 'config': legacy_config(), 'base_revision': 8}}
        self.state['revision'] = 8
        before = self.save()
        after = self.bind()
        expected = copy.deepcopy(before)
        expected['config']['bowls_morning_only'] = True
        for plan in (expected['config'], expected['pending_config'], *expected['schedule_history'][1:]):
            plan['slots'][2]['cleaning_tasks'] = ['litter']
        self.assertEqual({k: v for k, v in after.items() if k != 'audit'},
                         {k: v for k, v in expected.items() if k != 'audit'})
        entry, = self.migration_audits(after)
        self.assertEqual(entry['effective'], '2026-10-20')
        self.assertEqual(entry['task_ids'], [])
        self.assertEqual(after['audit'][:-2], before['audit'])
        self.assertEqual(after['audit'][-1]['kind'], 'standalone_started')

    def test_today_snapshot_uses_active_history_and_precedes_future_changes(self):
        old = self.state['schedule_history'][0]
        old['slots'][0]['time'] = '08:00'
        future = {'effective': '2026-10-22', 'slots': copy.deepcopy(old['slots'])}
        future['slots'][2]['time'] = '20:00'
        self.state['schedule_history'].append(future)
        before = self.save()
        after = self.bind()
        history = after['schedule_history']
        self.assertEqual([item['effective'] for item in history], ['2026-10-19', '2026-10-20', '2026-10-22'])
        self.assertEqual(history[0], before['schedule_history'][0])
        self.assertEqual(history[1]['slots'][0]['time'], '08:00')
        self.assertEqual(history[2]['slots'][2]['time'], '20:00')
        for day in (date(2026, 10, 20), date(2026, 10, 22), date(2026, 11, 2)):
            self.assertEqual(care.slots_for_day(after, day)[2]['cleaning_tasks'], ['litter'])
        self.assertEqual(care.slots_for_day(after, date(2026, 10, 19))[2]['cleaning_tasks'], ['litter', 'bowls'])
        self.assertEqual(after['tasks'], before['tasks'])
        self.assertEqual(after['checklists'], before['checklists'])

    def test_missing_history_gets_today_snapshot_without_creating_tasks(self):
        for history in (None, []):
            with self.subTest(history=history):
                self.state.pop('schedule_history', None)
                if history is not None:
                    self.state['schedule_history'] = history
                before = self.save()
                after = self.bind()
                self.assertEqual(after['schedule_history'], [{'effective': '2026-10-20', 'slots': after['config']['slots']}])
                self.assertEqual(after['tasks'], before['tasks'])
                self.assertEqual(after['checklists'], before['checklists'])

    def test_implicit_and_disabled_cleaning_lists_change_only_bowls(self):
        self.state['config']['slots'][2].pop('cleaning_tasks')
        self.state['config']['slots'][1]['cleaning_tasks'] = ['bowls', 'litter']
        self.state['schedule_history'][0]['slots'] = copy.deepcopy(self.state['config']['slots'])
        before = self.save()
        after = self.bind()
        slots = after['config']['slots']
        self.assertEqual(slots[0], before['config']['slots'][0])
        self.assertFalse(slots[1]['cleaning'])
        self.assertEqual(slots[1]['cleaning_tasks'], ['litter'])
        self.assertTrue(slots[2]['cleaning'])
        self.assertEqual(slots[2]['cleaning_tasks'], ['litter', 'water'])
        self.assertEqual(after['schedule_history'][0], before['schedule_history'][0])

    def test_only_open_today_and_future_routine_nonbreakfast_bowls_are_skipped(self):
        past = self.checklist('2026-10-19', 2)
        breakfast = self.checklist('2026-10-20', 0)
        lunch_slot = {**self.state['config']['slots'][1], 'cleaning': True, 'cleaning_tasks': ['bowls']}
        lunch = self.checklist('2026-10-20', 1, slot=lunch_slot)
        dinner = self.checklist('2026-10-20', 2)
        future = self.checklist('2026-10-21', 2)
        done = self.checklist('2026-10-22', 2)
        skipped = self.checklist('2026-10-23', 2)
        carried = self.checklist('2026-10-24', 2)
        close(self.state, done, 'bowls')
        close(self.state, skipped, 'bowls', 'skip')
        self.state['tasks'][dinner + '_bowls'].update(status='deferred', needs_clarification=True,
            next_repeat_at='2026-10-20T20:00:00+01:00', snoozed_until='2026-10-20T20:00:00+01:00')
        self.state['tasks'][carried + '_bowls']['carried_from'] = [past + '_bowls']
        self.state['tasks'][breakfast + '_wet']['carried_from'] = [past + '_wet']
        before = self.save()
        after = self.bind()
        cancelled = [lunch + '_bowls', dinner + '_bowls', future + '_bowls']
        expected = copy.deepcopy(before['tasks'])
        for key in cancelled:
            expected[key].update(status='skipped', skip_by='configuration', skip_at=care.iso(NOW),
                skip_reason='bowls_morning_only', next_repeat_at=None, snoozed_until=None, needs_clarification=False)
            task = after['tasks'][key]
            self.assertIsNone(task['done_at'])
            self.assertIsNone(task['done_by'])
            self.assertIn('Пропущено:', care.task_line(task, state=after))
        self.assertEqual(after['tasks'], expected)
        self.assertEqual(after['checklists'], before['checklists'])
        self.assertEqual(set(after['tasks']), set(before['tasks']))
        entry, = self.migration_audits(after)
        self.assertEqual(entry['task_ids'], cancelled)
        self.assertIn('only at breakfast', entry['reason'])
        self.assertEqual(after['standalone'], before['standalone'])

    def test_pending_0700_change_keeps_historical_confirmation_and_has_no_duplicate_food(self):
        self.state['config']['quiet_end'] = '08:00'
        self.state['config']['slots'][0]['time'] = '09:30'
        self.state['schedule_history'][0]['slots'] = copy.deepcopy(self.state['config']['slots'])
        past = self.checklist('2026-10-19', 0)
        close(self.state, past, 'wet')
        self.state['last_tick'] = '2026-10-19T09:30:00+01:00'
        slots = copy.deepcopy(self.state['config']['slots'])
        slots[0]['time'] = '07:00'
        self.state['pending_config'] = {'effective': '2026-10-20', 'slots': copy.deepcopy(slots), 'quiet_end': '07:00'}
        self.state['schedule_history'].append({'effective': '2026-10-20', 'slots': slots})
        before = self.save()
        after = self.bind(care.instant('2026-10-20T06:59:00+01:00'))
        self.assertEqual(after['tasks'], before['tasks'])
        self.assertEqual(after['checklists'], before['checklists'])
        self.assertEqual(after['config']['slots'][0]['time'], '09:30')
        self.assertEqual(after['config']['quiet_end'], '08:00')
        self.assertEqual(after['pending_config']['quiet_end'], '07:00')
        self.assertEqual(after['pending_config']['slots'][0]['time'], '07:00')
        self.assertEqual(care.tick(after, care.instant('2026-10-20T06:59:00+01:00')), 'NO_REPLY')
        text = care.tick(after, care.instant('2026-10-20T07:00:00+01:00'))
        self.assertIn('20 октября · 07:00', text)
        self.assertIn(care.CLEANING['bowls'], text)
        self.assertIsNone(after['pending_config'])
        self.assertEqual(care.tick(after, care.instant('2026-10-20T07:00:01+01:00')), 'NO_REPLY')
        care.tick(after, care.instant('2026-10-20T19:00:00+01:00'))
        self.assertEqual(after['tasks'][past + '_wet'], before['tasks'][past + '_wet'])
        self.assertEqual(sorted(cid for cid in after['checklists'] if cid.startswith('2026-10-20')),
                         ['2026-10-20_07:00', '2026-10-20_14:00', '2026-10-20_19:00'])
        today_food = [t for key, t in after['tasks'].items() if key.startswith('2026-10-20') and t['type'] in {'wet', 'dry'}]
        self.assertEqual(len(today_food), 6)
        self.assertEqual(sum(t['grams'] for t in today_food if t['type'] == 'wet'), 195)
        self.assertEqual(sum(t['grams'] for t in today_food if t['type'] == 'dry'), 30)
        self.assertNotIn('2026-10-20_19:00_bowls', after['tasks'])

    def test_already_current_plan_needs_no_snapshot_or_schedule_audit(self):
        self.state['config'] = copy.deepcopy(care.BOOT_CONFIG)
        self.state['schedule_history'][0]['slots'] = copy.deepcopy(self.state['config']['slots'])
        before = self.save()
        after = self.bind()
        self.assertEqual(self.migration_audits(after), [])
        self.assertEqual(after['audit'][:-1], before['audit'])
        self.assertEqual(after['audit'][-1]['kind'], 'standalone_started')
        self.assertEqual({k: v for k, v in after.items() if k != 'audit'},
                         {k: v for k, v in before.items() if k != 'audit'})

    def test_restarts_are_idempotent_and_false_or_omitted_flag_does_not_roll_back(self):
        self.checklist('2026-10-20', 2)
        self.save()
        before = self.bind()
        for now, flag in ((NOW, True), (care.instant('2026-10-21T06:00:00+01:00'), True),
                          (NOW, False), (NOW, None)):
            with self.subTest(now=now, flag=flag):
                after = self.bind(now, flag)
                self.assertEqual({k: v for k, v in after.items() if k != 'audit'},
                                 {k: v for k, v in before.items() if k != 'audit'})
                self.assertEqual(self.migration_audits(after), self.migration_audits(before))
                self.assertEqual(after['audit'][:-1], before['audit'])
                before = after
        self.assertEqual(len(self.migration_audits(before)), 1)

    def test_false_or_omitted_boot_flag_never_applies_migration_to_legacy_state(self):
        for saved_flag in (None, False, True):
            for boot_flag in (None, False):
                with self.subTest(saved_flag=saved_flag, boot_flag=boot_flag):
                    self.state['config'].pop('bowls_morning_only', None)
                    if saved_flag is not None:
                        self.state['config']['bowls_morning_only'] = saved_flag
                    before = self.save()
                    after = self.bind(flag=boot_flag)
                    self.assertEqual(after['audit'], before['audit'] + [after['audit'][-1]])
                    self.assertEqual(after['audit'][-1]['kind'], 'standalone_started')
                    self.assertEqual({k: v for k, v in after.items() if k != 'audit'},
                                     {k: v for k, v in before.items() if k != 'audit'})

    def test_applied_and_queued_transfers_and_confirmations_survive_and_apply_once(self):
        for precreated in (False, True):
            with self.subTest(precreated=precreated):
                with patch.object(care, 'BOOT_CONFIG', legacy_config()):
                    self.state = care.initial()
                self.state['standalone'] = {'schema': 1, 'identity': None, 'offset': 456, 'outbox': {},
                                           'fresh_start_cutoff': '2026-10-19T06:00:00+01:00'}
                self.state['config']['food_transfer_policy'] = 'add_full'
                source = self.checklist('2026-10-19', 0)
                close(self.state, source, 'wet')
                care.move_to_next(self.state, source + '_bowls', 'telegram:101',
                                  care.instant('2026-10-19T07:01:00+01:00'))
                lunch = self.checklist('2026-10-19', 1)
                self.assertEqual(self.state['tasks'][lunch + '_bowls']['carried_from'], [source + '_bowls'])
                today_breakfast = self.checklist('2026-10-20', 0)
                close(self.state, today_breakfast, 'wet')
                self.checklist('2026-10-20', 1)
                move_at = care.instant('2026-10-20T14:01:00+01:00')
                for key in (source + '_dry', lunch + '_bowls'):
                    care.move_to_next(self.state, key, 'telegram:101', move_at)
                if precreated:
                    # Keep a queued cleaning exception even before carried_from reaches its target.
                    queue = self.state.pop('carryovers')
                    self.checklist('2026-10-20', 2)
                    self.state['carryovers'] = queue
                self.state['last_tick'] = '2026-10-20T14:00:00+01:00'
                for checklist in self.state['checklists'].values():
                    checklist['announced_at'] = care.iso(move_at)
                before = self.save()
                after = self.bind()
                for field in ('tasks', 'checklists', 'carryovers', 'last_tick'):
                    self.assertEqual(after[field], before[field], field)
                care.save(self.path, after)
                self.assertEqual(self.bind()['carryovers'], before['carryovers'])
                at = care.instant('2026-10-20T19:00:00+01:00')
                with self.store.transaction() as state:
                    text = care.tick(state, at)
                self.assertIn(care.CLEANING['bowls'], text)
                after = self.store.snapshot()
                target = '2026-10-20_19:00'
                bowls = after['tasks'][target + '_bowls']
                self.assertEqual(bowls['carried_from'], [lunch + '_bowls'])
                self.assertEqual(bowls['status'], 'pending')
                dry = after['tasks'][target + '_dry']
                self.assertEqual((dry['planned_grams'], dry['carried_grams'], dry['grams']), (10, 10, 20))
                self.assertEqual(dry['carried_portions'], {source + '_dry': 10})
                self.assertEqual(after['carryovers'], {})
                for key in (source + '_wet', source + '_dry', lunch + '_bowls', today_breakfast + '_wet'):
                    self.assertEqual(after['tasks'][key], before['tasks'][key])
                food = {key: t for key, t in after['tasks'].items() if t['type'] in {'wet', 'dry'}}
                restarted = self.bind(care.instant('2026-10-20T19:00:01+01:00'))
                self.assertEqual(restarted['tasks'], after['tasks'])
                self.assertEqual(care.tick(restarted, care.instant('2026-10-20T19:00:01+01:00')), 'NO_REPLY')
                self.assertEqual({key: t for key, t in restarted['tasks'].items() if t['type'] in {'wet', 'dry'}}, food)
                today = [t for key, t in food.items() if key.startswith('2026-10-20')]
                self.assertEqual(sum(t['grams'] for t in today if t['type'] == 'wet'), 195)
                self.assertEqual(sum(t['grams'] for t in today if t['type'] == 'dry'), 40)

    def test_queued_bowls_reminder_is_cancelled_without_editing_outbox_at_startup(self):
        cid = self.checklist('2026-10-20', 2)
        key = cid + '_bowls'
        now = care.instant('2026-10-20T19:30:00+01:00')
        text = care.render_reminders(self.state, [self.state['tasks'][key]])
        self.state['emissions'] = {'saved': {'primary': None, 'repeat_ids': [key], 'morning': False, 'notices': []}}
        panel = inline_ui.prepare(self.state, [text], [key], now)
        self.state['standalone']['outbox']['reminder:saved'] = {
            'status': 'pending', 'panel': panel['nonce'], 'emission': 'saved', 'message_ids': [], 'retry_at': 0}
        before = self.save()
        after = self.bind(now)
        self.assertEqual(after['standalone'], before['standalone'])
        self.assertEqual(after['inline_panels'], before['inline_panels'])
        self.assertEqual(after['emissions'], before['emissions'])
        self.assertEqual(care.refresh(after, 'saved', now), 'NO_REPLY')
        self.assertEqual(care.emission_task_ids(after, 'saved'), [])
        self.assertEqual(inline_ui.keyboard(after, after['inline_panels'][panel['nonce']]), {'inline_keyboard': []})
        client = FakeClient()
        runtime = Runtime(CHAT, TOPIC, frozenset({101}), self.path)
        bot = Bot(runtime, client, BOT_ID, 'example_care_bot', self.store, lambda: now)
        bot.deliver('reminder:saved', now)
        self.assertEqual(client.calls, [])
        self.assertEqual(self.store.snapshot()['standalone']['outbox']['reminder:saved']['status'], 'suppressed')
        self.assertEqual(self.store.snapshot()['tasks'][key]['status'], 'skipped')
        self.assertIsNone(self.store.snapshot()['tasks'][key]['done_at'])

    def test_wrong_identity_and_failed_save_cannot_persist_partial_migration(self):
        self.checklist('2026-10-20', 2)
        self.save()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'different bot, chat, or topic'):
            self.store.bind(999, CHAT, TOPIC, NOW)
        self.assertEqual(self.path.read_bytes(), before)
        with patch.object(care, 'save', side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(OSError):
                self.bind()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(self.migration_audits(self.bind())), 1)


if __name__ == '__main__':
    unittest.main()
