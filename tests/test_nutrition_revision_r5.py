"""The November 9 evening approval keeps exact meals and advance notices."""
import copy
import json
from datetime import date, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import care
from configuration import validate_care_config
from nutrition_plan import apply_nutrition_plan
from storage import Store

ROOT = Path(__file__).resolve().parents[1]
PLAN = json.loads((ROOT / 'nutrition-plan-2026-11.json').read_text())
NOW = care.instant('2026-11-09T21:57:31+00:00')
KINDS = ('wet', 'wet_new', 'dry', 'dry_new')
EXPECTED = [
    ('2026-11-10', [32, 32, 65], [0, 0, 0], [15, 15, 16], [0, 0, 0]),
    ('2026-11-30', [23, 23, 45], [11, 11, 21], [15, 15, 16], [0, 0, 0]),
    ('2026-12-02', [16, 16, 33], [21, 21, 43], [14, 14, 15], [0, 0, 0]),
    ('2026-12-04', [10, 10, 20], [32, 32, 64], [13, 13, 13], [0, 0, 0]),
    ('2026-12-05', [41, 0, 0], [16, 48, 64], [13, 13, 13], [0, 0, 0]),
    ('2026-12-06', [0, 0, 0], [43, 42, 85], [10, 10, 11], [3, 3, 3]),
    ('2026-12-08', [0, 0, 0], [43, 42, 85], [7, 7, 7], [6, 7, 7]),
    ('2026-12-10', [0, 0, 0], [43, 42, 85], [3, 3, 4], [11, 11, 10]),
    ('2026-12-12', [0, 0, 0], [43, 42, 85], [0, 0, 0], [14, 14, 14]),
]
NOTICE_DAYS = ['2026-11-09', '2026-11-29', '2026-12-01', '2026-12-03',
               '2026-12-04', '2026-12-05', '2026-12-07', '2026-12-09', '2026-12-11']


def installed(now=NOW):
    state = care.initial()
    state['config'].update(automatic_repeats=False, mention='@palmette @LionZXY')
    apply_nutrition_plan(state, PLAN, now)
    return state


class LatestNutritionApprovalTests(unittest.TestCase):
    def test_approval_products_and_packages(self):
        self.assertEqual(PLAN['id'], 'nutrition-2026-11-09-r5')
        self.assertEqual(PLAN['approved_message'], 'telegram_-1004424243504_634')
        self.assertEqual(PLAN['approved_at'], NOW.isoformat())
        self.assertEqual(PLAN['approved_by'], 'palmette')
        self.assertEqual(PLAN['effective'], '2026-11-10')
        self.assertEqual({k: p['name'] for k, p in PLAN['food_products'].items()}, {
            'wet': 'Royal Canin Veterinary Diet Gastrointestinal Kitten Ultra Soft Mousse',
            'wet_new': 'Royal Canin Kitten Wet Food in Jelly',
            'dry': 'Royal Canin Veterinary Diet Gastrointestinal Kitten Dry Food',
            'dry_new': 'Royal Canin Kitten Dry Food'})
        self.assertEqual(PLAN['food_products']['wet']['pack_grams'], 195)
        self.assertEqual(PLAN['food_products']['wet_new']['pack_grams'], 85)
        self.assertEqual([PLAN['food_products'][k]['label'] for k in KINDS],
                         ['Мусс GI', 'Желе Kitten', 'Сухой GI', 'Сухой Kitten'])

    def test_each_meal_each_day_and_no_zero_food_tasks(self):
        state = installed()
        validate_care_config(state['config'])
        day = date(2026, 11, 10)
        while day <= date(2026, 12, 18):
            expected = [row for row in EXPECTED if row[0] <= day.isoformat()][-1]
            stage = care.stage_for(state, day)
            self.assertEqual(stage['start'], expected[0], day)
            for kind, weights in zip(KINDS, expected[1:]):
                self.assertEqual(stage[kind], weights, (day, kind))
                self.assertEqual(stage['daily_' + kind], sum(weights))
            for index, slot in enumerate(care.slots_for_day(state, day)):
                cid = care.ensure_checklist(state, day, slot, index)
                tasks = {state['tasks'][key]['type']: state['tasks'][key]
                         for key in state['checklists'][cid]['task_ids']}
                for kind, weights in zip(KINDS, expected[1:]):
                    if weights[index]:
                        self.assertEqual(tasks[kind]['grams'], weights[index], (day, index, kind))
                        self.assertEqual(tasks[kind]['food_name'], PLAN['food_products'][kind]['name'])
                    else:
                        self.assertNotIn(kind, tasks, (day, index, kind))
            day += timedelta(days=1)

    def test_future_install_preserves_today_and_removes_old_future_stages(self):
        before = care.initial()
        state = copy.deepcopy(before)
        self.assertTrue(apply_nutrition_plan(state, PLAN, NOW))
        self.assertEqual(care.stage_for(state, NOW.date()), care.stage_for(before, NOW.date()))
        self.assertEqual(state['config']['stages'][:3], before['config']['stages'][:3])
        self.assertNotIn('week_5_plus', [s['id'] for s in state['config']['stages']])
        self.assertEqual(care.stage_for(state, date(2026, 11, 10))['wet'], [32, 32, 65])
        self.assertEqual(state['tasks'], {})
        self.assertEqual(state['checklists'], {})

    def test_all_nine_notices_at_2200_once_with_mentions(self):
        self.assertEqual(PLAN['notice_time'], '22:00')
        self.assertEqual([(date.fromisoformat(s['start']) - timedelta(days=1)).isoformat()
                          for s in PLAN['stages']], NOTICE_DAYS)
        for stage, day in zip(PLAN['stages'], NOTICE_DAYS):
            with self.subTest(day=day):
                state = installed()
                yesterday = date.fromisoformat(day)
                state['last_tick'] = care.iso(care.wall(yesterday, '19:00'))
                care.tick(state, care.wall(yesterday, '19:00'))
                self.assertEqual(care.tick(state, care.wall(yesterday, '21:59')), 'NO_REPLY')
                text = care.tick(state, care.wall(yesterday, '22:00'))
                self.assertIn('😱', text)
                self.assertIn(stage['advance_notice'], text)
                self.assertIn('@palmette @LionZXY', text)
                self.assertEqual(care.tick(state, care.wall(yesterday, '22:01')), 'NO_REPLY')
                self.assertEqual(care.tick(state, care.wall(yesterday, '22:30')), 'NO_REPLY')
                self.assertEqual(len(state.get('nutrition_notices', {})), 1)

    def test_late_install_does_not_replay_yesterdays_notice(self):
        now = care.instant('2026-11-10T21:59:00+00:00')
        state = installed(now)
        state['last_tick'] = now.isoformat()
        text = care.tick(state, care.instant('2026-11-10T22:00:00+00:00'))
        self.assertNotIn('Завтра меняем порции', text)
        self.assertNotIn('nutrition_notices', state)

    def test_health_hold_keeps_care_active_but_suppresses_stage_notices(self):
        state = care.initial()
        care.health_hold(state, 'synthetic-owner', 'Synthetic hold', NOW)
        held = copy.deepcopy(care.stage_for(state, NOW.date()))
        apply_nutrition_plan(state, PLAN, NOW)
        state['config']['automatic_repeats'] = False
        state['last_tick'] = NOW.isoformat()
        text = care.tick(state, care.instant('2026-11-09T22:00:00+00:00'))
        self.assertNotIn('Завтра меняем порции', text)
        text = care.tick(state, care.instant('2026-11-10T07:00:00+00:00'))
        self.assertIn('Кормление', text)
        self.assertIn('смена этапов приостановлена', text)
        self.assertEqual(care.stage_for(state, date(2026, 12, 12)), held)
        self.assertNotIn('Сегодня новые порции', text)

    def test_december_fifth_breakfast_exception_and_final_allowance(self):
        state = installed()
        stage = care.stage_for(state, date(2026, 12, 5))
        self.assertEqual(stage['wet'], [41, 0, 0])
        self.assertEqual(stage['wet_new'], [16, 48, 64])
        final = care.stage_for(state, date(2026, 12, 12))
        self.assertEqual(final['daily_wet_new'], 170)
        self.assertEqual(final['daily_dry_new'], 42)
        for stage in PLAN['stages']:
            day = date.fromisoformat(stage['start'])
            for index, slot in enumerate(care.slots_for_day(state, day)):
                cid = care.ensure_checklist(state, day, slot, index)
                text = care.render_checklist(state, cid)
                self.assertEqual('Сегодня новые порции!' in text, index == 0)

    def test_startup_and_restart_preserve_old_confirmations_and_transfers(self):
        root = ROOT / '.test-tmp'
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            store = Store(Path(directory) / 'state.json')
            store.bind(12345, -1000000000001, 42, NOW, fresh_start=True)
            with store.transaction() as state:
                cid = care.ensure_checklist(state, NOW.date(), state['config']['slots'][0], 0)
                state['tasks'][cid + '_dry'].update(status='done', done_by='synthetic-owner', done_at=NOW.isoformat())
                state['carryovers'] = {'saved-transfer': {'source_ids': ['saved-source']}}
                state['inline_receipts'] = {'saved-receipt': {'saved': True}}
                state['standalone']['offset'] = 456
            before = store.snapshot()
            boot = {**copy.deepcopy(care.BOOT_CONFIG), 'nutrition_plan': copy.deepcopy(PLAN)}
            with patch.object(care, 'BOOT_CONFIG', boot):
                store.bind(12345, -1000000000001, 42, NOW, fresh_start=True)
                first = store.snapshot()
                store.bind(12345, -1000000000001, 42, NOW, fresh_start=True)
            second = store.snapshot()
            for key in ('tasks', 'checklists', 'carryovers', 'inline_receipts', 'standalone'):
                self.assertEqual(first[key], before[key], key)
                self.assertEqual(second[key], before[key], key)
            self.assertEqual(first['config']['nutrition_plan_id'], PLAN['id'])
            self.assertEqual(second['revision'], first['revision'])
            self.assertEqual(second['nutrition_plan_history'], first['nutrition_plan_history'])


if __name__ == '__main__':
    unittest.main()
