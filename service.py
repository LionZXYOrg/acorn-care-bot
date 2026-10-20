"""Standalone Acorn care service. One poller and one serialized dispatch worker."""
import argparse
import json
import logging
import os
import random
import re
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import care
import inline_ui
from configuration import Runtime, read_token
from storage import Store
from telegram_api import Client, TelegramError

LOG = logging.getLogger('acorn')
HELP = ('Используйте кнопки или точную команду для одного сохранённого пункта:\n'
        '/done YYYY-MM-DD_HH:MM_wet\n/skip YYYY-MM-DD_HH:MM_wet\n'
        '/snooze YYYY-MM-DD_HH:MM_wet 30\n'
        'Виды: wet, dry, litter, bowls, water. Отложить можно на 30 или 60 минут.\n'
        '/health_hold причина: остановить смену этапов, но не уход.\n'
        'Общее «готово» не закрывает задачи. Старые порции не нужно выдавать повторно.')


def chunks(text, limit=3500):
    """Bound UTF-16 size, which Telegram uses for text limits. Keep line breaks."""
    result, current, size = [], '', 0
    for line in text.splitlines(keepends=True):
        units = len(line.encode('utf-16-le')) // 2
        if units > limit:
            if current:
                result.append(current)
                current, size = '', 0
            for char in line:
                n = len(char.encode('utf-16-le')) // 2
                if size + n > limit:
                    result.append(current)
                    current, size = '', 0
                current += char
                size += n
        else:
            if size + units > limit:
                result.append(current)
                current, size = '', 0
            current += line
            size += units
    if current:
        result.append(current)
    return result or ['Нет открытых задач.']


def plain(text):
    return text.replace('**Сегодня меняются порции**', 'Сегодня меняются порции')


class Bot:
    def __init__(self, runtime, client, bot_id, username, store=None, clock=care.instant):
        self.runtime, self.client = runtime, client
        self.bot_id, self.username = bot_id, username
        self.store = store or Store(runtime.state_path)
        self.clock = clock
        self.stop = threading.Event()
        self.last_poll = self.last_worker = 0.0
        self.started = time.monotonic()
        care.BUTTON_BOT = '@' + username
        care.BUTTON_TEST = '✅ Тест кнопки @' + username

    def queue_text(self, state, key, text):
        state['standalone']['outbox'].setdefault(key, {
            'status': 'pending', 'texts': chunks(plain(text)), 'message_ids': [],
            'panel': None, 'retry_at': 0})

    def scoped_message(self, message):
        sender = message.get('from', {})
        return (type(sender.get('id')) is int and sender['id'] in self.runtime.allowed_ids
                and sender.get('is_bot') is False
                and message.get('chat', {}).get('id') == self.runtime.chat_id
                and message.get('message_thread_id') == self.runtime.topic_id
                and not any(message.get(k) for k in ('sender_chat', 'via_bot', 'forward_origin')))

    def status(self, state, now):
        view = json.loads(json.dumps(state))
        care.mark_old_feedings(view, now)
        tasks = [t for t in care.unresolved(view) if care.due(t) <= now]
        text = care.render_reminders(view, tasks) if tasks else 'Незакрытых задач нет.'
        stage = care.stage_for(view, now.date())
        return ('Acorn: ' + care.stage_label(stage, now.date(), view) + '\n' + text + '\n\n' + HELP)

    def message(self, state, message, now):
        if not self.scoped_message(message) or not isinstance(message.get('text'), str):
            return None
        text = message['text'].strip()
        actor = 'telegram:' + str(message['from']['id'])
        mid = 'telegram_message_' + str(message.get('message_id'))
        if text.startswith('/'):
            command, _, rest = text.partition(' ')
            command, _, addressed = command.partition('@')
            if addressed and addressed.lower() != self.username.lower():
                return None
            if command == '/status' and not rest:
                care.remember_caregiver(state, message['from'])
                return self.status(state, now)
            if command == '/health_hold' and 1 <= len(rest.strip()) <= 500:
                care.remember_caregiver(state, message['from'])
                answer = care.health_hold(state, actor, rest.strip(), now)
                care.audit(state, 'telegram_health_hold', actor, now,
                           telegram_user_id=message['from']['id'], message_id=mid)
                return answer
            match = re.fullmatch(r'(\d{4}-\d\d-\d\d_\d\d:\d\d)_(wet|dry|litter|bowls|water)(?: (30|60))?', rest)
            if command not in {'/done', '/skip', '/snooze'} or not match:
                return HELP
            if (command == '/snooze') != bool(match[3]):
                return HELP
            args = argparse.Namespace(checklist=match[1], types=[match[2]], action=command[1:],
                                      actor=actor, message_id=mid, original_message_id=None,
                                      minutes=int(match[3]) if match[3] else None)
            try:
                # Validate the future-task guard before changing old-feeding labels.
                key = match[1] + '_' + match[2]
                if key not in state['tasks'] or care.due(state['tasks'][key]) > now:
                    return 'Задача не найдена или её время ещё не наступило. Отметка не сохранена.'
                care.remember_caregiver(state, message['from'])
                care.mark_old_feedings(state, now)
                answer = care.update(state, args, now)
            except ValueError:
                return 'Точная команда не принята. Отметка не сохранена.\n' + HELP
        else:
            # Legacy reply-keyboard text must match one stored task, including grams/date/bot.
            if not text.startswith('✅ '):
                return HELP if text.lower() in {'done', 'готово', 'всё', 'все', 'сделано'} else None
            try:
                answer = care.complete_button(state, text, actor, mid, now, sender=message['from'])
            except ValueError:
                return 'Текст не соответствует одной доступной задаче. Отметка не сохранена.'
        care.audit(state, 'telegram_task_command', actor, now,
                   telegram_user_id=message['from']['id'], message_id=mid)
        return answer

    def refresh_caregivers(self):
        """Resolve existing numeric authors at startup without changing task records."""
        profiles = []
        for uid in sorted(self.runtime.allowed_ids):
            try:
                member = self.client.call('getChatMember', {'chat_id': self.runtime.chat_id,
                                                           'user_id': uid})
            except TelegramError as exc:
                LOG.warning('Caregiver name lookup unavailable (code %d)', exc.code)
                continue
            user = member.get('user') if isinstance(member, dict) else None
            if (isinstance(user, dict) and type(user.get('id')) is int
                    and user['id'] == uid and user.get('is_bot') is False):
                profiles.append(user)
        if profiles:
            with self.store.transaction() as state:
                for user in profiles:
                    care.remember_caregiver(state, user)

    def process_update(self, update):
        """Commit the care change, receipt, reply job, and offset before any acknowledgement."""
        uid = update.get('update_id')
        if type(uid) is not int or uid < 0:
            raise ValueError('Malformed Telegram update')
        answer, callback_id = None, None
        now = self.clock()
        with self.store.transaction() as state:
            meta = state['standalone']
            if uid < meta['offset']:
                return
            callback = update.get('callback_query')
            if isinstance(callback, dict):
                answer = inline_ui.accept_callback(state, callback, self.runtime.allowed_ids,
                                                   self.bot_id, now, self.runtime.chat_id,
                                                   self.runtime.topic_id)
                callback_id = callback.get('id')
            elif isinstance(update.get('message'), dict):
                text = self.message(state, update['message'], now)
                if text:
                    self.queue_text(state, 'update:' + str(uid), text)
            meta['offset'] = uid + 1
        if answer and isinstance(callback_id, str) and 0 < len(callback_id) <= 256:
            try:
                self.client.call('answerCallbackQuery', {'callback_query_id': callback_id, **answer})
            except TelegramError:
                LOG.warning('Callback acknowledgement failed; saved care is unchanged')

    def prepare_reminder(self, state, eid, text, now):
        item = state['emissions'][eid]
        return inline_ui.prepare(state, chunks(plain(text)), care.emission_task_ids(state, eid),
                                 now, primary=item['primary'])

    def dispatch_once(self):
        now = self.clock()
        # Serialize preparation, sending, and completion. No callback can race a stale send.
        with self.store.mutex:
            with self.store.transaction() as state:
                text = care.tick(state, now)
                if text != 'NO_REPLY':
                    eid = state['last_emission_id']
                    panel = self.prepare_reminder(state, eid, text, now)
                    state['standalone']['outbox']['reminder:' + eid] = {
                        'status': 'pending', 'panel': panel['nonce'], 'emission': eid,
                        'message_ids': [], 'retry_at': 0}
            keys = [k for k, j in self.store.snapshot()['standalone']['outbox'].items()
                    if j['status'] == 'pending' and j.get('retry_at', 0) <= time.time()]
            for key in keys[:10]:
                self.deliver(key, now)
            self.sync_panels(now)
        self.last_worker = time.monotonic()

    def deliver(self, key, now):
        """Persist 'sending' before the network. An unknown outcome is never replayed."""
        with self.store.transaction() as state:
            job = state['standalone']['outbox'][key]
            if job['status'] != 'pending':
                return
            if job.get('panel') and not state['config'].get('automatic_repeats', True):
                item = state['emissions'].get(job.get('emission'))
                if item:
                    automatic = set(item['repeat_ids']) - set(item.get('manual_repeat_ids', []))
                    pure_automatic = (automatic and not item['primary']
                                      and not item.get('manual_repeat_ids') and not item['notices'])
                    if pure_automatic or (automatic and job['message_ids']):
                        job['status'] = 'suppressed' if pure_automatic else 'needs_review'
                        state['inline_panels'][job['panel']]['status'] = job['status']
                        return
            if job.get('panel') and not job['message_ids']:
                text = care.refresh(state, job['emission'], now)
                if text == 'NO_REPLY':
                    job['status'] = 'suppressed'
                    state['inline_panels'][job['panel']]['status'] = 'suppressed'
                    return
                del state['inline_panels'][job['panel']]
                job['panel'] = self.prepare_reminder(state, job['emission'], text, now)['nonce']
            if job.get('panel'):
                panel = state['inline_panels'][job['panel']]
                texts = [(inline_ui.page_text(state, panel, p) if job['message_ids'] else p['template'])
                         for p in panel['pages']]
                keyboard = inline_ui.keyboard(state, panel)
            else:
                texts, keyboard = job['texts'], None
            index = len(job['message_ids'])
            job['status'] = 'sending'
        while index < len(texts):
            payload = {'chat_id': self.runtime.chat_id, 'message_thread_id': self.runtime.topic_id,
                       'text': texts[index]}
            if keyboard is not None and index == len(texts) - 1:
                payload['reply_markup'] = keyboard
            try:
                result = self.client.call('sendMessage', payload)
                if (not isinstance(result, dict) or type(result.get('message_id')) is not int
                        or result.get('chat', {}).get('id') != self.runtime.chat_id
                        or result.get('message_thread_id') != self.runtime.topic_id
                        or result.get('from', {}).get('id') != self.bot_id):
                    raise TelegramError(uncertain=True)
            except TelegramError as exc:
                with self.store.transaction() as state:
                    job = state['standalone']['outbox'][key]
                    # Only a definite 429 response is safe to resend. Other failures need review.
                    job['status'] = 'pending' if exc.code == 429 and not exc.uncertain else 'needs_review'
                    job['retry_at'] = time.time() + (exc.retry_after or 30)
                    if job.get('panel') and job['status'] == 'needs_review':
                        state['inline_panels'][job['panel']]['status'] = 'needs_review'
                LOG.warning('Send deferred or needs review (code %d)', exc.code)
                return
            with self.store.transaction() as state:
                job = state['standalone']['outbox'][key]
                job['message_ids'].append(result['message_id'])
                if job.get('panel'):
                    panel = state['inline_panels'][job['panel']]
                    panel['pages'][index]['message_id'] = result['message_id']
                index += 1
                if index == len(texts):
                    job['status'] = 'sent'
                    if job.get('panel'):
                        panel['status'] = 'sent'
                        panel['last_digest'] = inline_ui.digest(state, panel)
                        if panel.get('primary'):
                            state['checklists'][panel['primary']]['original_message_id'] = str(job['message_ids'][0])

    def sync_panels(self, now):
        state = self.store.snapshot()
        for nonce, panel in state.get('inline_panels', {}).items():
            if panel['status'] != 'sent' or panel.get('edit_retry_at', 0) > time.time():
                continue
            digest = inline_ui.digest(state, panel)
            if digest == panel['last_digest']:
                continue
            failed = False
            for index, page in enumerate(panel['pages']):
                payload = {'chat_id': self.runtime.chat_id, 'message_id': page['message_id'],
                           'text': inline_ui.page_text(state, panel, page)}
                if index == len(panel['pages']) - 1:
                    payload['reply_markup'] = inline_ui.keyboard(state, panel)
                try:
                    self.client.call('editMessageText', payload)
                except TelegramError as exc:
                    if exc.not_modified:
                        continue
                    failed = True
                    break
            with self.store.transaction() as saved:
                target = saved['inline_panels'][nonce]
                if failed:
                    target['edit_retry_at'] = time.time() + 60
                else:
                    target['last_digest'] = digest
                    target.pop('edit_retry_at', None)

    def poll_once(self):
        updates = self.client.call('getUpdates', {'offset': self.store.offset(), 'timeout': 30,
                                                  'limit': 100,
                                                  'allowed_updates': ['message', 'callback_query']})
        if not isinstance(updates, list):
            raise TelegramError(uncertain=True)
        for update in updates:
            self.process_update(update)
        self.last_poll = time.monotonic()

    def poll_loop(self):
        failures = 0
        while not self.stop.is_set():
            try:
                self.poll_once()
                failures = 0
            except TelegramError as exc:
                if exc.code in {401, 403, 409}:
                    LOG.error('Polling stopped for authentication or competing consumer (code %d)', exc.code)
                    self.stop.set()
                    return
                failures += 1
                self.stop.wait(exc.retry_after or min(60, 2 ** min(failures, 6)) + random.random())
            except Exception:
                LOG.error('Polling stopped; inspect state and configuration offline')
                self.stop.set()
                return

    def worker_loop(self):
        while not self.stop.is_set():
            try:
                self.dispatch_once()
            except Exception:
                LOG.error('Dispatch stopped; inspect state and storage offline')
                self.stop.set()
                return
            self.stop.wait(2)

    def ready(self):
        now = time.monotonic()
        return not self.stop.is_set() and 0 < self.last_poll and now - self.last_poll < 120 and 0 < self.last_worker and now - self.last_worker < 120

    def live(self):
        now = time.monotonic()
        return (not self.stop.is_set() and now - (self.last_poll or self.started) < 300
                and now - (self.last_worker or self.started) < 300)


def health_server(bot):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in {'/livez', '/readyz'}:
                self.send_error(404)
                return
            ok = bot.live() if self.path == '/livez' else bot.ready()
            body = b'ok\n' if ok else b'not ready\n'
            self.send_response(200 if ok else 503)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass
    return ThreadingHTTPServer((bot.runtime.http_host, bot.runtime.http_port), Handler)


def identity(client, stop):
    failures = 0
    while not stop.is_set():
        try:
            me = client.call('getMe')
            if (not isinstance(me, dict) or type(me.get('id')) is not int or me['id'] <= 0
                    or me.get('is_bot') is not True
                    or not re.fullmatch(r'[A-Za-z0-9_]{5,32}', me.get('username', ''))):
                raise ValueError('Invalid bot identity')
            webhook = client.call('getWebhookInfo')
            if not isinstance(webhook, dict) or not isinstance(webhook.get('url'), str):
                raise ValueError('Invalid webhook information')
            if webhook['url']:
                raise ValueError('Competing webhook exists; no webhook was deleted')
            return me
        except TelegramError as exc:
            if exc.code in {401, 403, 409}:
                raise ValueError('New bot is unavailable or has a competing consumer') from None
            failures += 1
            stop.wait(exc.retry_after or min(60, 2 ** min(failures, 6)) + random.random())
    raise ValueError('Startup stopped')


def configured_client():
    """Use the isolated gateway when configured. Keep standalone compatibility."""
    if 'TELEGRAM_GATEWAY_SOCKET' in os.environ:
        from gateway_client import UnixClient
        path = os.environ['TELEGRAM_GATEWAY_SOCKET']
        if not path or not os.path.isabs(path):
            raise ValueError('An absolute Telegram gateway socket is required')
        return UnixClient(path)
    return Client(read_token())


def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    # A socket configuration never falls back to the real Telegram token.
    client = configured_client()
    runtime = Runtime.from_env()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    store = Store(runtime.state_path)
    store.acquire_lease()
    server = None
    try:
        me = identity(client, stop)
        store.bind(me['id'], runtime.chat_id, runtime.topic_id, care.instant(),
                   fresh_start=runtime.fresh_start)
        bot = Bot(runtime, client, me['id'], me['username'], store)
        bot.stop = stop
        bot.refresh_caregivers()
        server = health_server(bot)
        threads = [threading.Thread(target=server.serve_forever, name='acorn-health', daemon=True),
                   threading.Thread(target=bot.worker_loop, name='acorn-dispatch', daemon=True)]
        for thread in threads:
            thread.start()
        bot.poll_loop()  # The sole getUpdates loop.
        stop.set()
        threads[1].join(timeout=100)
        if threads[1].is_alive():
            raise ValueError('Dispatch did not stop before shutdown')
    finally:
        stop.set()
        if server:
            server.shutdown()
            server.server_close()
        store.close()
    return 1 if stop.is_set() else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        LOG.error('Service cannot start; inspect token mount, numeric IDs, configuration, and state')
        raise SystemExit(1) from None
