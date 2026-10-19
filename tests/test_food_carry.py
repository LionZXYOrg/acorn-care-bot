import argparse
import copy
import inspect
import json
import unittest
from datetime import date

import care
import inline_ui


class FoodCarryTests(unittest.TestCase):
    def setUp(self):
        self.state = care.initial()
        self.state['config']['food_transfer_policy'] = 'add_full'
        self.slot = self.state['config']['slots'][0]
        self.now = care.instant('2026-10-19T11:44:22+01:00')
        care.tick(self.state, care.wall(self.now.date(), self.slot['time']))
        self.cid = care.checklist_id(self.now.date(), self.slot)
        self.target = '2026-10-19_14:00'

    def move(self, kind='dry', now=None, cid=None):
        return care.move_to_next(self.state, (cid or self.cid) + '_' + kind,
                                 'owner', now or self.now, 'confirmed-move')

    def materialize(self, stamp='2026-10-19T14:00:00+01:00'):
        return care.tick(self.state, care.instant(stamp))

    def test_entire_dry_portion_is_added_only_when_target_becomes_due(self):
        original = copy.deepcopy(self.state['config']['stages'])
        result = self.move()
        self.assertIn('добавлено: 10 г', care.move_answer(result))
        self.assertNotIn(self.target, self.state['checklists'])
        item = self.state['carryovers'][self.target + '_dry']
        self.assertEqual(item['food_grams'], {self.cid + '_dry': 10})
        text = self.materialize()
        task = self.state['tasks'][self.target + '_dry']
        self.assertEqual((task['planned_grams'], task['carried_grams'], task['grams']), (10, 10, 20))
        self.assertIn('Дать 20 г сухого корма. Включая перенос: 10 г.', text)
        self.assertEqual(self.state['config']['stages'], original)
        source = self.state['tasks'][self.cid + '_dry']
        self.assertEqual(source['status'], 'skipped')
        self.assertIsNone(source['next_repeat_at'])
        self.assertFalse(source['moved_food_without_addition'])
        self.assertEqual(source['moved_food_grams'], 10)
        self.assertIn('Весь объём добавлен', care.task_line(source))

    def test_wet_and_dry_remain_separate_and_wet_stays_in_can_fractions(self):
        self.move('wet')
        self.move('dry')
        text = self.materialize()
        self.assertEqual(self.state['tasks'][self.target + '_wet']['grams'], 98)
        self.assertEqual(self.state['tasks'][self.target + '_dry']['grams'], 20)
        self.assertIn('Дать ≈1/2 банки влажного корма. Включая перенос: ≈1/4 банки.', text)
        self.assertIn('Влажный ≈1/2 банки', care.button_text(self.state['tasks'][self.target + '_wet']))

    def test_repeated_press_cannot_add_or_replace_caregiver(self):
        self.move()
        before = copy.deepcopy(self.state)
        result = self.move()
        self.assertEqual(result['status'], 'already_closed')
        self.assertEqual(self.state, before)
        self.materialize()
        before = copy.deepcopy(self.state)
        care.apply_carryovers(self.state, self.target)
        self.assertEqual(self.state, before)
        self.assertEqual(self.state['tasks'][self.target + '_dry']['grams'], 20)

    def test_existing_future_target_is_increased_once(self):
        slot = self.state['config']['slots'][1]
        care.ensure_checklist(self.state, self.now.date(), slot, 1)
        self.move()
        self.assertEqual(self.state['tasks'][self.target + '_dry']['grams'], 20)
        self.assertNotIn(self.target + '_dry', self.state.get('carryovers', {}))
        self.materialize()
        self.assertEqual(self.state['tasks'][self.target + '_dry']['grams'], 20)

    def test_pending_queue_and_amount_survive_restart(self):
        self.move()
        self.state = json.loads(json.dumps(self.state))
        self.materialize()
        self.state = json.loads(json.dumps(self.state))
        care.ensure_checklist(self.state, self.now.date(), self.state['config']['slots'][1], 1)
        self.assertEqual(self.state['tasks'][self.target + '_dry']['grams'], 20)
        self.assertEqual(self.state['tasks'][self.target + '_dry']['carried_from'], [self.cid + '_dry'])

    def test_multiple_old_sources_merge_without_duplicates(self):
        self.materialize()
        now = care.instant('2026-10-19T15:00:00+01:00')
        self.move(now=now)
        self.move(now=now, cid=self.target)
        self.materialize('2026-10-19T19:00:00+01:00')
        task = self.state['tasks']['2026-10-19_19:00_dry']
        self.assertEqual(task['grams'], 30)
        self.assertEqual(task['planned_grams'], 10)
        self.assertEqual(task['carried_grams'], 20)
        self.assertEqual(len(task['carried_portions']), 2)
        ids = self.state['checklists']['2026-10-19_19:00']['task_ids']
        self.assertEqual(ids.count(task['id']), 1)

    def test_chained_transfer_carries_the_whole_aggregate_once(self):
        self.move()
        self.materialize()
        self.move(cid=self.target, now=care.instant('2026-10-19T14:01:00+01:00'))
        self.materialize('2026-10-19T19:00:00+01:00')
        task = self.state['tasks']['2026-10-19_19:00_dry']
        self.assertEqual(task['grams'], 30)
        self.assertEqual(task['carried_portions'], {self.target + '_dry': 20})

    def test_overnight_transfer_uses_confirmed_0700_schedule(self):
        slots = copy.deepcopy(self.state['config']['slots'])
        slots[0]['time'] = '07:00'
        self.state['pending_config'] = {'effective': '2026-10-20', 'slots': slots, 'quiet_end': '07:00'}
        self.state['schedule_history'].append({'effective': '2026-10-20', 'slots': slots})
        result = self.move(now=care.instant('2026-10-19T22:00:00+01:00'))
        self.assertEqual(result['due_at'], '2026-10-20T07:00:00+01:00')
        self.materialize('2026-10-20T07:00:00+01:00')
        self.assertEqual(self.state['tasks']['2026-10-20_07:00_dry']['grams'], 20)
        self.assertNotIn('2026-10-20_09:30', self.state['checklists'])

    def test_stage_change_uses_new_base_plus_old_carried_amount(self):
        result = self.move(now=care.instant('2026-11-01T22:00:00+00:00'))
        day = date(2026, 11, 2)
        slot = care.slots_for_day(self.state, day)[0]
        cid = care.ensure_checklist(self.state, day, slot, 0)
        self.assertEqual(result['target_id'], cid + '_dry')
        task = self.state['tasks'][result['target_id']]
        self.assertEqual((task['planned_grams'], task['carried_grams'], task['grams']), (15, 10, 25))

    def test_new_carry_is_not_lost_if_next_slot_has_no_planned_wet_food(self):
        result = self.move('wet', now=care.instant('2026-11-08T22:00:00+00:00'))
        day = date(2026, 11, 9)
        slot = care.slots_for_day(self.state, day)[0]
        cid = care.ensure_checklist(self.state, day, slot, 0)
        task = self.state['tasks'][result['target_id']]
        self.assertEqual((task['planned_grams'], task['carried_grams'], task['grams']), (0, 49, 49))
        self.assertEqual(self.state['checklists'][cid]['task_ids'].count(task['id']), 1)
        self.assertEqual(self.state['config']['stages'][2]['wet'][0], 0)

    def test_old_reminder_only_queue_is_not_reinterpreted_as_extra_food(self):
        self.state['config'].pop('food_transfer_policy')
        self.move()
        self.state['config']['food_transfer_policy'] = 'add_full'
        self.materialize()
        task = self.state['tasks'][self.target + '_dry']
        self.assertEqual(task['grams'], 10)
        self.assertNotIn('carried_grams', task)

    def test_recorded_grams_remain_due_even_if_policy_later_changes(self):
        self.move()
        self.state['config']['food_transfer_policy'] = 'planned_only'
        self.materialize()
        self.assertEqual(self.state['tasks'][self.target + '_dry']['grams'], 20)

    def test_cleaning_does_not_change_food_or_recurring_schedule(self):
        stages = copy.deepcopy(self.state['config']['stages'])
        self.move('water')
        self.materialize()
        self.assertEqual(self.state['tasks'][self.target + '_dry']['grams'], 10)
        self.assertEqual(self.state['tasks'][self.target + '_wet']['grams'], 49)
        self.assertEqual(self.state['config']['stages'], stages)
        self.assertIn(self.target + '_water', self.state['tasks'])
        self.assertFalse(self.state['config']['slots'][1]['cleaning'])

    def test_closed_target_rejects_without_mutating_source(self):
        care.ensure_checklist(self.state, self.now.date(), self.state['config']['slots'][1], 1)
        self.state['tasks'][self.target + '_dry']['status'] = 'done'
        before = copy.deepcopy(self.state)
        with self.assertRaises(ValueError):
            self.move()
        self.assertEqual(self.state, before)

    def test_done_source_never_moves(self):
        self.state['tasks'][self.cid + '_dry']['status'] = 'done'
        before = copy.deepcopy(self.state)
        self.assertEqual(self.move()['status'], 'already_closed')
        self.assertEqual(self.state, before)

    def test_health_hold_and_base_stage_are_preserved(self):
        hold = {'stage': 'weeks_1_2', 'since': '2026-10-19T10:00:00+01:00', 'actor': 'owner'}
        self.state['progression_hold'] = copy.deepcopy(hold)
        self.move()
        self.materialize()
        self.assertEqual(self.state['progression_hold'], hold)
        self.assertEqual(self.state['tasks'][self.target + '_dry']['grams'], 20)

    def test_decimal_portions_do_not_accumulate_float_drift(self):
        self.state['tasks'][self.cid + '_dry']['grams'] = 0.1
        care.ensure_checklist(self.state, self.now.date(), self.state['config']['slots'][1], 1)
        self.state['tasks'][self.target + '_dry']['grams'] = 0.2
        self.move()
        task = self.state['tasks'][self.target + '_dry']
        self.assertEqual(task['grams'], 0.3)

    def test_explicit_skip_does_not_automatically_transfer(self):
        args = argparse.Namespace(checklist=self.cid, types=['dry'], action='skip', actor='owner',
                                  minutes=None, message_id='skip', original_message_id=None)
        care.update(self.state, args, self.now)
        self.materialize()
        self.assertEqual(self.state['tasks'][self.target + '_dry']['grams'], 10)

    def test_unconfirmed_tasks_do_not_automatically_transfer(self):
        self.materialize()
        self.assertEqual(self.state['tasks'][self.target + '_dry']['grams'], 10)
        self.assertEqual(self.state['tasks'][self.cid + '_dry']['status'], 'needs_clarification')

    def test_policy_proposal_does_not_change_any_task(self):
        before = copy.deepcopy(self.state['tasks'])
        self.state['config'].pop('food_transfer_policy')
        text = care.proposal(self.state, {'kind': 'food_transfer', 'policy': 'add_full'}, 'owner', self.now)
        self.assertIn('включая следующий день', text)
        self.assertNotIn('food_transfer_policy', self.state['config'])
        self.assertEqual(self.state['tasks'], before)
        proposal = next(reversed(self.state['proposals'].values()))
        self.assertEqual(proposal['config']['food_transfer_policy'], 'add_full')

    def test_real_callback_and_replay_add_only_one_portion(self):
        panel = inline_ui.prepare(self.state, [care.render_checklist(self.state, self.cid)],
                                  self.state['checklists'][self.cid]['task_ids'], self.now, primary=self.cid)
        panel['pages'][-1]['message_id'] = 777
        panel['status'] = 'sent'
        index = panel['task_ids'].index(self.cid + '_dry')
        callback = {'id': 'full-food-transfer', 'from': {'id': 101, 'username': 'owner', 'is_bot': False},
                    'message': {'message_id': 777, 'message_thread_id': 42, 'chat': {'id': -1000000000001},
                                'from': {'id': 12345, 'is_bot': True}},
                    'data': inline_ui.PREFIX + panel['nonce'] + ':next:' + str(index)}
        extra = {'chat_id': -1000000000001, 'topic_id': 42} if 'chat_id' in inspect.signature(inline_ui.accept_callback).parameters else {}
        def accept():
            return inline_ui.accept_callback(self.state, callback, {101, '101'}, 12345, self.now, **extra)
        answer = accept()
        self.assertFalse(answer['show_alert'])
        self.assertIn('добавлено: 10 г', answer['text'])
        before = copy.deepcopy(self.state)
        self.assertEqual(accept(), answer)
        self.assertEqual(self.state, before)
        callback['id'] = 'another-press'
        self.assertIn('уже закрыт', accept()['text'])
        self.materialize()
        self.assertEqual(self.state['tasks'][self.target + '_dry']['grams'], 20)


if __name__ == '__main__':
    unittest.main()
