"""Offline calendar, prescription safety, durable outbox, and callback contracts."""
import argparse
import copy
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import care
import inline_ui
import treatments
from configuration import Runtime, validate_care_config
from service import Bot, HELP
from storage import Store
from telegram_api import TelegramError
from test_service import FakeClient, CHAT, TOPIC, BOT_ID

DEFINITION = {
    'id': 'credelio', 'start': '2026-11-10', 'interval_months': 3, 'slot': 'breakfast',
    'text': '💊 Credelio Chew 48 mg, 1 tablet, после еды',
    'approved_by': 'palmette', 'approved_message': 'telegram_-1004424243504_655',
    'approved_at': '2026-11-10T11:22:43+00:00',
}
CID = '2026-11-10_07:00'
KEY = CID + '_treatment_credelio'
OCCURRENCE = 'credelio:2026-11-10'
NOW = care.instant('2026-11-10T11:22:43+00:00')
MORNING = care.instant('2026-11-10T07:00:00+00:00')


def state_at(day='2026-11-10', enabled=True):
    state = care.initial()
    state['config']['automatic_repeats'] = False
    state['last_tick'] = care.iso(care.wall(date.fromisoformat(day), '06:59'))
    if enabled:
        treatments.apply_configuration(state, [DEFINITION], NOW)
    return state


def command(state, key=KEY, action='done', now=NOW, minutes=None):
    task = state['tasks'].get(key) or state['carryovers'][key]['treatment_task']
    args = argparse.Namespace(checklist=task['checklist'], types=[task['type']], action=action,
                              actor='telegram:101', message_id='message', original_message_id=None,
                              minutes=minutes)
    return care.update(state, args, now)


def medicines(state):
    queued = [item['treatment_task'] for item in state.get('carryovers', {}).values()
              if 'treatment_task' in item]
    return [task for task in list(state['tasks'].values()) + queued
            if treatments.is_treatment(task['type'])]


def panel_for(state, key=KEY, now=NOW):
    text = treatments.render(state, [state['tasks'][key]])
    panel = inline_ui.prepare(state, [text], [key], now)
    panel['status'] = 'sent'
    panel['pages'][-1]['message_id'] = 777
    return panel


def callback(panel, action='next:0', query='q1'):
    return {'id': query, 'data': inline_ui.PREFIX + panel['nonce'] + ':' + action,
            'from': {'id': 101, 'is_bot': False, 'username': 'palmette'},
            'message': {'message_id': panel['pages'][-1]['message_id'],
                        'chat': {'id': CHAT}, 'message_thread_id': TOPIC,
                        'from': {'id': BOT_ID, 'is_bot': True}}}


def accept(state, cb, now=NOW):
    return inline_ui.accept_callback(state, cb, {101, 102}, BOT_ID, now, CHAT, TOPIC)


class CalendarTests(unittest.TestCase):
    def test_quarters_use_calendar_months_across_years_not_ninety_days(self):
        actual = [treatments.occurrence_date(DEFINITION, n).isoformat() for n in range(9)]
        self.assertEqual(actual, ['2026-11-10', '2027-02-10', '2027-05-10', '2027-08-10',
                                  '2027-11-10', '2028-02-10', '2028-05-10', '2028-08-10', '2028-11-10'])
        for value in actual:
            self.assertTrue(treatments.scheduled_today(DEFINITION, date.fromisoformat(value)))
        for value in ('2026-11-09', '2026-08-10', '2026-12-10', '2027-02-08', '2027-02-09', '2027-02-11'):
            self.assertFalse(treatments.scheduled_today(DEFINITION, date.fromisoformat(value)))

    def test_month_end_clamps_without_drifting_and_handles_leap_years(self):
        cases = [('2027-01-31', 1, ['2027-01-31', '2027-02-28', '2027-03-31', '2027-04-30']),
                 ('2028-01-31', 1, ['2028-01-31', '2028-02-29', '2028-03-31', '2028-04-30']),
                 ('2027-11-30', 3, ['2027-11-30', '2028-02-29', '2028-05-30', '2028-08-30']),
                 ('2028-02-29', 12, ['2028-02-29', '2029-02-28', '2030-02-28', '2031-02-28', '2032-02-29'])]
        for start, interval, expected in cases:
            definition = {**DEFINITION, 'start': start, 'interval_months': interval}
            with self.subTest(start=start):
                self.assertEqual([treatments.occurrence_date(definition, n).isoformat()
                                  for n in range(len(expected))], expected)
                for value in expected:
                    self.assertTrue(treatments.scheduled_today(definition, date.fromisoformat(value)))

    def test_optional_config_and_valid_definition(self):
        config = copy.deepcopy(care.BOOT_CONFIG)
        config.pop('treatments', None)
        validate_care_config(config)
        validate_care_config({**config, 'treatments': []})
        validate_care_config({**config, 'treatments': [DEFINITION]})

    def test_invalid_config_is_rejected(self):
        config = copy.deepcopy(care.BOOT_CONFIG)
        invalid = [None, {}, 'credelio', [DEFINITION, DEFINITION], [{}]]
        changes = [('id', ''), ('id', 'Credelio'), ('id', 'x:y'), ('id', 'a' * 33),
                   ('interval_months', 0), ('interval_months', 3.0), ('interval_months', True),
                   ('interval_months', 121), ('interval_months', '3'), ('slot', 'midnight'), ('slot', []),
                   ('start', '2026-02-30'), ('start', '20261110'), ('start', None),
                   ('approved_at', '2026-11-10T11:22:43'), ('approved_at', None),
                   ('approved_by', ''), ('approved_message', ''), ('text', ''),
                   ('text', 'dose\nsecond dose'), ('text', '<dose>'), ('text', 'x' * 257)]
        invalid.extend([{**DEFINITION, key: value}] for key, value in changes)
        invalid.extend([{k: v for k, v in DEFINITION.items() if k != field}] for field in DEFINITION)
        invalid.append([{**DEFINITION, 'dose': 2}])
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_care_config({**config, 'treatments': value})


class TreatmentCareTests(unittest.TestCase):
    def test_first_checklist_section_order_and_exact_prescription(self):
        state = state_at()
        text = care.tick(state, MORNING)
        self.assertLess(text.index('Кормление'), text.index('💊 Обработка'))
        self.assertLess(text.index('💊 Обработка'), text.index('🧹'))
        self.assertIn(DEFINITION['text'], text)
        task = state['tasks'][KEY]
        self.assertEqual(task['treatment_occurrence'], OCCURRENCE)
        self.assertEqual(task['treatment_definition'], DEFINITION)
        self.assertIsNone(task['grams'])
        self.assertEqual(task['status'], 'pending')
        self.assertEqual(care.emission_task_ids(state, state['last_emission_id']).count(KEY), 1)
        self.assertEqual(care.tick(state, NOW), 'NO_REPLY')

    def test_today_already_announced_breakfast_supplement_once_without_reopening(self):
        state = state_at(enabled=False)
        care.tick(state, MORNING)
        command(state, CID + '_dry')
        before_tasks = copy.deepcopy(state['tasks'])
        before_checklist = copy.deepcopy(state['checklists'][CID])
        treatments.apply_configuration(state, [DEFINITION], NOW)
        text = care.tick(state, NOW)
        self.assertIn('💊 Обработка', text)
        self.assertIn(DEFINITION['text'], text)
        self.assertNotIn('Кормление', text)
        self.assertNotIn('🧹', text)
        self.assertNotIn('Сухой корм', text)
        self.assertNotIn('Миски', text)
        for key, task in before_tasks.items():
            self.assertEqual(state['tasks'][key], task)
        after = state['checklists'][CID]
        self.assertEqual(after['task_ids'], before_checklist['task_ids'] + [KEY])
        for field in ('announced_at', 'original_message_id', 'stage'):
            self.assertEqual(after[field], before_checklist[field])
        eid = state['last_emission_id']
        self.assertIsNone(state['emissions'][eid]['primary'])
        self.assertEqual(state['emissions'][eid]['repeat_ids'], [])
        self.assertEqual(care.emission_task_ids(state, eid), [KEY])
        self.assertEqual(care.refresh(state, eid, NOW), text)
        panel = panel_for(state)
        self.assertEqual(len(inline_ui.keyboard(state, panel)['inline_keyboard']), 1)
        self.assertEqual(len(inline_ui.keyboard(state, panel)['inline_keyboard'][0]), 2)
        self.assertEqual(care.tick(state, NOW + timedelta(seconds=2)), 'NO_REPLY')
        self.assertEqual(care.tick(json.loads(json.dumps(state)), NOW), 'NO_REPLY')

    def test_completed_breakfast_stays_closed_except_new_treatment(self):
        state = state_at(enabled=False)
        care.tick(state, MORNING)
        for key in list(state['tasks']):
            command(state, key)
        snapshot = copy.deepcopy(state['tasks'])
        treatments.apply_configuration(state, [DEFINITION], NOW)
        care.tick(state, NOW)
        self.assertEqual(care.unresolved(state), [state['tasks'][KEY]])
        for key in snapshot:
            self.assertEqual(state['tasks'][key], snapshot[key])

    def test_same_day_late_install_after_dinner_still_supplements_breakfast(self):
        state = state_at(enabled=False)
        late = care.instant('2026-11-10T20:00:00+00:00')
        care.tick(state, late)
        treatments.apply_configuration(state, [DEFINITION], late)
        text = care.tick(state, late)
        self.assertIn('2026-11-10 · 07:00', text)
        self.assertNotIn('Кормление', text)
        self.assertEqual(care.emission_task_ids(state, state['last_emission_id']), [KEY])

    def test_no_future_task_before_seven_and_no_backlog_on_later_install(self):
        for stamp in ('2026-11-10T06:59:00+00:00', '2026-11-11T11:00:00+00:00',
                      '2027-02-11T11:00:00+00:00', '2028-10-12T11:00:00+01:00'):
            now = care.instant(stamp)
            state = state_at(now.date().isoformat())
            care.tick(state, now)
            self.assertEqual(medicines(state), [], stamp)

    def test_restart_on_later_scheduled_day_creates_only_that_day(self):
        state = state_at('2028-05-10')
        care.tick(state, care.instant('2028-05-10T07:00:00+01:00'))
        self.assertEqual([t['treatment_occurrence'] for t in medicines(state)], ['credelio:2028-05-10'])

    def test_done_is_explicit_and_idempotent_no_automatic_completion(self):
        state = state_at()
        care.tick(state, MORNING)
        care.tick(state, NOW)
        self.assertEqual(state['tasks'][KEY]['status'], 'pending')
        command(state)
        task = copy.deepcopy(state['tasks'][KEY])
        command(state, now=NOW + timedelta(minutes=1))
        self.assertEqual(state['tasks'][KEY], task)
        self.assertEqual(task['done_by'], 'telegram:101')
        self.assertEqual(task['done_at'], care.iso(NOW))
        self.assertIsNone(task['next_repeat_at'])
        self.assertEqual(care.tick(state, NOW + timedelta(minutes=30)), 'NO_REPLY')

    def test_snooze_identity_and_manual_repeat_with_automatic_repeats_disabled(self):
        state = state_at()
        care.tick(state, MORNING)
        command(state, action='snooze', minutes=30)
        self.assertEqual(care.tick(state, NOW + timedelta(minutes=29)), 'NO_REPLY')
        text = care.tick(state, NOW + timedelta(minutes=30))
        self.assertIn(DEFINITION['text'], text)
        self.assertEqual(state['tasks'][KEY]['treatment_occurrence'], OCCURRENCE)
        eid = state['last_emission_id']
        self.assertEqual(state['emissions'][eid]['manual_repeat_ids'], [KEY])
        self.assertIn(DEFINITION['text'], care.refresh(state, eid, NOW + timedelta(minutes=30)))
        self.assertEqual(care.tick(state, NOW + timedelta(minutes=60)), 'NO_REPLY')

    def test_overdue_asks_for_clarification_not_more_doses(self):
        state = state_at()
        care.tick(state, MORNING)
        next_day = care.instant('2026-11-11T07:00:00+00:00')
        care.tick(state, next_day)
        task = state['tasks'][KEY]
        self.assertEqual(task['status'], 'needs_clarification')
        self.assertIn('Не давайте повторную или двойную дозу', care.task_line(task))
        self.assertEqual(len(medicines(state)), 1)
        self.assertIsNone(task['done_at'])

    def test_unresolved_prior_blocks_later_quarter_even_if_deferred_or_transferred(self):
        for mode in ('pending', 'snoozed', 'moved'):
            with self.subTest(mode=mode):
                state = state_at()
                care.tick(state, MORNING)
                if mode == 'snoozed':
                    command(state, action='snooze', minutes=30)
                elif mode == 'moved':
                    care.move_to_next(state, KEY, 'telegram:101', NOW)
                    care.tick(state, care.instant('2026-11-10T14:00:00+00:00'))
                later = care.instant('2027-02-10T07:00:00+00:00')
                state['last_tick'] = care.iso(later - timedelta(minutes=1))
                text = care.tick(state, later)
                opened = [t for t in medicines(state) if t['status'] not in care.CLOSED]
                self.assertEqual(len(opened), 1)
                self.assertEqual(opened[0]['treatment_occurrence'], OCCURRENCE)
                self.assertIn('заблокирована', care.task_line(opened[0]))
                self.assertEqual(state['treatment_occurrences']['credelio:2027-02-10']['status'], 'blocked')
                command(state, opened[0]['id'], now=later)
                care.tick(state, later + timedelta(minutes=1))
                self.assertFalse(any(t['treatment_date'] == '2027-02-10' for t in medicines(state)))

    def test_deferred_completion_does_not_change_future_calendar(self):
        state = state_at()
        care.tick(state, MORNING)
        result = care.move_to_next(state, KEY, 'telegram:101', NOW)
        late = care.instant('2026-11-11T10:00:00+00:00')
        care.tick(state, late)
        command(state, result['target_id'], now=late)
        for day in ('2027-02-10', '2027-05-10', '2027-08-10', '2027-11-10'):
            now = care.wall(date.fromisoformat(day), '07:00')
            state['last_tick'] = care.iso(now - timedelta(minutes=1))
            care.tick(state, now)
            key = day + '_07:00_treatment_credelio'
            self.assertEqual(state['tasks'][key]['treatment_occurrence'], 'credelio:' + day)
            command(state, key, now=now)

    def test_health_hold_does_not_suppress_but_global_pause_does(self):
        state = state_at()
        care.health_hold(state, 'telegram:101', 'diet only', MORNING)
        hold = copy.deepcopy(state['progression_hold'])
        state['reminder_pause'] = {'until': 'indefinite'}
        before = copy.deepcopy(state)
        self.assertEqual(care.tick(state, NOW), 'NO_REPLY')
        self.assertEqual(state, before)
        state['reminder_pause'] = None
        self.assertIn(DEFINITION['text'], care.tick(state, NOW))
        self.assertEqual(state['progression_hold'], hold)

    def test_missed_transfer_announcement_catches_up_as_one_safe_occurrence(self):
        state = state_at()
        care.tick(state, MORNING)
        result = care.move_to_next(state, KEY, 'telegram:101', NOW)
        next_day = care.instant('2026-11-11T10:00:00+00:00')
        state = json.loads(json.dumps(state))
        text = care.tick(state, next_day)
        self.assertIn('Нужно уточнить', text)
        self.assertNotIn('Кормление', text)
        self.assertEqual(care.emission_task_ids(state, state['last_emission_id']), [result['target_id']])
        self.assertEqual(len([t for t in medicines(state) if t['status'] not in care.CLOSED]), 1)
        self.assertFalse(state['tasks'][result['target_id']]['treatment_announcement_pending'])

    def test_all_treatments_fit_editable_pages_after_overdue_and_blocking(self):
        state = state_at(enabled=False)
        definitions = [{**DEFINITION, 'id': 'medicine_' + str(i), 'text': '💊 ' + 'я' * 250}
                       for i in range(16)]
        treatments.apply_configuration(state, definitions, NOW)
        text = care.tick(state, MORNING)
        eid = state['last_emission_id']
        panel = inline_ui.prepare(state, [text], care.emission_task_ids(state, eid),
                                  MORNING, primary=CID)
        for task in medicines(state):
            task['needs_clarification'] = True
            task['blocked_treatment_dates'] = ['2027-02-10'] * 20
        for page in panel['pages']:
            rendered = inline_ui.page_text(state, panel, page)
            self.assertLessEqual(len(rendered.encode('utf-16-le')) // 2, inline_ui.MESSAGE_LIMIT)

    def test_optional_config_missing_is_noop_for_existing_care(self):
        state = state_at(enabled=False)
        copy_state = copy.deepcopy(state)
        with patch.object(treatments, 'sync') as sync:
            expected = care.tick(copy_state, MORNING)
        actual = care.tick(state, MORNING)
        self.assertEqual(actual, expected)
        for field in ('tasks', 'checklists', 'config', 'schedule_history'):
            self.assertEqual(state[field], copy_state[field])


class TreatmentTransferTests(unittest.TestCase):
    def setUp(self):
        self.state = state_at()
        care.tick(self.state, MORNING)

    def test_chain_transfers_one_occurrence_once_without_moving_food(self):
        key = KEY
        food_snapshot = {k: copy.deepcopy(t) for k, t in self.state['tasks'].items()
                         if t['type'] in care.FOOD_TYPES}
        cases = [(NOW, '2026-11-10_14:00_treatment_credelio'),
                 (care.instant('2026-11-10T14:00:00+00:00'), '2026-11-10_19:00_treatment_credelio'),
                 (care.instant('2026-11-10T19:10:00+00:00'), '2026-11-11_07:00_treatment_credelio')]
        for now, target in cases:
            care.tick(self.state, now)
            result = care.move_to_next(self.state, key, 'telegram:101', now)
            self.assertEqual(result['target_id'], target)
            self.assertIn('одна доза', care.move_answer(result))
            queued = self.state['carryovers'][target]['treatment_task']
            self.assertEqual(queued['treatment_occurrence'], OCCURRENCE)
            self.assertEqual(queued['treatment_definition'], DEFINITION)
            self.assertNotIn(queued['checklist'], self.state['checklists'])
            self.assertEqual(self.state['treatment_occurrences'][OCCURRENCE]['current_task_id'], target)
            before = copy.deepcopy(self.state)
            care.move_to_next(self.state, key, 'telegram:102', now + timedelta(seconds=1))
            self.assertEqual(self.state, before)
            self.state = json.loads(json.dumps(self.state))
            open_medicine = [t for t in medicines(self.state) if t['status'] not in care.CLOSED]
            self.assertEqual([t['id'] for t in open_medicine], [target])
            for food_key, food_task in food_snapshot.items():
                # Old food may require clarification on a later dispatch, but no
                # food transfer, portion change, or automatic completion occurs.
                self.assertEqual(self.state['tasks'][food_key]['grams'], food_task['grams'])
                self.assertNotIn('moved_to', self.state['tasks'][food_key])
                self.assertNotIn('carried_from', self.state['tasks'][food_key])
            self.assertEqual(list(self.state['carryovers']), [target])
            key = target
        care.tick(self.state, care.instant('2026-11-11T07:00:00+00:00'))
        command(self.state, key, now=care.instant('2026-11-11T07:01:00+00:00'))
        self.assertFalse(any(t['status'] not in care.CLOSED for t in medicines(self.state)))

    def test_next_care_means_after_press_not_after_original_due_time(self):
        late = care.instant('2026-11-10T15:00:00+00:00')
        result = care.move_to_next(self.state, KEY, 'telegram:101', late)
        self.assertEqual(result['target_id'], '2026-11-10_19:00_treatment_credelio')

    def test_future_completion_and_transfer_rejected_without_mutation(self):
        result = care.move_to_next(self.state, KEY, 'telegram:101', NOW)
        key = result['target_id']
        before = copy.deepcopy(self.state)
        with self.assertRaises(ValueError):
            command(self.state, key)
        with self.assertRaises(ValueError):
            care.move_to_next(self.state, key, 'telegram:101', NOW)
        self.assertEqual(self.state, before)

    def test_existing_destination_never_merges_doses_or_mutates_on_error(self):
        slot = self.state['config']['slots'][1]
        cid = care.ensure_checklist(self.state, NOW.date(), slot, 1)
        target = cid + '_treatment_credelio'
        self.state['tasks'][target] = {**copy.deepcopy(self.state['tasks'][KEY]), 'id': target}
        for status in ('pending', 'done', 'skipped'):
            self.state['tasks'][target]['status'] = status
            before = copy.deepcopy(self.state)
            with self.assertRaises(ValueError):
                care.move_to_next(self.state, KEY, 'telegram:101', NOW)
            self.assertEqual(self.state, before)

    def test_auth_receipt_and_restart_idempotency_share_the_same_occurrence(self):
        panel = panel_for(self.state)
        cb = callback(panel)
        answer = accept(self.state, cb)
        self.assertFalse(answer['show_alert'])
        self.state = json.loads(json.dumps(self.state))
        before = copy.deepcopy(self.state)
        self.assertEqual(accept(self.state, cb), answer)
        self.assertEqual(self.state, before)
        for action in ('next:0', '0'):
            answer = accept(self.state, callback(panel, action, action))
            self.assertIn('закрыт', answer['text'])
            self.assertEqual(self.state['tasks'], before['tasks'])
        cb['from']['id'] = 999  # receipt lookup must not bypass authorization
        before = copy.deepcopy(self.state)
        self.assertTrue(accept(self.state, cb)['show_alert'])
        self.assertEqual(self.state, before)

    def test_callback_rejects_spoofed_origin_scope_nonce_or_binding(self):
        panel = panel_for(self.state)
        mutations = [lambda c: c['from'].update(id='101'),
                     lambda c: c['from'].update(is_bot=True),
                     lambda c: c['message']['chat'].update(id=-1),
                     lambda c: c['message'].update(message_thread_id=TOPIC + 1),
                     lambda c: c['message']['from'].update(id=BOT_ID + 1),
                     lambda c: c['message'].update(message_id=778),
                     lambda c: c.update(data=inline_ui.PREFIX + '0' * 16 + ':next:0')]
        for mutate in mutations:
            cb = callback(panel)
            mutate(cb)
            before = copy.deepcopy(self.state)
            self.assertTrue(accept(self.state, cb)['show_alert'])
            self.assertEqual(self.state, before)

    def test_inline_completion_updates_only_the_treatment_and_removes_buttons(self):
        panel = panel_for(self.state)
        before = copy.deepcopy(self.state['tasks'])
        self.assertFalse(accept(self.state, callback(panel, '0'))['show_alert'])
        self.assertEqual(self.state['tasks'][KEY]['status'], 'done')
        for key in before:
            if key != KEY:
                self.assertEqual(self.state['tasks'][key], before[key])
        self.assertEqual(inline_ui.keyboard(self.state, panel), {'inline_keyboard': []})
        self.assertIn('✅ ' + DEFINITION['text'], inline_ui.page_text(self.state, panel, panel['pages'][0]))


class TreatmentServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.temp) / 'state.json'
        self.store = Store(self.path)
        self.store.bind(BOT_ID, CHAT, TOPIC, MORNING)
        self.now = MORNING
        self.client = FakeClient()
        self.bot = Bot(Runtime(CHAT, TOPIC, frozenset({101}), self.path), self.client,
                       BOT_ID, 'example_care_bot', self.store, lambda: self.now)
        with self.store.transaction() as state:
            state['config']['automatic_repeats'] = False
            state['last_tick'] = care.iso(MORNING - timedelta(minutes=1))
        self.bot.dispatch_once()
        self.now = NOW

    def install(self):
        with patch.object(care, 'BOOT_CONFIG', {**care.BOOT_CONFIG, 'treatments': [DEFINITION]}):
            self.store.bind(BOT_ID, CHAT, TOPIC, self.now)

    def sends(self):
        return [payload for method, payload in self.client.calls if method == 'sendMessage']

    def test_startup_reload_preserves_all_state_and_dispatches_one_active_supplement(self):
        with self.store.transaction() as state:
            state['inline_receipts'] = {'old': {'answer': {'text': 'saved'}}}
            state['carryovers'] = {'preserve': {'checklist': 'future'}}
            care.health_hold(state, 'telegram:101', 'preserve diet hold', NOW)
            state['tasks'][CID + '_dry']['status'] = 'skipped'
        before = self.store.snapshot()
        self.install()
        saved = self.store.snapshot()
        for field in ('tasks', 'checklists', 'carryovers', 'inline_receipts', 'inline_panels',
                      'progression_hold', 'standalone', 'schedule_history'):
            self.assertEqual(saved[field], before[field], field)
        self.client.calls.clear()
        self.bot.dispatch_once()
        self.assertEqual(len(self.sends()), 1)
        supplement = self.sends()[0]
        self.assertIn(DEFINITION['text'], supplement['text'])
        self.assertNotIn('Кормление', supplement['text'])
        self.assertEqual(len(supplement['reply_markup']['inline_keyboard']), 1)
        self.assertEqual(len(supplement['reply_markup']['inline_keyboard'][0]), 2)
        state = self.store.snapshot()
        job = next(j for j in state['standalone']['outbox'].values()
                   if state['emissions'][j['emission']].get('treatment_ids'))
        self.assertEqual(job['status'], 'sent')
        panel = state['inline_panels'][job['panel']]
        self.assertEqual(panel['task_ids'], [KEY])
        self.assertEqual(panel['pages'][-1]['message_id'], supplement.get('message_id', self.client.next_id))
        self.install()
        self.bot.dispatch_once()
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.store.snapshot()['standalone']['outbox'], state['standalone']['outbox'])

    def test_supplement_callback_is_durable_before_ack_and_never_replays_after_restart(self):
        self.install()
        self.bot.dispatch_once()
        state = self.store.snapshot()
        panel = next(p for p in state['inline_panels'].values() if p['task_ids'] == [KEY])
        def check_saved():
            saved = Store(self.path).snapshot()
            self.assertEqual(saved['tasks'][KEY]['status'], 'done')
            self.assertIn('q1', saved['inline_receipts'])
            self.assertEqual(saved['standalone']['offset'], 101)
        self.client.before_ack = check_saved
        update = {'update_id': 100, 'callback_query': callback(panel, '0')}
        self.bot.process_update(update)
        check_saved()
        self.install()
        self.bot.process_update(update)
        count = len(self.sends())
        self.bot.dispatch_once()
        self.assertEqual(len(self.sends()), count)
        self.assertEqual(self.store.snapshot()['tasks'][KEY]['status'], 'done')

    def test_uncertain_send_is_not_replayed_on_restart(self):
        self.install()
        self.client.calls.clear()
        self.client.send_error = TelegramError(503, uncertain=True)
        self.bot.dispatch_once()
        state = self.store.snapshot()
        job = next(j for j in state['standalone']['outbox'].values() if j['status'] == 'needs_review')
        self.assertEqual(state['inline_panels'][job['panel']]['task_ids'], [KEY])
        self.client.send_error = None
        self.install()
        self.bot.dispatch_once()
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.store.snapshot()['tasks'][KEY]['status'], 'pending')

    def test_pending_supplement_survives_restart_and_disabled_automatic_repeats(self):
        self.install()
        with self.store.transaction() as state:
            text = care.tick(state, NOW)
            eid = state['last_emission_id']
            panel = self.bot.prepare_reminder(state, eid, text, NOW)
            state['standalone']['outbox']['reminder:' + eid] = {
                'status': 'pending', 'panel': panel['nonce'], 'emission': eid,
                'message_ids': [], 'retry_at': 0}
        self.install()
        self.client.calls.clear()
        self.bot.dispatch_once()
        self.assertEqual(len(self.sends()), 1)
        self.assertIn(DEFINITION['text'], self.sends()[0]['text'])
        self.assertEqual(len(self.sends()[0]['reply_markup']['inline_keyboard']), 1)
        self.bot.dispatch_once()
        self.assertEqual(len(self.sends()), 1)

    def test_429_retry_uses_existing_job_and_occurrence(self):
        self.install()
        self.client.calls.clear()
        self.client.send_error = TelegramError(429, retry_after=1)
        self.bot.dispatch_once()
        self.client.send_error = None
        with self.store.transaction() as state:
            for job in state['standalone']['outbox'].values():
                job['retry_at'] = 0
        self.bot.dispatch_once()
        self.assertEqual(len(self.sends()), 2)  # first was definitively rejected
        state = self.store.snapshot()
        self.assertEqual(len(medicines(state)), 1)
        self.assertEqual(len([e for e in state['emissions'].values() if e.get('treatment_ids')]), 1)

    def test_reload_rejects_changed_prescription_and_keeps_existing_file_atomic(self):
        self.install()
        self.bot.dispatch_once()
        before = self.path.read_bytes()
        for field, value in [('text', '2 tablets'), ('interval_months', 1), ('start', '2026-11-11'),
                             ('slot', 'dinner'), ('approved_by', 'someone_else')]:
            changed = {**DEFINITION, field: value}
            with patch.object(care, 'BOOT_CONFIG', {**care.BOOT_CONFIG, 'treatments': [changed]}):
                with self.assertRaises(ValueError):
                    self.store.bind(BOT_ID, CHAT, TOPIC, NOW)
            self.assertEqual(self.path.read_bytes(), before)

    def test_omitted_configuration_preserves_installed_treatment_and_explicit_empty_stops_future(self):
        self.install()
        self.bot.dispatch_once()
        with patch.object(care, 'BOOT_CONFIG', {k: v for k, v in care.BOOT_CONFIG.items() if k != 'treatments'}):
            self.store.bind(BOT_ID, CHAT, TOPIC, NOW)
        self.assertEqual(self.store.snapshot()['config']['treatments'], [DEFINITION])
        tasks = self.store.snapshot()['tasks']
        with patch.object(care, 'BOOT_CONFIG', {**care.BOOT_CONFIG, 'treatments': []}):
            self.store.bind(BOT_ID, CHAT, TOPIC, NOW)
        self.assertEqual(self.store.snapshot()['tasks'], tasks)
        self.assertEqual(self.store.snapshot()['config']['treatments'], [])
        # Reusing an inactive id cannot evade immutability.
        changed = {**DEFINITION, 'interval_months': 1}
        with patch.object(care, 'BOOT_CONFIG', {**care.BOOT_CONFIG, 'treatments': [changed]}):
            with self.assertRaises(ValueError):
                self.store.bind(BOT_ID, CHAT, TOPIC, NOW)

    def test_exact_done_skip_and_snooze_commands_for_treatment(self):
        self.assertIn('treatment_<id>', HELP)
        self.install()
        self.bot.dispatch_once()
        initial = self.store.snapshot()
        for action in ('done', 'skip', 'snooze'):
            state = copy.deepcopy(initial)
            text = '/' + action + ' ' + KEY + (' 30' if action == 'snooze' else '')
            message = {'message_id': 999, 'chat': {'id': CHAT}, 'message_thread_id': TOPIC,
                       'from': {'id': 101, 'is_bot': False}, 'text': text}
            self.bot.message(state, message, NOW)
            expected = {'done': 'done', 'skip': 'skipped', 'snooze': 'deferred'}[action]
            self.assertEqual(state['tasks'][KEY]['status'], expected)
            for key, task in initial['tasks'].items():
                if key != KEY:
                    self.assertEqual(state['tasks'][key], task)

    def test_fresh_start_cutoff_does_not_suppress_today_treatment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'new.json'
            store = Store(path)
            boot = {**care.BOOT_CONFIG, 'treatments': [DEFINITION], 'automatic_repeats': False}
            with patch.object(care, 'BOOT_CONFIG', boot):
                store.bind(BOT_ID, CHAT, TOPIC, NOW, fresh_start=True)
            client = FakeClient()
            bot = Bot(Runtime(CHAT, TOPIC, frozenset({101}), path), client, BOT_ID,
                      'example_care_bot', store, lambda: NOW)
            bot.dispatch_once()
            sends = [p for m, p in client.calls if m == 'sendMessage']
            self.assertEqual(len(sends), 1)
            self.assertIn(DEFINITION['text'], sends[0]['text'])
            self.assertNotIn('Кормление', sends[0]['text'])
            bot.dispatch_once()
            self.assertEqual(len([p for m, p in client.calls if m == 'sendMessage']), 1)

    def test_completion_before_supplement_delivery_suppresses_stale_message(self):
        self.install()
        self.client.calls.clear()
        self.client.send_error = TelegramError(429, retry_after=1)
        self.bot.dispatch_once()
        self.client.send_error = None
        with self.store.transaction() as state:
            command(state)
            for job in state['standalone']['outbox'].values():
                job['retry_at'] = 0
        self.bot.dispatch_once()
        self.assertEqual(len(self.sends()), 1)  # only the rejected send was attempted
        self.assertEqual(self.store.snapshot()['tasks'][KEY]['status'], 'done')
        jobs = self.store.snapshot()['standalone']['outbox'].values()
        self.assertTrue(any(job['status'] == 'suppressed' for job in jobs))

    def test_supplement_pending_job_keeps_emission_identity_past_retention_window(self):
        self.install()
        self.client.calls.clear()
        self.client.send_error = TelegramError(429, retry_after=3600)
        self.bot.dispatch_once()
        self.client.send_error = None
        with self.store.transaction() as state:
            eid = next(j['emission'] for j in state['standalone']['outbox'].values()
                       if j['status'] == 'pending')
            for offset in range(1, 16):
                # Keep the supplement pending while ordinary meals advance.
                now = care.wall(NOW.date() + timedelta(days=offset), '07:00')
                care.tick(state, now)
            self.assertIn(eid, state['emissions'])
            for job in state['standalone']['outbox'].values():
                job['retry_at'] = 0
        self.now = care.instant('2026-11-25T08:00:00+00:00')
        self.bot.dispatch_once()
        self.assertEqual(len(self.sends()), 2)
        self.assertIn('Нужно уточнить', self.sends()[-1]['text'])
        self.assertNotIn('Кормление', self.sends()[-1]['text'])

    def test_global_pause_suppresses_pending_supplement_delivery(self):
        self.install()
        self.client.send_error = TelegramError(429, retry_after=1)
        self.bot.dispatch_once()
        self.client.send_error = None
        with self.store.transaction() as state:
            state['reminder_pause'] = {'until': 'indefinite'}
            for job in state['standalone']['outbox'].values():
                job['retry_at'] = 0
        before = len(self.sends())
        self.bot.dispatch_once()
        self.assertEqual(len(self.sends()), before)
        self.assertEqual(self.store.snapshot()['tasks'][KEY]['status'], 'pending')
        # Resuming keeps the same pending first-announcement job, even when
        # automatic reminders are disabled.
        with self.store.transaction() as state:
            state['reminder_pause'] = None
            for job in state['standalone']['outbox'].values():
                job['retry_at'] = 0
        self.bot.dispatch_once()
        self.assertEqual(len(self.sends()), before + 1)
        self.assertIn(DEFINITION['text'], self.sends()[-1]['text'])
        self.bot.dispatch_once()
        self.assertEqual(len(self.sends()), before + 1)


if __name__ == '__main__':
    unittest.main()
