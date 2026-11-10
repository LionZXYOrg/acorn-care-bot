"""One approved lunch reminder preserves the quarterly breakfast schedule."""
import copy
import json
import unittest

import care
import treatments
from configuration import validate_care_config
from test_treatments import DEFINITION, medicines, state_at


class FirstSlotTests(unittest.TestCase):
    def configured(self):
        state = state_at(enabled=False)
        definition = {**DEFINITION, 'first_slot': 'lunch'}
        treatments.apply_configuration(state, [definition], care.instant('2026-11-10T11:42:49+00:00'))
        return state

    def test_no_medicine_before_lunch(self):
        state = self.configured()
        for stamp in ('07:00:00', '11:42:49', '13:59:59'):
            care.tick(state, care.instant('2026-11-10T' + stamp + '+00:00'))
            self.assertEqual(medicines(state), [])
        self.assertNotIn('credelio:2026-11-10', state.get('treatment_occurrences', {}))

    def test_lunch_contains_one_pending_occurrence_and_restart_does_not_duplicate(self):
        state = self.configured()
        care.tick(state, care.instant('2026-11-10T07:00:00+00:00'))
        breakfast = copy.deepcopy(state['checklists']['2026-11-10_07:00'])
        now = care.instant('2026-11-10T14:00:00+00:00')
        text = care.tick(state, now)
        self.assertIn('💊 Обработка', text)
        self.assertIn(DEFINITION['text'], text)
        tasks = medicines(state)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]['checklist'], '2026-11-10_14:00')
        self.assertEqual(tasks[0]['status'], 'pending')
        self.assertEqual(tasks[0]['treatment_occurrence'], 'credelio:2026-11-10')
        self.assertEqual(state['checklists']['2026-11-10_07:00'], breakfast)
        state = json.loads(json.dumps(state))
        care.tick(state, now)
        self.assertEqual(len(medicines(state)), 1)

    def test_next_quarter_stays_at_breakfast(self):
        state = self.configured()
        now = care.instant('2027-02-10T07:00:00+00:00')
        care.tick(state, now)
        tasks = medicines(state)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]['checklist'], '2027-02-10_07:00')
        self.assertEqual(tasks[0]['treatment_occurrence'], 'credelio:2027-02-10')

    def test_first_slot_is_validated(self):
        for value in ('midnight', None, [], 14):
            config = copy.deepcopy(care.BOOT_CONFIG)
            config['treatments'] = [{**DEFINITION, 'first_slot': value}]
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_care_config(config)
        config['treatments'] = [{**DEFINITION, 'first_slot': 'lunch'}]
        validate_care_config(config)


if __name__ == '__main__':
    unittest.main()
