"""One process lease, thread serialization, file locks, and durable atomic JSON."""
import copy
import fcntl
import json
import threading
from contextlib import contextmanager

import care
from configuration import validate_care_config


def morning_only_bowls_slots(slots):
    """Remove only routine nonbreakfast bowls, including implicit cleaning lists."""
    slots = copy.deepcopy(slots)
    for slot in slots:
        if slot['key'] == 'breakfast':
            continue
        kinds = slot.get('cleaning_tasks', list(care.CLEANING) if slot['cleaning'] else [])
        if 'bowls' in kinds:
            slot['cleaning_tasks'] = [kind for kind in kinds if kind != 'bowls']
    return slots


def migrate_morning_only_bowls(s, now):
    """Narrow the saved plan without replaying care or rewriting earlier history."""
    today = care.instant(now.isoformat()).date()
    effective = today.isoformat()
    active = care.slots_for_day(s, today)
    today_slots = morning_only_bowls_slots(active)
    needs_today = today_slots != active
    changed = s['config'].get('bowls_morning_only') is not True
    s['config']['bowls_morning_only'] = True

    plans = [s['config']]
    if s.get('pending_config'):
        plans.append(s['pending_config'])
    history = s.get('schedule_history', [])
    plans.extend(item for item in history if item['effective'] >= effective)
    for plan in plans:
        slots = morning_only_bowls_slots(plan['slots'])
        if slots != plan['slots']:
            plan['slots'] = slots
            changed = True
    if needs_today and not any(item['effective'] == effective for item in history):
        # Insert before future changes so slots_for_day keeps their original order.
        index = next((i for i, item in enumerate(history) if item['effective'] > effective), len(history))
        history.insert(index, {'effective': effective, 'slots': today_slots})
        s['schedule_history'] = history
        changed = True

    skipped = []
    for key, task in s['tasks'].items():
        # A queued transfer can reach an existing task before carried_from is applied.
        if (task['type'] != 'bowls' or task['status'] in care.CLOSED
                or 'carried_from' in task or key in s.get('carryovers', {})):
            continue
        checklist = s['checklists'][task['checklist']]
        if checklist['date'] < effective or checklist['slot'] == 'breakfast':
            continue
        task.update(status='skipped', skip_by='configuration', skip_at=care.iso(now),
                    skip_reason='bowls_morning_only', next_repeat_at=None,
                    snoozed_until=None, needs_clarification=False)
        skipped.append(key)
    if changed or skipped:
        care.audit(s, 'bowls_morning_only_applied', 'configuration', now,
                   effective=effective, task_ids=skipped,
                   reason='Routine bowls and feeding-area cleaning now run only at breakfast.')


class Store:
    def __init__(self, path):
        self.path = path
        self.mutex = threading.RLock()
        self._lease = None

    def acquire_lease(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(str(self.path) + '.service.lock', 'a')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise ValueError('Another service owns this state file') from None
        self._lease = handle

    def close(self):
        if self._lease:
            self._lease.close()
            self._lease = None

    @contextmanager
    def transaction(self):
        with self.mutex, care.locked(self.path) as state:
            yield state

    def snapshot(self):
        with self.mutex:
            return json.loads(self.path.read_text())

    def bind(self, bot_id, chat_id, topic_id, now, *, fresh_start=False):
        """Existing care-only state requires an explicit, offline migration."""
        with self.transaction() as s:
            validate_care_config(s['config'])
            if s['config']['timezone'] != str(care.TZ):
                raise ValueError('Mounted state and configuration timezones differ')
            if 'standalone' not in s:
                # initial() has no transport fields. Existing files cannot be adopted implicitly.
                if self.path.exists():
                    raise ValueError('Existing state requires explicit migration')
                s['standalone'] = {'schema': 1, 'identity': None, 'offset': 0, 'outbox': {}}
                if fresh_start:
                    # Only a new file gets a cutoff. Keep it unchanged on every later bind.
                    cutoff = care.instant(now.isoformat())
                    s['standalone']['fresh_start_cutoff'] = cutoff.isoformat()
            meta = s['standalone']
            if meta.get('schema') != 1:
                raise ValueError('Unsupported standalone state version')
            identity = {'bot_id': bot_id, 'chat_id': chat_id, 'topic_id': topic_id}
            if meta['identity'] is not None and meta['identity'] != identity:
                raise ValueError('State belongs to a different bot, chat, or topic')
            meta['identity'] = identity
            if type(meta['offset']) is not int or meta['offset'] < 0:
                raise ValueError('Invalid persisted Telegram offset')
            for job in meta['outbox'].values():
                if job['status'] == 'sending':
                    job['status'] = 'needs_review'
                    if job.get('panel'):
                        s['inline_panels'][job['panel']]['status'] = 'needs_review'
            # Apply only these explicit startup switches. Keep the rest of the saved plan.
            if care.BOOT_CONFIG.get('bowls_morning_only') is True:
                migrate_morning_only_bowls(s, now)
            if 'automatic_repeats' in care.BOOT_CONFIG:
                enabled = care.BOOT_CONFIG['automatic_repeats']
                if s['config'].get('automatic_repeats', True) != enabled:
                    s['config']['automatic_repeats'] = enabled
                    care.audit(s, 'automatic_repeats_changed', 'configuration', now, enabled=enabled)
            care.audit(s, 'standalone_started', 'service', now, bot_id=bot_id)

    def offset(self):
        return self.snapshot()['standalone']['offset']
