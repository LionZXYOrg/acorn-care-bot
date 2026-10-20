"""Shared Telegram gateway. Linux only; standard library only.

Run: python gateway.py --config /etc/gateway.json
Only this process receives TELEGRAM_BOT_TOKEN_FILE (or TELEGRAM_BOT_TOKEN).
Config: {"state_path":"/data/gateway.sqlite3", "workers":[{
  "id":"acorn", "uid":10001, "socket_path":"/run/acorn/telegram.sock",
  "chat_id":-100123, "topic_id":42, "allowed_user_ids":[101],
  "methods":["getMe","getWebhookInfo","getChatMember","getUpdates",
             "sendMessage","editMessageText","answerCallbackQuery"],
  "receives_updates":true, "legacy_message_ids":[700,701]}]}
Optional top-level legacy_path imports a gateway_seed.py manifest ONCE into a new
database, including its upstream/Acorn offset. The manifest must be gateway-owned
and outside worker-writable mounts. Optional per-worker queue_max_updates (10000),
queue_max_bytes (16777216).
Legacy IDs are administrator assertions of Acorn ownership, NOT a worker API.
No automatic retries of writes. Uncertain sends require operator reconciliation.
"""
import argparse
import contextlib
import fcntl
from html.parser import HTMLParser
from urllib.parse import urlsplit
from collections import deque
import os
from pathlib import Path
import re
import signal
import socket
import socketserver
import sqlite3
import stat
import struct
import threading
import time
import urllib.error
import urllib.request

from configuration import read_token
from gateway_client import MAX_BODY, MAX_RESPONSE, encode, strict_loads
from telegram_api import NoRedirect, TelegramError

UPSTREAM_METHODS = frozenset({
    'getMe', 'getWebhookInfo', 'getChatMember', 'getUpdates', 'sendMessage', 'sendRichMessage',
    'editMessageText', 'editMessageReplyMarkup', 'pinChatMessage',
    'unpinChatMessage', 'deleteMessage', 'answerCallbackQuery'})
WORKER_METHODS = UPSTREAM_METHODS | {'sendRichMessage'}
POLL_TIMEOUT = 20
NETWORK_TIMEOUT = 30
REQUEST_TIMEOUT = 5
SHUTDOWN_GRACE = 5
CALLBACK_TTL = 300
MAX_ID = 2 ** 63 - 1


def integer(value, low=1, high=MAX_ID):
    return type(value) is int and low <= value <= high


def text(value, maximum, *, empty=False, byte_limit=False):
    if not isinstance(value, str) or (not empty and not value):
        return False
    try:
        size = len(value.encode('utf-8')) if byte_limit else len(value.encode('utf-16-le')) // 2
        return size <= maximum and '\x00' not in value
    except UnicodeError:
        return False


def fields(payload, allowed, required=()):
    if not isinstance(payload, dict) or set(payload) - set(allowed) or set(required) - set(payload):
        raise TelegramError(400)


def private_directory(path):
    path = Path(path)
    info = path.stat()
    if (not path.is_absolute() or path.resolve() != path or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid() or info.st_mode & 0o022):
        raise ValueError('Unsafe gateway directory')


def validate_config(value):
    if (not isinstance(value, dict) or not {'state_path', 'workers'} <= set(value)
            or set(value) - {'state_path', 'workers', 'legacy_path'}
            or not isinstance(value['state_path'], str) or not os.path.isabs(value['state_path'])
            or not isinstance(value['workers'], list) or not 1 <= len(value['workers']) <= 16):
        raise ValueError('Invalid gateway configuration')
    if 'legacy_path' in value and (not isinstance(value['legacy_path'], str)
                                   or not os.path.isabs(value['legacy_path'])):
        raise ValueError('Invalid legacy manifest path')
    result = []
    seen = [set(), set(), set(), set()]
    required = {'id', 'uid', 'socket_path', 'chat_id', 'topic_id', 'methods',
                'receives_updates', 'allowed_user_ids'}
    for original in value['workers']:
        if (not isinstance(original, dict) or not required <= set(original)
                or set(original) - required - {'legacy_message_ids', 'queue_max_updates', 'queue_max_bytes'}):
            raise ValueError('Invalid worker configuration')
        w = dict(original)
        if (not isinstance(w['id'], str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', w['id'])
                or not integer(w['uid'], 0, 2 ** 32 - 2)
                or not isinstance(w['socket_path'], str) or not os.path.isabs(w['socket_path'])
                or len(os.fsencode(w['socket_path'])) > 100
                or not integer(w['chat_id'], -MAX_ID) or not w['chat_id']
                or not integer(w['topic_id']) or type(w['receives_updates']) is not bool
                or not isinstance(w['methods'], list) or not all(isinstance(m, str) for m in w['methods'])
                or len(w['methods']) != len(set(w['methods'])) or set(w['methods']) - WORKER_METHODS
                or not isinstance(w['allowed_user_ids'], list)
                or len(w['allowed_user_ids']) > 1000
                or not all(integer(uid) for uid in w['allowed_user_ids'])
                or len(w['allowed_user_ids']) != len(set(w['allowed_user_ids']))):
            raise ValueError('Invalid worker configuration')
        # A disabled receiver may call local getUpdates, but it always receives [].
        if w['receives_updates'] and not w['allowed_user_ids']:
            raise ValueError('An update receiver needs authorized users')
        w.setdefault('legacy_message_ids', [])
        if (not isinstance(w['legacy_message_ids'], list) or len(w['legacy_message_ids']) > 10000
                or not all(integer(mid) for mid in w['legacy_message_ids'])
                or len(w['legacy_message_ids']) != len(set(w['legacy_message_ids']))
                or (w['legacy_message_ids'] and w['id'] != 'acorn')):
            raise ValueError('Invalid legacy ownership list')
        w.setdefault('queue_max_updates', 10000)
        w.setdefault('queue_max_bytes', 16_777_216)
        if (not integer(w['queue_max_updates'], 1, 100000)
                or not integer(w['queue_max_bytes'], 1, 268_435_456)):
            raise ValueError('Invalid queue limit')
        for values, key in zip(seen, (w['id'], w['uid'], w['socket_path'], (w['chat_id'], w['topic_id']))):
            if key in values:
                raise ValueError('Worker identities, UIDs, sockets and routes must be distinct')
            values.add(key)
        result.append(w)
    normalized = {'state_path': value['state_path'], 'workers': result}
    if 'legacy_path' in value:
        normalized['legacy_path'] = value['legacy_path']
    return normalized


class LimitedUpstream:
    """Explicit Telegram allowlist, no redirects/proxies/retries, bounded responses."""
    def __init__(self, token, opener=None):
        self._token = token
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def call(self, method, payload=None):
        if method not in UPSTREAM_METHODS:
            raise ValueError('Unsupported upstream operation')
        request = urllib.request.Request(
            'https://api.telegram.org/bot' + self._token + '/' + method,
            data=encode(payload or {}), headers={'Content-Type': 'application/json'}, method='POST')
        status = 200
        try:
            with self._opener.open(request, timeout=NETWORK_TIMEOUT) as response:
                raw = response.read(2_000_001)
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                raw = exc.read(2_000_001)
            except Exception:
                raw = b''
            finally:
                exc.close()
        except Exception:
            raise TelegramError(uncertain=True) from None
        try:
            if len(raw) > 2_000_000:
                raise ValueError()
            data = strict_loads(raw)
            if not isinstance(data, dict):
                raise ValueError()
        except (ValueError, UnicodeError, RecursionError):
            raise TelegramError(status, uncertain=True) from None
        if status != 200 or data.get('ok') is not True:
            code = data.get('error_code', status)
            if not integer(code, 0, 599):
                code = status
            params = data.get('parameters', {})
            retry = params.get('retry_after', 0) if isinstance(params, dict) else 0
            failure = (data.get('ok') is False and integer(data.get('error_code'), 400, 599)
                       and 'result' not in data and isinstance(params, dict)
                       and integer(retry, 0) and isinstance(data.get('description', ''), str))
            uncertain = (not failure or status not in {200, code} or status >= 500
                         or 300 <= status < 400 or code >= 500 or 300 <= code < 400)
            # Only an explicit, consistent rejection can authorize a worker retry.
            raise TelegramError(code, retry_after=retry if code == 429 and not uncertain else 0,
                                uncertain=uncertain,
                                not_modified=not uncertain and code == 400
                                and 'message is not modified' in data.get('description', '').lower())
        if 'result' not in data:
            raise TelegramError(uncertain=True)
        return data['result']


class QueueFull(Exception):
    """No transaction was committed; do not acknowledge the upstream batch."""


class State:
    def __init__(self, path, workers):
        self.mutex = threading.RLock()
        self.db = self.lease = None
        path = Path(path)
        if not path.parent.exists():
            path.parent.mkdir(parents=True, mode=0o750)
            os.chmod(path.parent, 0o750)
        private_directory(path.parent)
        if path.is_symlink():
            raise ValueError('Unsafe state file')
        try:
            self.lease = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.fchmod(fd, 0o600)
            os.close(fd)
            self.db = sqlite3.connect(path, timeout=5, check_same_thread=False, isolation_level=None)
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS updates (
                    worker TEXT NOT NULL, uid INTEGER NOT NULL, body BLOB NOT NULL,
                    PRIMARY KEY(worker,uid));
                CREATE TABLE IF NOT EXISTS owners (
                    chat INTEGER NOT NULL, mid INTEGER NOT NULL, topic INTEGER NOT NULL,
                    worker TEXT NOT NULL, PRIMARY KEY(chat,mid));
                CREATE TABLE IF NOT EXISTS callbacks (
                    id TEXT PRIMARY KEY, worker TEXT NOT NULL, sender INTEGER NOT NULL,
                    chat INTEGER NOT NULL, topic INTEGER NOT NULL, mid INTEGER NOT NULL,
                    expires REAL NOT NULL, delivered INTEGER NOT NULL DEFAULT 0);
            ''')
            with self.transaction() as db:
                # Persist identities individually so adding a new module needs no reset.
                saved = {key: strict_loads(value) for key, value in db.execute(
                    "SELECT key,value FROM meta WHERE key LIKE 'worker:%'")}
                for w in workers:
                    identity = [w['id'], w['uid'], w['chat_id'], w['topic_id']]
                    key = 'worker:' + w['id']
                    if key in saved and saved[key] != identity:
                        raise ValueError('Worker identity changed; offline migration required')
                    for old_key, old in saved.items():
                        if old_key != key and (old[1] == identity[1] or old[2:] == identity[2:]):
                            raise ValueError('A retired worker identity cannot be reused')
                    db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', (key, encode(identity).decode()))
                db.execute("INSERT OR IGNORE INTO meta VALUES ('offset','0')")
                for w in workers:
                    db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('ack:' + w['id'], '0'))
        except Exception:
            self.close()
            raise

    @contextlib.contextmanager
    def transaction(self):
        with self.mutex:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield self.db
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise

    def offset(self):
        with self.mutex:
            return int(self.db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()[0])

    @staticmethod
    def owns(db, w, mid):
        row = db.execute('SELECT worker,topic FROM owners WHERE chat=? AND mid=?',
                         (w['chat_id'], mid)).fetchone()
        return row == (w['id'], w['topic_id'])

    @staticmethod
    def register(db, w, mid):
        row = db.execute('SELECT worker,topic FROM owners WHERE chat=? AND mid=?',
                         (w['chat_id'], mid)).fetchone()
        if row and row != (w['id'], w['topic_id']):
            raise ValueError('Conflicting message ownership')
        db.execute('INSERT OR IGNORE INTO owners VALUES (?,?,?,?)',
                   (w['chat_id'], mid, w['topic_id'], w['id']))

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        if self.lease is not None:
            os.close(self.lease)
            self.lease = None


class _HTML(HTMLParser):
    TAGS = {'b', 'strong', 'i', 'em', 'u', 'ins', 's', 'strike', 'del',
            'code', 'pre', 'blockquote', 'tg-spoiler'}

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            if len(attrs) != 1 or attrs[0][0] != 'href':
                raise TelegramError(400)
            value = attrs[0][1]
            try:
                parsed = urlsplit(value)
                if (parsed.scheme != 'https' or parsed.hostname != 'www.royalgreenwich.gov.uk'
                        or parsed.port not in (None, 443) or parsed.username or parsed.password
                        or any(ord(char) < 33 for char in value)):
                    raise ValueError()
            except Exception:
                raise TelegramError(400) from None
        elif tag not in self.TAGS or attrs:
            raise TelegramError(400)

    def handle_startendtag(self, tag, attrs):
        raise TelegramError(400)

    def handle_endtag(self, tag):
        if tag not in self.TAGS | {'a'}:
            raise TelegramError(400)

    def handle_comment(self, data):
        raise TelegramError(400)

    def handle_decl(self, decl):
        raise TelegramError(400)

    def handle_pi(self, data):
        raise TelegramError(400)


class _RichHTML(_HTML):
    TAGS = _HTML.TAGS | {'h1', 'h2', 'h3', 'p', 'ul', 'ol', 'li', 'footer', 'br'}



def keyboard(value):
    fields(value, {'inline_keyboard'}, {'inline_keyboard'})
    rows = value['inline_keyboard']
    if not isinstance(rows, list) or len(rows) > 20:
        raise TelegramError(400)
    count = 0
    for row in rows:
        if not isinstance(row, list) or not 1 <= len(row) <= 8:
            raise TelegramError(400)
        for button in row:
            fields(button, {'text', 'callback_data'}, {'text', 'callback_data'})
            if not text(button['text'], 64) or not text(button['callback_data'], 64, byte_limit=True):
                raise TelegramError(400)
            count += 1
    if count > 100:
        raise TelegramError(400)
    return value


def safe_user(user):
    if (not isinstance(user, dict) or not integer(user.get('id'))
            or type(user.get('is_bot')) is not bool):
        return None
    result = {'id': user['id'], 'is_bot': user['is_bot']}
    for key in ('username', 'first_name', 'last_name'):
        if text(user.get(key), 256):
            result[key] = user[key]
    return result


class Gateway:
    def __init__(self, config, upstream, *, pause_path=None):
        self.config = validate_config(config)
        self.workers = {w['id']: w for w in self.config['workers']}
        self.upstream = upstream
        self.pause_path = Path(pause_path) if pause_path is not None else None
        # Protected by state.mutex, like the durable ownership checked by _route.
        self.inflight_sends = {wid: 0 for wid in self.workers}
        self.stop = threading.Event()
        self.changed = threading.Condition()
        self.poll_lock = threading.Lock()
        self.api_slots = threading.BoundedSemaphore(4)
        self.worker_api_slots = {wid: threading.BoundedSemaphore(1) for wid in self.workers}
        self.worker_windows = {wid: deque() for wid in self.workers}
        self.rate_lock = threading.Lock()
        self.worker_polls = {wid: threading.Lock() for wid in self.workers}
        self.servers, self.threads = [], []
        self.last_contact = 0
        self.heartbeat = time.monotonic()
        self.blocked = False
        self.state = None
        # Acquire the process lease BEFORE any Telegram request, even getMe.
        # State binds to the confirmed identity immediately after startup checks.
        self.state = State(self.config['state_path'], self.config['workers'])
        try:
            me = safe_user(upstream.call('getMe'))
            if (not me or me['is_bot'] is not True
                    or not re.fullmatch(r'[A-Za-z0-9_]{5,32}', me.get('username', ''))):
                raise ValueError('Invalid gateway bot identity')
            self.me = me
            webhook = upstream.call('getWebhookInfo')
            if not isinstance(webhook, dict) or webhook.get('url') != '':
                raise ValueError('Webhook must be removed by administrator before startup')
            # State's stable route binding and the real bot binding are separate.
            with self.state.transaction() as db:
                old = db.execute("SELECT value FROM meta WHERE key='bot'").fetchone()
                if old and old[0] != str(me['id']):
                    raise ValueError('Gateway bot identity changed')
                initialized = db.execute("SELECT value FROM meta WHERE key='initialized'").fetchone()
                db.execute("INSERT OR IGNORE INTO meta VALUES ('bot',?)", (str(me['id']),))
                if not initialized:
                    for w in self.workers.values():
                        for mid in w['legacy_message_ids']:
                            State.register(db, w, mid)
                db.execute("INSERT OR IGNORE INTO meta VALUES ('initialized','1')")
            if 'legacy_path' in self.config:
                self.import_legacy(self.config['legacy_path'])
        except Exception:
            self.state.close()
            raise

    def _api(self, method, payload):
        if self.stop.is_set():
            raise TelegramError(503)
        if not self.api_slots.acquire(timeout=1):
            raise TelegramError(429, retry_after=1)
        try:
            return self.upstream.call(method, payload)
        except TelegramError:
            raise
        except Exception:
            raise TelegramError(uncertain=True) from None
        finally:
            self.api_slots.release()

    def _worker_api(self, w, method, payload):
        # One module cannot occupy all upstream request slots or grow storage forever.
        now = time.monotonic()
        with self.rate_lock:
            window = self.worker_windows[w['id']]
            while window and now - window[0] >= 60:
                window.popleft()
            if len(window) >= 60:
                raise TelegramError(429, retry_after=60)
            window.append(now)
        slot = self.worker_api_slots[w['id']]
        if not slot.acquire(timeout=1):
            raise TelegramError(429, retry_after=1)
        try:
            if method in {'sendMessage', 'sendRichMessage'}:
                with self.state.mutex:
                    count = self.state.db.execute('SELECT COUNT(*) FROM owners WHERE worker=?',
                                                   (w['id'],)).fetchone()[0]
                    if count >= 100000:
                        raise TelegramError(400)
            return self._api(method, payload)
        finally:
            slot.release()

    def import_legacy(self, path):
        """Adopt only the trusted pre-worker snapshot, once, under the gateway lease."""
        with self.state.transaction() as db:
            if db.execute("SELECT 1 FROM meta WHERE key='legacy_imported'").fetchone():
                return
            source = Path(path)
            if source.exists():
                info = source.lstat()
                if (not stat.S_ISREG(info.st_mode) or info.st_size > 1_000_000
                        or info.st_mode & 0o022
                        or info.st_uid not in {os.geteuid(), self.workers['acorn']['uid']}):
                    raise ValueError('Invalid legacy ownership snapshot')
                legacy = strict_loads(source.read_bytes())
                keys = {'schema', 'worker', 'bot_id', 'chat_id', 'topic_id', 'offset', 'message_ids'}
                w = self.workers.get('acorn')
                if (not isinstance(legacy, dict) or set(legacy) != keys or type(legacy['schema']) is not int or legacy['schema'] != 1
                        or not w or legacy['worker'] != 'acorn' or type(legacy['bot_id']) is not int or legacy['bot_id'] != self.me['id']
                        or type(legacy['chat_id']) is not int or legacy['chat_id'] != w['chat_id']
                        or type(legacy['topic_id']) is not int or legacy['topic_id'] != w['topic_id']
                        or not integer(legacy['offset'], 0) or not isinstance(legacy['message_ids'], list)
                        or len(legacy['message_ids']) > 100000
                        or not all(integer(mid) for mid in legacy['message_ids'])
                        or len(set(legacy['message_ids'])) != len(legacy['message_ids'])):
                    raise ValueError('Legacy ownership belongs to a different destination or bot')
                if self.state.offset() != 0 or db.execute('SELECT 1 FROM updates LIMIT 1').fetchone():
                    raise ValueError('Legacy ownership must precede gateway polling')
                for mid in legacy['message_ids']:
                    State.register(db, w, mid)
                db.execute("UPDATE meta SET value=? WHERE key='offset'", (str(legacy['offset']),))
                db.execute('UPDATE meta SET value=? WHERE key=?',
                           (str(legacy['offset']), 'ack:acorn'))
            db.execute("INSERT INTO meta VALUES ('legacy_imported','1')")

    def _scope(self, w, message):
        return (isinstance(message, dict) and isinstance(message.get('chat'), dict)
                and type(message['chat'].get('id')) is int and message['chat']['id'] == w['chat_id']
                and type(message.get('message_thread_id')) is int
                and message['message_thread_id'] == w['topic_id']
                and integer(message.get('message_id')))

    def _route(self, db, update):
        # Never deliver edited/channel/inline/unrelated updates, or mixed envelopes.
        kinds = set(update) - {'update_id'}
        if kinds not in ({'message'}, {'callback_query'}):
            return None
        cb = update.get('callback_query')
        message = cb.get('message') if isinstance(cb, dict) else update.get('message')
        for w in self.workers.values():
            if not w['receives_updates'] or not self._scope(w, message):
                continue
            if any(k in message for k in ('sender_chat', 'via_bot', 'forward_origin')):
                return None
            sender = safe_user(cb.get('from') if isinstance(cb, dict) else message.get('from'))
            if not sender or sender['is_bot'] or sender['id'] not in w['allowed_user_ids']:
                return None
            author = safe_user(message.get('from'))
            msg = {'message_id': message['message_id'], 'chat': {'id': w['chat_id']},
                   'message_thread_id': w['topic_id'], 'from': author}
            if integer(message.get('date'), 0):
                msg['date'] = message['date']
            if text(message.get('text'), 4096, empty=True):
                msg['text'] = message['text']
            if cb is not None:
                if (not author or author['id'] != self.me['id'] or not author['is_bot']
                        or not text(cb.get('id'), 256, byte_limit=True)
                        or not text(cb.get('data'), 64, byte_limit=True)):
                    return None
                if not State.owns(db, w, message['message_id']):
                    known = db.execute('SELECT 1 FROM owners WHERE chat=? AND mid=?',
                                       (w['chat_id'], message['message_id'])).fetchone()
                    if not known and self.inflight_sends[w['id']]:
                        # Telegram can expose a button before returning its send receipt.
                        # Roll back the WHOLE batch; callbacks never establish ownership.
                        raise QueueFull()
                    return None
                body = {'update_id': update['update_id'], 'callback_query': {
                    'id': cb['id'], 'data': cb['data'], 'from': sender, 'message': msg}}
                return w, body, (cb['id'], sender['id'], message['message_id'])
            # Keep the addressed event, but strip nested replies/forwards/media.
            msg['from'] = sender
            return w, {'update_id': update['update_id'], 'message': msg}, None
        return None

    def ingest(self, updates):
        if not isinstance(updates, list) or len(updates) > 100:
            raise TelegramError(uncertain=True)
        with self.state.transaction() as db:
            offset = int(db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()[0])
            previous = -1
            db.execute('DELETE FROM callbacks WHERE expires < ?', (time.time(),))
            for update in updates:
                if (not isinstance(update, dict) or not integer(update.get('update_id'), 0, MAX_ID - 1)
                        or update['update_id'] <= previous):
                    raise TelegramError(uncertain=True)
                previous = update['update_id']
                if update['update_id'] < offset:
                    continue
                routed = self._route(db, update)
                if routed:
                    w, body, cb = routed
                    raw = encode(body)
                    db.execute('INSERT OR IGNORE INTO updates VALUES (?,?,?)', (w['id'], update['update_id'], raw))
                    if cb:
                        old = db.execute('SELECT worker,sender,chat,topic,mid FROM callbacks WHERE id=?', (cb[0],)).fetchone()
                        expected = (w['id'], cb[1], w['chat_id'], w['topic_id'], cb[2])
                        if old and old != expected:
                            raise ValueError('Conflicting callback capability')
                        db.execute('INSERT OR IGNORE INTO callbacks(id,worker,sender,chat,topic,mid,expires) VALUES (?,?,?,?,?,?,?)',
                                   (cb[0], *expected, time.time() + CALLBACK_TTL))
                offset = update['update_id'] + 1
            for w in self.workers.values():
                count, size = db.execute('SELECT COUNT(*),COALESCE(SUM(LENGTH(body)),0) FROM updates WHERE worker=?', (w['id'],)).fetchone()
                callbacks = db.execute('SELECT COUNT(*) FROM callbacks WHERE worker=?', (w['id'],)).fetchone()[0]
                if count > w['queue_max_updates'] or size > w['queue_max_bytes'] or callbacks > w['queue_max_updates']:
                    raise QueueFull()
            db.execute("UPDATE meta SET value=? WHERE key='offset'", (str(offset),))
        self.blocked = False
        with self.changed:
            self.changed.notify_all()

    def poll_once(self):
        if not self.poll_lock.acquire(blocking=False):
            raise RuntimeError('Only one upstream poller is allowed')
        try:
            # Check under poll_lock too: /drainz must not race a newly started poll.
            if self.stop.is_set() or self.paused():
                return
            updates = self.upstream.call('getUpdates', {'offset': self.state.offset(),
                'timeout': POLL_TIMEOUT, 'limit': 100, 'allowed_updates': ['message', 'callback_query']})
            self.last_contact = time.monotonic()
            try:
                self.ingest(updates)
            except QueueFull:
                self.blocked = True
                raise
        finally:
            self.poll_lock.release()

    def poll_loop(self):
        failures = 0
        while not self.stop.is_set():
            self.heartbeat = time.monotonic()
            if self.paused():
                self.stop.wait(0.25)
                continue
            try:
                self.poll_once()
                failures = 0
                self.stop.wait(0.05)  # Also bound an immediately returning upstream.
            except QueueFull:
                self.stop.wait(0.5)  # Offset unchanged; Telegram must retain this batch.
            except TelegramError as exc:
                if exc.code in {401, 403, 409}:
                    self.stop.set()
                    break
                failures += 1
                self.stop.wait(exc.retry_after or min(30, 2 ** min(failures, 5)))
            except Exception:
                self.stop.set()
                break

    def _updates(self, w, p):
        fields(p, {'offset', 'limit', 'timeout', 'allowed_updates'})
        offset, limit, timeout = p.get('offset', 0), p.get('limit', 100), p.get('timeout', 0)
        if (not integer(offset, 0) or not integer(limit, 1, 100) or not integer(timeout, 0, 30)
                or ('allowed_updates' in p and p['allowed_updates'] != ['message', 'callback_query'])):
            raise TelegramError(400)
        if not w['receives_updates']:
            return []
        lock = self.worker_polls[w['id']]
        if not lock.acquire(blocking=False):
            raise TelegramError(409)
        try:
            deadline = time.monotonic() + timeout
            while True:
                with self.state.transaction() as db:
                    ack = int(db.execute('SELECT value FROM meta WHERE key=?', ('ack:' + w['id'],)).fetchone()[0])
                    if offset < ack or offset > self.state.offset():
                        raise TelegramError(400)
                    # Caller supplies its DURABLY SAVED offset. A worker can only prune itself.
                    db.execute('DELETE FROM updates WHERE worker=? AND uid<?', (w['id'], offset))
                    db.execute('UPDATE meta SET value=? WHERE key=?', (str(offset), 'ack:' + w['id']))
                    rows = db.execute('SELECT uid,body FROM updates WHERE worker=? ORDER BY uid LIMIT ?', (w['id'], limit)).fetchall()
                    result, size = [], 0
                    for uid, raw in rows:
                        if size + len(raw) > MAX_RESPONSE - 1000:
                            break
                        result.append(strict_loads(raw))
                        size += len(raw) + 1
                    for item in result:
                        if 'callback_query' in item:
                            db.execute('UPDATE callbacks SET delivered=1 WHERE id=? AND worker=?', (item['callback_query']['id'], w['id']))
                remaining = deadline - time.monotonic()
                if result or remaining <= 0 or self.stop.is_set():
                    return result
                with self.changed:
                    self.changed.wait(min(remaining, 0.25))
        finally:
            lock.release()

    def call(self, worker_id, method, payload=None):
        w = self.workers.get(worker_id)
        if not w or method not in w['methods']:
            raise TelegramError(403)
        p = dict(payload) if isinstance(payload, dict) else payload
        if p is None:
            p = {}
        if not isinstance(p, dict):
            raise TelegramError(400)
        if self.stop.is_set():
            raise TelegramError(503)
        if method in {'getMe', 'getWebhookInfo'}:
            fields(p, set())
            return dict(self.me) if method == 'getMe' else {'url': ''}
        if method == 'getUpdates':
            return self._updates(w, p)
        if method == 'answerCallbackQuery':
            fields(p, {'callback_query_id', 'text', 'show_alert', 'cache_time'}, {'callback_query_id'})
            if (not text(p['callback_query_id'], 256, byte_limit=True)
                    or ('text' in p and not text(p['text'], 200, empty=True))
                    or ('show_alert' in p and type(p['show_alert']) is not bool)
                    or ('cache_time' in p and p['cache_time'] != 0)):
                raise TelegramError(400)
            with self.state.mutex:
                row = self.state.db.execute('SELECT sender,mid,expires,delivered FROM callbacks WHERE id=? AND worker=? AND chat=? AND topic=?',
                    (p['callback_query_id'], w['id'], w['chat_id'], w['topic_id'])).fetchone()
                if (not row or row[0] not in w['allowed_user_ids'] or row[2] < time.time()
                        or not row[3] or not State.owns(self.state.db, w, row[1])):
                    raise TelegramError(403)
            return self._worker_api(w, method, p)
        if method == 'getChatMember':
            fields(p, {'chat_id', 'user_id'}, {'chat_id', 'user_id'})
            if (type(p['chat_id']) is not int or p['chat_id'] != w['chat_id']
                    or not integer(p['user_id']) or p['user_id'] not in w['allowed_user_ids'] + [self.me['id']]):
                raise TelegramError(403)
            result = self._worker_api(w, method, p)
            user = safe_user(result.get('user')) if isinstance(result, dict) else None
            if not user or user['id'] != p['user_id']:
                raise TelegramError(uncertain=True)
            member = {'user': user, 'status': result.get('status', '')}
            for capability in ('can_pin_messages', 'can_delete_messages'):
                if type(result.get(capability)) is bool:
                    member[capability] = result[capability]
            return member
        sends = method in {'sendMessage', 'sendRichMessage'}
        edits = method in {'editMessageText', 'editMessageReplyMarkup'}
        allowed = {'chat_id', 'message_thread_id'}
        required = {'chat_id'}
        if sends:
            allowed |= {'reply_markup', 'disable_notification', 'protect_content', 'link_preview_options', 'disable_web_page_preview'}
            if method == 'sendRichMessage':
                allowed |= {'rich_message'}
                required |= {'rich_message'}
            else:
                allowed |= {'text', 'parse_mode'}
                required |= {'text'}
        else:
            allowed.add('message_id')
            required.add('message_id')
            if method == 'editMessageText':
                allowed |= {'text', 'reply_markup', 'parse_mode', 'disable_web_page_preview'}
                required.add('text')
            elif method == 'editMessageReplyMarkup':
                allowed.add('reply_markup')
                required.add('reply_markup')
            elif method == 'pinChatMessage':
                allowed.add('disable_notification')
        fields(p, allowed, required)
        if (type(p['chat_id']) is not int or p['chat_id'] != w['chat_id']
                or ('message_thread_id' in p and (type(p['message_thread_id']) is not int or p['message_thread_id'] != w['topic_id']))):
            raise TelegramError(403)
        for key in ('disable_notification', 'protect_content', 'disable_web_page_preview'):
            if key in p and type(p[key]) is not bool:
                raise TelegramError(400)
        if 'text' in p and not text(p['text'], 4096):
            raise TelegramError(400)
        if 'reply_markup' in p:
            keyboard(p['reply_markup'])
        if 'link_preview_options' in p:
            fields(p['link_preview_options'], {'is_disabled'}, {'is_disabled'})
            if p['link_preview_options']['is_disabled'] is not True:
                raise TelegramError(400)
        if method == 'sendRichMessage':
            fields(p['rich_message'], {'html'}, {'html'})
            if not text(p['rich_message']['html'], 32768):
                raise TelegramError(400)
            parser = _RichHTML(convert_charrefs=True)
            parser.feed(p['rich_message']['html'])
            parser.close()
        if 'parse_mode' in p:
            if (p['parse_mode'] != 'HTML' or 'sendRichMessage' not in w['methods']
                    or method not in {'sendMessage', 'editMessageText'}):
                raise TelegramError(400)
            parser = _HTML(convert_charrefs=True)
            parser.feed(p['text'])
            parser.close()
        if sends:
            p['message_thread_id'] = w['topic_id']
        else:
            if not integer(p['message_id']):
                raise TelegramError(400)
            with self.state.mutex:
                if not State.owns(self.state.db, w, p['message_id']):
                    raise TelegramError(403)
            p.pop('message_thread_id', None)
        if 'text' in p:
            p['disable_web_page_preview'] = True
        if method == 'pinChatMessage':
            p['disable_notification'] = True
        if sends:
            with self.state.mutex:
                self.inflight_sends[w['id']] += 1
        try:
            result = self._worker_api(w, method, p)
            if sends or edits:
                author = safe_user(result.get('from')) if isinstance(result, dict) else None
                if (not self._scope(w, result) or not author or author['id'] != self.me['id']
                        or author['is_bot'] is not True
                        or (not sends and result['message_id'] != p['message_id'])):
                    raise TelegramError(uncertain=True)
                if sends:
                    try:
                        with self.state.transaction() as db:
                            State.register(db, w, result['message_id'])
                    except Exception:
                        raise TelegramError(uncertain=True) from None
            elif result is not True:
                raise TelegramError(uncertain=True)
            return result
        finally:
            if sends:
                with self.state.mutex:
                    self.inflight_sends[w['id']] -= 1

    def paused(self):
        return self.pause_path is not None and self.pause_path.exists()

    def drained(self):
        if not self.paused() or not self.poll_lock.acquire(blocking=False):
            return False
        try:
            with self.state.mutex:
                return (self.paused() and self.state.db is not None
                        and self.state.db.execute('SELECT 1 FROM updates LIMIT 1').fetchone() is None)
        finally:
            self.poll_lock.release()

    def live(self):
        return not self.stop.is_set() and time.monotonic() - self.heartbeat < 100

    def ready(self):
        return (self.live() and not self.paused() and not self.blocked
                and 0 < self.last_contact and time.monotonic() - self.last_contact < 90)

    def start(self, *, health_host='0.0.0.0', health_port=8081, poll=True):
        try:
            for w in self.workers.values():
                parent = Path(w['socket_path']).parent
                if not parent.exists():
                    parent.mkdir(parents=True, mode=0o750)
                    os.chmod(parent, 0o750)
                private_directory(parent)
                path = w['socket_path']
                if os.path.lexists(path):
                    if not stat.S_ISSOCK(os.lstat(path).st_mode):
                        raise ValueError('Refusing to replace a non-socket')
                    os.unlink(path)  # Lease held; only stale sockets in trusted directories.
                server = _UnixServer(path, self, w)
                self.servers.append(server)
                server.inode = os.lstat(path).st_ino
                os.chmod(path, 0o660)
                server.inode = os.lstat(path).st_ino
            if health_port is not None:
                self.servers.append(_HealthServer((health_host, health_port), self))
            for server in self.servers:
                thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
                thread.start()
                self.threads.append(thread)
                server.started.wait(1)
            if poll:
                thread = threading.Thread(target=self.poll_loop, name='telegram-poller', daemon=True)
                thread.start()
                self.threads.append(thread)
        except Exception:
            self.close()
            raise
        return self

    def close(self):
        self.stop.set()
        with self.changed:
            self.changed.notify_all()
        deadline = time.monotonic() + NETWORK_TIMEOUT + SHUTDOWN_GRACE
        for server in self.servers:
            # Stop all listeners first; process_request also rejects on stop.
            if getattr(server, 'serving', False):
                server.shutdown()
        for server in self.servers:
            server.close_active(deadline)
            server.server_close()
            if isinstance(server, _UnixServer) and getattr(server, 'inode', None):
                try:
                    if os.lstat(server.server_address).st_ino == server.inode:
                        os.unlink(server.server_address)
                except FileNotFoundError:
                    pass
        for thread in self.threads:
            thread.join(max(0, deadline - time.monotonic()))
        # Overdue daemon handlers cannot be killed. Keep the DB/lease until they exit.
        if any(t.is_alive() for t in self.threads) or any(server.active for server in self.servers):
            raise RuntimeError('Gateway did not stop within its network deadline')
        self.servers.clear()
        self.threads.clear()
        if self.state:
            self.state.close()


def error_body(exc):
    return {'ok': False, 'error': {'code': exc.code, 'retry_after': exc.retry_after,
                                  'uncertain': exc.uncertain, 'not_modified': exc.not_modified}}


def read_http(connection):
    """Small one-shot HTTP subset: absolute deadline, bounded headers/body, no chunks."""
    deadline = time.monotonic() + REQUEST_TIMEOUT
    raw = b''
    while b'\r\n\r\n' not in raw:
        connection.settimeout(max(0.001, deadline - time.monotonic()))
        part = connection.recv(1024)
        if not part or time.monotonic() >= deadline:
            raise ValueError()
        raw += part
        if len(raw.split(b'\r\n\r\n', 1)[0]) > 8192:
            raise ValueError()
    header, body = raw.split(b'\r\n\r\n', 1)
    lines = header.decode('ascii').split('\r\n')
    verb, path, version = lines[0].split(' ')
    if version not in {'HTTP/1.0', 'HTTP/1.1'}:
        raise ValueError()
    headers = {}
    for line in lines[1:]:
        key, value = line.split(':', 1)
        if not re.fullmatch(r'[A-Za-z0-9-]+', key):
            raise ValueError()
        key = key.lower()
        if key in headers:
            raise ValueError()
        headers[key] = value.strip()
    length = headers.get('content-length', '0')
    if (not re.fullmatch(r'[0-9]{1,6}', length) or int(length) > MAX_BODY
            or 'transfer-encoding' in headers or 'expect' in headers):
        raise ValueError()
    length = int(length)
    if len(body) > length:
        raise ValueError()
    while len(body) < length:
        connection.settimeout(max(0.001, deadline - time.monotonic()))
        part = connection.recv(min(8192, length - len(body)))
        if not part or time.monotonic() >= deadline:
            raise ValueError()
        body += part
    return verb, path, headers, body


def write_http(connection, status, body, content_type='application/json'):
    connection.settimeout(REQUEST_TIMEOUT)
    connection.sendall(('HTTP/1.1 %d Response\r\nContent-Type: %s\r\nContent-Length: %d\r\nConnection: close\r\n\r\n'
                        % (status, content_type, len(body))).encode('ascii') + body)


class _BoundedServer(socketserver.ThreadingMixIn):
    daemon_threads = True
    block_on_close = False

    def setup_bounds(self, gateway):
        self.gateway = gateway
        self.slots = threading.BoundedSemaphore(4)
        self.active_lock = threading.Condition()
        self.active = set()
        self.admitted = set()
        self.serving = False
        self.started = threading.Event()

    def serve_forever(self, poll_interval=0.05):
        self.serving = True
        self.started.set()
        try:
            super().serve_forever(poll_interval)
        finally:
            self.serving = False

    def process_request(self, request, address):
        with self.active_lock:
            if self.gateway.stop.is_set() or not self.slots.acquire(blocking=False):
                request.close()
                return
            self.active.add(request)
        try:
            super().process_request(request, address)
        except Exception:
            with self.active_lock:
                self.active.discard(request)
                self.active_lock.notify_all()
            self.slots.release()
            request.close()

    def admit(self, request):
        with self.active_lock:
            if self.gateway.stop.is_set() or request not in self.active:
                return False
            self.admitted.add(request)
            return True

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            with self.active_lock:
                self.active.discard(request)
                self.admitted.discard(request)
                self.active_lock.notify_all()
            self.slots.release()

    @staticmethod
    def _close_connection(connection):
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        connection.close()

    def close_active(self, deadline=None):
        if deadline is None:
            deadline = time.monotonic() + NETWORK_TIMEOUT + SHUTDOWN_GRACE
        with self.active_lock:
            # Incomplete/unadmitted requests have no upstream writes to preserve.
            for connection in self.active - self.admitted:
                self._close_connection(connection)
            while self.active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    for connection in self.active:
                        self._close_connection(connection)
                    return
                # Admitted handlers keep their sockets through the confirmed receipt.
                self.active_lock.wait(remaining)

    def handle_error(self, request, address):
        pass  # Never log payloads, addresses, socket paths, tokens or tracebacks.


class _UnixHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            _, uid, _ = struct.unpack('3i', self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != self.server.worker['uid']:
                write_http(self.request, 403, encode(error_body(TelegramError(403))))
                return
            verb, path, headers, body = read_http(self.request)
            if verb != 'POST' or path != '/call' or headers.get('content-type') != 'application/json':
                raise TelegramError(400)
            envelope = strict_loads(body)
            fields(envelope, {'method', 'payload'}, {'method', 'payload'})
            if not isinstance(envelope['method'], str) or not isinstance(envelope['payload'], dict):
                raise TelegramError(400)
            if not self.server.admit(self.request):
                raise TelegramError(503)
            try:
                result = self.server.gateway.call(self.server.worker['id'], envelope['method'], envelope['payload'])
                response = encode({'ok': True, 'result': result})
                if len(response) > MAX_RESPONSE:
                    raise TelegramError(uncertain=True)
            except TelegramError:
                raise
            except Exception:
                # Could be a DB/serialization error AFTER Telegram accepted a write.
                raise TelegramError(503, uncertain=True) from None
            write_http(self.request, 200, response)
        except TelegramError as exc:
            write_http(self.request, 200, encode(error_body(exc)))
        except (ValueError, UnicodeError, RecursionError):
            write_http(self.request, 400, encode(error_body(TelegramError(400))))
        except OSError:
            pass


class _UnixServer(_BoundedServer, socketserver.UnixStreamServer):
    request_queue_size = 8

    def __init__(self, path, gateway, worker):
        self.setup_bounds(gateway)
        self.worker = worker
        super().__init__(path, _UnixHandler)


class _HealthHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            verb, path, _, body = read_http(self.request)
            if verb != 'GET' or path not in {'/livez', '/readyz', '/drainz'} or body:
                write_http(self.request, 404, b'not found\n', 'text/plain')
                return
            check = {'/livez': self.server.gateway.live, '/readyz': self.server.gateway.ready,
                     '/drainz': self.server.gateway.drained}
            ok = check[path]()
            write_http(self.request, 200 if ok else 503, b'ok\n' if ok else b'not ready\n', 'text/plain')
        except (ValueError, UnicodeError, OSError):
            pass


class _HealthServer(_BoundedServer, socketserver.TCPServer):
    allow_reuse_address = True
    request_queue_size = 8

    def __init__(self, address, gateway):
        self.setup_bounds(gateway)
        super().__init__(address, _HealthHandler)


def trusted_pause_path(value, workers):
    path = Path(value)
    private_directory(path.parent)
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError('Unsafe gateway pause path')
    for w in workers:
        if w['uid'] == os.geteuid():
            raise ValueError('Pause path requires a separate gateway identity')
        parent = Path(w['socket_path']).parent
        if path == parent or parent in path.parents:
            raise ValueError('Pause path must be outside worker socket directories')
    if path.exists():
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o022):
            raise ValueError('Unsafe gateway pause file')
    return path


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='Private Unix Telegram gateway')
    parser.add_argument('--config', default=os.environ.get('GATEWAY_CONFIG_PATH'))
    parser.add_argument('--health-host', default='0.0.0.0')
    parser.add_argument('--health-port', type=int, default=int(os.environ.get('GATEWAY_HTTP_PORT', '8081')))
    args = parser.parse_args()
    gateway = None
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        with open(args.config, 'rb') as config_file:
            raw = config_file.read(262145)
        if len(raw) > 262144:
            raise ValueError()
        config = strict_loads(raw)
        if 'GATEWAY_STATE_PATH' in os.environ:
            config['state_path'] = os.environ['GATEWAY_STATE_PATH']
        # State creates its gateway-owned private directory before pause validation.
        # A Kubernetes volume root can be root-owned and group-writable.
        gateway = Gateway(config, LimitedUpstream(read_token()))
        default_pause = str(Path(config['state_path']).with_name('pause-polling'))
        gateway.pause_path = trusted_pause_path(
            os.environ.get('GATEWAY_PAUSE_PATH', default_pause), config['workers'])
        gateway.import_legacy(os.environ.get('GATEWAY_LEGACY_PATH', '/gateway/legacy.json'))
        if stop.is_set():
            return 0
        gateway.start(health_host=args.health_host, health_port=args.health_port)
        while not stop.wait(0.2) and not gateway.stop.is_set():
            pass
        return 0 if stop.is_set() else 1
    finally:
        if gateway:
            gateway.close()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        # Intentionally static: exception strings may contain secret URLs or file paths.
        print('Gateway stopped; inspect configuration and state offline', file=__import__('sys').stderr)
        raise SystemExit(1) from None
