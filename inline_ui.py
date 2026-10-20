"""Store inline buttons and apply authenticated care confirmations."""
import hashlib
import json
import re
import secrets

import care

PREFIX = 'acorn:1:'
TEST_TEXT = '✅ Тест кнопки'
MESSAGE_LIMIT = 4096  # Telegram measures message size in UTF-16 units.
PAGE_LIMIT = 3500


def task_line_size(task, primary, now):
    """Reserve room for later completion, skip, clarification, and snooze edits."""
    stamp = care.iso(now)
    actor = 'я' * care.MAX_CAREGIVER_LABEL_UNITS  # Reserve the longest display name.
    variants = [task, {**task, 'status': 'done', 'done_by': actor, 'done_at': stamp},
                {**task, 'status': 'skipped', 'skip_by': actor, 'skip_at': stamp,
                 'skip_reason': 'next_care', 'moved_due_at': stamp,
                 'moved_food_without_addition': task['type'] in {'wet', 'dry'}}]
    for clarify in (False, task['type'] in {'wet', 'dry'}):
        variants.append({**task, 'status': 'deferred', 'needs_clarification': clarify,
                         'snoozed_until': stamp})
    return max(len(care.task_line(value, primary=primary).encode('utf-16-le')) // 2
               for value in variants)


def prepare(state, parts, task_ids, now, primary=None, test=False):
    nonce = secrets.token_hex(8)
    keys = [key for key in dict.fromkeys(task_ids) if key in state['tasks']]
    pages = []
    cid = primary
    is_primary = bool(primary)
    for part in parts:
        bindings, lines, size = {}, [], 0
        for line in part.splitlines():
            match = re.search(r'список (\d{4}-\d\d-\d\d_\d\d:\d\d)$', line)
            if match:
                cid, is_primary = match.group(1), False
            candidates = [key for key in keys if state['tasks'][key]['checklist'] == cid]
            binding = None
            units = len(line.encode('utf-16-le')) // 2
            for key in candidates:
                if line == care.task_line(state['tasks'][key], primary=is_primary, state=state):
                    binding = {'task': key, 'primary': is_primary}
                    units = max(units, task_line_size(state['tasks'][key], is_primary, now))
                    break
            if lines and size + 1 + units > PAGE_LIMIT:
                pages.append({'template': '\n'.join(lines), 'bindings': bindings, 'message_id': None})
                bindings, lines, size = {}, [], 0
            if binding:
                bindings[str(len(lines))] = binding
            lines.append(line)
            size += units + 1
        pages.append({'template': '\n'.join(lines), 'bindings': bindings, 'message_id': None})
    panel = {'nonce': nonce, 'task_ids': keys, 'pages': pages,
             'created_at': care.iso(now), 'primary': primary, 'test': test,
             'status': 'sending', 'last_digest': None}
    state.setdefault('inline_panels', {})[nonce] = panel
    return panel


def button_label(task):
    """Keep the date in the message and the task identity in callback data."""
    return care.button_text(task).split(' · ', 1)[0]


def keyboard(state, panel):
    if panel['test']:
        if panel.get('test_done'):
            return {'inline_keyboard': []}
        return {'inline_keyboard': [[{'text': TEST_TEXT,
                                     'callback_data': PREFIX + panel['nonce'] + ':test'}]]}
    rows = []
    for index, key in enumerate(panel['task_ids']):
        task = state['tasks'][key]
        if task['status'] in care.CLOSED | {'deferred'}:
            continue
        label = button_label(task)
        rows.append([{'text': label,
                      'callback_data': PREFIX + panel['nonce'] + ':' + str(index)},
                      {'text': 'К следующему уходу',
                       'callback_data': PREFIX + panel['nonce'] + ':next:' + str(index)}])
    return {'inline_keyboard': rows}


def page_text(state, panel, page):
    if panel['test']:
        if panel.get('test_done'):
            return ('✅ Кнопка работает. Нажатие получено, отметка теста сохранена.\n'
                    'Кормления и задачи ухода не изменены.')
        return page['template']
    lines = page['template'].splitlines()
    for index, binding in page['bindings'].items():
        lines[int(index)] = care.task_line(state['tasks'][binding['task']],
                                          primary=binding['primary'], state=state)
    tasks = [state['tasks'][key] for key in panel['task_ids']]
    closed = bool(tasks) and all(task['status'] in care.CLOSED for task in tasks)
    all_done = closed and all(task['status'] == 'done' for task in tasks)
    for index, line in enumerate(lines):
        if line.startswith(state['config']['mention'] + ','):
            lines[index] = state['config']['mention'] + ', отметки обновлены.'
        if line == '✅ Отмечайте выполненное':
            lines[index] = ('✅ Всё выполнено. Спасибо!' if all_done else
                            'Список закрыт. Выполненные и пропущенные пункты указаны отдельно.'
                            if closed else line)
        elif (not state['config'].get('automatic_repeats', True)
              and (line.startswith('⏰ Через ') or line.startswith('⏰ Напомню только')
                   or line == 'Автоматические повторы выключены.')):
            lines[index] = '' if closed else 'Автоматические повторы выключены.'
        elif closed and line == 'Автоматические повторы выключены.':
            lines[index] = ''
        elif line.startswith('⏰ Через '):
            lines[index] = '' if closed else '⏰ Напомню только о неотмеченных пунктах.'
        elif closed and line.startswith('⏰ Напомню только'):
            lines[index] = ''
        elif closed and line in {'⏰ Пока не подтверждены:', '⏰ Незакрытые задачи после тихих часов:'}:
            lines[index] = '✅ Отметки обновлены:'
    text = '\n'.join(lines).rstrip()
    if len(text.encode('utf-16-le')) // 2 > MESSAGE_LIMIT:
        # Old saved panels did not reserve space for longer skipped-task lines.
        # Keep their message IDs and bindings. Only shorten closed display lines.
        for index, binding in page['bindings'].items():
            task = state['tasks'][binding['task']]
            if task['status'] == 'skipped':
                line = f'Пропущено: {care.short_task(task)} ({care.display_actor(state, task["skip_by"], max_name_units=32)}).'
                if task.get('skip_reason') == 'next_care':
                    at = care.instant(task['moved_due_at'])
                    line += f' Следующий уход {at:%d.%m %H:%M}'
                    line += ', без добавки.' if task.get('moved_food_without_addition') else '.'
                lines[int(index)] = line
            elif task['status'] == 'done':
                lines[int(index)] = f'✅ {care.short_task(task)}: {care.display_actor(state, task["done_by"], max_name_units=32)}.'
        text = '\n'.join(lines).rstrip()
        if len(text.encode('utf-16-le')) // 2 > MESSAGE_LIMIT:
            # Keep full usernames and transfer dates on dense pre-upgrade pages.
            for index, binding in page['bindings'].items():
                task = state['tasks'][binding['task']]
                if task['status'] == 'skipped' and task.get('skip_reason') == 'next_care':
                    lines[int(index)] = lines[int(index)].replace(' Следующий уход ', ' → ', 1)
            text = '\n'.join(lines).rstrip()
    return text


def digest(state, panel):
    payload = {'texts': [page_text(state, panel, p) for p in panel['pages']],
               'keyboard': keyboard(state, panel)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def accept_callback(state, callback, allowed_ids, bot_id, now, chat_id, topic_id):
    """Accept only registered messages in the care topic. Never infer completion."""
    data = callback.get('data', '')
    if not isinstance(data, str) or not data.startswith(PREFIX):
        return None
    rejection = {'text': 'Кнопка недоступна. Задачи не изменены.', 'show_alert': True}
    sender = callback.get('from', {})
    message = callback.get('message', {})
    if (type(sender.get('id')) is not int or sender.get('id') not in allowed_ids
            or sender.get('is_bot') is not False
            or message.get('chat', {}).get('id') != chat_id
            or message.get('message_thread_id') != topic_id
            or message.get('from', {}).get('id') != bot_id
            or message.get('from', {}).get('is_bot') is not True):
        return rejection
    match = re.fullmatch(re.escape(PREFIX) + r'([0-9a-f]{16}):(test|(?:next:)?\d{1,3})', data)
    if not match:
        return rejection
    panel = state.get('inline_panels', {}).get(match.group(1))
    if (not panel or panel['status'] != 'sent'
            or panel['pages'][-1]['message_id'] != message.get('message_id')):
        return rejection
    callback_id = callback.get('id')
    if not isinstance(callback_id, str) or not callback_id or len(callback_id) > 256:
        return rejection
    receipts = state.get('inline_receipts', {})
    if callback_id in receipts:
        return receipts[callback_id]['answer']
    actor = 'telegram:' + str(sender['id'])
    if match.group(2) == 'test':
        if not panel['test']:
            return rejection
        care.remember_caregiver(state, sender)
        panel['test_done'] = True
        panel['test_by'] = actor
        panel['test_at'] = care.iso(now)
        answer = {'text': '✅ Кнопка работает. Кормление и уборка не изменены.', 'show_alert': False}
        care.audit(state, 'inline_test', actor, now, telegram_user_id=str(sender['id']))
    else:
        action = match.group(2)
        move = action.startswith('next:')
        index = int(action.split(':')[-1])
        if panel['test'] or index >= len(panel['task_ids']):
            return rejection
        task = state['tasks'].get(panel['task_ids'][index])
        if not task or care.due(task) > now:
            return {'text': 'Время этой задачи ещё не наступило. Отметка не сохранена.', 'show_alert': True}
        if move:
            try:
                result = care.move_to_next(state, task['id'], actor, now,
                                           'telegram_callback_' + callback_id, str(sender['id']))
            except ValueError as error:
                return {'text': str(error), 'show_alert': True}
            care.remember_caregiver(state, sender)
            answer = {'text': care.move_answer(result), 'show_alert': False}
        else:
            already_closed = task['status'] in care.CLOSED
            care.remember_caregiver(state, sender)
            if not already_closed:
                care.complete_button(state, care.button_text(task), actor,
                                     'telegram_callback_' + callback_id, now)
                care.audit(state, 'inline_callback', actor, now, task_id=task['id'],
                           telegram_user_id=str(sender['id']), callback_id=callback_id)
            answer = {'text': ('Этот пункт уже закрыт. Повторную порцию не учитываю.' if already_closed
                               else '✅ Сохранено. Повторы для этого пункта отключены.'), 'show_alert': False}
    state['inline_receipts'] = receipts
    receipts[callback_id] = {'at': care.iso(now), 'answer': answer}
    while len(receipts) > 512:
        del receipts[next(iter(receipts))]
    return answer
