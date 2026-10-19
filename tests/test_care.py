import argparse
import copy
import unittest
from datetime import date, timedelta
import care


def at(value):
    return care.instant(value)


class CareTests(unittest.TestCase):
    def setUp(self):
        self.s = care.initial()

    def tick(self, value):
        return care.tick(self.s, at(value))

    def test_start_guard(self):
        self.assertEqual(self.tick('2026-10-18T22:00:00+01:00'), 'NO_REPLY')
        self.assertFalse(self.s['tasks'])

    def test_first_breakfast(self):
        text = self.tick('2026-10-19T07:00:00+01:00')
        self.assertTrue(text.startswith('🐾 Экорн · завтрак и утренняя уборка\n'))
        self.assertIn('Caregivers, доброе утро!', text)
        self.assertIn('Сегодня меняются порции', text)
        self.assertIn('Дать ≈1/4 банки влажного', text)
        self.assertIn('Дать 10 г сухого', text)
        self.assertEqual(len(self.s['tasks']), 5)
        self.assertEqual(self.tick('2026-10-19T07:00:10+01:00'), 'NO_REPLY')
        self.assertEqual(self.tick('2026-10-19T07:29:59+01:00'), 'NO_REPLY')
        repeat = self.tick('2026-10-19T07:30:00+01:00')
        self.assertIn('Влажный корм, ≈1/4 банки', repeat)
        self.assertNotIn('☐ Дать', repeat)

    def test_shared_completion_idempotent(self):
        self.tick('2026-10-19T07:00:00+01:00')
        args = argparse.Namespace(checklist='2026-10-19_07:00', types=['wet', 'litter'],
                                  action='done', actor='first', minutes=None,
                                  message_id='test1', original_message_id='321')
        care.update(self.s, args, at('2026-10-19T07:04:00+01:00'))
        args.actor = 'second'
        care.update(self.s, args, at('2026-10-19T07:05:00+01:00'))
        task = self.s['tasks']['2026-10-19_07:00_wet']
        self.assertEqual(task['done_by'], 'first')
        self.assertIsNone(task['next_repeat_at'])
        repeat = self.tick('2026-10-19T07:30:00+01:00')
        self.assertNotIn('Влажный корм', repeat)
        self.assertNotIn('Почистить лоток', repeat)
        self.assertIn('Сухой корм, 10 г', repeat)
        self.assertTrue(repeat.startswith('Caregivers\n'))
        refreshed = care.refresh(self.s, self.s['last_emission_id'], at('2026-10-19T07:30:01+01:00'))
        self.assertTrue(refreshed.startswith('Caregivers\n'))

    def test_next_meal_does_not_reissue_old_food(self):
        self.tick('2026-10-19T07:00:00+01:00')
        text = self.tick('2026-10-19T14:00:00+01:00')
        self.assertIn('Предыдущее кормление не подтверждено', text)
        task = self.s['tasks']['2026-10-19_07:00_wet']
        self.assertTrue(task['needs_clarification'])
        self.assertIn('была ли выдана порция', text)
        self.assertEqual(self.s['tasks']['2026-10-19_14:00_wet']['grams'], 49)

    def test_quiet_hours_and_morning_summary(self):
        self.tick('2026-10-19T22:00:00+01:00')
        self.tick('2026-10-19T22:30:00+01:00')
        self.assertEqual(self.tick('2026-10-19T23:00:00+01:00'), 'NO_REPLY')
        self.assertEqual(self.tick('2026-10-20T06:59:59+01:00'), 'NO_REPLY')
        text = self.tick('2026-10-20T07:00:00+01:00')
        self.assertIn('после тихих часов', text)
        self.assertIn('была ли выдана порция', text)
        self.assertEqual(self.tick('2026-10-20T07:00:05+01:00'), 'NO_REPLY')

    def test_snooze(self):
        self.tick('2026-10-19T07:00:00+01:00')
        args = argparse.Namespace(checklist='2026-10-19_07:00', types=['wet'],
                                  action='snooze', actor='owner', minutes=60,
                                  message_id='snooze', original_message_id=None)
        care.update(self.s, args, at('2026-10-19T07:11:00+01:00'))
        first = self.tick('2026-10-19T07:30:00+01:00')
        self.assertNotIn('Влажный корм', first)
        self.tick('2026-10-19T08:00:00+01:00')
        second = self.tick('2026-10-19T08:11:00+01:00')
        self.assertIn('Влажный корм, ≈1/4 банки', second)

    def test_stage_portions(self):
        self.assertEqual(care.stage_for(self.s, date(2026, 11, 1))['wet'], [49, 49, 97])
        self.assertEqual(care.stage_for(self.s, date(2026, 11, 2))['wet'], [33, 32, 65])
        self.assertEqual(care.stage_for(self.s, date(2026, 11, 9))['dry'], [21, 20, 21])
        self.assertEqual(care.stage_for(self.s, date(2026, 11, 16))['dry'], [26, 26, 26])
        for stage in self.s['config']['stages']:
            self.assertEqual(sum(stage['wet']), stage['daily_wet'])
            self.assertEqual(sum(stage['dry']), stage['daily_dry'])

    def test_dst(self):
        self.assertEqual(care.wall(date(2026, 10, 24), '07:00').utcoffset().total_seconds(), 3600)
        self.assertEqual(care.wall(date(2026, 10, 25), '07:00').utcoffset().total_seconds(), 0)
        self.assertEqual(care.wall(date(2027, 3, 28), '07:00').utcoffset().total_seconds(), 3600)

    def test_health_hold_keeps_care(self):
        self.s['progression_hold'] = {'stage': 'weeks_1_2', 'since': '2026-10-30T12:00:00+00:00'}
        self.assertEqual(care.stage_for(self.s, date(2026, 11, 2))['wet'], [49, 49, 97])
        self.s['last_tick'] = '2026-11-02T06:00:00+00:00'
        text = self.tick('2026-11-02T07:00:00+00:00')
        self.assertIn('смена этапов приостановлена', text)
        self.assertIn('Дать ≈1/4 банки влажного', text)
        self.assertIn('Почистить лоток', text)

    def test_proposal_does_not_apply(self):
        previous = copy.deepcopy(self.s['config'])
        text = care.proposal(self.s, {'kind': 'extend_stage', 'days': 3}, 'owner',
                             at('2026-10-25T12:00:00+00:00'))
        self.assertEqual(previous, self.s['config'])
        self.assertIn('2026-11-05', text)
        self.assertIn('24 банки', text)
        self.assertIn('не фактический остаток', text)

    def test_no_wet_task_when_not_planned(self):
        self.s['last_tick'] = '2026-11-09T06:00:00+00:00'
        self.tick('2026-11-09T07:00:00+00:00')
        checklist = self.s['checklists']['2026-11-09_07:00']
        kinds = [self.s['tasks'][key]['type'] for key in checklist['task_ids']]
        self.assertNotIn('wet', kinds)
        self.assertEqual(len(kinds), 4)
        self.s['last_tick'] = '2026-11-16T21:59:00+00:00'
        self.tick('2026-11-16T22:00:00+00:00')
        self.assertNotIn('2026-11-16_22:00', self.s['checklists'])
        self.assertFalse(any(key.startswith('2026-11-16_22:00') for key in self.s['tasks']))

    def test_skipped_not_all_done(self):
        self.tick('2026-10-19T07:00:00+01:00')
        args = argparse.Namespace(checklist='2026-10-19_07:00', types=['wet', 'dry', 'litter', 'bowls'],
                                  action='done', actor='owner', minutes=None,
                                  message_id='test', original_message_id=None)
        care.update(self.s, args, at('2026-10-19T07:04:00+01:00'))
        args.types, args.action = ['water'], 'skip'
        text = care.update(self.s, args, at('2026-10-19T07:05:00+01:00'))
        self.assertIn('Пропущено', text)
        self.assertNotIn('Всё выполнено', text)
        self.assertEqual(self.tick('2026-10-19T07:30:00+01:00'), 'NO_REPLY')

    def test_schedule_history_no_duplicate_yesterday(self):
        self.tick('2026-10-19T22:30:00+01:00')
        slots = copy.deepcopy(self.s['config']['slots'])
        slots[2]['time'] = '19:30'
        self.s['schedule_history'].append({'effective': '2026-10-20', 'slots': slots})
        self.s['pending_config'] = {'effective': '2026-10-20', 'slots': slots}
        self.tick('2026-10-20T07:00:00+01:00')
        self.assertNotIn('2026-10-19_19:30', self.s['checklists'])
        self.assertEqual(self.s['config']['slots'][2]['time'], '19:30')

    def test_runtime_jitter_does_not_skip_repeat(self):
        self.tick('2026-10-19T07:00:45+01:00')
        text = self.tick('2026-10-19T07:30:05+01:00')
        self.assertNotEqual(text, 'NO_REPLY')
        self.assertEqual(self.s['tasks']['2026-10-19_07:00_wet']['next_repeat_at'],
                         '2026-10-19T08:00:00+01:00')

    def test_refresh_suppresses_recently_closed_task(self):
        self.tick('2026-10-19T07:00:00+01:00')
        eid = self.s['last_emission_id']
        args = argparse.Namespace(checklist='2026-10-19_07:00',
                                  types=['wet', 'dry', 'litter', 'bowls', 'water'],
                                  action='done', actor='owner', minutes=None,
                                  message_id='test', original_message_id=None)
        care.update(self.s, args, at('2026-10-19T07:00:02+01:00'))
        self.assertEqual(care.refresh(self.s, eid, at('2026-10-19T07:00:03+01:00')), 'NO_REPLY')

    def test_three_meals_have_exact_portions_and_cleaning_scope(self):
        expected = [
            ('2026-10-19', [49, 49, 97], [10, 10, 10], 195, 30),
            ('2026-11-02', [33, 32, 65], [15, 15, 16], 130, 46),
            ('2026-11-09', [0, 0, 65], [21, 20, 21], 65, 62),
            ('2026-11-16', [0, 0, 0], [26, 26, 26], 0, 78),
        ]
        for day_string, wet, dry, daily_wet, daily_dry in expected:
            with self.subTest(day=day_string):
                state = care.initial()
                day = date.fromisoformat(day_string)
                slots = care.slots_for_day(state, day)
                self.assertEqual([slot['time'] for slot in slots], ['07:00', '14:00', '19:00'])
                for index, slot in enumerate(slots):
                    cid = care.ensure_checklist(state, day, slot, index)
                    keys = state['checklists'][cid]['task_ids']
                    tasks = {state['tasks'][key]['type']: state['tasks'][key] for key in keys}
                    self.assertEqual(tasks['dry']['grams'], dry[index])
                    if wet[index]:
                        self.assertEqual(tasks['wet']['grams'], wet[index])
                    else:
                        self.assertNotIn('wet', tasks)
                    for kind in care.CLEANING:
                        expected_cleaning = slot['key'] != 'lunch' and (kind != 'water' or slot['key'] == 'breakfast')
                        self.assertEqual(kind in tasks, expected_cleaning)
                self.assertEqual(len(state['checklists']), 3)
                self.assertEqual(sum(t['grams'] for t in state['tasks'].values() if t['type'] == 'wet'), daily_wet)
                self.assertEqual(sum(t['grams'] for t in state['tasks'].values() if t['type'] == 'dry'), daily_dry)
                self.assertFalse(any('_22:00' in key for key in state['tasks']))

    def test_late_meal_is_not_recreated_after_all_tasks_close(self):
        self.tick('2026-10-19T19:00:00+01:00')
        for cid, checklist in self.s['checklists'].items():
            types = [self.s['tasks'][key]['type'] for key in checklist['task_ids']]
            args = argparse.Namespace(checklist=cid, types=types, action='done', actor='owner',
                                      minutes=None, message_id='close-' + cid, original_message_id=None)
            care.update(self.s, args, at('2026-10-19T19:05:00+01:00'))
        self.assertEqual(self.tick('2026-10-19T22:00:00+01:00'), 'NO_REPLY')
        self.assertEqual(self.tick('2026-10-19T22:30:00+01:00'), 'NO_REPLY')
        self.assertEqual(len(self.s['checklists']), 3)
        self.assertFalse(any('_22:00' in key for key in self.s['checklists']))

    def test_stage_mornings_show_all_three_new_portions(self):
        for day_string in ['2026-10-19', '2026-11-02', '2026-11-09', '2026-11-16']:
            with self.subTest(day=day_string):
                state = care.initial()
                day = date.fromisoformat(day_string)
                state['last_tick'] = care.iso(care.wall(day, '06:00'))
                now = care.wall(day, '07:00')
                text = care.tick(state, now)
                self.assertIn('**Сегодня меняются порции**', text)
                for value in ['07:00:', '14:00:', '19:00:']:
                    self.assertIn(value, text)
                self.assertNotIn('22:00', text)
                self.assertNotIn(state['config']['portion_note'], text)
                self.assertEqual(care.tick(state, now + timedelta(seconds=1)), 'NO_REPLY')

    def test_vet_review_notice_on_day_before_stage_five(self):
        self.s['last_tick'] = '2026-11-15T06:00:00+00:00'
        text = self.tick('2026-11-15T07:00:00+00:00')
        self.assertIn('Завтра запланирован переход на сухой корм', text)
        self.assertIn('по возрасту, весу и рекомендациям ветеринара', text)
        self.assertIn('Новую норму самостоятельно не рассчитываю', text)
        self.assertEqual(self.s['vet_notice_for'], '2026-11-16')
        repeat = self.tick('2026-11-15T07:30:00+00:00')
        self.assertNotIn('Завтра запланирован переход на сухой корм', repeat)

    def test_health_hold_suppresses_transition_notice_not_care(self):
        self.s['progression_hold'] = {'stage': 'weeks_1_2', 'since': '2026-10-30T12:00:00+00:00'}
        self.s['last_tick'] = '2026-11-15T06:00:00+00:00'
        text = self.tick('2026-11-15T07:00:00+00:00')
        self.assertNotIn('Завтра запланирован переход на сухой корм', text)
        self.assertIn('Дать ≈1/4 банки влажного', text)
        self.assertIn('Дать 10 г сухого', text)
        self.assertIn('Почистить лоток', text)
        self.assertIn('Налить свежую питьевую воду', text)

    def test_portion_proposal_uses_current_number_of_meals(self):
        change = {'kind': 'portions', 'stage': 'week_3', 'wet': [33, 32, 65], 'dry': [15, 15, 16]}
        previous = copy.deepcopy(self.s['config'])
        text = care.proposal(self.s, change, 'owner', at('2026-10-20T12:00:00+01:00'))
        self.assertIn('Подтвердите изменение', text)
        self.assertEqual(self.s['config'], previous)
        change['wet'] = [33, 32, 65, 0]
        with self.assertRaises(ValueError):
            care.proposal(self.s, change, 'owner', at('2026-10-20T12:00:00+01:00'))

    def test_consumption_and_provisional_allowance(self):
        text = '\n'.join(care.plan_consumption(self.s['config']))
        self.assertIn('Всего влажного по плану: 21 банки', text)
        self.assertIn('14/3 банки', text)
        self.assertIn('7/3 банки', text)
        stage = care.stage_for(self.s, date(2026, 11, 16))
        self.assertTrue(stage['provisional'])
        self.assertEqual(care.daily_line(stage), 'Суточная порция: ориентировочно 78 г сухого.')
        self.assertTrue(self.s['config']['dry_amounts_are_full_daily_totals'])

    def test_grouped_first_breakfast_keeps_stage_notice(self):
        expected = (
            '🐾 Экорн · завтрак и утренняя уборка\n'
            '19 октября · 07:00\n'
            'Caregivers, доброе утро! Пора покормить Экорна и немного прибраться.\n\n'
            '**Сегодня меняются порции**\n'
            '07:00: ≈1/4 банки влажного + 10 г сухого.\n'
            '14:00: ≈1/4 банки влажного + 10 г сухого.\n'
            '19:00: ≈1/2 банки влажного + 10 г сухого.\n\n'
            '🍽 Кормление\n'
            '☐ Дать ≈1/4 банки влажного корма.\n'
            '☐ Дать 10 г сухого корма.\n\n'
            '🧹 Уборка и вода\n'
            '☐ Почистить лоток и убрать мусор вокруг.\n'
            '☐ Помыть использованные миски и протереть место кормления.\n'
            '☐ Налить свежую питьевую воду.\n\n'
            '✅ Отмечайте выполненное\n'
            '⏰ Через 30 минут напомню только о неотмеченных пунктах.'
        )
        text = self.tick('2026-10-19T07:00:00+01:00')
        self.assertEqual(text, expected)
        refreshed = care.refresh(self.s, self.s['last_emission_id'], at('2026-10-19T07:00:01+01:00'))
        self.assertEqual(refreshed, expected)
        self.assertIn('2026-10-19_07:00', self.s['checklists'])
        self.assertEqual(len(self.s['tasks']), 5)

    def test_grouped_regular_breakfast_matches_owner_layout(self):
        slot = self.s['config']['slots'][0]
        cid = care.ensure_checklist(self.s, date(2026, 10, 20), slot, 0)
        expected = (
            '🐾 Экорн · завтрак и утренняя уборка\n'
            '20 октября · 07:00\n'
            'Caregivers, доброе утро! Пора покормить Экорна и немного прибраться.\n\n'
            '🍽 Кормление\n'
            '☐ Дать ≈1/4 банки влажного корма.\n'
            '☐ Дать 10 г сухого корма.\n\n'
            '🧹 Уборка и вода\n'
            '☐ Почистить лоток и убрать мусор вокруг.\n'
            '☐ Помыть использованные миски и протереть место кормления.\n'
            '☐ Налить свежую питьевую воду.\n\n'
            '✅ Отмечайте выполненное\n'
            '⏰ Через 30 минут напомню только о неотмеченных пунктах.'
        )
        before = copy.deepcopy(self.s)
        self.assertEqual(care.render_checklist(self.s, cid), expected)
        self.assertEqual(self.s, before)

    def test_grouped_lunch_contains_only_current_feeding(self):
        slot = self.s['config']['slots'][1]
        cid = care.ensure_checklist(self.s, date(2026, 10, 20), slot, 1)
        expected = (
            '🐾 Экорн · обед\n'
            '20 октября · 14:00\n'
            'Caregivers, добрый день! Пора покормить Экорна.\n\n'
            '🍽 Кормление\n'
            '☐ Дать ≈1/4 банки влажного корма.\n'
            '☐ Дать 10 г сухого корма.\n\n'
            '✅ Отмечайте выполненное\n'
            '⏰ Через 30 минут напомню только о неотмеченных пунктах.'
        )
        self.assertEqual(care.render_checklist(self.s, cid), expected)
        updated = expected.replace('добрый день! Пора покормить Экорна.', 'отметки обновлены.')
        updated = updated.replace('Через 30 минут напомню', 'Напомню')
        self.assertEqual(care.render_checklist(self.s, cid, updated=True), updated)
        self.assertNotIn('Уборка и вода', expected)

    def test_grouped_dinner_uses_evening_greeting(self):
        slot = self.s['config']['slots'][2]
        cid = care.ensure_checklist(self.s, date(2026, 10, 20), slot, 2)
        text = care.render_checklist(self.s, cid)
        self.assertTrue(text.startswith('🐾 Экорн · ужин и вечерняя уборка\n20 октября · 19:00\n'))
        self.assertIn('Caregivers, добрый вечер!', text)
        self.assertIn('🍽 Кормление', text)
        self.assertIn('🧹 Уборка\n', text)
        self.assertNotIn('Уборка и вода', text)
        self.assertNotIn('питьевую воду', text)
        self.assertIn('Дать ≈1/2 банки влажного', text)

    def test_grouped_messages_include_mentions_once(self):
        text = self.tick('2026-10-19T07:00:00+01:00')
        self.assertEqual(text.count('Caregivers'), 1)
        refreshed = care.refresh(self.s, self.s['last_emission_id'], at('2026-10-19T07:00:01+01:00'))
        self.assertEqual(refreshed.count('Caregivers'), 1)
        repeat = self.tick('2026-10-19T07:30:00+01:00')
        self.assertEqual(repeat.count('Caregivers'), 1)
        combined = self.tick('2026-10-19T14:00:00+01:00')
        self.assertEqual(combined.count('Caregivers'), 1)
        refreshed = care.refresh(self.s, self.s['last_emission_id'], at('2026-10-19T14:00:01+01:00'))
        self.assertEqual(refreshed.count('Caregivers'), 1)

    def test_grouped_completion_does_not_prompt_another_feeding(self):
        self.tick('2026-10-19T07:00:00+01:00')
        args = argparse.Namespace(checklist='2026-10-19_07:00',
                                  types=['wet', 'dry', 'litter', 'bowls', 'water'],
                                  action='done', actor='owner', minutes=None,
                                  message_id='done', original_message_id=None)
        text = care.update(self.s, args, at('2026-10-19T07:04:00+01:00'))
        self.assertIn('отметки обновлены.', text)
        self.assertIn('Всё выполнено', text)
        self.assertNotIn('Пора покормить', text)
        self.assertNotIn('Через 30 минут', text)
        self.assertEqual(self.tick('2026-10-19T07:30:00+01:00'), 'NO_REPLY')

    def test_grouped_snooze_does_not_promise_thirty_minutes(self):
        self.tick('2026-10-19T07:00:00+01:00')
        args = argparse.Namespace(checklist='2026-10-19_07:00', types=['wet'],
                                  action='snooze', actor='owner', minutes=60,
                                  message_id='snooze', original_message_id=None)
        text = care.update(self.s, args, at('2026-10-19T07:11:00+01:00'))
        self.assertNotIn('Через 30 минут', text)
        self.assertIn('2026-10-19T08:11:00+01:00', text)

    def test_grouped_checklist_keeps_previous_feeding_warning(self):
        self.tick('2026-10-19T07:00:00+01:00')
        self.tick('2026-10-19T14:00:00+01:00')
        text = care.render_checklist(self.s, '2026-10-19_14:00')
        self.assertIn('Предыдущее кормление не подтверждено', text)
        self.assertIn('Не добавляйте их к новой порции', text)
        self.assertNotIn('Суточная порция:', text)
        self.assertNotIn('Список:', text)
        self.assertNotIn(self.s['config']['portion_note'], text)

    def test_evening_water_is_not_created_or_repeated(self):
        self.tick('2026-10-19T19:00:00+01:00')
        cid = '2026-10-19_19:00'
        self.assertNotIn(cid + '_water', self.s['tasks'])
        tasks = care.unresolved(self.s, cid)
        self.assertEqual({task['type'] for task in tasks}, {'wet', 'dry', 'litter', 'bowls'})
        repeat = care.render_reminders(self.s, tasks)
        self.assertNotIn('питьевую воду', repeat)
        self.assertEqual([task['checklist'] for task in self.s['tasks'].values() if task['type'] == 'water'],
                         ['2026-10-19_07:00'])

    def test_morning_water_still_repeats_until_confirmed(self):
        self.tick('2026-10-19T07:00:00+01:00')
        repeat = self.tick('2026-10-19T07:30:00+01:00')
        self.assertIn('Налить свежую питьевую воду.', repeat)
        self.assertIn('2026-10-19_07:00_water', self.s['tasks'])

    def test_time_change_proposal_preserves_evening_cleaning_scope(self):
        before = copy.deepcopy(self.s['config'])
        care.proposal(self.s, {'kind': 'schedule', 'slot': 'dinner', 'time': '19:30'},
                      'owner', at('2026-10-20T12:00:00+01:00'))
        self.assertEqual(self.s['config'], before)
        item = next(iter(self.s['proposals'].values()))
        dinner = next(slot for slot in item['config']['slots'] if slot['key'] == 'dinner')
        self.assertEqual(dinner['cleaning_tasks'], ['litter', 'bowls'])


if __name__ == '__main__':
    unittest.main()
