"""Previous feeding reminders are a display choice, not a change to care records."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import care
import inline_ui
import treatments
from configuration import Runtime, validate_care_config
from service import Bot
from storage import Store
from test_service import BOT_ID, CHAT, TOPIC, FakeClient
from test_treatments import DEFINITION

ROOT = Path(__file__).resolve().parents[1]
BREAKFAST = '2026-10-19_07:00'
LUNCH = '2026-10-19_14:00'
MEDICINE = BREAKFAST + '_treatment_credelio'


def at(hhmm='07:00', day='2026-10-19'):
    return care.instant(day + 'T' + hhmm + ':00+01:00')


def state_for(flag=False, repeats=True, medicine=False):
    state = care.initial()
    state['config'].update(previous_feeding_reminders=flag, automatic_repeats=repeats,
                           food_transfer_policy='add_full')
    # Exercise all four foods, including the transition products.
    for kind in ('wet_new', 'dry_new'):
        state['config'].setdefault('food_products', {})[kind] = {
            'name': kind, 'label': kind, 'button_label': kind}
        for stage in state['config']['stages']:
            stage[kind] = [1, 2, 3]
            stage['daily_' + kind] = 6
    if medicine:
        definition = {**DEFINITION, 'start': '2026-10-19',
                      'approved_at': '2026-10-18T12:00:00+01:00'}
        treatments.apply_configuration(state, [definition], at())
    return state


class PreviousFeedingAssertions:
    def assert_no_old_food_text(self, text):
        for phrase in ('Нужно уточнить: была ли выдана порция',
                       'Предыдущее кормление не подтверждено',
                       'Уточните, выдавали ли эти порции',
                       'По старым кормлениям', 'Не суммируйте старые порции',
                       'Если корм уже выдали'):
            self.assertNotIn(phrase, text)

    def assert_no_food_rows(self, state, eid, text, now):
        self.assert_no_old_food_text(text)
        self.assertNotIn('Кормление', text)
        keys = care.emission_task_ids(state, eid)
        self.assertTrue(all(state['tasks'][key]['type'] not in care.FOOD_TYPES for key in keys))
        if text != 'NO_REPLY':
            panel = inline_ui.prepare(state, [text], keys, now,
                                      primary=state['emissions'][eid]['primary'])
            keyboard = inline_ui.keyboard(state, panel)
            self.assertEqual(len(keyboard['inline_keyboard']),
                             sum(state['tasks'][key]['status'] not in care.CLOSED | {'deferred'}
                                 for key in keys))
            for kind in care.FOOD_TYPES:
                self.assertNotIn(care.task_line(state['tasks'][BREAKFAST + '_' + kind]), text)


class PreviousFeedingTests(PreviousFeedingAssertions, unittest.TestCase):
    def test_setting_is_optional_and_strictly_boolean(self):
        config = copy.deepcopy(care.BOOT_CONFIG)
        config.pop('previous_feeding_reminders', None)
        validate_care_config(config)
        for value in (True, False):
            validate_care_config({**config, 'previous_feeding_reminders': value})
        for value in (None, 0, 1, 0.0, 1.0, 'false', [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_care_config({**config, 'previous_feeding_reminders': value})

    def test_deployment_configs_match_and_opt_out_without_changing_suspension(self):
        import yaml
        home = json.loads((ROOT / 'deploy/standalone/config.json').read_text())
        chart = yaml.safe_load((ROOT / 'charts/telegram-bot-suite/values-home.yaml').read_text())
        embedded = json.loads(chart['workers'][0]['config']['config.json'])
        self.assertEqual(home, embedded)
        self.assertIs(home['previous_feeding_reminders'], False)
        self.assertEqual(chart['replicas'], 0)
        validate_care_config(home)

    def test_warnings_suppressed_without_mutating_old_tasks(self):
        state = state_for(flag=True)
        care.tick(state, at())
        care.tick(state, at('14:00'))
        self.assertTrue(care.prior_warning(state, LUNCH))
        before = copy.deepcopy(state['tasks'])
        state['config']['previous_feeding_reminders'] = False
        self.assertEqual(care.prior_warning(state, LUNCH), [])
        self.assertNotIn('Предыдущее кормление', care.render_checklist(state, LUNCH))
        self.assertEqual(state['tasks'], before)
        for kind in care.FOOD_TYPES:
            self.assertEqual(state['tasks'][BREAKFAST + '_' + kind]['status'], 'needs_clarification')

    def test_omitted_flag_is_identical_to_true_for_tick_refresh_and_buttons(self):
        omitted = state_for(flag=True, medicine=True)
        omitted['config'].pop('previous_feeding_reminders')
        explicit = copy.deepcopy(omitted)
        explicit['config']['previous_feeding_reminders'] = True
        for now in (at(), at('07:30'), at('14:00'), at('07:00', '2026-10-20')):
            self.assertEqual(care.tick(omitted, now), care.tick(explicit, now))
            left, right = omitted['last_emission_id'], explicit['last_emission_id']
            self.assertEqual(care.refresh(omitted, left, now), care.refresh(explicit, right, now))
            self.assertEqual(care.emission_task_ids(omitted, left), care.emission_task_ids(explicit, right))
            self.assertEqual(omitted['tasks'], explicit['tasks'])
        self.assertTrue(care.prior_warning(explicit, '2026-10-20_07:00'))

    def test_next_meal_and_overnight_keep_current_food_cleaning_and_treatment(self):
        state = state_for(medicine=True)
        care.tick(state, at())
        care.tick(state, at('07:30'))
        # Current food still repeats normally before the next meal.
        self.assertTrue(all(BREAKFAST + '_' + kind in care.emission_task_ids(
            state, state['last_emission_id']) for kind in care.FOOD_TYPES))
        for now, cid in ((at('14:00'), LUNCH),
                         (at('07:00', '2026-10-20'), '2026-10-20_07:00')):
            text = care.tick(state, now)
            keys = care.emission_task_ids(state, state['last_emission_id'])
            self.assertIn(BREAKFAST + '_litter', keys)
            self.assertIn(BREAKFAST + '_bowls', keys)
            self.assertIn(BREAKFAST + '_water', keys)
            self.assertIn(MEDICINE, keys)
            self.assertIn('💊 Обработка', text)
            if now.date() > at().date():
                self.assertIn('Не давайте повторную или двойную дозу', text)
            self.assertNotIn('Предыдущее кормление', text)
            for key in keys:
                task = state['tasks'][key]
                if task['type'] in care.FOOD_TYPES:
                    self.assertEqual(task['checklist'], cid)
                    self.assertFalse(task['needs_clarification'])
            for kind in care.FOOD_TYPES:
                self.assertIn(cid + '_' + kind, keys)
                old = state['tasks'][BREAKFAST + '_' + kind]
                self.assertEqual(old['status'], 'needs_clarification')
                self.assertIsNone(old['done_at'])
                self.assertIsNone(old['skip_at'])
        text = care.tick(state, at('07:30', '2026-10-20'))
        self.assertNotIn('Нужно уточнить: была ли выдана порция', text)

    def test_live_settings_announce_current_meals_without_previous_food_warnings(self):
        state = state_for(repeats=False, medicine=True)
        care.tick(state, at())
        for now, cid in ((at('14:00'), LUNCH),
                         (at('07:00', '2026-10-20'), '2026-10-20_07:00')):
            text = care.tick(state, now)
            self.assert_no_old_food_text(text)
            self.assertIn('Автоматические повторы выключены.', text)
            self.assertEqual(state['emissions'][state['last_emission_id']]['repeat_ids'], [])
            keys = care.emission_task_ids(state, state['last_emission_id'])
            for kind in care.FOOD_TYPES:
                self.assertIn(cid + '_' + kind, keys)
            care.refresh(state, state['last_emission_id'], now)
        self.assertEqual(care.tick(state, at('07:30', '2026-10-20')), 'NO_REPLY')

    def test_current_food_repeat_omits_old_portion_boilerplate(self):
        state = state_for()
        care.tick(state, at())
        text = care.tick(state, at('07:30'))
        self.assertIn('Если корм уже выдали, подтвердите выдачу.', text)
        self.assertNotIn('Не суммируйте старые порции', text)
        self.assertNotIn('Предыдущее кормление', text)
        keys = care.emission_task_ids(state, state['last_emission_id'])
        for kind in care.FOOD_TYPES:
            self.assertIn(BREAKFAST + '_' + kind, keys)

    def test_legacy_queued_first_breakfast_drops_stale_portion_announcement(self):
        state = care.initial()
        state['config']['previous_feeding_reminders'] = False
        care.tick(state, at())
        eid = state['last_emission_id']
        text = care.refresh(state, eid, at('14:00'))
        self.assert_no_old_food_text(text)
        self.assertNotIn('Сегодня меняются порции', text)
        self.assertNotIn('Кормление', text)
        self.assertNotIn('07:00: ', text)
        self.assertIn(care.CLEANING['water'], text)

    def test_queued_repeat_refresh_keeps_only_cleaning_and_treatment(self):
        state = state_for(flag=True, medicine=True)
        care.tick(state, at())
        care.tick(state, at('07:30'))
        eid = state['last_emission_id']
        state['config']['previous_feeding_reminders'] = False
        text = care.refresh(state, eid, at('14:00'))
        self.assert_no_food_rows(state, eid, text, at('14:00'))
        self.assertIn(MEDICINE, care.emission_task_ids(state, eid))
        self.assertIn('💊 Обработка', text)
        self.assertIn(care.CLEANING['water'], text)
        before = copy.deepcopy(state['tasks'])
        care.refresh(state, eid, at('14:00'))
        care.emission_task_ids(state, eid)
        self.assertEqual(state['tasks'], before)

    def test_queued_breakfast_primary_loses_stale_food_at_next_meal_and_overnight(self):
        for now in (at('14:00'), at('07:00', '2026-10-20')):
            with self.subTest(now=now):
                state = state_for(medicine=True)
                care.tick(state, at())
                eid = state['last_emission_id']
                text = care.refresh(state, eid, now)
                self.assert_no_food_rows(state, eid, text, now)
                self.assertNotIn('Сегодня меняются порции', text)
                self.assertNotIn('Пора покормить', text)
                self.assertIn(care.CLEANING['litter'], text)
                self.assertIn(MEDICINE, care.emission_task_ids(state, eid))

    def test_food_only_primary_and_repeat_suppress_when_stale(self):
        state = state_for()
        for slot in state['config']['slots']:
            slot['cleaning'] = False
        state['schedule_history'][0]['slots'] = copy.deepcopy(state['config']['slots'])
        care.tick(state, at())
        primary = state['last_emission_id']
        care.tick(state, at('07:30'))
        repeat = state['last_emission_id']
        for eid in (primary, repeat):
            self.assertEqual(care.refresh(state, eid, at('14:00')), 'NO_REPLY')
            self.assertEqual(care.emission_task_ids(state, eid), [])

    def test_stale_food_does_not_displace_visible_cleaning_buttons_in_bounded_backlog(self):
        state = state_for(flag=True)
        care.tick(state, at())
        care.tick(state, at('07:00', '2026-10-23'))
        eid = state['last_emission_id']
        state['config']['previous_feeding_reminders'] = False
        text = care.refresh(state, eid, at('07:00', '2026-10-23'))
        keys = care.emission_task_ids(state, eid)
        self.assertNotIn('По старым кормлениям', text)
        # Every displayed cleaning row has a binding/button, despite newer food-only lists.
        for task in state['tasks'].values():
            if (task['type'] in care.CLEANING
                    and ('список ' + task['checklist']) in text
                    and care.task_line(task) in text):
                self.assertIn(task['id'], keys)

    def test_saved_inline_warning_removed_but_historical_rows_and_bindings_preserved(self):
        state = state_for(flag=True)
        care.tick(state, at())
        text = care.tick(state, at('14:00'))
        eid = state['last_emission_id']
        panel = inline_ui.prepare(state, [text], care.emission_task_ids(state, eid),
                                  at('14:00'), primary=LUNCH)
        panel['status'] = 'sent'
        before = copy.deepcopy(state)
        state['config']['previous_feeding_reminders'] = False
        rendered = '\n'.join(inline_ui.page_text(state, panel, page) for page in panel['pages'])
        self.assertNotIn('Предыдущее кормление не подтверждено', rendered)
        self.assertNotIn('Уточните, выдавали ли эти порции.', rendered)
        self.assertIn('Нужно уточнить: была ли выдана порция', rendered)
        self.assertEqual(state['inline_panels'], before['inline_panels'])
        self.assertEqual(state['tasks'], before['tasks'])
        self.assertEqual(state['checklists'], before['checklists'])

    def test_explicit_history_show_and_confirmation_remain_accurate(self):
        state = state_for()
        care.tick(state, at())
        care.tick(state, at('14:00'))
        historical = care.render_checklist(state, BREAKFAST)
        self.assertIn('Нужно уточнить: была ли выдана порция', historical)
        direct = care.render_reminders(state, care.unresolved(state, BREAKFAST))
        self.assertIn('Нужно уточнить: была ли выдана порция', direct)
        key = BREAKFAST + '_wet_new'
        before = copy.deepcopy(state['tasks'])
        answer = care.complete_button(state, care.button_text(state['tasks'][key]),
                                      'telegram:101', 'explicit-old', at('14:00'))
        self.assertIn('✅', answer)
        self.assertEqual(state['tasks'][key]['status'], 'done')
        self.assertEqual(state['tasks'][key]['grams'], before[key]['grams'])
        self.assertEqual(state['tasks'][key]['done_by'], 'telegram:101')
        for tid in set(before) - {key}:
            self.assertEqual(state['tasks'][tid], before[tid])
        snapshot = copy.deepcopy(state['tasks'])
        care.complete_button(state, care.button_text(state['tasks'][key]),
                             'telegram:102', 'duplicate-old', at('14:00'))
        self.assertEqual(state['tasks'], snapshot)

    def test_old_saved_inline_callback_still_confirms_only_its_bound_food(self):
        from test_treatments import callback, accept
        state = state_for()
        text = care.tick(state, at())
        key = BREAKFAST + '_dry'
        panel = inline_ui.prepare(state, [text], [key], at(), primary=BREAKFAST)
        panel['status'] = 'sent'
        panel['pages'][-1]['message_id'] = 777
        care.tick(state, at('14:00'))
        before = copy.deepcopy(state['tasks'])
        answer = accept(state, callback(panel, action='0'), now=at('14:00'))
        self.assertFalse(answer['show_alert'])
        self.assertEqual(state['tasks'][key]['status'], 'done')
        self.assertEqual(state['tasks'][key]['done_by'], 'telegram:101')
        for tid in set(before) - {key}:
            self.assertEqual(state['tasks'][tid], before[tid])

    def test_explicit_transfers_keep_planned_plus_all_carried_food_and_completion(self):
        state = state_for()
        care.tick(state, at())
        first = care.move_to_next(state, BREAKFAST + '_dry', 'owner', at('08:00'))
        self.assertEqual(first['food_added_grams'], 10)
        state = json.loads(json.dumps(state))
        text = care.tick(state, at('14:00'))
        target = state['tasks'][LUNCH + '_dry']
        self.assertEqual((target['planned_grams'], target['carried_grams'], target['grams']), (10, 10, 20))
        self.assertIn('Включая перенос: 10 г.', text)
        care.tick(state, at('19:00'))
        # Even a stale, hidden destination can be explicitly moved with its whole amount.
        result = care.move_to_next(state, LUNCH + '_dry', 'owner', at('19:01'))
        self.assertEqual(result['food_added_grams'], 20)
        source = copy.deepcopy(state['tasks'][LUNCH + '_dry'])
        state = json.loads(json.dumps(state))
        text = care.tick(state, at('07:00', '2026-10-20'))
        key = result['target_id']
        target = state['tasks'][key]
        self.assertEqual((target['planned_grams'], target['carried_grams'], target['grams']), (10, 20, 30))
        self.assertIn('Включая перенос: 20 г.', text)
        self.assertIn(key, care.emission_task_ids(state, state['last_emission_id']))
        care.complete_button(state, care.button_text(target), 'owner', 'done-carry', at('07:01', '2026-10-20'))
        self.assertEqual(target['status'], 'done')
        self.assertEqual(target['grams'], 30)
        self.assertEqual(state['tasks'][LUNCH + '_dry'], source)


class PreviousFeedingPersistenceTests(PreviousFeedingAssertions, unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory(dir=ROOT / '.test-tmp'))
        self.path = Path(directory) / 'state.json'
        self.store = Store(self.path)
        self.store.bind(BOT_ID, CHAT, TOPIC, at())
        with self.store.transaction() as state:
            saved = state_for(flag=True, medicine=True)
            saved['standalone'] = state['standalone']
            state.clear()
            state.update(saved)
        self.client = FakeClient()
        self.bot = Bot(Runtime(CHAT, TOPIC, frozenset({101}), self.path), self.client,
                       BOT_ID, 'example_care_bot', self.store)

    def prepare(self, now):
        with self.store.transaction() as state:
            text = care.tick(state, now)
            eid = state['last_emission_id']
            panel = self.bot.prepare_reminder(state, eid, text, now)
            key = 'reminder:' + eid
            state['standalone']['outbox'][key] = {
                'status': 'pending', 'panel': panel['nonce'], 'emission': eid,
                'message_ids': [], 'retry_at': 0}
        return key

    def bind_flag(self, flag=False):
        with patch.object(care, 'BOOT_CONFIG', {'previous_feeding_reminders': flag}):
            Store(self.path).bind(BOT_ID, CHAT, TOPIC, at('14:00'))

    def test_bind_changes_only_explicit_flag_and_preserves_tasks_carries_schedule_treatments(self):
        self.prepare(at())
        with self.store.transaction() as state:
            care.move_to_next(state, BREAKFAST + '_dry', 'owner', at('08:00'))
            care.complete_button(state, care.button_text(state['tasks'][BREAKFAST + '_wet']),
                                 'owner', 'saved-done', at('08:00'))
        before = self.store.snapshot()
        self.bind_flag()
        saved = self.store.snapshot()
        self.assertEqual(saved['config'], {**before['config'], 'previous_feeding_reminders': False})
        for key in set(before) - {'config', 'audit'}:
            self.assertEqual(saved[key], before[key], key)
        self.assertEqual(saved['config']['treatments'], before['config']['treatments'])
        # Omission on restart preserves the saved choice, including queued work.
        with patch.object(care, 'BOOT_CONFIG', {}):
            Store(self.path).bind(BOT_ID, CHAT, TOPIC, at('14:00'))
        self.assertIs(self.store.snapshot()['config']['previous_feeding_reminders'], False)
        self.bind_flag(True)
        self.assertIs(self.store.snapshot()['config']['previous_feeding_reminders'], True)
        self.assertEqual(self.store.snapshot()['tasks'], before['tasks'])

    def test_switch_is_applied_after_nutrition_replaces_config(self):
        order = []
        def replace(state, plan, now):
            order.append('nutrition')
            state['config']['previous_feeding_reminders'] = True
        with patch.object(care, 'BOOT_CONFIG', {'previous_feeding_reminders': False,
                                               'nutrition_plan': {}}), patch(
                'nutrition_plan.apply_nutrition_plan', side_effect=replace):
            self.store.bind(BOT_ID, CHAT, TOPIC, at())
        self.assertEqual(order, ['nutrition'])
        self.assertIs(self.store.snapshot()['config']['previous_feeding_reminders'], False)

    def check_old_primary_delivery(self, now):
        key = self.prepare(at())
        self.bind_flag()
        self.bot.deliver(key, now)
        saved = self.store.snapshot()
        job = saved['standalone']['outbox'][key]
        self.assertEqual(job['status'], 'sent')
        panel = saved['inline_panels'][job['panel']]
        text = '\n'.join(payload['text'] for method, payload in self.client.calls
                         if method == 'sendMessage')
        self.assert_no_old_food_text(text)
        self.assertNotIn('Кормление', text)
        self.assertTrue(all(saved['tasks'][tid]['type'] not in care.FOOD_TYPES
                            for tid in panel['task_ids']))
        self.assertIn(MEDICINE, panel['task_ids'])

    def test_unsent_old_primary_refreshes_before_send_at_next_meal_after_restart(self):
        self.check_old_primary_delivery(at('14:00'))

    def test_unsent_old_primary_refreshes_before_send_after_overnight_restart(self):
        self.check_old_primary_delivery(at('07:00', '2026-10-20'))

    def test_unsent_food_only_lunch_is_suppressed_when_stale_without_closing_tasks(self):
        self.prepare(at())
        with self.store.transaction() as state:
            state['config']['automatic_repeats'] = False
        key = self.prepare(at('14:00'))
        self.bind_flag()
        self.bot.deliver(key, at('07:00', '2026-10-20'))
        saved = self.store.snapshot()
        self.assertEqual(saved['standalone']['outbox'][key]['status'], 'suppressed')
        self.assertEqual(self.client.calls, [])
        for kind in care.FOOD_TYPES:
            task = saved['tasks'][LUNCH + '_' + kind]
            self.assertEqual(task['status'], 'needs_clarification')
            self.assertIsNone(task['done_at'])
            self.assertIsNone(task['skip_at'])

    def test_partial_old_primary_stops_for_review_without_rebinding_or_sending(self):
        key = self.prepare(at())
        with self.store.transaction() as state:
            state['standalone']['outbox'][key]['message_ids'] = [777]
        self.bind_flag()
        panel_id = self.store.snapshot()['standalone']['outbox'][key]['panel']
        self.bot.deliver(key, at('14:00'))
        saved = self.store.snapshot()
        self.assertEqual(saved['standalone']['outbox'][key]['status'], 'needs_review')
        self.assertEqual(saved['standalone']['outbox'][key]['panel'], panel_id)
        self.assertEqual(self.client.calls, [])


if __name__ == '__main__':
    unittest.main()
