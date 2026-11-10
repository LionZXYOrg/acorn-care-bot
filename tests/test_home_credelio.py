"""Keep the owner-approved treatment identical in both home deployments."""
import json
from pathlib import Path
import unittest

import yaml

from configuration import validate_care_config


ROOT = Path(__file__).resolve().parents[1]
APPROVED = {
    'id': 'credelio',
    'start': '2026-11-10',
    'interval_months': 3,
    'slot': 'breakfast',
    'first_slot': 'lunch',
    'text': '💊 Credelio Chew 48 mg, 1 tablet, после еды',
    'approved_by': 'palmette',
    'approved_message': 'telegram_-1004424243504_655',
    'approved_at': '2026-11-10T11:22:43+00:00',
}


class ApprovedTreatmentTests(unittest.TestCase):
    def test_standalone_and_suspended_suite_keep_the_same_approved_treatment(self):
        config = json.loads((ROOT / 'deploy/standalone/config.json').read_text())
        values = yaml.safe_load((ROOT / 'charts/telegram-bot-suite/values-home.yaml').read_text())
        worker = next(item for item in values['workers'] if item['id'] == 'acorn')
        suite = json.loads(worker['config']['config.json'])
        self.assertEqual(config, suite)
        for care_config in (config, suite):
            validate_care_config(care_config)
            self.assertEqual(care_config['treatments'], [APPROVED])
            self.assertEqual([slot['time'] for slot in care_config['slots']],
                             ['07:00', '14:00', '19:00'])
            self.assertEqual(care_config['nutrition_plan']['id'], 'nutrition-2026-11-09-r5')
            self.assertIs(care_config['automatic_repeats'], False)


if __name__ == '__main__':
    unittest.main()
