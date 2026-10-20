#!/usr/bin/env python3
"""Persistent, shared care checklists. No network access or credentials."""
import argparse
import copy
import fcntl
import json
import os
import re
import sys
import tempfile
import uuid
import unicodedata
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from fractions import Fraction
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get('ACORN_CONFIG_PATH', ROOT / 'config.example.json'))
STATE = Path(os.environ.get('ACORN_STATE_PATH', '/data/state.json'))
from configuration import load_care_config
BOOT_CONFIG = load_care_config(CONFIG_PATH)
TZ = ZoneInfo(BOOT_CONFIG['timezone'])
DEFAULT_WET_CAN_GRAMS = BOOT_CONFIG['wet_can_grams']
CLOSED = {'done', 'skipped'}
MAX_CAREGIVER_LABEL_UNITS = 64
CLEANING = {
    'litter': 'Почистить лоток и убрать мусор вокруг.',
    'bowls': 'Помыть использованные миски и протереть место кормления.',
    'water': 'Налить свежую питьевую воду.',
}
MONTHS = ['', 'января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
          'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря']


def iso(dt):
    return dt.isoformat(timespec='seconds')


def instant(value=None):
    if value is None:
        return datetime.now(TZ)
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError('Нужно указать часовой пояс в метке времени.')
    return dt.astimezone(TZ)


def wall(day, hhmm):
    return datetime.combine(day, time.fromisoformat(hhmm), TZ)


def due(task):
    return instant(task['due_at'])


def plus_minutes(dt, count):
    # Use elapsed time across seasonal clock changes.
    return datetime.fromtimestamp(dt.timestamp() + count * 60, TZ)


def quiet(dt, config):
    value = dt.strftime('%H:%M')
    return value >= config['quiet_start'] or value < config['quiet_end']


def allowed_time(dt, config):
    if dt.strftime('%H:%M') >= config['quiet_start']:
        return wall(dt.date() + timedelta(days=1), config['quiet_end'])
    if dt.strftime('%H:%M') < config['quiet_end']:
        return wall(dt.date(), config['quiet_end'])
    return dt


def initial():
    config = copy.deepcopy(BOOT_CONFIG)
    return {
        'version': 1, 'config': config, 'checklists': {}, 'tasks': {},
        'current_stage': 'weeks_1_2', 'progression_hold': None,
        'reminder_pause': None, 'proposals': {}, 'audit': [],
        'morning_summary_date': None, 'vet_notice_for': None,
        'last_tick': None, 'revision': 0,
        'schedule_history': [{'effective': config['start_date'], 'slots': copy.deepcopy(config['slots'])}],
    }


def save(path, state):
    handle, name = tempfile.mkstemp(prefix='.state-', dir=path.parent)
    try:
        with os.fdopen(handle, 'w') as out:
            json.dump(state, out, ensure_ascii=False, indent=2)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def locked(path=STATE):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(path.read_text()) if path.exists() else initial()
        yield state
        save(path, state)


def audit(s, kind, actor, now, **extra):
    s['audit'].append({'kind': kind, 'actor': actor, 'at': iso(now), **extra})


def stage_for(s, day):
    stages = s['config']['stages']
    hold = s.get('progression_hold')
    if hold and day >= date.fromisoformat(hold['since'][:10]):
        return next(x for x in stages if x['id'] == hold['stage'])
    eligible = [x for x in stages if date.fromisoformat(x['start']) <= day]
    return eligible[-1] if eligible else stages[0]


def day_label(day):
    return f'{day.day} {MONTHS[day.month]}'


def stage_label(stage, day, s):
    if stage['id'] == 'weeks_1_2':
        days = (day - date.fromisoformat(stage['start'])).days
        text = 'Неделя 1' if days < 7 else 'Неделя 2'
    else:
        text = stage['label']
    if s.get('progression_hold'):
        text = stage['label'] + ' · смена этапов приостановлена'
    return text


def checklist_id(day, slot):
    return f'{day.isoformat()}_{slot["time"]}'


def ensure_checklist(s, day, slot, index):
    cid = checklist_id(day, slot)
    if cid in s['checklists']:
        apply_carryovers(s, cid)
        return cid
    stage = stage_for(s, day)
    checklist = {
        'id': cid, 'date': day.isoformat(), 'time': slot['time'],
        'slot': slot['key'], 'label': slot['label'], 'stage': copy.deepcopy(stage),
        'task_ids': [], 'announced_at': None, 'original_message_id': None,
    }
    entries = []
    if stage['wet'][index]:
        entries.append(('wet', stage['wet'][index]))
    if stage['dry'][index]:
        entries.append(('dry', stage['dry'][index]))
    if slot['cleaning']:
        entries.extend((key, None) for key in slot.get('cleaning_tasks', CLEANING))
    at = wall(day, slot['time'])
    for kind, grams in entries:
        tid = f'{cid}_{kind}'
        task = {
            'id': tid, 'checklist': cid, 'type': kind, 'grams': grams,
            'due_at': iso(at), 'status': 'pending', 'needs_clarification': False,
            'next_repeat_at': None, 'snoozed_until': None,
            'done_by': None, 'done_at': None, 'skip_by': None, 'skip_at': None,
        }
        if kind == 'wet':
            task['wet_can_grams'] = s['config']['wet_can_grams']
        s['tasks'][tid] = task
        checklist['task_ids'].append(tid)
    s['checklists'][cid] = checklist
    apply_carryovers(s, cid)
    return cid


def slots_for_day(s, day):
    history = s.get('schedule_history', [])
    active = [item for item in history if item['effective'] <= day.isoformat()]
    if active:
        return active[-1]['slots']
    return s['config']['slots']


def next_care(s, now):
    """Find the next planned checklist, including a confirmed schedule change."""
    for offset in range(3):
        day = now.date() + timedelta(days=offset)
        if day.isoformat() < s['config']['start_date']:
            continue
        candidates = [(wall(day, slot['time']), index, slot)
                      for index, slot in enumerate(slots_for_day(s, day))
                      if wall(day, slot['time']) > now]
        if candidates:
            at, index, slot = min(candidates, key=lambda item: item[0])
            return at, index, slot
    raise ValueError('Следующий уход не найден. Пункт не изменён.')


def food_grams(value):
    """Keep whole grams integral and add decimal portions without float drift."""
    value = Fraction(str(value))
    return int(value) if value.denominator == 1 else float(value)


def add_carried_food(task, portions):
    """Add each source portion once, including earlier carries in that source."""
    if not portions:
        return
    if task['status'] in CLOSED:
        raise ValueError('Пункт назначения уже закрыт. Перенос сохранён для проверки.')
    carried = task.setdefault('carried_portions', {})
    task.setdefault('planned_grams', task['grams'])
    for source, grams in portions.items():
        if source not in carried:
            carried[source] = grams
    total = sum((Fraction(str(value)) for value in carried.values()), Fraction(0))
    task['carried_grams'] = food_grams(total)
    task['grams'] = food_grams(Fraction(str(task['planned_grams'])) + total)


def apply_carryovers(s, cid):
    """Apply one-time tasks and explicitly recorded food amounts at their due time."""
    checklist = s['checklists'][cid]
    queue = s.get('carryovers', {})
    for key, item in list(queue.items()):
        if item['checklist'] != cid:
            continue
        task = s['tasks'].get(key)
        food = item['type'] in {'wet', 'dry'}
        portions = item.get('food_grams', {})
        if task is None and (item['type'] in CLEANING or (food and portions)):
            task = {'id': key, 'checklist': cid, 'type': item['type'], 'grams': 0 if food else None,
                    'due_at': item['due_at'], 'status': 'pending', 'needs_clarification': False,
                    'next_repeat_at': None, 'snoozed_until': None,
                    'done_by': None, 'done_at': None, 'skip_by': None, 'skip_at': None}
            s['tasks'][key] = task
            checklist['task_ids'].append(key)
            if item['type'] == 'wet':
                task['wet_can_grams'] = s['config']['wet_can_grams']
        if food and task is not None:
            add_carried_food(task, portions)
        # Legacy entries without recorded grams remain reminder-only transfers.
        if task is not None:
            sources = task.setdefault('carried_from', [])
            for source in item['source_ids']:
                if source not in sources:
                    sources.append(source)
        del queue[key]


def move_to_next(s, task_id, actor, now, message_id=None, telegram_user_id=None):
    """Move an open task, using the owner-confirmed policy for food amounts."""
    task = s['tasks'].get(task_id)
    if task is None or due(task) > now:
        raise ValueError('Задача не найдена или её время ещё не наступило. Пункт не изменён.')
    if task['status'] in CLOSED:
        return {'status': 'already_closed', 'food': task['type'] in {'wet', 'dry'}}
    at, index, slot = next_care(s, now)
    cid = checklist_id(at.date(), slot)
    key = cid + '_' + task['type']
    target = s['tasks'].get(key)
    if target is not None and target['status'] in CLOSED:
        raise ValueError('Этот пункт следующего ухода уже закрыт. Текущий пункт не изменён.')
    food = task['type'] in {'wet', 'dry'}
    add_food = food and s['config'].get('food_transfer_policy') == 'add_full'
    planned = not food or add_food or stage_for(s, at.date())[task['type']][index] > 0
    if planned:
        if target is not None:
            if add_food:
                add_carried_food(target, {task_id: task['grams']})
            sources = target.setdefault('carried_from', [])
            if task_id not in sources:
                sources.append(task_id)
        else:
            item = s.setdefault('carryovers', {}).setdefault(key, {
                'checklist': cid, 'type': task['type'], 'due_at': iso(at), 'source_ids': []})
            if task_id not in item['source_ids']:
                item['source_ids'].append(task_id)
            if add_food:
                item.setdefault('food_grams', {}).setdefault(task_id, task['grams'])
    task.update(status='skipped', skip_by=actor, skip_at=iso(now),
                next_repeat_at=None, snoozed_until=None, needs_clarification=False,
                skip_reason='next_care', moved_to=key if planned else None,
                moved_due_at=iso(at), moved_food_without_addition=food and not add_food)
    extra = {}
    if add_food:
        task['moved_food_grams'] = task['grams']
        extra['food_added_grams'] = task['grams']
    audit(s, 'move_to_next', actor, now, task_id=task_id, target_id=key if planned else None,
          target_due_at=iso(at), food_without_addition=food and not add_food,
          message_id=message_id, telegram_user_id=telegram_user_id, **extra)
    if add_food:
        extra.update(type=task['type'],
                     wet_can_grams=task.get('wet_can_grams', s['config']['wet_can_grams']))
    return {'status': 'moved', 'food': food,
            'target_id': key if planned else None, 'due_at': iso(at), **extra}


def move_answer(result):
    if result['status'] == 'already_closed':
        return 'Этот пункт уже закрыт. Ничего не перенесено повторно.'
    at = instant(result['due_at'])
    if result['food']:
        if 'food_added_grams' in result:
            amount = (wet_amount(result['food_added_grams'], result['wet_can_grams'])
                      if result['type'] == 'wet' else f'{result["food_added_grams"]} г')
            return f'Перенесено на {at:%d.%m %H:%M}. К следующей порции добавлено: {amount}.'
        if not result.get('target_id'):
            return 'Порция пропущена. На следующий уход этот корм не запланирован. Добавки не будет.'
        return f'Порция пропущена. Следующее кормление {at:%d.%m %H:%M} по плану, без добавления пропущенной порции.'
    return f'Перенесено на {at:%d.%m %H:%M}. Повторы текущего пункта отключены.'


def generate_due(s, now):
    start = date.fromisoformat(s['config']['start_date'])
    if now.date() < start:
        return []
    previous = instant(s['last_tick']).date() if s.get('last_tick') else start
    cutoff_value = s.get('standalone', {}).get('fresh_start_cutoff')
    cutoff = instant(cutoff_value) if cutoff_value is not None else None
    day = max(start, previous)
    if cutoff is not None:
        day = max(day, cutoff.date())
    fresh = []
    while day <= now.date():
        for index, slot in enumerate(slots_for_day(s, day)):
            at = wall(day, slot['time'])
            # Compare instants, including offsets on either side of a clock change.
            if at > now or (cutoff is not None and at.timestamp() <= cutoff.timestamp()):
                continue
            cid = checklist_id(day, slot)
            ensure_checklist(s, day, slot, index)
            # An imported or early-created checklist still needs its first announcement.
            if s['checklists'][cid].get('announced_at') is None:
                fresh.append(cid)
        day += timedelta(days=1)
    return fresh


def mark_old_feedings(s, now):
    for task in s['tasks'].values():
        if task['status'] in CLOSED or task['type'] not in {'wet', 'dry'}:
            continue
        at = due(task)
        next_slots = [wall(at.date(), slot['time']) for slot in slots_for_day(s, at.date())
                      if wall(at.date(), slot['time']) > at]
        next_day = at.date() + timedelta(days=1)
        next_at = min(next_slots) if next_slots else wall(
            next_day, slots_for_day(s, next_day)[0]['time'])
        if at.date() < now.date() or next_at <= now:
            task['needs_clarification'] = True
            if task['status'] != 'deferred':
                task['status'] = 'needs_clarification'


def wet_amount(grams, can_grams=None, accusative=False):
    """Display can fractions without changing the stored gram portion."""
    weight = Fraction(str(grams))
    can = Fraction(str(DEFAULT_WET_CAN_GRAMS if can_grams is None else can_grams))
    if weight < 0 or can <= 0:
        raise ValueError('Неверная порция или масса банки.')
    exact = weight / can
    simple = exact.limit_denominator(8)
    # Simplify only within the original whole-gram rounding margin.
    amount = simple if abs(simple * can - weight) <= Fraction(1, 2) else exact
    prefix = '≈' if amount != exact else ''
    noun = 'банки'
    if amount.denominator == 1:
        n = amount.numerator
        if n % 10 == 1 and n % 100 != 11:
            noun = 'банку' if accusative else 'банка'
        elif n % 10 not in (2, 3, 4) or n % 100 in (12, 13, 14):
            noun = 'банок'
    return f'{prefix}{amount} {noun}'


def short_task(task):
    kind = task['type']
    if kind == 'wet':
        return f'Влажный корм, {wet_amount(task["grams"], task.get("wet_can_grams"))}'
    if kind == 'dry':
        return f'Сухой корм, {task["grams"]} г'
    return {'litter': 'Лоток и мусор вокруг', 'bowls': 'Место кормления и миски',
            'water': 'Свежая вода'}[kind]


def caregiver_name(value, max_units=MAX_CAREGIVER_LABEL_UNITS):
    """Keep display names on one line and bound their Telegram UTF-16 length."""
    if not isinstance(value, str):
        return ''
    value = ' '.join(value.split())
    value = ''.join(ch for ch in value if not unicodedata.category(ch).startswith('C'))
    units, result = 0, []
    for ch in value:
        width = len(ch.encode('utf-16-le')) // 2
        if units + width > max_units:
            break
        result.append(ch)
        units += width
    return ''.join(result).strip()


def remember_caregiver(state, sender):
    """Store display metadata only after the caller validates numeric access and scope."""
    username = sender.get('username')
    if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_]{1,32}', username):
        username = None
    name = caregiver_name(' '.join(value for key in ('first_name', 'last_name')
                                  if isinstance(value := sender.get(key), str)))
    state.setdefault('caregivers', {})['telegram:' + str(sender['id'])] = {
        'username': username, 'name': name}


def display_actor(state, actor, max_name_units=MAX_CAREGIVER_LABEL_UNITS):
    """Display a Telegram username or name, never its internal numeric identity."""
    actor = str(actor or '')
    if re.fullmatch(r'(?:telegram:)?[0-9]+', actor):
        key = actor if actor.startswith('telegram:') else 'telegram:' + actor
        profile = (state or {}).get('caregivers', {}).get(key, {})
        username = profile.get('username')
        if isinstance(username, str) and re.fullmatch(r'[A-Za-z0-9_]{1,32}', username):
            return '@' + username
        return caregiver_name(profile.get('name'), max_name_units) or 'участник'
    # Imported records can already contain a username rather than a numeric identity.
    if re.fullmatch(r'@?[A-Za-z0-9_]{1,32}', actor):
        return '@' + actor.lstrip('@')
    return caregiver_name(actor, max_name_units) or 'участник'


def task_line(task, primary=False, state=None):
    kind = task['type']
    if task['status'] == 'done':
        stamp = instant(task['done_at'])
        return f'✅ {short_task(task)}: {display_actor(state, task["done_by"])}, {stamp:%H:%M}.'
    if task['status'] == 'skipped':
        stamp = instant(task['skip_at'])
        text = f'Пропущено: {short_task(task)} ({display_actor(state, task["skip_by"])}, {stamp:%H:%M}).'
        if task.get('skip_reason') == 'next_care':
            target_at = instant(task['moved_due_at'])
            if task.get('moved_food_without_addition'):
                text += f' Следующий уход {target_at:%d.%m %H:%M}, без добавления этой порции.'
            else:
                text += f' Перенесено на {target_at:%d.%m %H:%M}.'
                if task.get('moved_food_grams'):
                    text += ' Весь объём добавлен к следующей порции.'
        return text
    if task.get('needs_clarification'):
        text = f'☐ Нужно уточнить: была ли выдана порция «{short_task(task)}»?'
    elif kind == 'wet':
        amount = wet_amount(task['grams'], task.get('wet_can_grams'), accusative=True)
        text = f'☐ Дать {amount} влажного корма.' if primary else f'☐ {short_task(task)}.'
    elif kind == 'dry':
        text = f'☐ Дать {task["grams"]} г сухого корма.' if primary else f'☐ {short_task(task)}.'
    else:
        text = '☐ ' + CLEANING[kind]
    if task.get('carried_grams'):
        amount = (wet_amount(task['carried_grams'], task.get('wet_can_grams'))
                  if kind == 'wet' else f'{task["carried_grams"]} г')
        text += f' Включая перенос: {amount}.'
    if task['status'] == 'deferred' and task.get('snoozed_until'):
        text += f' Отложено до {instant(task["snoozed_until"]):%d.%m %H:%M}.'
    return text


def unresolved(s, cid=None):
    tasks = s['tasks'].values() if cid is None else [
        s['tasks'][key] for key in s['checklists'][cid]['task_ids']]
    return [task for task in tasks if task['status'] not in CLOSED]


BUTTON_BOT = '@example_care_bot'  # Replaced only after getMe authenticates the new bot.
BUTTON_TEST = '✅ Тест кнопки ' + BUTTON_BOT


def button_text(task, legacy=False):
    """Include the full date and bot mention in every text button."""
    names = {'wet': 'Влажный', 'dry': 'Сухой', 'litter': 'Лоток',
             'bowls': 'Миски', 'water': 'Вода'}
    label = names[task['type']]
    if task['type'] == 'wet' and not legacy:
        label += ' ' + wet_amount(task['grams'], task.get('wet_can_grams'))
    elif task['type'] in {'wet', 'dry'}:
        label += f' {task["grams"]} г'
    stamp = due(task).strftime('%d.%m.%Y %H:%M')
    return f'✅ {label} · {stamp} {BUTTON_BOT}'


def reply_keyboard(s, task_ids):
    """Keep text buttons as a fallback for messages from the old interface."""
    tasks = [s['tasks'][key] for key in dict.fromkeys(task_ids)
             if key in s['tasks'] and s['tasks'][key]['status'] not in CLOSED | {'deferred'}]
    if not tasks:
        return {'remove_keyboard': True}
    return {'keyboard': [[{'text': button_text(task)}] for task in tasks],
            'resize_keyboard': True, 'one_time_keyboard': False,
            'is_persistent': False, 'selective': False,
            'input_field_placeholder': 'Отметьте только выполненные пункты'}


def emission_task_ids(s, emission_id):
    item = s.get('emissions', {}).get(emission_id)
    if not item:
        return []
    keys = list(s['checklists'][item['primary']]['task_ids']) if item['primary'] else []
    repeats = [s['tasks'][key] for key in item['repeat_ids']
               if s['tasks'][key]['status'] not in CLOSED | {'deferred'}
               and (s['config'].get('automatic_repeats', True)
                    or key in item.get('manual_repeat_ids', []))]
    shown = sorted({task['checklist'] for task in repeats})[-6:]
    keys.extend(task['id'] for task in repeats if task['checklist'] in shown)
    return list(dict.fromkeys(keys))


def complete_button(s, text, actor, message_id, now, sender=None):
    if text.strip() == BUTTON_TEST:
        if sender is not None:
            remember_caregiver(s, sender)
        return 'Кнопка работает: отметка пришла. Кормления и задачи ухода не изменены.'
    matches = [task for task in s['tasks'].values()
               if text.strip() in {button_text(task), button_text(task, legacy=True)}]
    if len(matches) != 1:
        raise ValueError('Кнопка не соответствует сохранённой задаче. Уточните дату, время и пункт.')
    task = matches[0]
    if due(task) > now:
        raise ValueError('Время этой задачи ещё не наступило. Отметка не сохранена.')
    if sender is not None:
        remember_caregiver(s, sender)
    mark_old_feedings(s, now)
    args = argparse.Namespace(checklist=task['checklist'], types=[task['type']],
                              action='done', actor=actor, minutes=None,
                              message_id=message_id, original_message_id=None)
    return update(s, args, now)


def daily_line(stage):
    dry = stage['daily_dry']
    wet = stage['daily_wet']
    if wet:
        return f'Суточная порция: {wet_amount(wet)} влажного + {dry} г сухого.'
    qualifier = 'ориентировочно ' if stage.get('provisional') else ''
    return f'Суточная порция: {qualifier}{dry} г сухого.'


def prior_warning(s, cid):
    at = s['checklists'][cid]['date'] + '_' + s['checklists'][cid]['time']
    old = [task for task in unresolved(s) if task['type'] in {'wet', 'dry'}
           and task['checklist'] < at and task.get('needs_clarification')]
    if not old:
        return []
    dates = sorted({task['checklist'] for task in old})
    refs = ', '.join(key.replace('_', ' ') for key in dates[-4:])
    more = f' и ещё {len(dates) - 4}' if len(dates) > 4 else ''
    return [f'⚠️ Предыдущее кормление не подтверждено: {refs}{more}.',
            'Уточните, выдавали ли эти порции. Не добавляйте их к новой порции.']


def render_checklist(s, cid, updated=False):
    checklist = s['checklists'][cid]
    stage = checklist['stage']
    day = date.fromisoformat(checklist['date'])
    tasks = [s['tasks'][key] for key in checklist['task_ids']]
    feeding = [task for task in tasks if task['type'] in {'wet', 'dry'}]
    cleaning = [task for task in tasks if task['type'] in CLEANING]
    greeting = {'breakfast': 'доброе утро', 'lunch': 'добрый день',
                'dinner': 'добрый вечер'}.get(checklist['slot'], 'здравствуйте')
    if updated:
        introduction = f'{s["config"]["mention"]}, отметки обновлены.'
    else:
        action = 'Пора покормить Acorn и немного прибраться.' if cleaning else 'Пора покормить Acorn.'
        if any(task['status'] != 'pending' or task.get('needs_clarification') for task in tasks):
            action = 'Проверьте оставшиеся пункты.'
        introduction = f'{s["config"]["mention"]}, {greeting}! {action}'
    lines = [f'🐾 Acorn · {checklist["label"].lower()}',
             f'{day_label(day)} · {checklist["time"]}', introduction]
    if s.get('progression_hold'):
        lines.extend(['', '⚠️ ' + stage_label(stage, day, s)])
    if not updated:
        warning = prior_warning(s, cid)
        if warning:
            lines.extend([''] + warning)
    first_slot = s['config']['slots'][0]['time']
    if not updated and day.isoformat() == stage['start'] and checklist['time'] == first_slot:
        lines.extend(['', '**Сегодня меняются порции**'])
        for i, slot in enumerate(s['config']['slots']):
            values = []
            if stage['wet'][i]:
                values.append(f'{wet_amount(stage["wet"][i], s["config"]["wet_can_grams"])} влажного')
            if stage['dry'][i]:
                values.append(f'{stage["dry"][i]} г сухого')
            lines.append(f'{slot["time"]}: ' + ' + '.join(values) + '.')
    cleaning_title = '🧹 Уборка и вода' if any(task['type'] == 'water' for task in cleaning) else '🧹 Уборка'
    for title, items in [('🍽 Кормление', feeding), (cleaning_title, cleaning)]:
        if items:
            lines.extend(['', title])
            lines.extend(task_line(task, primary=True, state=s) for task in items)
    lines.append('')
    if all(task['status'] == 'done' for task in tasks):
        lines.append('✅ Всё выполнено. Спасибо!')
    elif all(task['status'] in CLOSED for task in tasks):
        lines.append('Список закрыт. Выполненные и пропущенные пункты указаны отдельно.')
    else:
        lines.append('✅ Отмечайте выполненное')
        if not s['config'].get('automatic_repeats', True):
            lines.append('Автоматические повторы выключены.')
        elif updated or any(task['status'] == 'deferred' for task in tasks):
            lines.append('⏰ Напомню только о неотмеченных пунктах.')
        else:
            minutes = s['config']['repeat_minutes']
            lines.append(f'⏰ Через {minutes} минут напомню только о неотмеченных пунктах.')
    return '\n'.join(lines)


def render_reminders(s, tasks, morning=False):
    if not tasks:
        return ''
    lines = ['⏰ Незакрытые задачи после тихих часов:' if morning
             else '⏰ Пока не подтверждены:']
    grouped = {}
    for task in sorted(tasks, key=lambda x: x['id']):
        grouped.setdefault(task['checklist'], []).append(task)
    # Bound message size when owners leave old lists open for many days.
    groups = list(grouped.items())
    older = groups[:-6]
    shown = groups[-6:]
    if older:
        older_tasks = [task for _, values in older for task in values]
        lines.append(f'Также открыто старых пунктов: {len(older_tasks)}. '
                     'Для списка напишите «Что осталось сделать?».' )
        if any(task['type'] in {'wet', 'dry'} for task in older_tasks):
            lines.append('По старым кормлениям нужно только уточнить, выдавали ли порции.')
    for cid, values in shown:
        checklist = s['checklists'][cid]
        lines.append(f'\n{checklist["date"]} · {checklist["time"]} · список {cid}')
        lines.extend(task_line(task, state=s) for task in values)
    lines.extend(['', 'Проверьте, пожалуйста: возможно, это уже сделано.',
                  'Если корм уже выдали, подтвердите выдачу. Повторно давать эту порцию не нужно.',
                  'Не суммируйте старые порции с новыми.',
                  'Можно ответить: «Отложить [пункты] на 30 минут» или «на 1 час».'])
    return '\n'.join(lines)


def tick(s, now):
    pending = s.get('pending_config')
    if pending and now.date().isoformat() >= pending['effective']:
        s['config']['slots'] = pending['slots']
        if 'quiet_end' in pending:
            s['config']['quiet_end'] = pending['quiet_end']
        s['pending_config'] = None
    config = s['config']
    if now.date() < date.fromisoformat(config['start_date']) or quiet(now, config):
        return 'NO_REPLY'
    pause = s.get('reminder_pause')
    if pause and (pause['until'] == 'indefinite' or instant(pause['until']) > now):
        return 'NO_REPLY'
    if pause:
        s['reminder_pause'] = None
    fresh = generate_due(s, now)
    mark_old_feedings(s, now)
    s['current_stage'] = stage_for(s, now.date())['id']
    s['last_tick'] = iso(now)
    all_open = unresolved(s)
    manual_repeats = set()
    for task in all_open:
        if task['status'] == 'deferred' and instant(task['snoozed_until']) <= now:
            manual_repeats.add(task['id'])
            task['status'] = 'needs_clarification' if task['needs_clarification'] else 'pending'
            task['snoozed_until'] = None
    eligible = [task for task in all_open if task['status'] != 'deferred']
    morning = s.get('morning_summary_date') != now.date().isoformat()
    s['morning_summary_date'] = now.date().isoformat()
    today_new = [cid for cid in fresh if s['checklists'][cid]['date'] == now.date().isoformat()]
    primary = max(today_new) if today_new else None
    blocks = []
    extra_notices = []
    if primary:
        blocks.append(render_checklist(s, primary))
    repeated = []
    for task in eligible:
        checklist = s['checklists'][task['checklist']]
        if task['checklist'] == primary:
            continue
        if not config.get('automatic_repeats', True) and task['id'] not in manual_repeats:
            continue
        next_at = instant(task['next_repeat_at']) if task.get('next_repeat_at') else due(task)
        if next_at <= now:
            repeated.append(task)
    if repeated:
        blocks.append(render_reminders(s, repeated, morning=morning))
    emitted = repeated + [task for task in eligible if task['checklist'] == primary]
    for task in emitted:
        if not config.get('automatic_repeats', True):
            task['next_repeat_at'] = None
            continue
        next_at = instant(task['next_repeat_at']) if task.get('next_repeat_at') else due(task)
        while next_at <= now:
            next_at = plus_minutes(next_at, config['repeat_minutes'])
        task['next_repeat_at'] = iso(allowed_time(next_at, config))
    for cid in fresh:
        s['checklists'][cid]['announced_at'] = iso(now)
    # Remind one day before stage five. A health hold cancels this transition notice.
    final_start = date.fromisoformat(config['stages'][-1]['start'])
    if (not s.get('progression_hold') and now.date() == final_start - timedelta(days=1)
            and now.strftime('%H:%M') >= config['slots'][0]['time']
            and s.get('vet_notice_for') != final_start.isoformat()):
        notice = ('Завтра запланирован переход на сухой корм. '
                  'Проверьте актуальную суточную норму по возрасту, весу и рекомендациям ветеринара. '
                  '78 г в плане указаны ориентировочно. Новую норму самостоятельно не рассчитываю.')
        blocks.append(notice)
        extra_notices.append(notice)
        s['vet_notice_for'] = final_start.isoformat()
    if not blocks:
        return 'NO_REPLY'
    eid = uuid.uuid4().hex
    s.setdefault('emissions', {})[eid] = {
        'id': eid, 'at': iso(now), 'primary': primary,
        'repeat_ids': [t['id'] for t in repeated], 'morning': morning,
        'manual_repeat_ids': [t['id'] for t in repeated if t['id'] in manual_repeats],
        'notices': extra_notices,
    }
    s['last_emission_id'] = eid
    if len(s['emissions']) > 12:
        oldest = next(iter(s['emissions']))
        del s['emissions'][oldest]
    audit(s, 'reminder_emitted', 'scheduler', now, task_ids=[t['id'] for t in emitted])
    text = '\n\n'.join(blocks)
    return text if primary else config['mention'] + '\n\n' + text


def refresh(s, emission_id, now):
    item = s.get('emissions', {}).get(emission_id)
    if not item or quiet(now, s['config']):
        return 'NO_REPLY'
    pause = s.get('reminder_pause')
    if pause and (pause['until'] == 'indefinite' or instant(pause['until']) > now):
        return 'NO_REPLY'
    mark_old_feedings(s, now)
    blocks = []
    has_primary = bool(item['primary'] and any(task['status'] != 'deferred'
                                             for task in unresolved(s, item['primary'])))
    if has_primary:
        blocks.append(render_checklist(s, item['primary']))
    tasks = [s['tasks'][key] for key in item['repeat_ids']
             if s['tasks'][key]['status'] not in CLOSED | {'deferred'}
             and (s['config'].get('automatic_repeats', True)
                  or key in item.get('manual_repeat_ids', []))]
    if tasks:
        blocks.append(render_reminders(s, tasks, morning=item['morning']))
    blocks.extend(item['notices'])
    if not blocks:
        return 'NO_REPLY'
    text = '\n\n'.join(blocks)
    return text if has_primary else s['config']['mention'] + '\n\n' + text


def update(s, args, now):
    cid = args.checklist
    if cid not in s['checklists']:
        raise ValueError('Такого чек-листа нет. Уточните дату и время.')
    if any(due(s['tasks'][f'{cid}_{kind}']) > now
           for kind in args.types if f'{cid}_{kind}' in s['tasks']):
        raise ValueError('Время этой задачи ещё не наступило. Отметка не сохранена.')
    if args.original_message_id:
        s['checklists'][cid]['original_message_id'] = args.original_message_id
    keys = [f'{cid}_{kind}' for kind in args.types]
    if any(key not in s['tasks'] for key in keys):
        raise ValueError('В этом чек-листе нет одного из указанных пунктов.')
    if args.action == 'snooze' and args.minutes not in {30, 60}:
        raise ValueError('Можно отложить на 30 или 60 минут.')
    changed = []
    for key in keys:
        task = s['tasks'][key]
        if task['status'] in CLOSED:
            continue
        if args.action == 'done':
            task.update(status='done', done_by=args.actor, done_at=iso(now),
                        next_repeat_at=None, snoozed_until=None)
        elif args.action == 'skip':
            task.update(status='skipped', skip_by=args.actor, skip_at=iso(now),
                        next_repeat_at=None, snoozed_until=None)
        else:
            until = allowed_time(plus_minutes(now, args.minutes), s['config'])
            task.update(status='deferred', snoozed_until=iso(until), next_repeat_at=iso(until))
        changed.append(key)
    audit(s, args.action, args.actor, now, task_ids=changed, message_id=args.message_id)
    result = render_checklist(s, cid, updated=True)
    if not changed:
        result = 'Эти пункты уже закрыты. Повторную выдачу корма не учитываю.\n\n' + result
    if args.action == 'snooze' and changed:
        times = {s['tasks'][key]['next_repeat_at'] for key in changed}
        result += '\nСледующее напоминание: ' + ', '.join(sorted(times)) + '.'
    return result


def plan_consumption(config):
    stages = config['stages']
    lines = []
    total = Fraction(0)
    for i, stage in enumerate(stages[:-1]):
        start = date.fromisoformat(stage['start'])
        finish = date.fromisoformat(stages[i + 1]['start'])
        days = (finish - start).days
        cans = days * Fraction(str(stage['daily_wet'])) / config['wet_can_grams']
        total += cans
        lines.append(f'{stage["label"]}: {start}–{finish - timedelta(days=1)}, '
                     f'{days} дн., {cans} банки по плану.')
    lines.append(f'Всего влажного по плану: {total} банки. Это не фактический остаток.')
    return lines


def proposal(s, change, actor, now):
    config = copy.deepcopy(s['config'])
    kind = change['kind']
    explanation = []
    if kind == 'food_transfer':
        policy = change['policy']
        if policy not in {'add_full', 'planned_only'}:
            raise ValueError('Неизвестное правило переноса корма.')
        config['food_transfer_policy'] = policy
        explanation.append('После нажатия «К следующему уходу» весь объём корма добавляется к следующей порции того же вида, включая следующий день.'
                           if policy == 'add_full' else
                           'После нажатия «К следующему уходу» следующая порция остаётся по плану без добавки.')
        explanation.append('Новая настройка не меняет выданные порции, прежние пропуски или старые переносы.')
    elif kind == 'extend_stage':
        days = change['days']
        if not isinstance(days, int) or days <= 0:
            raise ValueError('Укажите положительное целое число дней.')
        stage = stage_for(s, now.date())
        index = next(i for i, x in enumerate(config['stages']) if x['id'] == stage['id'])
        if index == len(config['stages']) - 1:
            raise ValueError('Последний этап не имеет даты окончания.')
        if s.get('progression_hold'):
            raise ValueError('Смена этапов приостановлена. Сначала согласуйте новые даты с ветеринаром.')
        for later in config['stages'][index + 1:]:
            later['start'] = (date.fromisoformat(later['start']) + timedelta(days=days)).isoformat()
        explanation.append(f'Продлить «{stage["label"]}» на {days} дн. Порции не меняются.')
    elif kind == 'schedule':
        time.fromisoformat(change['time'])
        slots = [x for x in config['slots'] if x['key'] == change['slot']]
        if len(slots) != 1:
            raise ValueError('Неизвестное кормление.')
        old = slots[0]['time']
        slots[0]['time'] = change['time']
        if 'quiet_end' in change:
            time.fromisoformat(change['quiet_end'])
            if change['quiet_end'] >= config['quiet_start']:
                raise ValueError('Начало дня должно быть раньше начала тихих часов.')
            config['quiet_end'] = change['quiet_end']
        times = [x['time'] for x in config['slots']]
        if times != sorted(times) or len(set(times)) != len(times):
            raise ValueError('Времена кормлений должны сохранять порядок и не совпадать.')
        if any(value < config['quiet_end'] or value >= config['quiet_start'] for value in times):
            raise ValueError('Кормления должны быть вне тихих часов.')
        explanation.append(f'{slots[0]["label"]}: {old} → {change["time"]}, начиная со следующего дня.')
    elif kind == 'portions':
        stages = [x for x in config['stages'] if x['id'] == change['stage']]
        if len(stages) != 1:
            raise ValueError('Неизвестный этап.')
        stage = stages[0]
        for nutrient in ['wet', 'dry']:
            values = change[nutrient]
            count = len(config['slots'])
            if len(values) != count or any(not isinstance(x, (int, float)) or x < 0 for x in values):
                raise ValueError(f'Нужно неотрицательных порций каждого вида корма: {count}.')
            stage[nutrient] = values
            stage['daily_' + nutrient] = sum(values)
        stage['provisional'] = False
        explanation.append(f'Порции этапа «{stage["label"]}» изменятся только для будущих чек-листов.')
        explanation.append('Влажный: ' + ', '.join(map(str, stage['wet'])) + ' г.')
        explanation.append('Сухой: ' + ', '.join(map(str, stage['dry'])) + ' г.')
    elif kind in {'stage_dates', 'resume_progression'}:
        starts = change['starts']
        if len(starts) != len(config['stages']):
            raise ValueError('Нужны даты начала всех четырёх этапов.')
        parsed = [date.fromisoformat(value) for value in starts]
        if parsed != sorted(set(parsed)) or starts[0] != config['start_date']:
            raise ValueError('Даты должны идти по порядку. Дата первоначального старта не меняется.')
        for stage, start in zip(config['stages'], starts):
            stage['start'] = start
        if kind == 'resume_progression':
            explanation.append('После подтверждения автоматическая смена этапов возобновится.')
    else:
        raise ValueError('Неизвестный тип изменения.')
    explanation.extend(plan_consumption(config))
    pid = uuid.uuid4().hex[:10]
    s['proposals'][pid] = {'id': pid, 'change': change, 'config': config,
                           'base_revision': s.get('revision', 0),
                           'created_at': iso(now), 'created_by': actor, 'status': 'pending'}
    audit(s, 'proposal_created', actor, now, proposal=pid)
    return '\n'.join(explanation + [f'Предложение: {pid}. Подтвердите изменение.'])


def health_hold(s, actor, note, now):
    stage = stage_for(s, now.date())
    if not s.get('progression_hold'):
        s['progression_hold'] = {'stage': stage['id'], 'since': iso(now),
                                 'actor': actor, 'note': note}
    audit(s, 'health_hold', actor, now, note=note)
    return ('Автоматическая смена этапов приостановлена. Напоминания об уходе остаются. '
            'Согласуйте дальнейший рацион с ветеринаром.')
