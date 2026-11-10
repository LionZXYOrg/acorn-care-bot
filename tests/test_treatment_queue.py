"""Treatment transfers must not create future feeding tasks."""
import copy
import json
import unittest
from datetime import timedelta

import care
from test_treatments import state_at, MORNING, NOW, KEY, OCCURRENCE


class TreatmentQueueTests(unittest.TestCase):
    def test_transfer_queues_without_future_checklist_or_food(self):
        state = state_at()
        care.tick(state, MORNING)
        before = copy.deepcopy(state)
        result = care.move_to_next(state, KEY, 'telegram:101', NOW)
        destination = '2026-11-10_14:00'
        target = result['target_id']
        self.assertNotIn(destination, state['checklists'])
        self.assertNotIn(target, state['tasks'])
        self.assertEqual(set(state['tasks']), set(before['tasks']))
        self.assertEqual(state['carryovers'][target]['treatment_task']['treatment_occurrence'], OCCURRENCE)
        snapshot = copy.deepcopy(state)
        care.move_to_next(state, KEY, 'telegram:102', NOW)
        self.assertEqual(state, snapshot)
        state = json.loads(json.dumps(state))
        self.assertEqual(care.tick(state, NOW + timedelta(minutes=1)), 'NO_REPLY')
        lunch = care.instant('2026-11-10T14:00:00+00:00')
        text = care.tick(state, lunch)
        self.assertIn('💊 Обработка', text)
        self.assertIn(target, state['tasks'])
        self.assertNotIn(target, state['carryovers'])
        self.assertEqual(state['tasks'][target]['treatment_occurrence'], OCCURRENCE)
        self.assertEqual(state['checklists'][destination]['task_ids'].count(target), 1)
        care.tick(state, lunch + timedelta(seconds=1))
        self.assertEqual(state['checklists'][destination]['task_ids'].count(target), 1)
        self.assertEqual(state['treatment_occurrences'][OCCURRENCE]['current_task_id'], target)

    def test_queued_treatment_still_blocks_a_later_occurrence(self):
        state = state_at()
        care.tick(state, MORNING)
        result = care.move_to_next(state, KEY, 'telegram:101', NOW)
        target = result['target_id']
        later = care.instant('2027-02-10T07:00:00+00:00')
        state['last_tick'] = care.iso(later - timedelta(minutes=1))
        care.tick(state, later)
        self.assertEqual(state['treatment_occurrences']['credelio:2027-02-10']['status'], 'blocked')
        self.assertNotIn('2027-02-10_07:00_treatment_credelio', state['tasks'])
        self.assertIn(target, state['carryovers'])
        queued = state['carryovers'][target]['treatment_task']
        self.assertTrue(queued['needs_clarification'])
        self.assertIn('2027-02-10', queued['blocked_treatment_dates'])

    def test_initial_pages_reserve_space_for_later_medication_warnings(self):
        import inline_ui
        import treatments
        from service import chunks
        from test_treatments import DEFINITION
        state = state_at(enabled=False)
        definitions = [{**DEFINITION, 'id': 'medicine_' + str(index), 'text': 'x' * 256}
                       for index in range(16)]
        treatments.apply_configuration(state, definitions, NOW)
        text = care.tick(state, MORNING)
        eid = state['last_emission_id']
        panel = inline_ui.prepare(state, chunks(text), care.emission_task_ids(state, eid),
                                  MORNING, primary='2026-11-10_07:00')
        for task in state['tasks'].values():
            if treatments.is_treatment(task['type']):
                task.update(needs_clarification=True, status='deferred',
                            snoozed_until=care.iso(NOW),
                            blocked_treatment_dates=['2027-02-10'] * 100)
        for page in panel['pages']:
            rendered = inline_ui.page_text(state, panel, page)
            self.assertLessEqual(len(rendered.encode('utf-16-le')) // 2, 4096)


if __name__ == '__main__':
    unittest.main()
