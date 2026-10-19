"""Offline next-care contracts, including food safety and persisted provenance."""
import copy
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import care
import inline_ui
import migrate
from configuration import Runtime
from service import Bot, chunks
from storage import Store
from telegram_api import TelegramError
from test_service import FakeClient

TMP = Path(__file__).resolve().parents[1] / '.test-tmp'
CHAT, TOPIC, BOT_ID, USER_ID = -1000000000001, 42, 12345, 101
SOURCE = '2026-10-19_07:00'
TARGET = '2026-10-19_14:00'
NOW = care.instant('2026-10-19T07:01:00+01:00')


def started(day='2026-10-19', hhmm='07:00'):
    state = care.initial()
    now = care.wall(date.fromisoformat(day), hhmm)
    state['last_tick'] = care.iso(care.wall(now.date(), '06:00'))
    care.tick(state, now)
    return state, now


def move(state, key, now=NOW):
    return care.move_to_next(state, key, 'telegram:101', now, 'telegram_callback_move', '101')


def sent_panel(state, cid=SOURCE, message_id=777, now=NOW):
    panel = inline_ui.prepare(state, [care.render_checklist(state, cid)],
                              state['checklists'][cid]['task_ids'], now, primary=cid)
    panel['status'] = 'sent'
    panel['pages'][-1]['message_id'] = message_id
    panel['last_digest'] = inline_ui.digest(state, panel)
    return panel


def callback(panel, action='next:0', query_id='q1'):
    return {'id': query_id, 'data': inline_ui.PREFIX + panel['nonce'] + ':' + action,
            'from': {'id': USER_ID, 'is_bot': False, 'username': 'display_only'},
            'message': {'message_id': panel['pages'][-1]['message_id'],
                        'chat': {'id': CHAT}, 'message_thread_id': TOPIC,
                        'from': {'id': BOT_ID, 'is_bot': True}}}


def accept(state, cb, now=NOW):
    return inline_ui.accept_callback(state, cb, {USER_ID, 102}, BOT_ID, now, CHAT, TOPIC)


class CarryoverTests(unittest.TestCase):
    def setUp(self):
        self.state, _ = started()

    def test_each_kind_skips_only_source_and_queues_without_creating_future_tasks(self):
        for kind in ('wet', 'dry', *care.CLEANING):
            with self.subTest(kind=kind):
                state = copy.deepcopy(self.state)
                key = SOURCE + '_' + kind
                before = copy.deepcopy(state)
                result = move(state, key)
                target = TARGET + '_' + kind
                self.assertEqual(result, {'status': 'moved', 'food': kind in {'wet', 'dry'},
                                          'target_id': target, 'due_at': '2026-10-19T14:00:00+01:00'})
                self.assertEqual(state['carryovers'], {target: {
                    'checklist': TARGET, 'type': kind, 'due_at': result['due_at'], 'source_ids': [key]}})
                self.assertEqual(state['config'], before['config'])
                self.assertEqual(state['schedule_history'], before['schedule_history'])
                self.assertEqual(state['checklists'], before['checklists'])
                self.assertEqual(set(state['tasks']), set(before['tasks']))
                for other in state['tasks']:
                    if other != key:
                        self.assertEqual(state['tasks'][other], before['tasks'][other])
                task = state['tasks'][key]
                self.assertEqual(task['status'], 'skipped')
                self.assertEqual(task['skip_by'], 'telegram:101')
                self.assertEqual(task['skip_at'], care.iso(NOW))
                self.assertEqual(task['skip_reason'], 'next_care')
                self.assertEqual(task['moved_to'], target)
                self.assertEqual(task['moved_due_at'], result['due_at'])
                self.assertEqual(task['moved_food_without_addition'], result['food'])
                self.assertIsNone(task['done_by'])
                self.assertIsNone(task['done_at'])
                self.assertIsNone(task['next_repeat_at'])
                self.assertIsNone(task['snoozed_until'])
                self.assertFalse(task['needs_clarification'])
                self.assertEqual(task['grams'], before['tasks'][key]['grams'])
                entry = state['audit'][-1]
                self.assertEqual(entry, {'kind': 'move_to_next', 'actor': 'telegram:101',
                    'at': care.iso(NOW), 'task_id': key, 'target_id': target,
                    'target_due_at': result['due_at'], 'food_without_addition': result['food'],
                    'message_id': 'telegram_callback_move', 'telegram_user_id': '101'})

    def test_old_state_without_queue_needs_no_migration_or_task_changes(self):
        self.assertNotIn('carryovers', self.state)
        before = copy.deepcopy(self.state)
        care.apply_carryovers(self.state, SOURCE)
        slot = self.state['config']['slots'][0]
        self.assertEqual(care.ensure_checklist(self.state, NOW.date(), slot, 0), SOURCE)
        self.assertEqual(self.state, before)
        move(self.state, SOURCE + '_litter')
        self.assertIn(TARGET + '_litter', self.state['carryovers'])

    def test_repeat_and_snooze_are_cancelled_without_affecting_other_reminders(self):
        key = SOURCE + '_wet'
        self.state['tasks'][key].update(status='deferred', needs_clarification=True,
            snoozed_until='2026-10-19T08:00:00+01:00', next_repeat_at='2026-10-19T08:00:00+01:00')
        move(self.state, key)
        care.tick(self.state, care.instant('2026-10-19T08:00:00+01:00'))
        repeats = self.state['emissions'][self.state['last_emission_id']]['repeat_ids']
        self.assertNotIn(key, repeats)
        self.assertIn(SOURCE + '_dry', repeats)
        self.assertEqual(self.state['tasks'][key]['status'], 'skipped')
        self.assertIsNone(self.state['tasks'][key]['next_repeat_at'])
        self.assertIsNone(self.state['tasks'][key]['snoozed_until'])
        self.assertFalse(self.state['tasks'][key]['needs_clarification'])

    def test_task_line_shows_skipped_and_next_due_without_an_extra_food_request(self):
        for kind in ('wet', 'dry', 'litter'):
            key = SOURCE + '_' + kind
            move(self.state, key)
            line = care.task_line(self.state['tasks'][key], primary=True)
            self.assertIn('Пропущено:', line)
            self.assertIn('19.10 14:00', line)
            self.assertNotIn('☐', line)
            if kind in {'wet', 'dry'}:
                self.assertIn('без добавления этой порции', line)
            else:
                self.assertIn('Перенесено на', line)

    def test_missing_and_future_source_leave_entire_state_unchanged(self):
        for key, now in [('unknown', NOW), (SOURCE + '_wet', NOW - timedelta(minutes=2))]:
            with self.subTest(key=key):
                before = copy.deepcopy(self.state)
                with self.assertRaises(ValueError):
                    move(self.state, key, now)
                self.assertEqual(self.state, before)

    def test_closed_source_is_a_pure_noop(self):
        for status in care.CLOSED:
            with self.subTest(status=status):
                state = copy.deepcopy(self.state)
                state['tasks'][SOURCE + '_wet']['status'] = status
                before = copy.deepcopy(state)
                result = move(state, SOURCE + '_wet')
                self.assertEqual(result, {'status': 'already_closed', 'food': True})
                self.assertEqual(state, before)
                self.assertIn('уже закрыт', care.move_answer(result))

    def test_repeated_move_preserves_first_actor_queue_and_audit(self):
        key = SOURCE + '_litter'
        move(self.state, key)
        before = copy.deepcopy(self.state)
        care.move_to_next(self.state, key, 'telegram:102', NOW + timedelta(minutes=1))
        self.assertEqual(self.state, before)

    def test_closed_target_rejects_before_any_source_or_queue_mutation(self):
        slot = self.state['config']['slots'][1]
        care.ensure_checklist(self.state, NOW.date(), slot, 1)
        for status in care.CLOSED:
            with self.subTest(status=status):
                self.state['tasks'][TARGET + '_wet']['status'] = status
                before = copy.deepcopy(self.state)
                with self.assertRaises(ValueError):
                    move(self.state, SOURCE + '_wet')
                self.assertEqual(self.state, before)

    def test_next_care_uses_strictly_future_slot_and_next_day_after_dinner(self):
        cases = [('06:59', '2026-10-19_07:00', 0), ('07:00', TARGET, 1),
                 ('14:00', '2026-10-19_19:00', 2), ('19:00', '2026-10-20_07:00', 0)]
        for hhmm, cid, expected_index in cases:
            with self.subTest(time=hhmm):
                before = copy.deepcopy(self.state)
                at, index, slot = care.next_care(self.state, care.wall(NOW.date(), hhmm))
                self.assertEqual(care.checklist_id(at.date(), slot), cid)
                self.assertEqual(index, expected_index)
                self.assertEqual(before, self.state)

    def test_unavailable_next_slot_rejects_without_mutation(self):
        self.state['config']['start_date'] = '2026-10-25'
        before = copy.deepcopy(self.state)
        with self.assertRaises(ValueError):
            move(self.state, SOURCE + '_litter')
        self.assertEqual(before, self.state)

    def test_next_day_uses_local_time_across_clock_change(self):
        for day, expected in [('2026-10-24', '2026-10-25T07:00:00+00:00'),
                              ('2027-03-27', '2027-03-28T07:00:00+01:00')]:
            with self.subTest(day=day):
                state, now = started(day, '19:00')
                result = move(state, day + '_19:00_litter', now)
                self.assertEqual(result['due_at'], expected)

    def test_cleaning_is_added_once_at_lunch_without_changing_routine_scope(self):
        before = copy.deepcopy(self.state['config'])
        for kind in care.CLEANING:
            move(self.state, SOURCE + '_' + kind)
        care.tick(self.state, care.wall(NOW.date(), '14:00'))
        checklist = self.state['checklists'][TARGET]
        self.assertEqual(len(checklist['task_ids']), 5)
        for kind in care.CLEANING:
            target = self.state['tasks'][TARGET + '_' + kind]
            self.assertIsNone(target['grams'])
            self.assertEqual(target['status'], 'pending')
            self.assertEqual(target['carried_from'], [SOURCE + '_' + kind])
            self.assertEqual(target['due_at'], '2026-10-19T14:00:00+01:00')
        self.assertEqual(self.state['carryovers'], {})
        slot = before['slots'][1]
        snapshot = copy.deepcopy(self.state)
        care.ensure_checklist(self.state, NOW.date(), slot, 1)
        self.assertEqual(self.state, snapshot)
        care.tick(self.state, care.wall(NOW.date(), '19:00'))
        self.assertNotIn('2026-10-19_19:00_water', self.state['tasks'])
        care.tick(self.state, care.wall(NOW.date() + timedelta(days=1), '14:00'))
        next_lunch = self.state['checklists']['2026-10-20_14:00']
        self.assertEqual({self.state['tasks'][k]['type'] for k in next_lunch['task_ids']}, {'wet', 'dry'})
        self.assertEqual(self.state['config'], before)

    def test_multiple_sources_merge_into_one_queued_kind_with_full_provenance(self):
        care.tick(self.state, care.wall(NOW.date() + timedelta(days=1), '07:00'))
        now = care.wall(NOW.date() + timedelta(days=1), '07:01')
        sources = [SOURCE + '_litter', '2026-10-20_07:00_litter']
        for key in sources:
            move(self.state, key, now)
        target = '2026-10-20_14:00_litter'
        self.assertEqual(self.state['carryovers'][target]['source_ids'], sources)
        self.assertEqual(len(self.state['carryovers']), 1)
        care.tick(self.state, care.wall(now.date(), '14:00'))
        self.assertEqual(self.state['tasks'][target]['carried_from'], sources)
        self.assertEqual(self.state['checklists']['2026-10-20_14:00']['task_ids'].count(target), 1)
        self.assertEqual(len([x for x in self.state['audit'] if x['kind'] == 'move_to_next']), 2)

    def test_existing_routine_target_merges_without_changing_grams_or_task_count(self):
        slot = self.state['config']['slots'][1]
        care.ensure_checklist(self.state, NOW.date(), slot, 1)
        before = copy.deepcopy(self.state['tasks'][TARGET + '_wet'])
        count = len(self.state['tasks'])
        move(self.state, SOURCE + '_wet')
        after = self.state['tasks'][TARGET + '_wet']
        self.assertEqual({k: v for k, v in after.items() if k != 'carried_from'}, before)
        self.assertEqual(after['carried_from'], [SOURCE + '_wet'])
        self.assertEqual(len(self.state['tasks']), count)
        self.assertNotIn('carryovers', self.state)

    def test_queued_food_never_adds_grams_to_same_stage_portion(self):
        for kind, grams in [('wet', 49), ('dry', 10)]:
            move(self.state, SOURCE + '_' + kind)
        care.tick(self.state, care.wall(NOW.date(), '14:00'))
        for kind, grams in [('wet', 49), ('dry', 10)]:
            task = self.state['tasks'][TARGET + '_' + kind]
            self.assertEqual(task['grams'], grams)
            self.assertEqual(task['carried_from'], [SOURCE + '_' + kind])
        self.assertEqual(self.state['carryovers'], {})

    def test_stage_boundaries_use_new_plan_and_never_recreate_absent_food(self):
        cases = [('2026-11-01', '2026-11-02', 33, 15),
                 ('2026-11-08', '2026-11-09', 0, 21),
                 ('2026-11-15', '2026-11-16', 0, 26)]
        for day, next_day, wet, dry in cases:
            for kind, grams in [('wet', wet), ('dry', dry)]:
                with self.subTest(day=day, kind=kind):
                    state, now = started(day, '19:00')
                    key, target = day + '_19:00_' + kind, next_day + '_07:00_' + kind
                    result = move(state, key, now)
                    self.assertEqual(result['target_id'], target if grams else None)
                    self.assertNotIn(next_day + '_07:00', state['checklists'])
                    answer = care.move_answer(result)
                    self.assertIn('Порция пропущена', answer)
                    self.assertTrue('без добавления' in answer or 'Добавки не будет' in answer)
                    text = care.tick(state, care.wall(date.fromisoformat(next_day), '07:00'))
                    self.assertIn('Сегодня меняются порции', text)
                    if grams:
                        self.assertEqual(state['tasks'][target]['grams'], grams)
                        self.assertEqual(state['tasks'][target]['carried_from'], [key])
                    else:
                        self.assertNotIn(target, state['tasks'])
                        self.assertIsNone(state['tasks'][key]['moved_to'])
                    self.assertEqual(state['tasks'][key]['status'], 'skipped')

    def test_health_hold_controls_target_plan_without_changing_the_hold(self):
        state, now = started('2026-11-08', '19:00')
        care.health_hold(state, 'telegram:101', 'Veterinary review', now)
        hold = copy.deepcopy(state['progression_hold'])
        result = move(state, '2026-11-08_19:00_wet', now)
        self.assertEqual(result['target_id'], '2026-11-09_07:00_wet')
        text = care.tick(state, care.wall(date(2026, 11, 9), '07:00'))
        self.assertIn('смена этапов приостановлена', text)
        self.assertEqual(state['tasks'][result['target_id']]['grams'], 33)
        self.assertEqual(state['progression_hold'], hold)

    def test_food_removed_after_queue_does_not_create_an_unplanned_portion(self):
        move(self.state, SOURCE + '_wet')
        self.state['config']['stages'][0]['wet'][1] = 0
        care.tick(self.state, care.wall(NOW.date(), '14:00'))
        self.assertNotIn(TARGET + '_wet', self.state['tasks'])
        self.assertEqual(self.state['carryovers'], {})
        self.assertEqual(self.state['tasks'][SOURCE + '_wet']['status'], 'skipped')

    def test_food_portion_changed_after_queue_stays_the_new_planned_amount(self):
        move(self.state, SOURCE + '_wet')
        self.state['config']['stages'][0]['wet'][1] = 12
        care.tick(self.state, care.wall(NOW.date(), '14:00'))
        self.assertEqual(self.state['tasks'][TARGET + '_wet']['grams'], 12)
        self.assertEqual(self.state['tasks'][SOURCE + '_wet']['grams'], 49)

    def test_queue_waits_until_due_and_does_not_suppress_primary_announcement(self):
        key = SOURCE + '_litter'
        move(self.state, key)
        queued = copy.deepcopy(self.state['carryovers'])
        care.tick(self.state, care.instant('2026-10-19T13:59:59+01:00'))
        self.assertEqual(self.state['carryovers'], queued)
        self.assertNotIn(TARGET, self.state['checklists'])
        self.assertNotIn(TARGET + '_litter', self.state['tasks'])
        text = care.tick(self.state, care.wall(NOW.date(), '14:00'))
        eid = self.state['last_emission_id']
        self.assertEqual(self.state['emissions'][eid]['primary'], TARGET)
        self.assertEqual(self.state['checklists'][TARGET]['announced_at'], '2026-10-19T14:00:00+01:00')
        self.assertTrue(text.startswith('🐾 Acorn · обед\n'))
        self.assertIn(care.CLEANING['litter'], text)
        self.assertNotIn(key, self.state['emissions'][eid]['repeat_ids'])
        self.assertIn(TARGET + '_litter', care.emission_task_ids(self.state, eid))
        self.assertIn(care.CLEANING['litter'], care.refresh(self.state, eid,
                                                        care.wall(NOW.date(), '14:00')))
        self.assertEqual(care.tick(self.state, care.instant('2026-10-19T14:00:01+01:00')), 'NO_REPLY')

    def test_confirmed_earlier_breakfast_uses_history_before_pending_config_activates(self):
        state, now = started('2026-10-19', '19:00')
        state['config']['slots'][0]['time'] = '09:30'
        state['config']['quiet_end'] = '08:00'
        state['schedule_history'][0]['slots'] = copy.deepcopy(state['config']['slots'])
        slots = copy.deepcopy(state['config']['slots'])
        slots[0]['time'] = '07:00'
        state['schedule_history'].append({'effective': '2026-10-20', 'slots': slots})
        state['pending_config'] = {'effective': '2026-10-20', 'slots': slots, 'quiet_end': '07:00'}
        pending = copy.deepcopy(state['pending_config'])
        result = move(state, '2026-10-19_19:00_litter', now)
        self.assertEqual(result['target_id'], '2026-10-20_07:00_litter')
        self.assertEqual(state['pending_config'], pending)
        self.assertNotIn('2026-10-20_07:00', state['checklists'])
        self.assertEqual(care.tick(state, care.instant('2026-10-20T06:59:59+01:00')), 'NO_REPLY')
        text = care.tick(state, care.instant('2026-10-20T07:00:00+01:00'))
        self.assertIn('20 октября · 07:00', text)
        self.assertEqual(state['emissions'][state['last_emission_id']]['primary'], '2026-10-20_07:00')
        self.assertEqual(state['tasks'][result['target_id']]['carried_from'], ['2026-10-19_19:00_litter'])
        self.assertNotIn('2026-10-20_09:30', state['checklists'])
        self.assertIsNone(state['pending_config'])

    def test_confirmed_later_dinner_is_used_without_moving_past_checklists(self):
        state, now = started('2026-10-20', '14:00')
        slots = copy.deepcopy(state['config']['slots'])
        slots[2]['time'] = '19:30'
        state['schedule_history'].append({'effective': '2026-10-20', 'slots': slots})
        state['pending_config'] = {'effective': '2026-10-20', 'slots': slots}
        result = move(state, '2026-10-20_14:00_dry', now)
        self.assertEqual(result['target_id'], '2026-10-20_19:30_dry')
        care.tick(state, care.wall(now.date(), '19:00'))
        self.assertNotIn('2026-10-20_19:00', state['checklists'])
        care.tick(state, care.wall(now.date(), '19:30'))
        self.assertEqual(state['tasks'][result['target_id']]['grams'], 10)
        self.assertEqual(state['emissions'][state['last_emission_id']]['primary'], '2026-10-20_19:30')

    def test_duplicate_provenance_is_not_appended_when_queue_is_reapplied(self):
        key, target = SOURCE + '_wet', TARGET + '_wet'
        move(self.state, key)
        care.tick(self.state, care.wall(NOW.date(), '14:00'))
        self.state['carryovers'][target] = {'checklist': TARGET, 'type': 'wet',
            'due_at': '2026-10-19T14:00:00+01:00', 'source_ids': [key, key]}
        care.apply_carryovers(self.state, TARGET)
        self.assertEqual(self.state['tasks'][target]['carried_from'], [key])
        self.assertEqual(self.state['checklists'][TARGET]['task_ids'].count(target), 1)
        self.assertEqual(self.state['carryovers'], {})

    def test_durable_queue_round_trip_and_offline_migration_preserve_care_history(self):
        move(self.state, SOURCE + '_wet')
        move(self.state, SOURCE + '_litter')
        self.state['inline_panels'] = {'discarded_transport': {}}
        self.state['inline_receipts'] = {'discarded_transport': {}}
        original = copy.deepcopy(self.state)
        with tempfile.TemporaryDirectory(dir=TMP) as directory:
            path = Path(directory) / 'record.json'
            with care.locked(path) as stored:
                stored.clear()
                stored.update(self.state)
            loaded = json.loads(path.read_text())
            self.assertEqual(loaded, original)
            imported = migrate.migrate(loaded, USER_ID, NOW)
            self.assertEqual(loaded, original)
            for field in ('carryovers', 'tasks', 'config', 'schedule_history'):
                self.assertEqual(imported[field], original[field])
            self.assertEqual(imported['audit'][:-1], original['audit'])
            self.assertNotIn('inline_panels', imported)
            self.assertNotIn('inline_receipts', imported)
            path.write_text(json.dumps(imported))
            store = Store(path)
            store.bind(BOT_ID, CHAT, TOPIC, NOW)
            with store.transaction() as state:
                care.tick(state, care.wall(NOW.date(), '14:00'))
            saved = Store(path).snapshot()
            self.assertEqual(saved['carryovers'], {})
            self.assertEqual(saved['tasks'][TARGET + '_wet']['grams'], 49)
            for kind in ('wet', 'litter'):
                self.assertEqual(saved['tasks'][TARGET + '_' + kind]['carried_from'], [SOURCE + '_' + kind])
                self.assertEqual(saved['tasks'][SOURCE + '_' + kind], original['tasks'][SOURCE + '_' + kind])

    def test_preexisting_unannounced_target_still_gets_primary_at_due(self):
        slot = self.state['config']['slots'][1]
        care.ensure_checklist(self.state, NOW.date(), slot, 1)
        move(self.state, SOURCE + '_wet')
        move(self.state, SOURCE + '_litter')
        care.tick(self.state, care.instant('2026-10-19T13:59:59+01:00'))
        self.assertIsNone(self.state['checklists'][TARGET]['announced_at'])
        text = care.tick(self.state, care.wall(NOW.date(), '14:00'))
        self.assertTrue(text.startswith('🐾 Acorn · обед\n'))
        self.assertEqual(self.state['emissions'][self.state['last_emission_id']]['primary'], TARGET)
        self.assertEqual(self.state['checklists'][TARGET]['announced_at'], '2026-10-19T14:00:00+01:00')
        self.assertEqual(self.state['tasks'][TARGET + '_wet']['grams'], 49)
        self.assertEqual(self.state['tasks'][TARGET + '_litter']['carried_from'], [SOURCE + '_litter'])
        self.assertEqual(care.tick(self.state, care.instant('2026-10-19T14:00:01+01:00')), 'NO_REPLY')


class NextCareInlineTests(unittest.TestCase):
    def setUp(self):
        care.BUTTON_BOT = '@example_care_bot'
        self.state, _ = started()
        self.panel = sent_panel(self.state)

    def test_every_task_row_has_completion_on_left_and_exact_next_label_on_right(self):
        for day in ('2026-10-19', '2026-11-02', '2026-11-09', '2026-11-16'):
            state, now = started(day, '19:00')
            for cid in state['checklists']:
                with self.subTest(day=day, checklist=cid):
                    panel = sent_panel(state, cid, now=now)
                    rows = inline_ui.keyboard(state, panel)['inline_keyboard']
                    self.assertEqual(len(rows), len(panel['task_ids']))
                    for index, row in enumerate(rows):
                        self.assertEqual(len(row), 2)
                        self.assertEqual(row[0]['text'], inline_ui.button_label(state['tasks'][panel['task_ids'][index]]))
                        self.assertEqual(row[1]['text'], 'К следующему уходу')
                        self.assertEqual(row[0]['callback_data'], inline_ui.PREFIX + panel['nonce'] + ':' + str(index))
                        self.assertEqual(row[1]['callback_data'], inline_ui.PREFIX + panel['nonce'] + ':next:' + str(index))
                        for button in row:
                            self.assertLessEqual(len(button['callback_data'].encode('utf-8')), 64)
                            self.assertNotIn('@', button['text'])

    def test_three_digit_indices_fit_the_64_byte_callback_limit(self):
        panel = copy.deepcopy(self.panel)
        panel['task_ids'] = [SOURCE + '_wet'] * 1000
        rows = inline_ui.keyboard(self.state, panel)['inline_keyboard']
        self.assertEqual(rows[-1][1]['callback_data'], inline_ui.PREFIX + panel['nonce'] + ':next:999')
        for row in rows:
            self.assertTrue(all(len(b['callback_data'].encode('utf-8')) <= 64 for b in row))

    def test_layout_preparation_rendering_and_upgrade_change_no_care_records(self):
        move(self.state, SOURCE + '_wet')
        before = copy.deepcopy(self.state)
        panel = inline_ui.prepare(self.state, [care.render_checklist(self.state, SOURCE)],
                                  self.panel['task_ids'], NOW, primary=SOURCE)
        new = copy.deepcopy(self.state)
        new['inline_panels'] = before['inline_panels']
        self.assertEqual(new, before)
        prepared = copy.deepcopy(self.state)
        for existing in self.state['inline_panels'].values():
            inline_ui.keyboard(self.state, existing)
            inline_ui.digest(self.state, existing)
            for page in existing['pages']:
                inline_ui.page_text(self.state, existing, page)
        self.assertEqual(self.state, prepared)
        self.assertNotEqual(panel['nonce'], self.panel['nonce'])

    def test_next_button_for_each_kind_calls_core_with_numeric_provenance(self):
        for index, key in enumerate(self.panel['task_ids']):
            with self.subTest(task=key):
                state = copy.deepcopy(self.state)
                with patch.object(care, 'move_to_next', wraps=care.move_to_next) as core:
                    answer = accept(state, callback(self.panel, 'next:' + str(index)))
                core.assert_called_once_with(state, key, 'telegram:101', NOW, 'telegram_callback_q1', '101')
                self.assertFalse(answer['show_alert'])
                self.assertEqual(state['tasks'][key]['status'], 'skipped')
                self.assertEqual(state['inline_receipts']['q1']['answer'], answer)
                if state['tasks'][key]['type'] in {'wet', 'dry'}:
                    self.assertIn('без добавления пропущенной порции', answer['text'])
                else:
                    self.assertIn('Повторы текущего пункта отключены', answer['text'])

    def test_future_move_has_no_receipt_queue_audit_or_task_mutation(self):
        before = copy.deepcopy(self.state)
        answer = accept(self.state, callback(self.panel), NOW - timedelta(minutes=2))
        self.assertTrue(answer['show_alert'])
        self.assertEqual(self.state, before)

    def test_duplicate_callback_and_json_reload_are_exact_noops(self):
        cb = callback(self.panel)
        answer = accept(self.state, cb)
        loaded = json.loads(json.dumps(self.state))
        before = copy.deepcopy(loaded)
        self.assertEqual(accept(loaded, cb), answer)
        self.assertEqual(loaded, before)

    def test_distinct_callback_on_closed_task_does_not_overwrite_the_skip(self):
        accept(self.state, callback(self.panel))
        before = copy.deepcopy(self.state)
        cb = callback(self.panel, query_id='second')
        cb['from'].update(id=102, username='another_name')
        answer = accept(self.state, cb)
        self.assertIn('уже закрыт', answer['text'])
        self.assertEqual(self.state['tasks'], before['tasks'])
        self.assertEqual(self.state['carryovers'], before['carryovers'])
        self.assertEqual(self.state['audit'], before['audit'])

    def test_completion_after_move_and_move_after_completion_never_change_closed_source(self):
        accept(self.state, callback(self.panel))
        before = copy.deepcopy(self.state['tasks'])
        accept(self.state, callback(self.panel, '0', 'complete_after_skip'))
        self.assertEqual(self.state['tasks'], before)
        accept(self.state, callback(self.panel, '1', 'complete_dry'))
        before = copy.deepcopy(self.state['tasks'])
        queue = copy.deepcopy(self.state['carryovers'])
        accept(self.state, callback(self.panel, 'next:1', 'move_after_complete'))
        self.assertEqual(self.state['tasks'], before)
        self.assertEqual(self.state['carryovers'], queue)

    def test_scope_numeric_identity_bot_origin_and_registered_binding_are_required(self):
        mutations = [
            lambda c: c['from'].update(id=999),
            lambda c: c['from'].update(id='101'),
            lambda c: c['from'].update(id=101.0),
            lambda c: c['from'].update(id=True),
            lambda c: c['from'].pop('id'),
            lambda c: c['from'].update(is_bot=True),
            lambda c: c['from'].pop('is_bot'),
            lambda c: c['message']['chat'].update(id=-2),
            lambda c: c['message'].update(message_thread_id=43),
            lambda c: c['message'].pop('message_thread_id'),
            lambda c: c['message']['from'].update(id=BOT_ID + 1),
            lambda c: c['message']['from'].update(is_bot=False),
            lambda c: c['message']['from'].pop('is_bot'),
            lambda c: c['message'].update(message_id=778),
            lambda c: c.update(data=inline_ui.PREFIX + '0' * 16 + ':next:0'),
            lambda c: c.update(id=''),
            lambda c: c.update(id='x' * 257),
            lambda c: c.update(id=123),
        ]
        for index, mutation in enumerate(mutations):
            with self.subTest(case=index):
                cb = callback(self.panel)
                mutation(cb)
                before = copy.deepcopy(self.state)
                self.assertTrue(accept(self.state, cb)['show_alert'])
                self.assertEqual(self.state, before)

    def test_unrelated_prefixes_are_ignored_even_for_a_known_nonce(self):
        for prefix in ('other:1:', 'acorn:2:', 'approval:'):
            with self.subTest(prefix=prefix):
                cb = callback(self.panel)
                cb['data'] = prefix + self.panel['nonce'] + ':next:0'
                before = copy.deepcopy(self.state)
                self.assertIsNone(accept(self.state, cb))
                self.assertEqual(self.state, before)

    def test_malformed_action_and_out_of_range_index_do_not_mutate(self):
        for action in ('next:', 'next:-1', 'next:1000', 'next:99', 'next:0junk',
                       'next:1:0', 'next:test', 'done:0', '+0', 'test:0'):
            with self.subTest(action=action):
                before = copy.deepcopy(self.state)
                answer = accept(self.state, callback(self.panel, action))
                self.assertTrue(answer['show_alert'])
                self.assertEqual(self.state, before)

    def test_unsent_or_suppressed_panels_are_rejected(self):
        for status in ('sending', 'suppressed', 'needs_review'):
            with self.subTest(status=status):
                self.panel['status'] = status
                before = copy.deepcopy(self.state)
                self.assertTrue(accept(self.state, callback(self.panel))['show_alert'])
                self.assertEqual(self.state, before)

    def test_multipart_callback_accepts_only_the_page_that_owns_the_keyboard(self):
        panel = inline_ui.prepare(self.state, ['header', 'body'], self.panel['task_ids'], NOW, primary=SOURCE)
        panel['status'] = 'sent'
        panel['pages'][0]['message_id'], panel['pages'][1]['message_id'] = 800, 801
        cb = callback(panel)
        cb['message']['message_id'] = 800
        before = copy.deepcopy(self.state)
        self.assertTrue(accept(self.state, cb)['show_alert'])
        self.assertEqual(self.state, before)
        cb['message']['message_id'] = 801
        self.assertFalse(accept(self.state, cb)['show_alert'])
        self.assertEqual(self.state['tasks'][SOURCE + '_wet']['status'], 'skipped')

    def test_nonce_cannot_be_rebound_to_another_registered_message(self):
        second = sent_panel(self.state, message_id=778)
        cb = callback(self.panel)
        cb['message']['message_id'] = second['pages'][-1]['message_id']
        before = copy.deepcopy(self.state)
        self.assertTrue(accept(self.state, cb)['show_alert'])
        self.assertEqual(self.state, before)

    def test_original_and_repeat_messages_share_one_durable_move(self):
        now = care.instant('2026-10-19T07:30:00+01:00')
        text = care.tick(self.state, now)
        eid = self.state['last_emission_id']
        repeat = inline_ui.prepare(self.state, [text], care.emission_task_ids(self.state, eid), now)
        repeat['status'] = 'sent'
        repeat['pages'][-1]['message_id'] = 778
        key = SOURCE + '_litter'
        index = repeat['task_ids'].index(key)
        self.assertFalse(accept(self.state, callback(repeat, 'next:' + str(index)), now)['show_alert'])
        before = copy.deepcopy(self.state)
        original_index = self.panel['task_ids'].index(key)
        answer = accept(self.state, callback(self.panel, 'next:' + str(original_index), 'original'), now)
        self.assertIn('уже закрыт', answer['text'])
        for field in ('tasks', 'carryovers', 'audit'):
            self.assertEqual(self.state[field], before[field])
        for panel in (self.panel, repeat):
            rendered = inline_ui.page_text(self.state, panel, panel['pages'][0])
            self.assertIn('Пропущено: Лоток', rendered)
            self.assertIn('19.10 14:00', rendered)
            self.assertNotEqual(panel['last_digest'], inline_ui.digest(self.state, panel))
            rows = inline_ui.keyboard(self.state, panel)['inline_keyboard']
            self.assertFalse(any(b['callback_data'].endswith(':next:' + str(original_index)) for row in rows for b in row))

    def test_identical_food_labels_in_different_meals_skip_only_bound_source(self):
        now = care.instant('2026-10-19T14:01:00+01:00')
        care.tick(self.state, now)
        second = sent_panel(self.state, TARGET, 778, now)
        old_button = inline_ui.keyboard(self.state, self.panel)['inline_keyboard'][0][1]
        new_button = inline_ui.keyboard(self.state, second)['inline_keyboard'][0][1]
        self.assertEqual(old_button['text'], new_button['text'])
        self.assertNotEqual(old_button['callback_data'], new_button['callback_data'])
        before = copy.deepcopy(self.state['tasks'][SOURCE + '_wet'])
        accept(self.state, callback(second), now)
        self.assertEqual(self.state['tasks'][SOURCE + '_wet'], before)
        self.assertEqual(self.state['tasks'][TARGET + '_wet']['status'], 'skipped')
        self.assertIn('2026-10-19_19:00_wet', self.state['carryovers'])

    def test_skipped_done_and_deferred_rows_remove_both_buttons(self):
        for index, status in enumerate(('skipped', 'done', 'deferred')):
            self.state['tasks'][self.panel['task_ids'][index]]['status'] = status
        rows = inline_ui.keyboard(self.state, self.panel)['inline_keyboard']
        self.assertEqual(len(rows), 2)
        for index, row in zip((3, 4), rows):
            self.assertTrue(row[0]['callback_data'].endswith(':' + str(index)))
            self.assertTrue(row[1]['callback_data'].endswith(':next:' + str(index)))

    def test_closed_target_error_has_no_receipt_or_partial_mutation(self):
        care.ensure_checklist(self.state, NOW.date(), self.state['config']['slots'][1], 1)
        self.state['tasks'][TARGET + '_wet']['status'] = 'done'
        before = copy.deepcopy(self.state)
        self.assertTrue(accept(self.state, callback(self.panel))['show_alert'])
        self.assertEqual(self.state, before)

    def test_test_panel_stays_single_button_and_rejects_task_actions(self):
        panel = inline_ui.prepare(self.state, ['test'], [], NOW, test=True)
        panel['status'] = 'sent'
        panel['pages'][-1]['message_id'] = 800
        rows = inline_ui.keyboard(self.state, panel)['inline_keyboard']
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), 1)
        for action in ('next:0', '0'):
            before = copy.deepcopy(self.state)
            self.assertTrue(accept(self.state, callback(panel, action))['show_alert'])
            self.assertEqual(self.state, before)
        before = copy.deepcopy(self.state['tasks'])
        self.assertFalse(accept(self.state, callback(panel, 'test'))['show_alert'])
        self.assertEqual(self.state['tasks'], before)
        self.assertEqual(inline_ui.keyboard(self.state, panel), {'inline_keyboard': []})

    def test_receipt_lookup_does_not_bypass_numeric_auth(self):
        cb = callback(self.panel)
        accept(self.state, cb)
        cb['from']['id'] = 999
        before = copy.deepcopy(self.state)
        self.assertTrue(accept(self.state, cb)['show_alert'])
        self.assertEqual(self.state, before)

    def test_receipts_are_bounded_without_losing_the_latest_move(self):
        self.state['inline_receipts'] = {str(i): {'at': care.iso(NOW), 'answer': {}} for i in range(512)}
        accept(self.state, callback(self.panel))
        self.assertEqual(len(self.state['inline_receipts']), 512)
        self.assertIn('q1', self.state['inline_receipts'])
        self.assertNotIn('0', self.state['inline_receipts'])


class NextCareServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TMP)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'record.json'
        self.store = Store(self.path)
        self.store.bind(BOT_ID, CHAT, TOPIC, NOW)
        self.runtime = Runtime(CHAT, TOPIC, frozenset({USER_ID, 102}), self.path, '127.0.0.1', 8080)
        self.client = FakeClient()
        self.now = NOW
        self.bot = Bot(self.runtime, self.client, BOT_ID, 'example_care_bot', self.store, lambda: self.now)
        self.bot.dispatch_once()
        self.panel = next(iter(self.store.snapshot()['inline_panels'].values()))

    def update(self, uid=10, action='next:0'):
        return {'update_id': uid, 'callback_query': callback(self.panel, action, 'q' + str(uid))}

    def test_skip_queue_receipt_and_offset_are_durable_before_acknowledgement(self):
        def saved_before_ack():
            saved = Store(self.path).snapshot()
            self.assertEqual(saved['tasks'][SOURCE + '_wet']['status'], 'skipped')
            self.assertEqual(saved['tasks'][SOURCE + '_wet']['skip_by'], 'telegram:101')
            self.assertIsNone(saved['tasks'][SOURCE + '_wet']['next_repeat_at'])
            self.assertIn(TARGET + '_wet', saved['carryovers'])
            self.assertNotIn(TARGET, saved['checklists'])
            self.assertEqual(saved['standalone']['offset'], 11)
            self.assertIn('q10', saved['inline_receipts'])
            self.assertIn('без добавления', saved['inline_receipts']['q10']['answer']['text'])
        self.client.before_ack = saved_before_ack
        self.bot.process_update(self.update())
        saved_before_ack()
        before = self.store.snapshot()
        self.bot.process_update(self.update())
        self.assertEqual(self.store.snapshot(), before)
        self.store.bind(BOT_ID, CHAT, TOPIC, NOW)
        self.assertEqual(self.store.snapshot()['carryovers'], before['carryovers'])
        self.now = care.wall(NOW.date(), '14:00')
        self.bot.dispatch_once()
        saved = self.store.snapshot()
        self.assertEqual(saved['tasks'][TARGET + '_wet']['grams'], 49)
        self.assertEqual(saved['tasks'][TARGET + '_wet']['carried_from'], [SOURCE + '_wet'])
        self.assertEqual(saved['carryovers'], {})

    def test_failed_save_never_acknowledges_or_persists_a_partial_skip(self):
        before = self.path.read_bytes()
        self.client.calls.clear()
        with patch.object(care, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.bot.process_update(self.update())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.client.calls, [])
        self.bot.process_update(self.update())
        self.assertEqual(self.store.snapshot()['tasks'][SOURCE + '_wet']['status'], 'skipped')

    def test_ack_and_edit_failures_do_not_undo_move_or_repeat_source(self):
        original = self.client.call
        def fail_ack(method, payload=None):
            if method == 'answerCallbackQuery':
                raise TelegramError(503, uncertain=True)
            return original(method, payload)
        with patch.object(self.client, 'call', side_effect=fail_ack):
            self.bot.process_update(self.update())
        self.client.edit_error = TelegramError(503, uncertain=True)
        self.now = care.instant('2026-10-19T07:30:00+01:00')
        self.bot.dispatch_once()
        saved = self.store.snapshot()
        self.assertEqual(saved['tasks'][SOURCE + '_wet']['status'], 'skipped')
        self.assertIn(TARGET + '_wet', saved['carryovers'])
        self.assertIn('q10', saved['inline_receipts'])
        self.assertNotIn(SOURCE + '_wet', saved['emissions'][saved['last_emission_id']]['repeat_ids'])

    def test_sync_edits_old_panel_to_skipped_and_removes_both_source_buttons(self):
        self.bot.process_update(self.update(action='next:2'))
        self.client.calls.clear()
        self.bot.dispatch_once()
        edits = [p for method, p in self.client.calls if method == 'editMessageText']
        original = next(p for p in edits if p['message_id'] == self.panel['pages'][-1]['message_id'])
        self.assertIn('Пропущено: Лоток', original['text'])
        self.assertIn('19.10 14:00', original['text'])
        rows = original['reply_markup']['inline_keyboard']
        self.assertEqual(len(rows), 4)
        self.assertFalse(any(b['callback_data'].endswith(':2') for row in rows for b in row))
        self.assertTrue(all(len(row) == 2 for row in rows))

    def test_layout_only_sync_after_upgrade_changes_no_tasks_queue_or_configuration(self):
        with self.store.transaction() as state:
            # Simulate the stored digest of an old one-column panel.
            state['inline_panels'][self.panel['nonce']]['last_digest'] = 'old_layout'
        before = self.store.snapshot()
        self.bot.sync_panels(NOW)
        after = self.store.snapshot()
        for field in ('tasks', 'checklists', 'config', 'audit'):
            self.assertEqual(after[field], before[field])
        self.assertNotIn('carryovers', after)
        self.assertNotIn('inline_receipts', after)
        edits = [p for method, p in self.client.calls if method == 'editMessageText']
        self.assertTrue(edits)
        self.assertTrue(all(len(row) == 2 for row in edits[-1]['reply_markup']['inline_keyboard']))

    def test_one_off_lunch_cleaning_gets_buttons_and_normal_primary_announcement(self):
        self.bot.process_update(self.update(action='next:2'))
        self.now = care.instant('2026-10-19T13:59:59+01:00')
        self.bot.dispatch_once()
        self.assertNotIn(TARGET, self.store.snapshot()['checklists'])
        self.client.calls.clear()
        self.now = care.wall(NOW.date(), '14:00')
        self.bot.dispatch_once()
        sends = [p for method, p in self.client.calls if method == 'sendMessage']
        lunch = next(p for p in sends if p['text'].startswith('🐾 Acorn · обед\n'))
        self.assertIn(care.CLEANING['litter'], lunch['text'])
        # The same message also carries four unresolved breakfast task buttons.
        self.assertEqual(len(lunch['reply_markup']['inline_keyboard']), 7)
        saved = self.store.snapshot()
        self.assertEqual(saved['emissions'][saved['last_emission_id']]['primary'], TARGET)
        self.assertEqual(len(saved['checklists'][TARGET]['task_ids']), 3)
        for row in lunch['reply_markup']['inline_keyboard']:
            self.assertEqual(row[1]['text'], 'К следующему уходу')
        self.assertEqual(saved['carryovers'], {})


    def test_large_backlog_edits_keep_all_task_bindings_and_fit_telegram_limit(self):
        self.check_backlog_edits()

    def test_pre_upgrade_panel_edits_fit_without_rebinding_messages_or_changing_care(self):
        self.check_backlog_edits(legacy=True)

    def check_backlog_edits(self, legacy=False):
        state = care.initial()
        now = care.wall(date(2026, 10, 21), '07:00')
        care.tick(state, now)
        # Model imported one-off cleaning in six old checklists and the current one.
        prototype = copy.deepcopy(state['tasks']['2026-10-21_07:00_litter'])
        for cid, checklist in state['checklists'].items():
            for kind in care.CLEANING:
                key = cid + '_' + kind
                if key not in state['tasks']:
                    state['tasks'][key] = {**prototype, 'id': key, 'checklist': cid,
                        'type': kind, 'due_at': care.iso(care.wall(date.fromisoformat(checklist['date']), checklist['time']))}
                    checklist['task_ids'].append(key)
        primary = '2026-10-21_07:00'
        old = [t for t in state['tasks'].values() if t['checklist'] != primary]
        text = care.render_checklist(state, primary) + '\n\n' + care.render_reminders(state, old)
        keys = state['checklists'][primary]['task_ids'] + [t['id'] for t in old]
        self.assertEqual(len(keys), 35)
        before = copy.deepcopy(state['tasks'])
        if legacy:
            # Simulate saved pages from before edit-space reservation existed.
            with patch.object(inline_ui, 'task_line_size', return_value=0):
                panel = inline_ui.prepare(state, chunks(text), keys, now, primary=primary)
            self.assertEqual(len(panel['pages']), 1)
        else:
            panel = inline_ui.prepare(state, chunks(text), keys, now, primary=primary)
            self.assertGreater(len(panel['pages']), 1)
        self.assertEqual(state['tasks'], before)
        self.assertEqual(sum(len(page['bindings']) for page in panel['pages']), 35)
        panel['status'] = 'sent'
        for index, page in enumerate(panel['pages']):
            page['message_id'] = 900 + index
        panel['last_digest'] = inline_ui.digest(state, panel)
        for key in keys:
            move(state, key, now)
        if legacy:
            page = panel['pages'][0]
            raw_lines = page['template'].splitlines()
            for index, binding in page['bindings'].items():
                raw_lines[int(index)] = care.task_line(state['tasks'][binding['task']], primary=binding['primary'])
            self.assertGreater(len('\n'.join(raw_lines).encode('utf-16-le')) // 2, 4096)
        moved_tasks = copy.deepcopy(state['tasks'])
        bindings = copy.deepcopy(panel['pages'])
        with self.store.transaction() as stored:
            meta = stored['standalone']
            stored.clear()
            stored.update(state)
            stored['standalone'] = meta
        self.client.calls.clear()
        self.bot.sync_panels(now)
        edits = [p for method, p in self.client.calls if method == 'editMessageText']
        self.assertEqual(len(edits), len(panel['pages']))
        self.assertTrue(all(len(p['text'].encode('utf-16-le')) // 2 <= 4096 for p in edits))
        self.assertEqual(sum(p['text'].count('Пропущено:') for p in edits), 35)
        self.assertEqual(edits[-1]['reply_markup'], {'inline_keyboard': []})
        saved = self.store.snapshot()
        self.assertEqual(saved['tasks'], moved_tasks)
        self.assertEqual(saved['inline_panels'][panel['nonce']]['pages'], bindings)
        if legacy:
            self.assertIn('Следующий уход 21.10 14:00', edits[0]['text'])
            self.assertIn('без добавки', edits[0]['text'])
            self.assertIn('telegram:101', edits[0]['text'])


if __name__ == '__main__':
    unittest.main()
