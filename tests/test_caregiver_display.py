import copy
import json
import unittest

import care
import inline_ui
from service import chunks

NOW = care.instant('2026-10-19T07:01:00+01:00')
CID = '2026-10-19_07:00'


class CaregiverDisplayTests(unittest.TestCase):
    def setUp(self):
        self.state = care.initial()
        care.tick(self.state, NOW)
        self.key = CID + '_wet'
        care.complete_button(self.state, care.button_text(self.state['tasks'][self.key]),
                             'telegram:101', 'test', NOW)

    def test_username_replaces_id_without_changing_saved_care(self):
        before = copy.deepcopy(self.state)
        care.remember_caregiver(self.state, {'id': 101, 'username': 'caregiver_a',
                                             'first_name': 'Alice'})
        text = care.render_checklist(self.state, CID, updated=True)
        self.assertIn(': @caregiver_a, 07:01.', text)
        self.assertNotIn('telegram:101', text)
        self.assertEqual({k: v for k, v in self.state.items() if k != 'caregivers'}, before)
        restored = json.loads(json.dumps(self.state))
        self.assertEqual(care.display_actor(restored, 'telegram:101'), '@caregiver_a')
        self.assertEqual(care.display_actor(restored, '101'), '@caregiver_a')

    def test_missing_username_uses_name_and_missing_profile_hides_id(self):
        self.assertEqual(care.display_actor(self.state, 'telegram:101'), 'участник')
        care.remember_caregiver(self.state, {'id': 101, 'first_name': 'Алиса', 'last_name': 'Иванова'})
        self.assertEqual(care.display_actor(self.state, 'telegram:101'), 'Алиса Иванова')
        self.assertNotIn('telegram:101', care.render_checklist(self.state, CID))

    def test_renamed_or_removed_username_updates_display_not_original_author(self):
        task = copy.deepcopy(self.state['tasks'][self.key])
        for user, expected in [({'username': 'old_name'}, '@old_name'),
                               ({'username': 'new_name'}, '@new_name'),
                               ({'first_name': 'Alice'}, 'Alice'), ({}, 'участник')]:
            care.remember_caregiver(self.state, {'id': 101, **user})
            self.assertEqual(care.display_actor(self.state, task['done_by']), expected)
            self.assertEqual(self.state['tasks'][self.key], task)

    def test_invalid_username_does_not_inject_lines_or_mentions(self):
        for username in ('@fake', 'name\n@other', '<b>name</b>', 'a' * 33, 123, None):
            with self.subTest(username=username):
                care.remember_caregiver(self.state, {'id': 101, 'username': username,
                                                     'first_name': 'Alice'})
                self.assertEqual(care.display_actor(self.state, 'telegram:101'), 'Alice')

    def test_names_are_single_line_without_bidi_controls_or_lone_surrogates(self):
        care.remember_caregiver(self.state, {'id': 101, 'first_name': 'A\nB\tC\u202eD\ud800'})
        self.assertEqual(care.display_actor(self.state, 'telegram:101'), 'A B CD')
        self.assertEqual(care.caregiver_name('😀' * 100), '😀' * 32)
        self.assertEqual(care.caregiver_name('😀' * 100, max_units=33), '😀' * 16)

    def test_imported_username_is_displayed_with_one_at_sign(self):
        for actor in ('caregiver_a', '@caregiver_a'):
            self.assertEqual(care.display_actor(self.state, actor), '@caregiver_a')

    def test_profile_resolution_updates_existing_panel_without_rebinding(self):
        text = care.render_checklist(self.state, CID)
        keys = self.state['checklists'][CID]['task_ids']
        panel = inline_ui.prepare(self.state, chunks(text), keys, NOW, primary=CID)
        before = copy.deepcopy(panel)
        old_digest = inline_ui.digest(self.state, panel)
        care.remember_caregiver(self.state, {'id': 101, 'username': 'caregiver_a'})
        self.assertNotEqual(old_digest, inline_ui.digest(self.state, panel))
        self.assertEqual(panel, before)
        rendered = '\n'.join(inline_ui.page_text(self.state, panel, page) for page in panel['pages'])
        self.assertIn('@caregiver_a', rendered)
        self.assertNotIn('telegram:101', rendered)

    def test_new_panels_reserve_space_for_maximum_utf16_names(self):
        self.state['tasks'][self.key]['status'] = 'pending'
        keys = self.state['checklists'][CID]['task_ids']
        text = care.render_checklist(self.state, CID)
        panel = inline_ui.prepare(self.state, chunks(text), keys, NOW, primary=CID)
        care.remember_caregiver(self.state, {'id': 101, 'first_name': '😀' * 100})
        for key in keys:
            self.state['tasks'][key].update(status='done', done_by='telegram:101', done_at=care.iso(NOW))
        for page in panel['pages']:
            rendered = inline_ui.page_text(self.state, panel, page)
            self.assertLessEqual(len(rendered.encode('utf-16-le')) // 2, inline_ui.MESSAGE_LIMIT)
            self.assertNotIn('telegram:101', rendered)


if __name__ == '__main__':
    unittest.main()
