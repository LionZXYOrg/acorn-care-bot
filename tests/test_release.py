import argparse
import copy
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import care
import migrate

ROOT = Path(__file__).resolve().parents[1]


class ReleaseContractTests(unittest.TestCase):
    def test_tracked_paths_and_text_have_no_retired_name(self):
        # Escape retired spellings so this test does not keep them in tracked text.
        retired = ('\u0065\u006b\u006f\u0072\u006e', '\u044d\u043a\u043e\u0440\u043d')
        paths = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
        self.assertIn('config.example.json', paths)
        self.assertIn('service.py', paths)
        for name in filter(None, paths):
            with self.subTest(path=name):
                text = (ROOT / name).read_text(encoding='utf-8').casefold()
                for spelling in retired:
                    self.assertNotIn(spelling, name.casefold())
                    self.assertNotIn(spelling, text)

    def test_configured_cat_name_and_messages_use_acorn(self):
        config = json.loads((ROOT / 'config.example.json').read_text())
        self.assertEqual(config['name'], 'Acorn')
        state = care.initial()
        self.assertEqual(state['config']['name'], 'Acorn')
        text = care.tick(state, care.instant('2026-10-19T07:00:00+01:00'))
        self.assertTrue(text.startswith('🐾 Acorn · '))
        self.assertIn('Пора покормить Acorn и немного прибраться.', text)

    def test_example_has_the_requested_three_meals_and_early_quiet_end(self):
        state = care.initial()
        self.assertEqual([s['time'] for s in state['config']['slots']], ['07:00', '14:00', '19:00'])
        self.assertEqual(state['config']['quiet_end'], '07:00')
        self.assertEqual(state['config']['timezone'], 'Europe/London')

    def test_container_uses_unprivileged_identity_and_whitelisted_runtime_sources(self):
        text = (ROOT / 'Dockerfile').read_text()
        self.assertIn('USER 10001:10001', text)
        self.assertIn('ca-certificates tzdata', text)
        self.assertIn('ACORN_CONFIG_PATH=/app/config.example.json', text)
        self.assertIn('ACORN_STATE_PATH=/data/state.json', text)
        self.assertIn('groupadd --gid 10001 acorn', text)
        self.assertIn('--home-dir /app acorn', text)
        self.assertIn('CMD ["python3", "service.py"]', text)
        self.assertNotRegex(text, r'(?m)^COPY\s+(?:\.\s|\*\s)')
        for source in ('care.py', 'configuration.py', 'inline_ui.py', 'migrate.py', 'service.py', 'storage.py', 'telegram_api.py'):
            self.assertIn(source, text)
            self.assertIn('!' + source, (ROOT / '.dockerignore').read_text())

    def test_greenwich_image_contains_only_the_worker_sources(self):
        text = (ROOT / 'Dockerfile').read_text()
        self.assertIn('PYTHONPATH=/app', text)
        self.assertIn('COPY --chown=10001:10001 greenwich/ ./greenwich/', text)
        self.assertFalse((ROOT / 'greenwich' / '__init__.py').exists())
        rules = (ROOT / '.dockerignore').read_text().splitlines()
        greenwich_rules = [rule for rule in rules if 'greenwich' in rule]
        self.assertEqual(greenwich_rules, [
            '!greenwich/', 'greenwich/*', '!greenwich/bot.py', '!greenwich/council.py',
            '!greenwich/migrate_storage.py', '!greenwich/runner.py', '!greenwich/storage.py',
        ])
        for name in ('bot.py', 'council.py', 'migrate_storage.py', 'runner.py', 'storage.py'):
            with self.subTest(source=name):
                self.assertTrue((ROOT / 'greenwich' / name).is_file())

    def test_greenwich_scripts_keep_local_imports_and_find_the_shared_gateway(self):
        # Reproduce /app without test modules or the checkout on the import path.
        with tempfile.TemporaryDirectory(prefix='acorn-image-layout-') as temporary:
            base = Path(temporary)
            app = base / 'app'
            (app / 'greenwich').mkdir(parents=True)
            for name in ('storage.py', 'gateway_client.py', 'telegram_api.py'):
                shutil.copyfile(ROOT / name, app / name)
            for name in ('bot.py', 'council.py', 'migrate_storage.py', 'runner.py', 'storage.py'):
                shutil.copyfile(ROOT / 'greenwich' / name, app / 'greenwich' / name)
            property_path = base / 'invented-property.json'
            property_path.write_text(json.dumps({
                'address_label': '9001 - Example Test Street - Sample Town - ZZ1 1ZZ',
                'property': {'sao': '', 'pao': '9001', 'streetName': 'Example Test Street',
                             'postTown': 'Sample Town', 'Postcode': 'ZZ1 1ZZ'},
            }))
            environment = {
                'PYTHONPATH': str(app), 'PYTHONDONTWRITEBYTECODE': '1',
                'BINS_STATE_PATH': str(base / 'state.json'),
                'BINS_RUNNER_STATE_PATH': str(base / 'attempts.json'),
                'BINS_PROPERTY_PATH': str(property_path),
                'TELEGRAM_GATEWAY_SOCKET': str(base / 'missing.sock'),
                'TELEGRAM_BOT_TOKEN_FILE': str(base / 'missing-token'),
            }
            def execute(script, *arguments):
                return subprocess.run(
                    [sys.executable, str(app / 'greenwich' / script), *arguments],
                    cwd=base, env=environment, text=True, capture_output=True, timeout=10,
                )

            status = execute('bot.py', 'status')
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(status.stderr, '')
            self.assertEqual(json.loads(status.stdout)['status'], 'ok')
            # A missing synthetic Unix socket must reach the real shared client,
            # not fail its import or fall back to a token or the Telegram API.
            verify = execute('bot.py', 'verify')
            self.assertEqual(verify.returncode, 1, verify.stderr)
            self.assertEqual(verify.stderr, '')
            self.assertEqual(json.loads(verify.stdout), {
                'status': 'error',
                'description': 'The gateway did not confirm the request. Automatic send retries are blocked.',
            })
            runner = execute('runner.py', 'invalid-command')
            self.assertEqual((runner.returncode, runner.stdout, runner.stderr), (1, '', ''))
            self.assertFalse((base / 'attempts.json').exists())
            migration = execute('migrate_storage.py', '--help')
            self.assertEqual(migration.returncode, 0, migration.stderr)
            self.assertIn('--confirm-workers-stopped', migration.stdout)

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
