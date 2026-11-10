"""Calendar-anchored, veterinarian-approved single treatment occurrences.

Task kind: treatment_<definition id>. A transfer creates a new checklist task,
but keeps treatment_occurrence (<id>:<nominal ISO date>) unchanged. The ledger
never uses completion or transfer time as a recurrence anchor. Missed calendar
dates are not backfilled. A blocked occurrence needs human clarification, not
an extra dose, even if the previous occurrence is subsequently closed.
"""
import calendar
import copy
import re
from datetime import date, datetime


ID_PATTERN = r'[a-z][a-z0-9_]{0,31}'
KIND_PATTERN = 'treatment_' + ID_PATTERN


def is_treatment(kind):
    return isinstance(kind, str) and re.fullmatch(KIND_PATTERN, kind) is not None


def validate(definitions, slot_keys, safe_text):
    if not isinstance(definitions, list) or len(definitions) > 16:
        raise ValueError('Invalid treatments list')
    seen = set()
    required = {'id', 'start', 'interval_months', 'slot', 'text',
                'approved_by', 'approved_message', 'approved_at'}
    for item in definitions:
        if (not isinstance(item, dict) or not required <= set(item)
                or set(item) - required - {'first_slot'}):
            raise ValueError('Invalid treatment definition')
        ident = item['id']
        if not isinstance(ident, str) or not re.fullmatch(ID_PATTERN, ident) or ident in seen:
            raise ValueError('Invalid or duplicate treatment id')
        seen.add(ident)
        try:
            start = date.fromisoformat(item['start'])
            approved = datetime.fromisoformat(item['approved_at'])
        except (TypeError, ValueError):
            raise ValueError('Invalid treatment dates') from None
        if (start.isoformat() != item['start'] or approved.tzinfo is None
                or approved.utcoffset() is None):
            raise ValueError('Treatment dates require ISO date and aware approval time')
        if type(item['interval_months']) is not int or not 1 <= item['interval_months'] <= 120:
            raise ValueError('Invalid treatment calendar interval')
        if not isinstance(item['slot'], str) or item['slot'] not in slot_keys:
            raise ValueError('Invalid treatment meal slot')
        if ('first_slot' in item and (not isinstance(item['first_slot'], str)
                or item['first_slot'] not in slot_keys)):
            raise ValueError('Invalid first treatment meal slot')
        if not safe_text(item['text'], 256):
            raise ValueError('Invalid treatment text')
        if not all(safe_text(item[key], 128) for key in ('approved_by', 'approved_message')):
            raise ValueError('Treatment approval provenance is required')


def occurrence_date(definition, index):
    """Clamp to month end from the ORIGINAL anchor (Jan31 -> Feb28 -> Mar31)."""
    start = date.fromisoformat(definition['start'])
    if type(index) is not int or index < 0:
        raise ValueError('Invalid treatment occurrence index')
    month = start.year * 12 + start.month - 1 + index * definition['interval_months']
    year, zero_month = divmod(month, 12)
    return date(year, zero_month + 1,
                min(start.day, calendar.monthrange(year, zero_month + 1)[1]))


def scheduled_today(definition, day):
    start = date.fromisoformat(definition['start'])
    months = (day.year - start.year) * 12 + day.month - start.month
    interval = definition['interval_months']
    return (months >= 0 and months % interval == 0
            and occurrence_date(definition, months // interval) == day)


def apply_configuration(state, definitions, now):
    """Only explicit startup configuration changes future generation; history stays intact.

    An id's prescription and calendar anchor are immutable once installed. Use a
    new id for a separately approved replacement. Omitting the startup key is a
    no-op; an explicit empty list disables future generation, not saved tasks.
    """
    import care
    from configuration import safe_text
    validate(definitions, {slot['key'] for slot in state['config']['slots']}, safe_text)
    known = copy.deepcopy(state.get('treatment_definitions', {}))
    for item in state['config'].get('treatments', []):
        known.setdefault(item['id'], copy.deepcopy(item))
    for item in definitions:
        if item['id'] in known and known[item['id']] != item:
            raise ValueError('Installed treatment id cannot change prescription or schedule')
        known[item['id']] = copy.deepcopy(item)
    changed = state['config'].get('treatments', []) != definitions
    state['config']['treatments'] = copy.deepcopy(definitions)
    state['treatment_definitions'] = known
    if changed:
        care.audit(state, 'treatments_configured', 'configuration', now,
                   treatment_ids=[item['id'] for item in definitions])


def mark_overdue(state, now):
    """Old tasks ask for confirmation, never another dose."""
    import care
    queued = [item['treatment_task'] for item in state.get('carryovers', {}).values()
              if 'treatment_task' in item]
    for task in list(state['tasks'].values()) + queued:
        if (is_treatment(task['type']) and task['status'] not in care.CLOSED
                and task['treatment_date'] < now.date().isoformat()):
            task['needs_clarification'] = True
            if task['status'] != 'deferred':
                task['status'] = 'needs_clarification'


def sync(state, now):
    """Generate only today's scheduled occurrence, once, regardless of food holds."""
    import care
    mark_overdue(state, now)
    for definition in state['config'].get('treatments', []):
        if not scheduled_today(definition, now.date()):
            continue
        slot_key = (definition.get('first_slot', definition['slot'])
                    if now.date().isoformat() == definition['start'] else definition['slot'])
        index, slot = next((i, slot) for i, slot in enumerate(care.slots_for_day(state, now.date()))
                           if slot['key'] == slot_key)
        at = care.wall(now.date(), slot['time'])
        if at > now:
            continue
        occurrence = definition['id'] + ':' + now.date().isoformat()
        ledger = state.setdefault('treatment_occurrences', {})
        if occurrence in ledger:
            continue
        kind = 'treatment_' + definition['id']
        queued = [item['treatment_task'] for item in state.get('carryovers', {}).values()
                  if 'treatment_task' in item]
        prior = [t for t in list(state['tasks'].values()) + queued
                 if t['type'] == kind and t['status'] not in care.CLOSED]
        if prior:
            # Never create a second medicine task, including against a deferred
            # or transferred occurrence. Closing the old task cannot unblock this
            # date automatically; humans must clarify the prescription first.
            ledger[occurrence] = {'status': 'blocked', 'date': now.date().isoformat(),
                                  'blocked_by': [t['treatment_occurrence'] for t in prior]}
            for task in prior:
                task['needs_clarification'] = True
                if task['status'] != 'deferred':
                    task['status'] = 'needs_clarification'
                task.setdefault('blocked_treatment_dates', []).append(now.date().isoformat())
                task['treatment_announcement_pending'] = True
            care.audit(state, 'treatment_blocked', 'scheduler', now, occurrence=occurrence,
                       task_ids=[t['id'] for t in prior])
            continue
        cid = care.ensure_checklist(state, now.date(), slot, index)
        key = cid + '_' + kind
        if key in state['tasks']:
            raise ValueError('Treatment occurrence collision; manual clarification required')
        task = {'id': key, 'checklist': cid, 'type': kind, 'grams': None,
                'due_at': care.iso(at), 'status': 'pending', 'needs_clarification': False,
                'next_repeat_at': None, 'snoozed_until': None,
                'done_by': None, 'done_at': None, 'skip_by': None, 'skip_at': None,
                'treatment_occurrence': occurrence, 'treatment_date': now.date().isoformat(),
                'treatment_definition': copy.deepcopy(definition),
                'treatment_text': definition['text'], 'treatment_announcement_pending': True}
        state['tasks'][key] = task
        state['checklists'][cid]['task_ids'].append(key)
        ledger[occurrence] = {'date': now.date().isoformat(), 'current_task_id': key}
        care.audit(state, 'treatment_created', 'scheduler', now, occurrence=occurrence, task_id=key)


def move(state, task, actor, now, message_id=None, telegram_user_id=None):
    """Move the one occurrence, not its food or its calendar anchor. Never merge."""
    import care
    at, index, slot = care.next_care(state, now)
    cid = care.checklist_id(at.date(), slot)
    key = cid + '_' + task['type']
    if key in state['tasks'] or key in state.get('carryovers', {}):
        raise ValueError('Обработка следующего ухода уже существует. Уточните перенос; дозы не объединяю.')
    target = copy.deepcopy(task)
    target.update(id=key, checklist=cid, due_at=care.iso(at),
                  status='needs_clarification' if task['needs_clarification'] else 'pending',
                  next_repeat_at=None, snoozed_until=None,
                  treatment_announcement_pending=True,
                  carried_from=task.get('carried_from', []) + [task['id']])
    if cid in state['checklists']:
        state['tasks'][key] = target
        state['checklists'][cid]['task_ids'].append(key)
    else:
        # Save only the medicine transfer. Never create future food or cleaning.
        state.setdefault('carryovers', {})[key] = {
            'checklist': cid, 'type': task['type'], 'due_at': care.iso(at),
            'source_ids': [task['id']], 'treatment_task': target}
    state['treatment_occurrences'][task['treatment_occurrence']]['current_task_id'] = key
    task.update(status='skipped', skip_by=actor, skip_at=care.iso(now),
                skip_reason='next_care', moved_to=key, moved_due_at=care.iso(at),
                next_repeat_at=None, snoozed_until=None, treatment_announcement_pending=False)
    care.audit(state, 'move_to_next', actor, now, task_id=task['id'], target_id=key,
               occurrence=task['treatment_occurrence'], target_due_at=care.iso(at),
               message_id=message_id, telegram_user_id=telegram_user_id)
    return {'status': 'moved', 'food': False, 'treatment': True,
            'target_id': key, 'due_at': care.iso(at)}



def apply_carryover(state, key, item):
    """Attach one queued occurrence when its normal checklist becomes due."""
    target = state['tasks'].get(key)
    queued = item['treatment_task']
    if target is not None:
        if target.get('treatment_occurrence') != queued['treatment_occurrence']:
            raise ValueError('Treatment carry conflicts with another occurrence; review required')
        # An already attached occurrence must not be reopened or duplicated.
        return
    state['tasks'][key] = copy.deepcopy(queued)
    checklist = state['checklists'][item['checklist']]
    if key not in checklist['task_ids']:
        checklist['task_ids'].append(key)


def emission_contains_treatment(state, item):
    """Keep unsent medication emissions addressable while ordinary meals age out."""
    keys = list(item.get('treatment_ids', [])) + list(item.get('repeat_ids', []))
    if item.get('primary'):
        keys.extend(state['checklists'][item['primary']]['task_ids'])
    return any(is_treatment(state['tasks'][key]['type']) for key in keys)


def render(state, tasks):
    """A supplement contains medicine only, with registered task bindings."""
    import care
    lines = ['💊 Обработка']
    for task in tasks:
        checklist = state['checklists'][task['checklist']]
        lines.extend(['', f'{checklist["date"]} · {checklist["time"]} · список {checklist["id"]}',
                      care.task_line(task, state=state)])
    lines.extend(['', 'Подтвердите только уже выполненную обработку.',
                  'Если препарат уже дали, не давайте повторно. Если не уверены, уточните у ветеринара.'])
    return '\n'.join(lines)
