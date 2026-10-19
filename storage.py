"""One process lease, thread serialization, file locks, and durable atomic JSON."""
import copy
import fcntl
import json
import threading
from contextlib import contextmanager

import care
from configuration import validate_care_config


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

    def bind(self, bot_id, chat_id, topic_id, now):
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
            care.audit(s, 'standalone_started', 'service', now, bot_id=bot_id)

    def offset(self):
        return self.snapshot()['standalone']['offset']
