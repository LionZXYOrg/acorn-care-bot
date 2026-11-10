"""The owner's correction removes routine evening bowls and water, not transfers."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import care
from configuration import validate_care_config
from storage import Store, migrate_morning_only_bowls
from test_morning_bowls import legacy_config
from test_service import BOT_ID, CHAT, TOPIC

NOW = care.instant('2026-11-10T20:19:14+00:00')
ROOT = Path(__file__).resolve().parents[1]


class MorningWaterTests(unittest.TestCase):
    def state(self):
        boot = legacy_config()
        boot['slots'][2]['cleaning_tasks'] = ['litter', 'bowls', 'water']
        boot['slots'][2]['cleaning_labels'] = {
            'litter': 'Лоток', 'bowls': 'Миски', 'water': 'Вода'}
        with patch.object(care, 'BOOT_CONFIG', boot):
            state = care.initial()
        care.tick(state, NOW)
        return state

    def test_only_boolean_water_flag(self):
        for value in (None, 0, 1, [], 'true'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_care_config({**care.BOOT_CONFIG, 'water_morning_only': value})

    def test_today_open_routine_items_close_without_completing_or_changing_food(self):
        state = self.state()
        breakfast = copy.deepcopy(state['checklists']['2026-11-10_07:00'])
        foods = {k: copy.deepcopy(t) for k, t in state['tasks'].items() if t['type'] in ('wet', 'dry')}
        for kind in ('bowls', 'water'):
            migrate_morning_only_bowls(state, NOW, kind)
            task = state['tasks']['2026-11-10_19:00_' + kind]
            self.assertEqual(task['status'], 'skipped')
            self.assertEqual(task['skip_reason'], kind + '_morning_only')
            self.assertIsNone(task['done_at'])
            self.assertIsNone(task['next_repeat_at'])
        self.assertEqual(state['checklists']['2026-11-10_07:00'], breakfast)
        for key, task in foods.items():
            self.assertEqual(state['tasks'][key], task)
        self.assertEqual(care.slots_for_day(state, NOW.date())[2]['cleaning_tasks'], ['litter'])
        before = copy.deepcopy(state)
        for kind in ('bowls', 'water'):
            migrate_morning_only_bowls(state, NOW, kind)
        self.assertEqual(state, before)

    def test_transfers_and_closed_items_remain_unchanged(self):
        for marker in ('carried_from', 'queued', 'done'):
            with self.subTest(marker=marker):
                state = self.state()
                key = '2026-11-10_19:00_water'
                task = state['tasks'][key]
                if marker == 'carried_from':
                    task['carried_from'] = ['source']
                elif marker == 'queued':
                    state.setdefault('carryovers', {})[key] = {'source_ids': ['source']}
                else:
                    task['status'] = 'done'
                before = copy.deepcopy(task)
                migrate_morning_only_bowls(state, NOW, 'water')
                self.assertEqual(state['tasks'][key], before)

    def test_startup_correction_wins_after_nutrition_application(self):
        state = self.state()
        home = json.loads((ROOT / 'deploy/standalone/config.json').read_text())
        home.pop('treatments', None)
        state['standalone'] = {'schema': 1, 'identity': None, 'offset': 0, 'outbox': {}}
        with tempfile.TemporaryDirectory(dir=ROOT / '.test-tmp') as directory:
            path = Path(directory) / 'state.json'
            care.save(path, state)
            with patch.object(care, 'BOOT_CONFIG', home):
                Store(path).bind(BOT_ID, CHAT, TOPIC, NOW)
            after = json.loads(path.read_text())
        self.assertEqual(care.slots_for_day(after, NOW.date())[2]['cleaning_tasks'], ['litter'])
        for kind in ('bowls', 'water'):
            self.assertEqual(after['tasks']['2026-11-10_19:00_' + kind]['status'], 'skipped')
        self.assertEqual(after['config']['nutrition_plan_id'], 'nutrition-2026-11-09-r5')


if __name__ == '__main__':
    unittest.main()
