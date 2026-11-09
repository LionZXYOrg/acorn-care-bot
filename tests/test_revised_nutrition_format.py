"""Presentation tests use synthetic portions, not the installed feeding plan."""
import copy
import tempfile
import unittest
from datetime import date
from pathlib import Path

import care
import inline_ui
from configuration import Runtime
from service import Bot, chunks
from storage import Store
from test_service import FakeClient, CHAT, TOPIC, BOT_ID


PRODUCTS = {
    'wet': {'name': 'Royal Canin Gastrointestinal Kitten Wet',
            'label': 'Старый влажный', 'button_label': 'Старый влажный', 'pack_grams': 195},
    'wet_new': {'name': 'Royal Canin Kitten in Jelly',
                'label': 'Новый влажный', 'button_label': 'Новый влажный', 'pack_grams': 85},
    'dry': {'name': 'Royal Canin Gastrointestinal Kitten Dry',
            'label': 'Старый сухой', 'button_label': 'Старый сухой'},
    'dry_new': {'name': 'Royal Canin Kitten Dry',
                'label': 'Новый сухой', 'button_label': 'Новый сухой'},
}


def state():
    result = care.initial()
    stage = {'id': 'synthetic', 'start': '2026-11-23', 'label': 'Тестовый этап',
             'wet': [11, 12, 13], 'wet_new': [14, 15, 16], 'dry': [20, 21, 22],
             'dry_new': [0, 0, 0], 'morning_notice': True,
             'advance_notice': 'Завтра новые порции.'}
    stage.update({'daily_' + kind: sum(stage[kind]) for kind in care.FOOD_TYPES})
    result['config'].update(food_products=copy.deepcopy(PRODUCTS),
                            nutrition_plan_id='synthetic-format-only',
                            nutrition_notice_time='19:30', automatic_repeats=False,
                            food_transfer_policy='add_full', mention='@palmette @LionZXY')
    result['config']['stages'] = [stage]
    return result


def at(day='2026-11-23', hhmm='07:00'):
    return care.wall(date.fromisoformat(day), hhmm)


def checklist(s, index=0, day='2026-11-23'):
    day = date.fromisoformat(day)
    return care.ensure_checklist(s, day, care.slots_for_day(s, day)[index], index)


class RevisedNutritionFormatTests(unittest.TestCase):
    def test_exact_breakfast_header_food_rows_and_cleaning(self):
        s = state()
        cid = checklist(s)
        text = care.render_checklist(s, cid)
        self.assertTrue(text.startswith('@palmette @LionZXY\n\n🐾 Экорн · Завтрак\n'
                                        '📅 23 ноября · 07:00\n'))
        self.assertIn('\n🍽️ Кормление\n'
                      '☐ 11 г Gastrointestinal Kitten — старый влажный.\n'
                      '☐ 14 г Kitten in Jelly — новый влажный.\n'
                      '☐ 20 г Gastrointestinal Kitten — сухой.', text)
        self.assertIn('\n🧹 Утренний уход\n', text)
        for kind in ('litter', 'bowls', 'water'):
            self.assertIn(care.CLEANING[kind], text)
        self.assertNotIn('14:00', text)
        self.assertNotIn('19:00', text)
        self.assertNotIn('Суточная', text)
        self.assertNotIn('Тестовый этап', text)

    def test_only_first_breakfast_has_stage_change_banner(self):
        s = state()
        cid = checklist(s)
        banner = '🔄 Сегодня новые порции!'
        self.assertEqual(care.render_checklist(s, cid).count(banner), 1)
        self.assertNotIn(banner, care.render_checklist(s, cid, updated=True))
        for index, day in ((1, '2026-11-23'), (2, '2026-11-23'), (0, '2026-11-24')):
            text = care.render_checklist(s, checklist(s, index, day))
            self.assertNotIn(banner, text)
            self.assertNotIn('Тестовый этап', text)
        s['last_tick'] = care.iso(at(hhmm='06:00'))
        care.tick(s, at())
        self.assertNotIn(banner, care.render_checklist(s, cid))
        self.assertNotIn(banner, care.refresh(s, s['last_emission_id'], at(hhmm='07:01')))

    def test_lunch_has_no_cleaning_and_dinner_only_litter(self):
        s = state()
        lunch = care.render_checklist(s, checklist(s, 1))
        self.assertIn('🐾 Экорн · Обед\n📅 23 ноября · 14:00', lunch)
        self.assertNotIn('🧹', lunch)
        self.assertIn('☐ 12 г Gastrointestinal Kitten — старый влажный.', lunch)
        self.assertNotIn('11 г', lunch)
        dinner = care.render_checklist(s, checklist(s, 2))
        self.assertIn('🐾 Экорн · Ужин\n📅 23 ноября · 19:00', dinner)
        self.assertIn('🧹 Вечерний уход', dinner)
        self.assertIn(care.CLEANING['litter'], dinner)
        self.assertNotIn(care.CLEANING['bowls'], dinner)
        self.assertNotIn(care.CLEANING['water'], dinner)

    def test_done_skip_deferred_clarification_carries_and_actors(self):
        s = state()
        cid = checklist(s)
        wet = s['tasks'][cid + '_wet']
        wet.update(status='done', done_by='telegram:101', done_at=care.iso(at(hhmm='07:01')))
        care.remember_caregiver(s, {'id': 101, 'username': 'palmette'})
        new = s['tasks'][cid + '_wet_new']
        new.update(status='skipped', skip_by='LionZXY', skip_at=care.iso(at(hhmm='07:02')))
        dry = s['tasks'][cid + '_dry']
        care.add_carried_food(dry, {'source': 3})
        dry.update(status='deferred', snoozed_until=care.iso(at(hhmm='07:30')))
        text = care.render_checklist(s, cid, updated=True)
        self.assertIn('@palmette, 07:01', text)
        self.assertIn('Пропущено:', text)
        self.assertIn('@LionZXY, 07:02', text)
        self.assertIn('☐ 23 г Gastrointestinal Kitten — сухой.', text)
        self.assertIn('Включая перенос: 3 г.', text)
        self.assertIn('Отложено до 23.11 07:30.', text)
        care.mark_old_feedings(s, at(hhmm='14:00'))
        lunch = care.render_checklist(s, checklist(s, 1), updated=True)
        self.assertIn('Предыдущее кормление не подтверждено', lunch)
        self.assertIn('Не добавляйте их к новой порции.', lunch)
        self.assertIn('Нужно уточнить: была ли выдана порция',
                      care.render_checklist(s, cid, updated=True))

    def test_cleaning_transfer_to_lunch_and_food_transfer_keep_buttons(self):
        s = state()
        cid = checklist(s)
        care.move_to_next(s, cid + '_litter', 'palmette', at(hhmm='07:01'))
        care.move_to_next(s, cid + '_wet', 'palmette', at(hhmm='07:01'))
        target = checklist(s, 1)
        text = care.render_checklist(s, target)
        self.assertIn('🧹 Перенесённый уход', text)
        self.assertIn('☐ 23 г Gastrointestinal Kitten — старый влажный.', text)
        self.assertIn('Включая перенос: 11 г.', text)
        self.assertEqual(s['tasks'][cid + '_wet']['status'], 'skipped')
        self.assertIn('Весь объём добавлен к следующей порции.', care.render_checklist(s, cid))
        panel = inline_ui.prepare(s, chunks(text), s['checklists'][target]['task_ids'],
                                  at(hhmm='14:00'), primary=target)
        self.assertEqual(sum(len(p['bindings']) for p in panel['pages']), len(panel['task_ids']))
        self.assertEqual(len(inline_ui.keyboard(s, panel)['inline_keyboard']), len(panel['task_ids']))

    def test_inline_callback_updates_bound_row_and_removes_stage_banner(self):
        s = state()
        cid = checklist(s)
        panel = inline_ui.prepare(s, chunks(care.render_checklist(s, cid)),
                                  s['checklists'][cid]['task_ids'], at(), primary=cid)
        panel['status'] = 'sent'
        for page in panel['pages']:
            page['message_id'] = 777
        callback = {'id': 'format-test', 'from': {'id': 101, 'is_bot': False, 'username': 'palmette'},
                    'message': {'message_id': 777, 'chat': {'id': CHAT}, 'message_thread_id': TOPIC,
                                'from': {'id': BOT_ID, 'is_bot': True}},
                    'data': inline_ui.PREFIX + panel['nonce'] + ':0'}
        result = inline_ui.accept_callback(s, callback, {101}, BOT_ID,
                                           at(hhmm='07:01'), CHAT, TOPIC)
        self.assertFalse(result['show_alert'])
        text = '\n'.join(inline_ui.page_text(s, panel, page) for page in panel['pages'])
        self.assertIn('✅ Royal Canin Gastrointestinal Kitten Wet, 11 г: @palmette, 07:01.', text)
        self.assertIn('☐ 14 г Kitten in Jelly — новый влажный.', text)
        self.assertNotIn('🔄 Сегодня новые порции!', text)
        before = copy.deepcopy(s)
        inline_ui.accept_callback(s, callback, {101}, BOT_ID, at(hhmm='07:02'), CHAT, TOPIC)
        self.assertEqual(s, before)

    def test_notice_exact_totals_mentions_and_dedup(self):
        s = state()
        s['last_tick'] = care.iso(at('2026-11-22', '06:00'))
        care.tick(s, at('2026-11-22', '19:00'))
        text = care.tick(s, at('2026-11-22', '19:30'))
        self.assertTrue(text.startswith('@palmette @LionZXY\n\n🔄 Экорн · Завтра меняем порции!'))
        self.assertIn('- 36 г старого влажного.\n- 45 г нового влажного.\n- 63 г старого сухого.', text)
        self.assertTrue(text.endswith('🐾 С завтрашнего завтрака кормим по новым граммовкам!'))
        self.assertEqual(care.tick(s, at('2026-11-22', '19:31')), 'NO_REPLY')

    def test_food_products_without_plan_id_also_use_new_format(self):
        s = state()
        del s['config']['nutrition_plan_id']
        self.assertIn('☐ 11 г Gastrointestinal Kitten — старый влажный.',
                      care.render_checklist(s, checklist(s)))

    def test_first_delivery_keeps_banner_but_later_panel_refresh_drops_it(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / 'state.json')
            store.bind(BOT_ID, CHAT, TOPIC, at(hhmm='06:00'), fresh_start=True)
            with store.transaction() as saved:
                saved['config'] = state()['config']
            client = FakeClient()
            bot = Bot(Runtime(CHAT, TOPIC, frozenset({101}), store.path), client,
                      BOT_ID, 'example_care_bot', store, lambda: at())
            bot.dispatch_once()
            sends = [p for method, p in client.calls if method == 'sendMessage']
            self.assertTrue(sends)
            self.assertIn('🔄 Сегодня новые порции!', sends[0]['text'])
            with store.transaction() as saved:
                care.mark_old_feedings(saved, at(hhmm='07:01'))
                panel = next(iter(saved['inline_panels'].values()))
                self.assertNotIn('🔄 Сегодня новые порции!', inline_ui.page_text(saved, panel, panel['pages'][0]))


if __name__ == '__main__':
    unittest.main()
