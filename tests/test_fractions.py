import copy
import unittest
from datetime import date

import care
import inline_ui


class FractionDisplayTests(unittest.TestCase):
    def setUp(self):
        care.BUTTON_BOT = '@example_care_bot'

    def test_simple_fractions_keep_rounding_visible(self):
        expected = {
            49: '≈1/4 банки',
            97: '≈1/2 банки',
            24: '≈1/8 банки',
            33: '≈1/6 банки',
            32: '≈1/6 банки',
            65: '1/3 банки',
        }
        for grams, text in expected.items():
            with self.subTest(grams=grams):
                self.assertEqual(care.wet_amount(grams, 195), text)

    def test_exact_fractions_have_no_approximation_marker(self):
        self.assertEqual(care.wet_amount('48.75', 195), '1/4 банки')
        self.assertEqual(care.wet_amount('24.375', 195), '1/8 банки')
        self.assertEqual(care.wet_amount('97.5', 195), '1/2 банки')

    def test_unplanned_portion_is_not_rounded_to_a_different_allowance(self):
        self.assertEqual(care.wet_amount(19, 195), '19/195 банки')
        self.assertNotEqual(care.wet_amount(33, 195), '≈1/8 банки')
        self.assertNotEqual(care.wet_amount(32, 195), '≈1/8 банки')

    def test_can_size_and_whole_can_grammar(self):
        self.assertEqual(care.wet_amount(50, 100), '1/2 банки')
        self.assertEqual(care.wet_amount(195, 195), '1 банка')
        self.assertEqual(care.wet_amount(195, 195, accusative=True), '1 банку')
        self.assertEqual(care.wet_amount(975, 195), '5 банок')
        with self.assertRaises(ValueError):
            care.wet_amount(49, 0)

    def test_display_does_not_change_plan_or_task_record(self):
        state = care.initial()
        care.tick(state, care.instant('2026-10-19T07:00:00+01:00'))
        before = copy.deepcopy(state)
        cid = '2026-10-19_07:00'
        text = care.render_checklist(state, cid)
        task = state['tasks'][cid + '_wet']
        self.assertIn('Дать ≈1/4 банки влажного корма.', text)
        self.assertIn('Дать 10 г сухого корма.', text)
        self.assertIn('19:00: ≈1/2 банки влажного + 10 г сухого.', text)
        self.assertIn('Влажный ≈1/4 банки', care.button_text(task))
        self.assertEqual(before, state)
        self.assertEqual(task['grams'], 49)

    def test_legacy_gram_button_still_closes_exactly_one_task(self):
        state = care.initial()
        now = care.instant('2026-10-19T07:01:00+01:00')
        care.tick(state, care.instant('2026-10-19T07:00:00+01:00'))
        label = '✅ Влажный 49 г · 19.10.2026 07:00 @example_care_bot'
        care.complete_button(state, label, 'caregiver_b', 'legacy-test', now)
        self.assertEqual(sum(task['status'] == 'done' for task in state['tasks'].values()), 1)
        task = state['tasks']['2026-10-19_07:00_wet']
        self.assertEqual(task['grams'], 49)
        self.assertIsNone(task['next_repeat_at'])
        care.complete_button(state, care.button_text(task), 'caregiver_a', 'second-test', now)
        self.assertEqual(task['done_by'], 'caregiver_b')

    def test_all_stage_displays_keep_original_gram_portions(self):
        state = care.initial()
        original = copy.deepcopy(state['config'])
        for stage in state['config']['stages']:
            day = date.fromisoformat(stage['start'])
            for index, slot in enumerate(state['config']['slots']):
                cid = care.ensure_checklist(state, day, slot, index)
                care.render_checklist(state, cid)
                wet = state['tasks'].get(cid + '_wet')
                if stage['wet'][index]:
                    self.assertEqual(wet['grams'], stage['wet'][index])
                    self.assertEqual(wet['wet_can_grams'], 195)
                else:
                    self.assertIsNone(wet)
        self.assertEqual(original, state['config'])

    def test_inline_wet_label_uses_fractions_and_unchanged_task_index(self):
        state = care.initial()
        now = care.instant('2026-10-19T07:00:00+01:00')
        care.tick(state, now)
        cid = '2026-10-19_07:00'
        keys = state['checklists'][cid]['task_ids']
        panel = inline_ui.prepare(state, [care.render_checklist(state, cid)], keys, now, primary=cid)
        button = inline_ui.keyboard(state, panel)['inline_keyboard'][0][0]
        self.assertIn('≈1/4 банки', button['text'])
        self.assertNotIn('49 г', button['text'])
        self.assertEqual(button['callback_data'], inline_ui.PREFIX + panel['nonce'] + ':0')


if __name__ == '__main__':
    unittest.main()
