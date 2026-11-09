"""Optional owner-approved care changes preserve saved care and schedules."""
import copy
import json
from datetime import timedelta
from pathlib import Path
import unittest

import care
from configuration import validate_care_config
from nutrition_plan import apply_nutrition_plan, replacement_config, validate_nutrition_plan

ROOT = Path(__file__).resolve().parents[1]
CARE_SLOTS = {
    'breakfast': {
        'cleaning_tasks': ['litter', 'bowls', 'water'],
        'cleaning_labels': {'litter': 'Почистить лоток.', 'bowls': 'Помыть миски.',
                            'water': 'Налить свежую воду.'}},
    'lunch': {'cleaning_tasks': [], 'cleaning_labels': {}},
    'dinner': {
        'cleaning_tasks': ['litter', 'bowls', 'water'],
        'cleaning_labels': {'litter': 'Почистить лоток.', 'bowls': 'Помыть использованные миски.',
                            'water': 'Проверить питьевую воду.'}},
}


def synthetic_plan():
    plan = json.loads((ROOT / 'tests/fixtures/nutrition-plan-r4.json').read_text())
    plan.update(id='synthetic-care-slots', approved_by='synthetic-owner',
                approved_message='synthetic-owner-approval')
    plan['care_slots'] = copy.deepcopy(CARE_SLOTS)
    return plan


class NutritionCareSlotsTests(unittest.TestCase):
    def setUp(self):
        self.plan = synthetic_plan()
        self.now = care.instant(self.plan['approved_at'])
        self.state = care.initial()
        self.state['config'].pop('nutrition_plan', None)

    def assert_care(self, slots):
        for slot in slots:
            approved = CARE_SLOTS[slot['key']]
            self.assertEqual(slot['cleaning'], bool(approved['cleaning_tasks']))
            for field in ('cleaning_tasks', 'cleaning_labels'):
                self.assertEqual(slot[field], approved[field])

    def test_replacement_is_deep_and_changes_no_times_or_metadata(self):
        config = self.state['config']
        config['bowls_morning_only'] = True
        for slot in config['slots']:
            slot['custom'] = {'retained': [slot['key']]}
        before = copy.deepcopy(config)
        plan_before = copy.deepcopy(self.plan)
        candidate = validate_nutrition_plan(self.plan, config)
        self.assert_care(candidate['slots'])
        self.assertIs(candidate['bowls_morning_only'], False)
        for old, new in zip(before['slots'], candidate['slots']):
            self.assertEqual({k: v for k, v in old.items() if not k.startswith('cleaning')},
                             {k: v for k, v in new.items() if not k.startswith('cleaning')})
        candidate['slots'][0]['custom']['retained'].append('changed')
        candidate['slots'][0]['cleaning_labels']['water'] = 'Changed'
        self.assertEqual(config, before)
        self.assertEqual(self.plan, plan_before)

    def test_invalid_care_schema_is_rejected_without_mutation(self):
        cases = [None, [], {}, {'breakfast': CARE_SLOTS['breakfast']}]
        for key, value in [
            ('cleaning_tasks', 'litter'), ('cleaning_tasks', ('litter',)),
            ('cleaning_tasks', ['litter', 'litter']), ('cleaning_tasks', ['unknown']),
            ('cleaning_tasks', [[]]), ('cleaning_labels', None),
            ('cleaning_labels', {'water': 'Safe.'}),
            ('cleaning_labels', {**CARE_SLOTS['breakfast']['cleaning_labels'], 'unknown': 'Safe.'}),
            ('extra', True),
        ]:
            slots = copy.deepcopy(CARE_SLOTS)
            slots['breakfast'][key] = value
            cases.append(slots)
        missing = copy.deepcopy(CARE_SLOTS)
        del missing['dinner']['cleaning_labels']
        cases.append(missing)
        for slots in cases:
            with self.subTest(slots=slots):
                plan = {**self.plan, 'care_slots': slots}
                before = copy.deepcopy(self.state)
                plan_before = copy.deepcopy(plan)
                with self.assertRaises(ValueError):
                    apply_nutrition_plan(self.state, plan, self.now)
                self.assertEqual(self.state, before)
                self.assertEqual(plan, plan_before)

    def test_label_safety_for_plan_and_configuration(self):
        for label in ['', ' padded', 'padded ', 'a\nb', 'a\rb', 'a\tb', '<b>bad</b>',
                      'a\x00b', 'a\u202eb', 'a · b', 'x' * 257, '😀' * 129, None, 42]:
            with self.subTest(label=label):
                plan = copy.deepcopy(self.plan)
                plan['care_slots']['breakfast']['cleaning_labels']['water'] = label
                before = copy.deepcopy(self.state)
                with self.assertRaises(ValueError):
                    apply_nutrition_plan(self.state, plan, self.now)
                self.assertEqual(self.state, before)
                config = copy.deepcopy(self.state['config'])
                config['slots'][0].update(cleaning=True, cleaning_tasks=['water'],
                                          cleaning_labels={'water': label})
                with self.assertRaises(ValueError):
                    validate_care_config(config)
        validate_nutrition_plan(self.plan, self.state['config'])

    def test_optional_configuration_labels_follow_effective_tasks(self):
        config = copy.deepcopy(self.state['config'])
        for labels in [[], None, {'unknown': 'Safe.'}, {'water': 'Safe.'}]:
            config['slots'][0].update(cleaning=True, cleaning_tasks=['litter'], cleaning_labels=labels)
            with self.assertRaises(ValueError):
                validate_care_config(config)
        config['slots'][0].update(cleaning=False, cleaning_tasks=['litter'],
                                  cleaning_labels={'litter': 'Safe.'})
        with self.assertRaises(ValueError):
            validate_care_config(config)
        config['slots'][0].update(cleaning=True, cleaning_labels={'litter': 'Safe.'})
        validate_care_config(config)
        del config['slots'][0]['cleaning_tasks']
        config['slots'][0]['cleaning_labels'] = {'water': 'Safe.'}
        validate_care_config(config)
        del config['slots'][0]['cleaning_labels']
        validate_care_config(config)

    def test_history_pending_and_existing_care_are_preserved(self):
        today = self.now.date().isoformat()
        tomorrow = (self.now.date() + timedelta(days=1)).isoformat()
        old = self.state['schedule_history'][0]
        old['metadata'] = 'old snapshot'
        current_slots = copy.deepcopy(old['slots'])
        current_slots[1]['time'] = '14:15'
        self.state['schedule_history'].append({
            'effective': (self.now.date() - timedelta(days=1)).isoformat(),
            'slots': current_slots, 'metadata': 'active snapshot'})
        future_slots = copy.deepcopy(current_slots)
        future_slots[0]['time'] = '07:30'
        self.state['schedule_history'].append({'effective': tomorrow, 'slots': future_slots,
                                                'metadata': 'future snapshot'})
        self.state['pending_config'] = {'effective': tomorrow, 'slots': copy.deepcopy(future_slots),
                                         'quiet_end': '07:00', 'custom': {'keep': True}}
        care.ensure_checklist(self.state, self.now.date(), current_slots[0], 0)
        task = next(iter(self.state['tasks'].values()))
        task.update(status='done', done_by='synthetic-owner', done_at=care.iso(self.now),
                    next_repeat_at=care.iso(self.now), snoozed_until=care.iso(self.now))
        self.state.update(carryovers={'synthetic': {'source_ids': ['old']}},
                          inline_receipts={'receipt': {'saved': True}},
                          standalone={'outbox': {'old': {'status': 'pending'}}, 'offset': 123},
                          reminder_pause={'until': 'indefinite'},
                          progression_hold={'since': care.iso(self.now), 'actor': 'synthetic-owner',
                                            'stage': care.stage_for(self.state, self.now.date())['id']})
        before = copy.deepcopy(self.state)
        self.assertTrue(apply_nutrition_plan(self.state, self.plan, self.now))
        for key in ('tasks', 'checklists', 'carryovers', 'inline_receipts', 'standalone',
                    'reminder_pause', 'progression_hold', 'proposals', 'last_tick'):
            self.assertEqual(self.state[key], before[key], key)
        self.assertEqual(self.state['schedule_history'][:2], before['schedule_history'][:2])
        current = self.state['schedule_history'][2]
        self.assertEqual(current['effective'], today)
        self.assertEqual([s['time'] for s in current['slots']], [s['time'] for s in current_slots])
        self.assert_care(current['slots'])
        future = self.state['schedule_history'][3]
        self.assertEqual(future['metadata'], 'future snapshot')
        self.assertEqual([s['time'] for s in future['slots']], [s['time'] for s in future_slots])
        self.assert_care(future['slots'])
        pending = self.state['pending_config']
        self.assertEqual({k: v for k, v in pending.items() if k != 'slots'},
                         {k: v for k, v in before['pending_config'].items() if k != 'slots'})
        self.assertEqual([s['time'] for s in pending['slots']], [s['time'] for s in future_slots])
        self.assert_care(pending['slots'])
        after = copy.deepcopy(self.state)
        self.assertFalse(apply_nutrition_plan(self.state, self.plan, self.now))
        self.assertEqual(self.state, after)
        changed = copy.deepcopy(self.plan)
        changed['care_slots']['dinner']['cleaning_labels']['water'] = 'Changed.'
        with self.assertRaisesRegex(ValueError, 'identity cannot be reused'):
            apply_nutrition_plan(self.state, changed, self.now)
        self.assertEqual(self.state, after)

    def test_empty_or_missing_history_and_no_task_creation(self):
        for history in (None, [], [{'effective': self.now.date().isoformat(),
                                   'slots': copy.deepcopy(self.state['config']['slots'])}]):
            state = copy.deepcopy(self.state)
            state.pop('schedule_history', None)
            if history is not None:
                state['schedule_history'] = history
            self.assertTrue(apply_nutrition_plan(state, self.plan, self.now))
            self.assertEqual(len(state['schedule_history']), 1)
            self.assertEqual(state['schedule_history'][0]['effective'], self.now.date().isoformat())
            self.assert_care(state['schedule_history'][0]['slots'])
            self.assertEqual(state['tasks'], {})
            self.assertEqual(state['checklists'], {})
            self.assertNotIn('pending_config', state)

    def test_absent_optional_fields_keep_legacy_behavior(self):
        plan = copy.deepcopy(self.plan)
        del plan['care_slots']
        self.state['config']['bowls_morning_only'] = True
        self.state['pending_config'] = {'effective': '2099-01-01',
                                        'slots': copy.deepcopy(self.state['config']['slots'])}
        before = copy.deepcopy(self.state)
        self.assertTrue(apply_nutrition_plan(self.state, plan, self.now))
        for key in ('schedule_history', 'pending_config', 'tasks', 'checklists'):
            self.assertEqual(self.state[key], before[key])
        self.assertEqual(self.state['config']['slots'], before['config']['slots'])
        self.assertIs(self.state['config']['bowls_morning_only'], True)
        no_dinner_bowls = copy.deepcopy(self.plan)
        no_dinner_bowls['care_slots']['dinner']['cleaning_tasks'].remove('bowls')
        del no_dinner_bowls['care_slots']['dinner']['cleaning_labels']['bowls']
        self.assertIs(replacement_config(before['config'], no_dinner_bowls)['bowls_morning_only'], True)


if __name__ == '__main__':
    unittest.main()
