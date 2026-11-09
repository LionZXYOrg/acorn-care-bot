"""Exact owner portions and restart-safe installation use synthetic saved care."""
import argparse
import copy
import json
from datetime import date, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import care
from configuration import validate_care_config
from nutrition_plan import apply_nutrition_plan, validate_nutrition_plan
from storage import Store

ROOT = Path(__file__).resolve().parents[1]
PLAN = json.loads((ROOT / 'nutrition-plan-2026-11.json').read_text())
NOW = care.instant('2026-11-09T10:47:58+00:00')
EXPECTED = [
    ('2026-11-09', [43, 43, 44], [0, 0, 0], [15, 15, 16], [0, 0, 0]),
    ('2026-11-20', [33, 33, 34], [7, 7, 6], [16, 16, 16], [0, 0, 0]),
    ('2026-11-23', [22, 21, 22], [13, 14, 13], [17, 17, 18], [0, 0, 0]),
    ('2026-11-26', [11, 11, 11], [20, 20, 20], [18, 18, 19], [0, 0, 0]),
    ('2026-11-28', [0, 0, 0], [28, 28, 29], [19, 19, 19], [0, 0, 0]),
    ('2026-11-30', [0, 0, 0], [28, 28, 29], [14, 14, 15], [5, 5, 5]),
    ('2026-12-03', [0, 0, 0], [28, 28, 29], [10, 9, 10], [10, 10, 10]),
    ('2026-12-06', [0, 0, 0], [28, 28, 29], [5, 4, 5], [15, 15, 15]),
    ('2026-12-09', [0, 0, 0], [28, 28, 29], [0, 0, 0], [20, 20, 20]),
]


def approved_state():
    state = care.initial()
    apply_nutrition_plan(state, PLAN, NOW)
    return state


class ExactNutritionPlanTests(unittest.TestCase):
    def test_all_products_and_pack_sizes(self):
        self.assertEqual(PLAN['approved_message'], 'telegram_-1004424243504_602')
        products = PLAN['food_products']
        self.assertEqual({key: p['name'] for key, p in products.items()}, {
            'wet': 'Royal Canin Gastrointestinal Kitten Wet',
            'wet_new': 'Royal Canin Kitten in Jelly',
            'dry': 'Royal Canin Gastrointestinal Kitten Dry',
            'dry_new': 'Royal Canin Kitten Dry'})
        self.assertEqual(products['wet']['pack_grams'], 195)
        self.assertEqual(products['wet_new']['pack_grams'], 85)

    def test_every_meal_on_every_date_matches_the_owner(self):
        state = approved_state()
        validate_care_config(state['config'])
        day = date(2026, 11, 9)
        while day <= date(2026, 12, 15):
            expected = [entry for entry in EXPECTED if entry[0] <= day.isoformat()][-1]
            stage = care.stage_for(state, day)
            self.assertEqual(stage['start'], expected[0])
            for kind, weights in zip(('wet', 'wet_new', 'dry', 'dry_new'), expected[1:]):
                self.assertEqual(stage[kind], weights, (day, kind))
                self.assertEqual(stage['daily_' + kind], sum(weights))
            for index, slot in enumerate(care.slots_for_day(state, day)):
                cid = care.ensure_checklist(state, day, slot, index)
                tasks = {state['tasks'][key]['type']: state['tasks'][key]
                         for key in state['checklists'][cid]['task_ids']}
                for kind, weights in zip(('wet', 'wet_new', 'dry', 'dry_new'), expected[1:]):
                    if weights[index]:
                        self.assertEqual(tasks[kind]['grams'], weights[index], (day, slot['time'], kind))
                        self.assertEqual(tasks[kind]['food_name'], PLAN['food_products'][kind]['name'])
                    else:
                        self.assertNotIn(kind, tasks, (day, slot['time'], kind))
                expected_cleaning = ({'litter', 'bowls', 'water'}, set(), {'litter', 'bowls', 'water'})[index]
                self.assertEqual(set(tasks) & set(care.CLEANING), expected_cleaning)
            day += timedelta(days=1)

    def test_old_history_is_unchanged_and_old_final_stage_is_removed(self):
        before = care.initial()
        state = copy.deepcopy(before)
        apply_nutrition_plan(state, PLAN, NOW)
        self.assertEqual(state['config']['stages'][:2], before['config']['stages'][:2])
        self.assertNotIn('week_5_plus', [stage['id'] for stage in state['config']['stages']])
        self.assertEqual(care.stage_for(state, date(2026, 11, 19))['dry'], [15, 15, 16])
        self.assertEqual(care.stage_for(state, date(2026, 12, 9))['daily_dry_new'], 60)
        self.assertEqual(care.stage_for(state, date(2026, 12, 9))['daily_wet_new'], 85)

    def test_all_eight_advance_dates_and_morning_flags(self):
        notices = [stage for stage in PLAN['stages'] if stage.get('advance_notice')]
        self.assertEqual([(date.fromisoformat(stage['start']) - timedelta(days=1)).isoformat()
                          for stage in notices],
                         ['2026-11-19', '2026-11-22', '2026-11-25', '2026-11-27',
                          '2026-11-29', '2026-12-02', '2026-12-05', '2026-12-08'])
        self.assertEqual(PLAN['notice_time'], '19:30')
        self.assertTrue(all(stage['morning_notice'] is True for stage in notices))
        self.assertFalse(PLAN['stages'][0].get('morning_notice', False))
        state = approved_state()
        state['config']['automatic_repeats'] = False
        state['config']['mention'] = '@palmette @LionZXY'
        for stage in notices:
            yesterday = date.fromisoformat(stage['start']) - timedelta(days=1)
            state['last_tick'] = care.iso(care.wall(yesterday, '19:00'))
            care.tick(state, care.wall(yesterday, '19:00'))
            self.assertEqual(care.tick(state, care.wall(yesterday, '19:29')), 'NO_REPLY')
            text = care.tick(state, care.wall(yesterday, '19:30'))
            self.assertIn(stage['advance_notice'], text)
            self.assertIn('@palmette @LionZXY', text)
            for kind in ('wet', 'wet_new', 'dry', 'dry_new'):
                total = stage['daily_' + kind]
                label = PLAN['food_products'][kind]['label']
                if total:
                    self.assertIn(f'- {total} г {care.notice_food_label(PLAN, kind)}.', text)
                else:
                    self.assertNotIn(label, text)
            self.assertEqual(care.tick(state, care.wall(yesterday, '19:31')), 'NO_REPLY')

    def test_latest_message_examples_keep_exact_portions_and_care_meanings(self):
        state = approved_state()
        state['config']['mention'] = '@palmette @LionZXY'
        state['config']['automatic_repeats'] = False
        day = date(2026, 11, 23)
        slots = care.slots_for_day(state, day)
        cid = care.ensure_checklist(state, day, slots[0], 0)
        text = care.render_checklist(state, cid)
        for line in ('☐ 22 г Gastrointestinal Kitten — старый влажный.',
                     '☐ 13 г Kitten in Jelly — новый влажный.',
                     '☐ 17 г Gastrointestinal Kitten — сухой.',
                     '🔄 Этап 2 · Переход на новый корм',
                     '☐ Почистить лоток.', '☐ Помыть миски.', '☐ Налить свежую воду.'):
            self.assertIn(line, text)
        self.assertNotIn('Loaf', text)
        self.assertNotIn('14:00', text)
        day = date(2026, 12, 10)
        cid = care.ensure_checklist(state, day, care.slots_for_day(state, day)[2], 2)
        text = care.render_checklist(state, cid)
        for line in ('☐ 29 г Royal Canin Kitten in Jelly.',
                     '☐ 20 г Royal Canin Kitten Dry.', '☐ Почистить лоток.',
                     '☐ Помыть использованные миски.', '☐ Проверить питьевую воду.'):
            self.assertIn(line, text)
        self.assertNotIn('Налить', text)
        self.assertNotIn('Этап', text)
        self.assertNotIn('Сегодня новые порции', text)
        import inline_ui
        from service import chunks
        panel = inline_ui.prepare(state, chunks(text), state['checklists'][cid]['task_ids'],
                                  care.wall(day, '19:00'), primary=cid)
        self.assertEqual(len(panel['task_ids']), 5)
        self.assertEqual(sum(len(page['bindings']) for page in panel['pages']), 5)
        self.assertEqual(len(inline_ui.keyboard(state, panel)['inline_keyboard']), 5)

    def test_prior_product_transfers_never_mix_loaf_into_jelly(self):
        state = approved_state()
        state['config']['food_transfer_policy'] = 'add_full'
        day = date(2026, 11, 23)
        cid = care.ensure_checklist(state, day, care.slots_for_day(state, day)[0], 0)
        task = state['tasks'][cid + '_wet_new']
        task['food_name'] = 'Royal Canin Kitten in Loaf'
        before = copy.deepcopy(state)
        with self.assertRaisesRegex(ValueError, 'другой продукт'):
            care.move_to_next(state, task['id'], 'palmette', care.wall(day, '07:01'))
        self.assertEqual(state, before)
        target = care.ensure_checklist(state, day, care.slots_for_day(state, day)[1], 1)
        key = target + '_wet_new'
        state['carryovers'] = {key: {
            'checklist': target, 'type': 'wet_new', 'due_at': care.iso(care.wall(day, '14:00')),
            'source_ids': ['old-product-source'], 'food_grams': {'old-product-source': 13},
            'food_metadata': {'food_name': 'Royal Canin Kitten in Loaf'}}}
        grams = state['tasks'][key]['grams']
        care.apply_carryovers(state, target)
        self.assertEqual(state['tasks'][key]['grams'], grams)
        self.assertEqual(state['tasks'][key]['food_name'], 'Royal Canin Kitten in Jelly')
        self.assertEqual(state['carryovers'][key]['needs_review'], 'food_product_changed')
        count = len(state['audit'])
        care.apply_carryovers(state, target)
        self.assertEqual(len(state['audit']), count)

    def test_invalid_or_unapproved_replacements_do_not_change_state(self):
        for field, value in [('approved_by', ''), ('approved_message', ''),
                             ('approved_at', '2026-11-09T10:00:00'),
                             ('effective', '2026-11-08'), ('id', 'not an id')]:
            with self.subTest(field=field):
                plan = {**copy.deepcopy(PLAN), field: value}
                state = care.initial()
                before = copy.deepcopy(state)
                with self.assertRaises(ValueError):
                    apply_nutrition_plan(state, plan, NOW)
                self.assertEqual(state, before)
        with self.assertRaises(ValueError):
            validate_nutrition_plan({**PLAN, 'unapproved': True}, care.BOOT_CONFIG)


class NutritionStartupTests(unittest.TestCase):
    def setUp(self):
        root = ROOT / '.test-tmp'
        root.mkdir(exist_ok=True)
        directory = self.enterContext(tempfile.TemporaryDirectory(dir=root))
        self.path = Path(directory) / 'state.json'
        self.store = Store(self.path)
        self.store.bind(12345, -1000000000001, 42, NOW, fresh_start=True)
        self.boot = {**copy.deepcopy(care.BOOT_CONFIG), 'bowls_morning_only': False,
                     'nutrition_plan': copy.deepcopy(PLAN)}

    def bind(self):
        with patch.object(care, 'BOOT_CONFIG', self.boot):
            self.store.bind(12345, -1000000000001, 42, NOW, fresh_start=True)
        return self.store.snapshot()

    def test_saved_tasks_carryovers_receipts_and_transport_are_preserved(self):
        with self.store.transaction() as state:
            cid = care.ensure_checklist(state, date(2026, 11, 9), state['config']['slots'][0], 0)
            args = argparse.Namespace(checklist=cid, types=['dry'], action='done', actor='telegram:101',
                                      minutes=None, message_id='synthetic-owner-report', original_message_id='777')
            care.update(state, args, NOW)
            state['carryovers'] = {'2026-11-09_14:00_wet': {
                'checklist': '2026-11-09_14:00', 'type': 'wet',
                'due_at': '2026-11-09T14:00:00+00:00', 'source_ids': ['synthetic_old_wet'],
                'food_grams': {'synthetic_old_wet': 65}}}
            state['inline_receipts'] = {'synthetic-callback': {'answer': {'text': 'Saved'}}}
            state['reminder_pause'] = {'until': 'indefinite', 'actor': 'telegram:101'}
            state['proposals'] = {'old': {'base_revision': 0, 'status': 'pending'}}
            state['standalone']['offset'] = 456
            state['standalone']['outbox']['synthetic'] = {
                'status': 'pending', 'texts': ['Saved reply'], 'panel': None,
                'message_ids': [], 'retry_at': 0}
        before = self.store.snapshot()
        after = self.bind()
        for key in ('tasks', 'checklists', 'carryovers', 'inline_receipts', 'standalone',
                    'reminder_pause', 'proposals', 'last_tick'):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(after['revision'], 1)
        self.assertEqual([slot['time'] for slot in after['config']['slots']],
                         [slot['time'] for slot in before['config']['slots']])
        self.assertEqual(after['schedule_history'][0], before['schedule_history'][0])
        self.assertEqual(after['config']['quiet_end'], before['config']['quiet_end'])
        self.assertEqual(after['applied_nutrition_plans'][PLAN['id']]['approved_message'], PLAN['approved_message'])

    def test_restart_does_not_reapply_or_discard_later_owner_changes(self):
        first = self.bind()
        with self.store.transaction() as state:
            state['config']['stages'][-1]['dry_new'] = [21, 21, 21]
            state['config']['stages'][-1]['daily_dry_new'] = 63
            state['revision'] = 2
        second = self.bind()
        self.assertEqual(second['config']['stages'][-1]['dry_new'], [21, 21, 21])
        self.assertEqual(second['revision'], 2)
        self.assertEqual(second['nutrition_plan_history'], first['nutrition_plan_history'])
        self.assertEqual(len([a for a in second['audit'] if a['kind'] == 'nutrition_plan_applied']), 1)

    def test_hold_freezes_the_old_stage_without_cancelling_care(self):
        with self.store.transaction() as state:
            care.health_hold(state, 'telegram:101', 'Synthetic health hold', NOW)
            old_stage = copy.deepcopy(care.stage_for(state, NOW.date()))
        before = self.store.snapshot()
        after = self.bind()
        self.assertEqual(after['progression_hold'], before['progression_hold'])
        self.assertEqual(after['nutrition_plan_held_stage'], old_stage)
        self.assertEqual(care.stage_for(after, date(2026, 12, 9)), old_stage)
        after['last_tick'] = '2026-11-20T06:59:00+00:00'
        text = care.tick(after, care.instant('2026-11-20T07:00:00+00:00'))
        self.assertIn('Кормление', text)
        self.assertIn('смена этапов приостановлена', text)
        self.assertNotIn('Сегодня новые порции', text)

    def test_earlier_snapshot_does_not_override_a_later_hold_of_the_same_stage(self):
        with self.store.transaction() as state:
            care.health_hold(state, 'telegram:101', 'First synthetic hold', NOW)
        after = self.bind()
        after['progression_hold'] = None
        active = next(stage for stage in after['config']['stages'] if stage['id'] == 'week_4')
        active['dry'] = [22, 20, 21]
        active['daily_dry'] = 63
        later = care.instant('2026-11-12T09:00:00+00:00')
        care.health_hold(after, 'telegram:101', 'Later synthetic hold', later)
        self.assertEqual(care.stage_for(after, date(2026, 12, 9))['dry'], [22, 20, 21])

    def test_revised_plan_supersedes_earlier_approval_without_rewriting_tasks(self):
        earlier = copy.deepcopy(PLAN)
        earlier.pop('care_slots', None)
        earlier.update(id='nutrition-2026-11-09',
                       approved_message='telegram_-1004424243504_592',
                       approved_at='2026-11-09T10:12:57+00:00')
        for stage in earlier['stages']:
            for kind in ('wet', 'wet_new'):
                stage[kind] = [0, 0, stage['daily_' + kind]]
        with self.store.transaction() as state:
            apply_nutrition_plan(state, earlier, NOW)
            cid = care.ensure_checklist(state, NOW.date(), state['config']['slots'][0], 0)
            self.assertNotIn(cid + '_wet', state['tasks'])
            state['tasks'][cid + '_dry'].update(
                status='done', done_by='telegram:101', done_at=care.iso(NOW))
        before = self.store.snapshot()
        after = self.bind()
        for key in ('tasks', 'checklists', 'carryovers', 'standalone'):
            self.assertEqual(after.get(key), before.get(key), key)
        self.assertEqual(after['config']['nutrition_plan_id'], PLAN['id'])
        self.assertEqual(after['config']['stages'][2]['wet'], [43, 43, 44])
        self.assertEqual(after['revision'], 2)
        self.assertEqual(set(after['applied_nutrition_plans']), {earlier['id'], PLAN['id']})
        newday = date(2026, 11, 10)
        cid = care.ensure_checklist(after, newday, after['config']['slots'][0], 0)
        self.assertEqual(after['tasks'][cid + '_wet']['grams'], 43)
        self.assertEqual(after['tasks'][cid + '_dry']['grams'], 15)

    def test_reused_identity_with_different_plan_is_rejected_atomically(self):
        self.bind()
        before = self.store.snapshot()
        self.boot['nutrition_plan']['stages'][-1]['dry_new'] = [21, 21, 21]
        self.boot['nutrition_plan']['stages'][-1]['daily_dry_new'] = 63
        with self.assertRaisesRegex(ValueError, 'identity cannot be reused'):
            self.bind()
        self.assertEqual(self.store.snapshot(), before)

    def test_a_fresh_state_also_installs_the_approved_plan(self):
        path = self.path.parent / 'new-state.json'
        with patch.object(care, 'BOOT_CONFIG', self.boot):
            Store(path).bind(12345, -1000000000001, 42, NOW, fresh_start=True)
        state = json.loads(path.read_text())
        self.assertEqual(state['config']['nutrition_plan_id'], PLAN['id'])
        self.assertNotIn('nutrition_plan', state['config'])
        self.assertEqual(state['tasks'], {})
        self.assertEqual(state['carryovers'] if 'carryovers' in state else {}, {})


if __name__ == '__main__':
    unittest.main()
