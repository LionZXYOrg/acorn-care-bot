"""Apply an owner-approved food plan once without resetting saved care."""
import copy
import hashlib
import json
import re
from datetime import date, datetime

from configuration import safe_text, validate_care_config


def validate_nutrition_plan(plan, config):
    """Validate the complete replacement before changing the shared record."""
    required = {'id', 'approved_by', 'approved_message', 'approved_at', 'effective',
                'food_products', 'notice_time', 'stages'}
    if (not isinstance(plan, dict) or not required <= set(plan)
            or set(plan) - required - {'care_slots'}):
        raise ValueError('Invalid approved nutrition plan fields')
    if not isinstance(plan['id'], str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', plan['id']):
        raise ValueError('Invalid approved nutrition plan identity')
    for field in ('approved_by', 'approved_message'):
        if not isinstance(plan[field], str) or not plan[field].strip() or len(plan[field]) > 256:
            raise ValueError('Missing nutrition plan approval')
    approved = datetime.fromisoformat(plan['approved_at'])
    if approved.tzinfo is None:
        raise ValueError('Nutrition plan approval requires a timezone')
    effective = date.fromisoformat(plan['effective'])
    if effective < date.fromisoformat(config['start_date']) or effective < approved.date():
        raise ValueError('Nutrition plan cannot change an earlier date')
    if not isinstance(plan['stages'], list) or not plan['stages'] or plan['stages'][0]['start'] != plan['effective']:
        raise ValueError('Nutrition plan requires an initial stage on its effective date')
    if 'care_slots' in plan:
        validate_care_slots(plan['care_slots'])
    candidate = replacement_config(config, plan)
    validate_care_config(candidate)
    return candidate


def validate_care_slots(slots):
    """Require a complete, explicit owner-approved cleaning replacement."""
    if not isinstance(slots, dict) or set(slots) != {'breakfast', 'lunch', 'dinner'}:
        raise ValueError('Invalid nutrition plan care slots')
    for slot in slots.values():
        if not isinstance(slot, dict) or set(slot) != {'cleaning_tasks', 'cleaning_labels'}:
            raise ValueError('Invalid nutrition plan care slot fields')
        tasks, labels = slot['cleaning_tasks'], slot['cleaning_labels']
        if (not isinstance(tasks, list)
                or any(not isinstance(task, str) or task not in {'litter', 'bowls', 'water'}
                       for task in tasks)
                or len(tasks) != len(set(tasks))):
            raise ValueError('Invalid nutrition plan cleaning tasks')
        if (not isinstance(labels, dict) or set(labels) != set(tasks)
                or any(not safe_text(label, 256) for label in labels.values())):
            raise ValueError('Invalid nutrition plan cleaning labels')


def replacement_care_slots(slots, care_slots):
    """Merge only care fields, retaining each schedule's times and metadata."""
    result = copy.deepcopy(slots)
    for slot in result:
        approved = care_slots[slot['key']]
        slot['cleaning'] = bool(approved['cleaning_tasks'])
        slot.update(copy.deepcopy(approved))
    return result


def replacement_config(config, plan):
    candidate = copy.deepcopy(config)
    candidate.pop('nutrition_plan', None)
    candidate['stages'] = [copy.deepcopy(stage) for stage in config['stages']
                           if stage['start'] < plan['effective']] + copy.deepcopy(plan['stages'])
    candidate['food_products'] = copy.deepcopy(plan['food_products'])
    candidate['nutrition_plan_id'] = plan['id']
    candidate['nutrition_notice_time'] = plan['notice_time']
    if 'care_slots' in plan:
        candidate['slots'] = replacement_care_slots(candidate['slots'], plan['care_slots'])
        if 'bowls' in plan['care_slots']['dinner']['cleaning_tasks']:
            candidate['bowls_morning_only'] = False
    return candidate


def apply_nutrition_plan(state, plan, now):
    """Install future portions, preserving task snapshots, transfers, and holds."""
    # Import here because care loads configuration while its module starts.
    import care

    digest = hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    previous = state.get('applied_nutrition_plans', {}).get(plan['id'])
    if previous:
        if previous['sha256'] != digest:
            raise ValueError('An applied nutrition plan identity cannot be reused with different content')
        return False
    candidate = validate_nutrition_plan(plan, state['config'])
    if now.date().isoformat() < plan['effective']:
        return False
    if 'care_slots' in plan:
        today = now.date().isoformat()
        history = copy.deepcopy(state.get('schedule_history', []))
        active = [entry for entry in history if entry['effective'] <= today]
        if not active or active[-1]['effective'] < today:
            slots = active[-1]['slots'] if active else state['config']['slots']
            history.append({'effective': today, 'slots': copy.deepcopy(slots)})
            history.sort(key=lambda entry: entry['effective'])
        for entry in history:
            if entry['effective'] >= today:
                entry['slots'] = replacement_care_slots(entry['slots'], plan['care_slots'])
        pending = copy.deepcopy(state.get('pending_config'))
        if pending:
            pending['slots'] = replacement_care_slots(pending['slots'], plan['care_slots'])
    hold = state.get('progression_hold')
    held_stage = copy.deepcopy(care.stage_for(state, now.date())) if hold else None
    previous_plan = {
        'id': plan['id'], 'at': care.iso(now),
        'stages': copy.deepcopy(state['config']['stages']),
        'food_products': copy.deepcopy(state['config'].get('food_products', {})),
    }
    state.setdefault('nutrition_plan_history', []).append(previous_plan)
    state['config'] = candidate
    if 'care_slots' in plan:
        state['schedule_history'] = history
        if pending:
            state['pending_config'] = pending
    if held_stage is not None:
        state['nutrition_plan_held_stage'] = held_stage
        state['nutrition_plan_hold_since'] = hold['since']
    state['revision'] = state.get('revision', 0) + 1
    state.setdefault('applied_nutrition_plans', {})[plan['id']] = {
        'at': care.iso(now), 'sha256': digest,
        'approved_by': plan['approved_by'], 'approved_at': plan['approved_at'],
        'approved_message': plan['approved_message'],
    }
    care.audit(state, 'nutrition_plan_applied', plan['approved_by'], now,
               plan_id=plan['id'], effective=plan['effective'],
               approved_message=plan['approved_message'], sha256=digest,
               health_hold_preserved=bool(hold))
    return True
