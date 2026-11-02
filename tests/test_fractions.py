import copy
import unittest
from datetime import date

import care
import inline_ui


class WetFoodDisplayTests(unittest.TestCase):
    def setUp(self):
        care.BUTTON_BOT = '@example_care_bot'

    def test_legacy_fractions_keep_rounding_visible(self):
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
                self.assertEqual(care._legacy_wet_fraction(grams, 195), text)

    def test_legacy_exact_fractions_have_no_approximation_marker(self):
        self.assertEqual(care._legacy_wet_fraction('48.75', 195), '1/4 банки')
        self.assertEqual(care._legacy_wet_fraction('24.375', 195), '1/8 банки')
        self.assertEqual(care._legacy_wet_fraction('97.5', 195), '1/2 банки')

    def test_legacy_unplanned_portion_is_not_rounded_to_a_different_allowance(self):
        self.assertEqual(care._legacy_wet_fraction(19, 195), '19/195 банки')
        self.assertNotEqual(care._legacy_wet_fraction(33, 195), '≈1/8 банки')
        self.assertNotEqual(care._legacy_wet_fraction(32, 195), '≈1/8 банки')

    def test_legacy_can_size_and_whole_can_grammar(self):
        self.assertEqual(care._legacy_wet_fraction(50, 100), '1/2 банки')
        self.assertEqual(care._legacy_wet_fraction(195, 195), '1 банка')
        self.assertEqual(care._legacy_wet_fraction(195, 195, accusative=True), '1 банку')
        self.assertEqual(care._legacy_wet_fraction(975, 195), '5 банок')
        with self.assertRaises(ValueError):
            care._legacy_wet_fraction(49, 0)

    def test_gram_display_preserves_exact_weights(self):
        for grams in (0, 19, 24, 32, 33, 49, 65, 97, 98, 195, 975):
            with self.subTest(grams=grams):
                self.assertEqual(care.wet_amount(grams, 195), f'{grams} г')
        self.assertEqual(care.wet_amount('48.75', 195), '48.75 г')
        self.assertEqual(care.wet_amount('24.375', 195), '24.375 г')
        self.assertEqual(care.wet_amount(65, 100, accusative=True), '65 г')
        for grams, can in ((-1, 195), (49, 0)):
            with self.assertRaises(ValueError):
                care.wet_amount(grams, can)

    def test_old_fraction_button_still_closes_only_its_task(self):
        state = care.initial()
        care.tick(state, care.instant('2026-10-19T07:00:00+01:00'))
        label = '✅ Влажный ≈1/4 банки · 19.10.2026 07:00 @example_care_bot'
        result = care.complete_button(state, label, 'caregiver_a', 'fraction-legacy',
                                      care.instant('2026-10-19T07:01:00+01:00'))
        task = state['tasks']['2026-10-19_07:00_wet']
        self.assertEqual(task['status'], 'done')
        self.assertEqual(task['grams'], 49)
        self.assertEqual(sum(t['status'] == 'done' for t in state['tasks'].values()), 1)
        self.assertIn('Влажный корм, 49 г', result)
        self.assertNotIn('банки', result)

    def test_week_three_displays_distinct_weights_without_changing_the_plan(self):
        state = care.initial()
        original = copy.deepcopy(state['config'])
        day = date(2026, 11, 2)
        for slot, index, grams in zip(state['config']['slots'], range(3), (33, 32, 65)):
            with self.subTest(slot=slot['key']):
                cid = care.ensure_checklist(state, day, slot, index)
                before = copy.deepcopy(state)
                text = care.render_checklist(state, cid)
                self.assertIn(f'Дать {grams} г влажного корма.', text)
                self.assertNotIn('банки', text)
                self.assertIn(f'Влажный {grams} г', care.button_text(state['tasks'][cid + '_wet']))
                self.assertEqual(state, before)
        self.assertEqual(state['config'], original)

    def test_matching_fraction_labels_keep_different_meal_weights_separate(self):
        state = care.initial()
        day = date(2026, 11, 2)
        first = care.ensure_checklist(state, day, state['config']['slots'][0], 0)
        second = care.ensure_checklist(state, day, state['config']['slots'][1], 1)
        label = '✅ Влажный ≈1/6 банки · 02.11.2026 07:00 @example_care_bot'
        care.complete_button(state, label, 'caregiver_a', 'old-third-week',
                             care.instant('2026-11-02T14:01:00+00:00'))
        self.assertEqual(state['tasks'][first + '_wet']['grams'], 33)
        self.assertEqual(state['tasks'][first + '_wet']['status'], 'done')
        self.assertEqual(state['tasks'][second + '_wet']['grams'], 32)
        self.assertNotEqual(state['tasks'][second + '_wet']['status'], 'done')
        before = copy.deepcopy(state)
        care.complete_button(state, label, 'caregiver_b', 'old-third-week-again',
                             care.instant('2026-11-02T14:02:00+00:00'))
        self.assertEqual(state['tasks'], before['tasks'])
        self.assertEqual(state['config'], before['config'])
        self.assertEqual(state['audit'][-1]['task_ids'], [])

    def test_future_and_unknown_fraction_buttons_do_not_change_state(self):
        state = care.initial()
        care.ensure_checklist(state, date(2026, 10, 19), state['config']['slots'][0], 0)
        before = copy.deepcopy(state)
        for label in (
                '✅ Влажный ≈1/4 банки · 19.10.2026 07:00 @example_care_bot',
                '✅ Влажный ≈1/4 банки · 20.10.2026 07:00 @example_care_bot'):
            with self.subTest(label=label), self.assertRaises(ValueError):
                care.complete_button(state, label, 'caregiver_a', 'invalid-fraction',
                                     care.instant('2026-10-19T06:59:00+01:00'))
            self.assertEqual(state, before)

    def test_display_does_not_change_plan_or_task_record(self):
        state = care.initial()
        care.tick(state, care.instant('2026-10-19T07:00:00+01:00'))
        before = copy.deepcopy(state)
        cid = '2026-10-19_07:00'
        text = care.render_checklist(state, cid)
        task = state['tasks'][cid + '_wet']
        self.assertIn('Дать 49 г влажного корма.', text)
        self.assertIn('Дать 10 г сухого корма.', text)
        self.assertIn('19:00: 97 г влажного + 10 г сухого.', text)
        self.assertIn('Влажный 49 г', care.button_text(task))
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

    def test_inline_wet_label_uses_grams_and_unchanged_task_index(self):
        state = care.initial()
        now = care.instant('2026-10-19T07:00:00+01:00')
        care.tick(state, now)
        cid = '2026-10-19_07:00'
        keys = state['checklists'][cid]['task_ids']
        panel = inline_ui.prepare(state, [care.render_checklist(state, cid)], keys, now, primary=cid)
        button = inline_ui.keyboard(state, panel)['inline_keyboard'][0][0]
        self.assertIn('49 г', button['text'])
        self.assertNotIn('бан', button['text'])
        self.assertEqual(button['callback_data'], inline_ui.PREFIX + panel['nonce'] + ':0')


if __name__ == '__main__':
    unittest.main()
