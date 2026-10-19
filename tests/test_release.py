import argparse
import copy
from pathlib import Path
import re
import unittest

import care
import migrate

ROOT = Path(__file__).resolve().parents[1]


class ReleaseContractTests(unittest.TestCase):
    def test_example_has_the_requested_three_meals_and_early_quiet_end(self):
        state = care.initial()
        self.assertEqual([s['time'] for s in state['config']['slots']], ['07:00', '14:00', '19:00'])
        self.assertEqual(state['config']['quiet_end'], '07:00')
        self.assertEqual(state['config']['timezone'], 'Europe/London')

    def test_container_uses_unprivileged_identity_and_whitelisted_runtime_sources(self):
        text = (ROOT / 'Dockerfile').read_text()
        self.assertIn('USER 10001:10001', text)
        self.assertIn('ca-certificates tzdata', text)
        self.assertIn('EKORN_STATE_PATH=/data/state.json', text)
        self.assertIn('CMD ["python3", "service.py"]', text)
        self.assertNotRegex(text, r'(?m)^COPY\s+(?:\.\s|\*\s)')
        for source in ('care.py', 'configuration.py', 'inline_ui.py', 'migrate.py', 'service.py', 'storage.py', 'telegram_api.py'):
            self.assertIn(source, text)
            self.assertIn('!' + source, (ROOT / '.dockerignore').read_text())

    def test_workflow_pins_actions_and_publishes_both_architectures_with_commit_tags(self):
        text = (ROOT / '.github/workflows/ci.yaml').read_text()
        actions = re.findall(r'uses:\s+([^\s#]+)', text)
        self.assertTrue(actions)
        self.assertTrue(all(re.fullmatch(r'[^@]+@[0-9a-f]{40}', action) for action in actions))
        self.assertIn('linux/amd64,linux/arm64', text)
        self.assertIn('$IMAGE:sha-$GITHUB_SHA', text)
        self.assertIn('packages: write', text)
        self.assertIn('github.event_name != \'pull_request\'', text)
        self.assertNotIn('TELEGRAM_BOT_TOKEN', text)

    def test_import_preserves_the_pending_0700_change_and_original_confirmation(self):
        state = care.initial()
        state['config']['quiet_end'] = '08:00'
        state['config']['slots'][0]['time'] = '09:30'
        state['schedule_history'][0]['slots'] = copy.deepcopy(state['config']['slots'])
        at = care.instant('2026-10-19T09:30:00+01:00')
        care.tick(state, at)
        args = argparse.Namespace(checklist='2026-10-19_09:30', types=['wet'], action='done', actor='test_owner',
                                  minutes=None, message_id='fixture', original_message_id=None)
        care.update(state, args, at)
        original = copy.deepcopy(state['tasks']['2026-10-19_09:30_wet'])
        slots = copy.deepcopy(state['config']['slots'])
        slots[0]['time'] = '07:00'
        state['pending_config'] = {'slots': slots, 'effective': '2026-10-20', 'quiet_end': '07:00'}
        state['schedule_history'].append({'effective': '2026-10-20', 'slots': slots})
        imported = migrate.migrate(state, 101, at)
        self.assertEqual(imported['pending_config'], state['pending_config'])
        self.assertEqual(care.tick(imported, care.instant('2026-10-20T06:59:00+01:00')), 'NO_REPLY')
        text = care.tick(imported, care.instant('2026-10-20T07:00:00+01:00'))
        self.assertIn('20 октября · 07:00', text)
        self.assertEqual(imported['tasks']['2026-10-19_09:30_wet'], original)
        self.assertNotIn('2026-10-20_09:30_wet', imported['tasks'])
        self.assertNotIn('2026-10-19_07:00_wet', imported['tasks'])
        self.assertEqual(imported['tasks']['2026-10-20_07:00_wet']['grams'], 49)


if __name__ == '__main__':
    unittest.main()
