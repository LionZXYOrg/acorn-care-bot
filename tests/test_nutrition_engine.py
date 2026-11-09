"""Offline product-specific engine regressions; no installation or network required."""
import copy
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import care
import inline_ui
from configuration import FOOD_TYPES, Runtime, validate_care_config
from service import Bot, HELP, chunks
from storage import Store
from telegram_api import TelegramError
from test_service import FakeClient, CHAT, TOPIC, BOT_ID


PRODUCTS = {
    'wet': {'name': 'Royal Canin Gastrointestinal Kitten Wet',
            'label': 'Старый влажный', 'button_label': 'Старый влажный', 'pack_grams': 195},
    'wet_new': {'name': 'Royal Canin Kitten in Jelly',
                'label': 'Новый влажный', 'button_label': 'Новый влажный', 'pack_grams': 85},
    'dry': {'name': 'Royal Canin Gastrointestinal Kitten Dry',
            'label': 'Старый сухой', 'button_label': 'Старый сухой'},
    'dry_new': {'name': 'Royal Canin Kitten Dry',
                'label': 'Новый сухой', 'button_label': 'Новый сухой'},
}
# The engine accepts already-installed config, not the parent's boot-plan schema.
STEPS = [
    ('2026-11-20', 'wet_step_1', [0, 0, 49], [0, 0, 21], [20, 20, 21], [0, 0, 0]),
    ('2026-11-23', 'wet_step_2', [0, 0, 33], [0, 0, 43], [20, 19, 20], [0, 0, 0]),
    ('2026-11-26', 'wet_step_3', [0, 0, 16], [0, 0, 64], [20, 19, 20], [0, 0, 0]),
    ('2026-11-28', 'wet_complete', [0, 0, 0], [0, 0, 85], [19, 19, 20], [0, 0, 0]),
    ('2026-11-30', 'dry_step_1', [0, 0, 0], [0, 0, 85], [15, 14, 15], [5, 5, 5]),
    ('2026-12-03', 'dry_step_2', [0, 0, 0], [0, 0, 85], [10, 9, 10], [10, 10, 10]),
    ('2026-12-06', 'dry_step_3', [0, 0, 0], [0, 0, 85], [5, 5, 5], [15, 15, 15]),
    ('2026-12-09', 'regular_kitten', [0, 0, 0], [0, 0, 85], [0, 0, 0], [20, 20, 20]),
]


def state_with_plan():
    state = care.initial()
    config = state['config']
    config.update(food_products=copy.deepcopy(PRODUCTS), nutrition_plan_id='engine-test-plan',
                  nutrition_notice_time='19:30', mention='@palmette @LionZXY',
                  automatic_repeats=False, food_transfer_policy='add_full')
    config['stages'] = [stage for stage in config['stages'] if stage['start'] <= '2026-11-09']
    for start, identity, *portions in STEPS:
        stage = {'id': identity, 'start': start, 'label': identity, 'morning_notice': True,
                 'advance_notice': f'С {date.fromisoformat(start).day} числа меняем порции корма.'}
        for kind, values in zip(FOOD_TYPES, portions):
            stage[kind] = values[:]
            stage['daily_' + kind] = sum(values)
        config['stages'].append(stage)
    return state


def at(day, hhmm):
    return care.wall(date.fromisoformat(day), hhmm)


def checklist(state, day, index):
    day = date.fromisoformat(day)
    return care.ensure_checklist(state, day, care.slots_for_day(state, day)[index], index)


def tick_today(state, day, hhmm):
    state['last_tick'] = care.iso(at(day, '06:00'))
    return care.tick(state, at(day, hhmm))


def panel_for(state, cid, now):
    panel = inline_ui.prepare(state, chunks(care.render_checklist(state, cid)),
                              state['checklists'][cid]['task_ids'], now, primary=cid)
    panel['status'] = 'sent'
    for index, page in enumerate(panel['pages']):
        page['message_id'] = 777 + index
    return panel


def callback(panel, key, *, move=False, receipt='test-callback'):
    index = panel['task_ids'].index(key)
    return {'id': receipt, 'from': {'id': 101, 'is_bot': False, 'username': 'palmette'},
            'message': {'message_id': panel['pages'][-1]['message_id'],
                        'chat': {'id': CHAT}, 'message_thread_id': TOPIC,
                        'from': {'id': BOT_ID, 'is_bot': True}},
            'data': inline_ui.PREFIX + panel['nonce'] + ':' + ('next:' if move else '') + str(index)}


class NutritionConfigurationTests(unittest.TestCase):
    def test_food_types_and_missing_optional_arrays_are_compatible(self):
        self.assertEqual(FOOD_TYPES, ('wet', 'wet_new', 'dry', 'dry_new'))
        self.assertIs(care.FOOD_TYPES, FOOD_TYPES)
        config = copy.deepcopy(care.BOOT_CONFIG)
        before = copy.deepcopy(config)
        validate_care_config(config)
        self.assertEqual(config, before)
        config = state_with_plan()['config']
        validate_care_config(config)
        baseline = config['stages'][2]
        self.assertNotIn('wet_new', baseline)
        self.assertNotIn('dry_new', baseline)
        baseline.update(daily_wet_new=0, daily_dry_new=0)
        validate_care_config(config)

    def test_finite_nonnegative_numeric_three_meal_portions_and_totals(self):
        for kind in FOOD_TYPES:
            for bad in (-1, True, '1', None, float('nan'), float('inf'), float('-inf')):
                for field in (kind, 'daily_' + kind):
                    with self.subTest(kind=kind, field=field, bad=bad):
                        config = state_with_plan()['config']
                        stage = config['stages'][3]
                        stage[field] = [0, bad, 0] if field == kind else bad
                        with self.assertRaises(ValueError):
                            validate_care_config(config)
            for values in ([], [1, 2], [1, 2, 3, 4], None, '123', {}):
                with self.subTest(kind=kind, values=values):
                    config = state_with_plan()['config']
                    config['stages'][3][kind] = values
                    with self.assertRaises(ValueError):
                        validate_care_config(config)

    def test_decimal_totals_are_exact_not_float_accumulations(self):
        config = state_with_plan()['config']
        stage = config['stages'][3]
        for kind in FOOD_TYPES:
            stage[kind] = [0.1, 0.2, 0]
            stage['daily_' + kind] = 0.3
        validate_care_config(config)
        for kind in FOOD_TYPES:
            bad = copy.deepcopy(config)
            bad['stages'][3]['daily_' + kind] = 0.30000000000000004
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                validate_care_config(bad)
        config['stages'][2]['daily_wet_new'] = 1
        with self.assertRaises(ValueError):
            validate_care_config(config)

    def test_every_used_new_kind_requires_metadata(self):
        for kind in ('wet_new', 'dry_new'):
            config = state_with_plan()['config']
            del config['food_products'][kind]
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                validate_care_config(config)
        config = copy.deepcopy(care.BOOT_CONFIG)
        config['stages'][0].update(wet_new=[0, 0, 0], dry_new=[0, 0, 0],
                                    daily_wet_new=0, daily_dry_new=0)
        validate_care_config(config)

    def test_product_metadata_is_bounded_safe_text_and_positive_pack_weight(self):
        for field, limit in (('name', 256), ('label', 64), ('button_label', 64)):
            for value in ('', ' ', '\nunsafe', 'two\nlines', '<b>food</b>', 'bad\x00name',
                          '\u202ename', 'name · wrong', 'x' * (limit + 1), None, 123):
                config = state_with_plan()['config']
                config['food_products']['wet_new'][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    validate_care_config(config)
        for value in (0, -1, True, '85', None, float('inf'), float('nan')):
            config = state_with_plan()['config']
            config['food_products']['wet_new']['pack_grams'] = value
            with self.subTest(pack=value), self.assertRaises(ValueError):
                validate_care_config(config)
        for value in (None, [], {'fish': PRODUCTS['wet']}, {'wet': None}, {'wet': {}}):
            config = state_with_plan()['config']
            config['food_products'] = value
            with self.subTest(products=value), self.assertRaises(ValueError):
                validate_care_config(config)

    def test_optional_notice_configuration_is_strict(self):
        for field, values in (('nutrition_plan_id', ('', None, 'bad\nidentity')),
                              ('nutrition_notice_time', ('7:30', '24:00', None, '19:30:00'))):
            for value in values:
                config = state_with_plan()['config']
                config[field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    validate_care_config(config)
        for field, values in (('morning_notice', (0, 1, None, 'true')),
                              ('advance_notice', ('', None, 'bad\nnotice', 'x' * 2049))):
            for value in values:
                config = state_with_plan()['config']
                config['stages'][3][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    validate_care_config(config)


class NutritionTaskTests(unittest.TestCase):
    def setUp(self):
        care.BUTTON_BOT = '@example_care_bot'
        self.state = state_with_plan()

    def all_foods(self):
        stage = self.state['config']['stages'][3]
        for kind in FOOD_TYPES:
            stage[kind] = [10, 10, 10]
            stage['daily_' + kind] = 30
        return checklist(self.state, '2026-11-20', 0)

    def test_all_four_products_have_separate_ids_snapshots_and_distinct_buttons(self):
        cid = self.all_foods()
        panel = panel_for(self.state, cid, at('2026-11-20', '07:00'))
        rows = inline_ui.keyboard(self.state, panel)['inline_keyboard'][:4]
        self.assertEqual([r[0]['text'] for r in rows],
                         ['✅ ' + PRODUCTS[k]['button_label'] + ' 10 г' for k in FOOD_TYPES])
        self.assertEqual(len({r[0]['callback_data'] for r in rows}), 4)
        text = care.render_checklist(self.state, cid)
        for kind in FOOD_TYPES:
            task = self.state['tasks'][cid + '_' + kind]
            self.assertEqual(task['type'], kind)
            self.assertEqual(task['food_name'], PRODUCTS[kind]['name'])
            self.assertEqual(task['food_label'], PRODUCTS[kind]['label'])
            self.assertEqual(task['button_name'], PRODUCTS[kind]['button_label'])
            self.assertIn(care.product_row(task), text)
        self.assertEqual(self.state['tasks'][cid + '_wet']['wet_can_grams'], 195)
        self.assertEqual(self.state['tasks'][cid + '_wet_new']['wet_can_grams'], 85)
        before = copy.deepcopy(self.state['tasks'])
        self.state['config']['food_products']['wet_new']['name'] = 'Different future name'
        self.assertEqual(self.state['tasks'], before)
        self.assertEqual(care.render_checklist(self.state, cid), text)

    def test_product_specific_inline_and_text_completions_close_only_one_product(self):
        for kind in FOOD_TYPES:
            for use_inline in (True, False):
                with self.subTest(kind=kind, inline=use_inline):
                    self.state = state_with_plan()
                    cid = self.all_foods()
                    now = at('2026-11-20', '07:01')
                    key = cid + '_' + kind
                    if use_inline:
                        panel = panel_for(self.state, cid, now)
                        cb = callback(panel, key)
                        answer = inline_ui.accept_callback(self.state, cb, {101}, BOT_ID, now, CHAT, TOPIC)
                        self.assertFalse(answer['show_alert'])
                        before = copy.deepcopy(self.state)
                        inline_ui.accept_callback(self.state, cb, {101}, BOT_ID, now, CHAT, TOPIC)
                        self.assertEqual(self.state, before)
                    else:
                        task = self.state['tasks'][key]
                        care.complete_button(self.state, care.button_text(task), 'palmette', 'text', now)
                    self.assertEqual(self.state['tasks'][key]['status'], 'done')
                    self.assertEqual(sum(t['status'] == 'done' for t in self.state['tasks'].values()), 1)

    def test_zero_and_missing_optional_products_have_no_tasks(self):
        for start, _, *portions in STEPS:
            for index in range(3):
                cid = checklist(self.state, start, index)
                for kind, values in zip(FOOD_TYPES, portions):
                    with self.subTest(day=start, meal=index, kind=kind):
                        self.assertEqual(cid + '_' + kind in self.state['tasks'], bool(values[index]))
        cid = checklist(self.state, '2026-11-19', 0)
        self.assertNotIn(cid + '_wet_new', self.state['tasks'])
        self.assertNotIn(cid + '_dry_new', self.state['tasks'])

    def test_wet_pack_fallback_applies_to_both_wet_products(self):
        for kind in ('wet', 'wet_new'):
            del self.state['config']['food_products'][kind]['pack_grams']
        cid = checklist(self.state, '2026-11-20', 2)
        for kind in ('wet', 'wet_new'):
            self.assertEqual(self.state['tasks'][cid + '_' + kind]['wet_can_grams'], 195)

    def test_legacy_saved_tasks_do_not_gain_metadata_or_change_rendering(self):
        state = care.initial()
        cid = checklist(state, '2026-10-20', 0)
        before = copy.deepcopy(state['tasks'])
        text = care.render_checklist(state, cid)
        buttons = [care.button_text(t) for t in before.values()]
        state['config']['food_products'] = copy.deepcopy(PRODUCTS)
        care.ensure_checklist(state, date(2026, 10, 20), state['config']['slots'][0], 0)
        self.assertEqual(state['tasks'], before)
        self.assertEqual(care.render_checklist(state, cid), text)
        self.assertEqual([care.button_text(t) for t in state['tasks'].values()], buttons)

    def test_new_foods_get_old_feeding_clarification_and_prior_warning(self):
        cid = self.all_foods()
        for kind in ('wet', 'dry'):
            care.complete_button(self.state, care.button_text(self.state['tasks'][cid + '_' + kind]),
                                 'palmette', kind, at('2026-11-20', '07:01'))
        care.mark_old_feedings(self.state, at('2026-11-20', '14:00'))
        next_cid = checklist(self.state, '2026-11-20', 1)
        self.assertTrue(care.prior_warning(self.state, next_cid))
        reminders = care.render_reminders(self.state, care.unresolved(self.state))
        for kind in ('wet_new', 'dry_new'):
            task = self.state['tasks'][cid + '_' + kind]
            self.assertEqual(task['status'], 'needs_clarification')
            self.assertIn('была ли выдана порция «' + PRODUCTS[kind]['name'], reminders)
        self.assertNotIn('carried_grams', self.state['tasks'][next_cid + '_wet_new'])

    def test_exact_commands_and_snoozed_reminder_support_new_identifiers(self):
        cid = self.all_foods()
        now = at('2026-11-20', '07:01')
        runtime = Runtime(CHAT, TOPIC, frozenset({101}), Path('/unused'))
        bot = Bot(runtime, FakeClient(), BOT_ID, 'example_care_bot', clock=lambda: now)
        for kind in ('wet_new', 'dry_new'):
            for command, suffix, status in (('snooze', ' 30', 'deferred'), ('done', '', 'done'), ('skip', '', 'skipped')):
                with self.subTest(kind=kind, command=command):
                    state = copy.deepcopy(self.state)
                    state['checklists'][cid]['announced_at'] = care.iso(now)
                    key = cid + '_' + kind
                    message = {'from': {'id': 101, 'is_bot': False}, 'chat': {'id': CHAT},
                               'message_thread_id': TOPIC, 'message_id': 10,
                               'text': '/' + command + ' ' + key + suffix}
                    self.assertNotEqual(bot.message(state, message, now), HELP)
                    self.assertEqual(state['tasks'][key]['status'], status)
                    if command == 'snooze':
                        state['last_tick'] = care.iso(now)
                        text = care.tick(state, at('2026-11-20', '07:31'))
                        self.assertIn(PRODUCTS[kind]['name'], text)
                        self.assertEqual(state['emissions'][state['last_emission_id']]['manual_repeat_ids'], [key])
                        self.assertEqual(care.tick(state, at('2026-11-20', '08:01')), 'NO_REPLY')
        for kind in FOOD_TYPES:
            self.assertIn(kind, HELP)

    def test_portion_proposal_and_consumption_keep_products_separate(self):
        before = copy.deepcopy(self.state['config'])
        change = {'kind': 'portions', 'stage': 'wet_step_1',
                  'wet': [1, 2, 3], 'wet_new': [0.1, 0.2, 0],
                  'dry': [4, 5, 6], 'dry_new': [7, 8, 9]}
        text = care.proposal(self.state, change, 'palmette', at('2026-11-19', '12:00'))
        self.assertEqual(self.state['config'], before)
        proposed = next(iter(self.state['proposals'].values()))['config']
        stage = next(s for s in proposed['stages'] if s['id'] == 'wet_step_1')
        self.assertEqual(stage['daily_wet_new'], 0.3)
        for kind in FOOD_TYPES:
            self.assertEqual(stage[kind], change[kind])
            self.assertIn(PRODUCTS[kind]['label'], text)
            self.assertIn(PRODUCTS[kind]['label'], care.daily_line(stage, proposed))
        self.assertIn('Новый влажный: 0.9 г', '\n'.join(care.plan_consumption(proposed)))

    def test_inline_pages_reserve_space_for_all_product_edit_variants(self):
        for product in self.state['config']['food_products'].values():
            product['name'] = 'К' * 256
        cid = self.all_foods()
        now = at('2026-11-20', '07:01')
        panel = panel_for(self.state, cid, now)
        self.assertEqual(sum(len(p['bindings']) for p in panel['pages']), len(panel['task_ids']))
        for kind in FOOD_TYPES:
            task = self.state['tasks'][cid + '_' + kind]
            reserved = inline_ui.task_line_size(task, True, now)
            variants = [dict(task, status='deferred', needs_clarification=True,
                             snoozed_until=care.iso(now))]
            for without_addition in (False, True):
                variants.append(dict(task, status='skipped', skip_by='я' * 64,
                                     skip_at=care.iso(now), skip_reason='next_care',
                                     moved_due_at=care.iso(now), moved_food_grams=task['grams'],
                                     moved_food_without_addition=without_addition))
            for variant in variants:
                self.assertLessEqual(len(care.task_line(variant, primary=True).encode('utf-16-le')) // 2, reserved)
            care.move_to_next(self.state, task['id'], 'я' * 64, now)
        for page in panel['pages']:
            self.assertLessEqual(len(inline_ui.page_text(self.state, panel, page).encode('utf-16-le')) // 2, 4096)


class NutritionTransferTests(unittest.TestCase):
    def setUp(self):
        self.state = state_with_plan()

    def test_midnight_stage_boundary_carries_each_product_without_substitution(self):
        for source_day, target_day in (('2026-11-27', '2026-11-28'), ('2026-12-08', '2026-12-09')):
            with self.subTest(source=source_day):
                state = state_with_plan()
                source = checklist(state, source_day, 2)
                now = at(source_day, '22:00')
                source_tasks = [state['tasks'][key] for key in state['checklists'][source]['task_ids']
                                if state['tasks'][key]['type'] in FOOD_TYPES]
                saved = copy.deepcopy(source_tasks)
                for task in source_tasks:
                    result = care.move_to_next(state, task['id'], 'palmette', now)
                    self.assertEqual(result['target_id'], target_day + '_07:00_' + task['type'])
                    self.assertIn(task['food_label'], care.move_answer(result))
                    self.assertIn(str(task['grams']) + ' г', care.move_answer(result))
                before = copy.deepcopy(state)
                for task in source_tasks:
                    self.assertEqual(care.move_to_next(state, task['id'], 'other', now)['status'], 'already_closed')
                self.assertEqual(state, before)
                state = json.loads(json.dumps(state))
                target = checklist(state, target_day, 0)
                stage = care.stage_for(state, date.fromisoformat(target_day))
                for task in saved:
                    carried = state['tasks'][target + '_' + task['type']]
                    planned = stage.get(task['type'], [0, 0, 0])[0]
                    self.assertEqual(carried['planned_grams'], planned)
                    self.assertEqual(carried['grams'], planned + task['grams'])
                    self.assertEqual(carried['food_name'], task['food_name'])
                    self.assertEqual(carried['carried_portions'], {task['id']: task['grams']})
                    if task['type'] in ('wet', 'wet_new'):
                        self.assertEqual(carried['wet_can_grams'], task['wet_can_grams'])
                before = copy.deepcopy(state)
                care.apply_carryovers(state, target)
                self.assertEqual(state, before)

    def test_carry_only_tasks_retain_source_metadata_and_no_unconfirmed_portions(self):
        source = checklist(self.state, '2026-11-27', 2)
        key = source + '_wet_new'
        task = self.state['tasks'][key]
        now = at('2026-11-27', '22:00')
        panel = panel_for(self.state, source, now)
        cb = callback(panel, key, move=True)
        answer = inline_ui.accept_callback(self.state, cb, {101}, BOT_ID, now, CHAT, TOPIC)
        self.assertFalse(answer['show_alert'])
        self.assertIn('Новый влажный', answer['text'])
        self.state['config']['food_products']['wet_new']['name'] = 'A future product label'
        target = checklist(self.state, '2026-11-28', 0)
        carried = self.state['tasks'][target + '_wet_new']
        self.assertEqual(carried['grams'], 64)
        self.assertEqual(carried['food_name'], PRODUCTS['wet_new']['name'])
        self.assertNotIn(target + '_wet', self.state['tasks'])
        self.assertEqual(self.state['tasks'][source + '_wet']['status'], 'pending')
        self.assertEqual(self.state['tasks'][target + '_dry']['grams'], 19)
        self.assertEqual(task['status'], 'skipped')

    def test_midday_new_products_add_only_to_same_type_and_once(self):
        source = checklist(self.state, '2026-12-03', 0)
        target = checklist(self.state, '2026-12-03', 1)
        for kind in ('dry', 'dry_new'):
            result = care.move_to_next(self.state, source + '_' + kind, 'palmette', at('2026-12-03', '07:01'))
            self.assertEqual(result['target_id'], target + '_' + kind)
            self.assertEqual(self.state['tasks'][target + '_' + kind]['grams'], 19 if kind == 'dry' else 20)
        self.assertNotIn('carryovers', self.state)
        self.assertNotIn(target + '_wet_new', self.state['tasks'])

    def test_planned_only_and_legacy_queues_do_not_create_extra_food(self):
        self.state['config']['food_transfer_policy'] = 'planned_only'
        source = checklist(self.state, '2026-11-27', 2)
        for kind in ('wet', 'wet_new', 'dry'):
            result = care.move_to_next(self.state, source + '_' + kind, 'palmette', at('2026-11-27', '22:00'))
            if kind != 'dry':
                self.assertIsNone(result['target_id'])
                self.assertIn('Добавки не будет', care.move_answer(result))
        self.state['config']['food_transfer_policy'] = 'add_full'
        target = checklist(self.state, '2026-11-28', 0)
        self.assertNotIn(target + '_wet', self.state['tasks'])
        self.assertNotIn(target + '_wet_new', self.state['tasks'])
        self.assertEqual(self.state['tasks'][target + '_dry']['grams'], 19)
        self.assertNotIn('carried_grams', self.state['tasks'][target + '_dry'])
        source = checklist(self.state, '2026-12-03', 0)
        self.state['config']['food_transfer_policy'] = 'planned_only'
        care.move_to_next(self.state, source + '_dry_new', 'palmette', at('2026-12-03', '07:01'))
        self.state['config']['food_transfer_policy'] = 'add_full'
        target = checklist(self.state, '2026-12-03', 1)
        self.assertEqual(self.state['tasks'][target + '_dry_new']['grams'], 10)
        self.assertNotIn('carried_grams', self.state['tasks'][target + '_dry_new'])

    def test_optional_absent_next_stage_never_raises_or_substitutes(self):
        source = checklist(self.state, '2026-12-03', 0)
        stage = self.state['config']['stages'][-3]
        stage.pop('dry_new')
        stage.pop('daily_dry_new')
        self.state['config']['food_transfer_policy'] = 'planned_only'
        result = care.move_to_next(self.state, source + '_dry_new', 'palmette', at('2026-12-03', '07:01'))
        self.assertIsNone(result['target_id'])
        target = checklist(self.state, '2026-12-03', 1)
        self.assertNotIn(target + '_dry_new', self.state['tasks'])
        self.assertEqual(self.state['tasks'][target + '_dry']['grams'], 9)


class NutritionNoticeTests(unittest.TestCase):
    def setUp(self):
        self.state = state_with_plan()

    def evening(self, day='2026-11-19'):
        tick_today(self.state, day, '19:00')
        return care.tick(self.state, at(day, '19:30'))

    def test_advance_notice_exact_text_at_1930_independent_of_repeats(self):
        tick_today(self.state, '2026-11-19', '19:00')
        self.assertEqual(care.tick(self.state, at('2026-11-19', '19:29')), 'NO_REPLY')
        text = care.tick(self.state, at('2026-11-19', '19:30'))
        stage = self.state['config']['stages'][3]
        expected = ('@palmette @LionZXY\n\n😱 Экорн · Завтра меняем порции!\n\n'
                    + '📅 ' + stage['advance_notice'] + '\n\n🍽️ Новая суточная дозировка:\n'
                    '- 49 г старого влажного.\n- 21 г нового влажного.\n- 61 г старого сухого.\n\n'
                    '🐾 С завтрашнего завтрака кормим по новым граммовкам!')
        self.assertEqual(text, expected)
        self.assertNotIn('Новый сухой', text)
        self.assertEqual(self.state['nutrition_notices']['wet_step_1']['prepared_at'],
                         '2026-11-19T19:30:00+00:00')
        item = self.state['emissions'][self.state['last_emission_id']]
        self.assertEqual(item['nutrition_notices'], {'wet_step_1': expected.split('\n\n', 1)[1]})
        self.assertEqual(care.emission_task_ids(self.state, item['id']), [])

    def test_automatic_repeats_do_not_control_or_duplicate_transition_notice(self):
        self.state['config']['automatic_repeats'] = True
        text = self.evening()
        self.assertEqual(text.count('Завтра меняем порции!'), 1)
        text = care.tick(self.state, at('2026-11-19', '20:00'))
        self.assertNotEqual(text, 'NO_REPLY')
        self.assertNotIn('Завтра меняем порции!', text)
        self.assertEqual(list(self.state['nutrition_notices']), ['wet_step_1'])

    def test_notice_is_prepared_once_across_ticks_and_restart(self):
        self.evening()
        prepared = copy.deepcopy(self.state['nutrition_notices'])
        self.assertEqual(care.tick(self.state, at('2026-11-19', '19:31')), 'NO_REPLY')
        self.state = json.loads(json.dumps(self.state))
        self.assertEqual(care.tick(self.state, at('2026-11-19', '20:00')), 'NO_REPLY')
        self.assertEqual(self.state['nutrition_notices'], prepared)
        text = care.tick(self.state, at('2026-11-20', '07:00'))
        self.assertNotIn('Завтра меняем порции', text)
        self.assertIsNone(self.state['progression_hold'])

    def test_every_transition_has_its_own_notice_and_no_legacy_78g_warning(self):
        for start, identity, *_ in STEPS:
            day = (date.fromisoformat(start) - timedelta(days=1)).isoformat()
            with self.subTest(stage=identity):
                text = self.evening(day)
                self.assertIn('Завтра меняем порции!', text)
                self.assertNotIn('78 г', text)
                self.assertNotIn('переход на сухой корм', text)
                self.assertIn(identity, self.state['nutrition_notices'])
                self.assertEqual(care.tick(self.state, at(day, '19:31')), 'NO_REPLY')
        final = self.state['config']['stages'][-1]
        self.assertIn('- 85 г нового влажного.', text)
        self.assertIn('- 60 г нового сухого.', text)
        self.assertNotIn('Старый сухой:', text)
        self.assertFalse(final.get('provisional'))
        self.assertIsNone(self.state['vet_notice_for'])

    def test_daily_totals_use_exact_stored_meals_not_cached_totals(self):
        stage = self.state['config']['stages'][3]
        stage['wet_new'] = [0.1, 0.2, 0.1234567890123456]
        stage['daily_wet_new'] = 999
        stage['daily_dry'] = 999
        text = self.evening()
        self.assertIn('- 0.4234567890123456 г нового влажного.', text)
        self.assertIn('- 61 г старого сухого.', text)
        self.assertNotIn('999', text)

    def test_downtime_does_not_replay_stale_day_before_notices(self):
        text = tick_today(self.state, '2026-11-20', '07:00')
        self.assertNotIn('Завтра меняем порции!', text)
        self.assertNotIn('nutrition_notices', self.state)
        self.state['last_tick'] = care.iso(at('2026-11-19', '06:00'))
        text = care.tick(self.state, at('2026-11-23', '07:00'))
        self.assertNotIn('Завтра меняем порции!', text)
        self.assertNotIn('nutrition_notices', self.state)

    def test_hold_pause_and_quiet_hours_suppress_preparation(self):
        tick_today(self.state, '2026-11-19', '19:00')
        care.health_hold(self.state, 'palmette', 'Health check', at('2026-11-19', '19:01'))
        hold = copy.deepcopy(self.state['progression_hold'])
        self.assertEqual(care.tick(self.state, at('2026-11-19', '19:30')), 'NO_REPLY')
        self.assertNotIn('nutrition_notices', self.state)
        self.assertEqual(self.state['progression_hold'], hold)
        self.state['progression_hold'] = None
        self.state['reminder_pause'] = {'until': 'indefinite'}
        self.assertEqual(care.tick(self.state, at('2026-11-19', '20:00')), 'NO_REPLY')
        self.assertNotIn('nutrition_notices', self.state)
        self.state['reminder_pause'] = None
        self.assertEqual(care.tick(self.state, at('2026-11-19', '23:00')), 'NO_REPLY')
        self.assertNotIn('nutrition_notices', self.state)
        self.assertIn('Завтра меняем порции!', care.tick(self.state, at('2026-11-19', '22:59')))

    def test_pause_until_next_day_does_not_send_stale_notice_after_resume(self):
        tick_today(self.state, '2026-11-19', '19:00')
        self.state['reminder_pause'] = {'until': '2026-11-20T06:00:00+00:00'}
        self.assertEqual(care.tick(self.state, at('2026-11-19', '19:30')), 'NO_REPLY')
        text = care.tick(self.state, at('2026-11-20', '07:00'))
        self.assertNotIn('Завтра меняем порции!', text)
        self.assertNotIn('nutrition_notices', self.state)

    def test_refresh_suppresses_prepared_notice_after_new_hold_or_date_change(self):
        self.evening()
        eid = self.state['last_emission_id']
        before = copy.deepcopy(self.state['nutrition_notices'])
        self.assertIn('Завтра меняем порции!', care.refresh(self.state, eid, at('2026-11-19', '19:31')))
        care.health_hold(self.state, 'palmette', 'Health check', at('2026-11-19', '19:31'))
        self.assertEqual(care.refresh(self.state, eid, at('2026-11-19', '19:32')), 'NO_REPLY')
        self.assertEqual(self.state['nutrition_notices'], before)
        self.state['progression_hold'] = None
        self.assertEqual(care.refresh(self.state, eid, at('2026-11-20', '07:00')), 'NO_REPLY')

    def test_refresh_keeps_primary_and_other_notices_under_hold(self):
        text = tick_today(self.state, '2026-11-19', '19:30')
        self.assertIn('Завтра меняем порции!', text)
        eid = self.state['last_emission_id']
        self.state['emissions'][eid]['notices'].append('A separate existing notice')
        care.health_hold(self.state, 'palmette', 'Health check', at('2026-11-19', '19:31'))
        text = care.refresh(self.state, eid, at('2026-11-19', '19:32'))
        self.assertIn('г Gastrointestinal Kitten — старый влажный.', text)
        self.assertIn('A separate existing notice', text)
        self.assertNotIn('Завтра меняем порции!', text)

    def test_first_breakfast_has_exact_new_line_and_only_current_meal(self):
        for start, _, *portions in STEPS:
            with self.subTest(day=start):
                state = state_with_plan()
                text = tick_today(state, start, '07:00')
                self.assertIn('\n🔄 Сегодня новые порции!\n', text)
                self.assertNotIn('**Сегодня меняются порции**', text)
                self.assertNotIn('14:00:', text)
                self.assertNotIn('19:00:', text)
                self.assertNotIn('Суточная порция', text)
                self.assertEqual(care.tick(state, at(start, '07:01')), 'NO_REPLY')

    def test_hold_suppresses_morning_change_notice_for_new_and_legacy_stages(self):
        for state, day in ((state_with_plan(), '2026-11-20'), (care.initial(), '2026-10-19')):
            now = at(day, '07:00')
            care.health_hold(state, 'palmette', 'Health check', now)
            text = tick_today(state, day, '07:00')
            self.assertIn('смена этапов приостановлена', text)
            self.assertNotIn('Сегодня меняются порции', text)
            self.assertNotIn('С сегодняшнего дня новые порции!', text)
            self.assertIn('☐ ', text)

    def test_legacy_vet_notice_prepared_before_hold_is_also_suppressed(self):
        state = care.initial()
        text = tick_today(state, '2026-11-15', '07:00')
        self.assertIn('78 г в плане указаны ориентировочно', text)
        eid = state['last_emission_id']
        care.health_hold(state, 'palmette', 'Health check', at('2026-11-15', '07:01'))
        text = care.refresh(state, eid, at('2026-11-15', '07:02'))
        self.assertNotIn('78 г в плане указаны ориентировочно', text)
        self.assertIn('☐ Дать', text)

    def test_held_stage_snapshot_requires_matching_active_hold(self):
        legacy = care.initial()['config']['stages'][-1]
        snapshot = copy.deepcopy(legacy)
        self.state['nutrition_plan_held_stage'] = snapshot
        self.state['nutrition_plan_hold_since'] = '2026-11-18T10:00:00+00:00'
        self.state['progression_hold'] = {'stage': snapshot['id'], 'since': '2026-11-18T10:00:00+00:00'}
        self.assertEqual(care.stage_for(self.state, date(2026, 12, 10)), snapshot)
        cid = checklist(self.state, '2026-12-10', 0)
        self.assertEqual(self.state['tasks'][cid + '_dry']['grams'], 26)
        self.assertNotIn(cid + '_dry_new', self.state['tasks'])
        self.state['progression_hold'] = {'stage': 'week_4', 'since': '2026-11-18T10:00:00+00:00'}
        self.assertEqual(care.stage_for(self.state, date(2026, 12, 10))['id'], 'week_4')
        self.state['progression_hold'] = None
        self.assertEqual(care.stage_for(self.state, date(2026, 12, 10))['id'], 'regular_kitten')


class NutritionOutboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.temp) / 'state.json'
        self.store = Store(self.path)
        self.store.bind(BOT_ID, CHAT, TOPIC, at('2026-11-19', '06:00'), fresh_start=True)
        with self.store.transaction() as state:
            state['config'] = state_with_plan()['config']
        self.runtime = Runtime(CHAT, TOPIC, frozenset({101}), self.path)
        self.client = FakeClient()
        self.now = at('2026-11-19', '19:00')
        self.bot = Bot(self.runtime, self.client, BOT_ID, 'example_care_bot', self.store, lambda: self.now)
        self.bot.dispatch_once()
        self.client.calls.clear()
        self.now = at('2026-11-19', '19:30')

    def test_notice_uses_existing_dispatch_and_persisted_outbox_once(self):
        self.bot.dispatch_once()
        sends = [p for method, p in self.client.calls if method == 'sendMessage']
        self.assertEqual(len(sends), 1)
        self.assertIn('Завтра меняем порции!', sends[0]['text'])
        self.assertEqual(sends[0]['reply_markup'], {'inline_keyboard': []})
        saved = self.store.snapshot()
        eid = saved['last_emission_id']
        self.assertEqual(saved['standalone']['outbox']['reminder:' + eid]['status'], 'sent')
        self.store.bind(BOT_ID, CHAT, TOPIC, self.now)
        self.bot.dispatch_once()
        self.assertEqual(len([1 for method, _ in self.client.calls if method == 'sendMessage']), 1)

    def test_new_hold_suppresses_prepared_retry_without_a_send(self):
        self.client.send_error = TelegramError(429, retry_after=10)
        self.bot.dispatch_once()
        state = self.store.snapshot()
        key = 'reminder:' + state['last_emission_id']
        self.assertEqual(state['standalone']['outbox'][key]['status'], 'pending')
        self.client.calls.clear()
        self.client.send_error = None
        with self.store.transaction() as state:
            care.health_hold(state, 'palmette', 'Health check', self.now)
        self.bot.deliver(key, self.now)
        self.assertFalse(any(method == 'sendMessage' for method, _ in self.client.calls))
        state = self.store.snapshot()
        self.assertEqual(state['standalone']['outbox'][key]['status'], 'suppressed')
        self.assertIn('wet_step_1', state['nutrition_notices'])

    def test_pending_notice_retry_obeys_pause_and_stale_date_filters(self):
        self.client.send_error = TelegramError(429, retry_after=10)
        self.bot.dispatch_once()
        saved = self.store.snapshot()
        key = 'reminder:' + saved['last_emission_id']
        self.client.calls.clear()
        self.client.send_error = None
        with self.store.transaction() as state:
            state['reminder_pause'] = {'until': 'indefinite'}
        self.bot.deliver(key, self.now)
        self.assertEqual(self.store.snapshot()['standalone']['outbox'][key]['status'], 'suppressed')
        self.assertFalse(any(method == 'sendMessage' for method, _ in self.client.calls))
        with self.store.transaction() as state:
            state['reminder_pause'] = None
            state['standalone']['outbox'][key]['status'] = 'pending'
        self.bot.deliver(key, at('2026-11-20', '07:00'))
        self.assertEqual(self.store.snapshot()['standalone']['outbox'][key]['status'], 'suppressed')
        self.assertFalse(any(method == 'sendMessage' for method, _ in self.client.calls))


if __name__ == '__main__':
    unittest.main()
