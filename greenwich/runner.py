#!/usr/bin/env python3
"""Run the bin worker once per Monday window. Never retry a claimed attempt."""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from bot import CHAT_ID
from council import LONDON
from storage import atomic_json, strict_json

HEARTBEAT = Path('/tmp/heartbeat')


def gateway_ready(*, check_permissions=True):
    """Check the worker's scoped socket with reads only, never a token fallback."""
    socket_path = os.environ.get('TELEGRAM_GATEWAY_SOCKET', '')
    if not os.path.isabs(socket_path) or '\0' in socket_path:
        return False
    try:
        from gateway_client import UnixClient
        client = UnixClient(socket_path, timeout=5 if check_permissions else 0.4)
        me = client.call('getMe', {})
        if (not isinstance(me, dict) or type(me.get('id')) is not int
                or me['id'] <= 0 or me.get('is_bot') is not True):
            return False
        if not check_permissions:
            return True
        member = client.call('getChatMember', {'chat_id': CHAT_ID, 'user_id': me['id']})
        user = member.get('user') if isinstance(member, dict) else None
        return (isinstance(user, dict) and type(user.get('id')) is int
                and user['id'] == me['id'] and user.get('is_bot') is True
                and (member.get('status') == 'creator' or (
                    member.get('status') == 'administrator'
                    and member.get('can_pin_messages') is True)))
    except Exception:
        # Reads can be retried safely. Never expose gateway response/error text.
        return False


def due_key(now):
    local = now.astimezone(LONDON)
    if local.weekday() == 0 and local.hour == 12:
        return local.date().isoformat()
    return None


def claim(path, key):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = strict_json(path.read_text()) if path.exists() else {'schema': 1, 'attempts': {}}
        if (not isinstance(state, dict) or state.get('schema') != 1
                or not isinstance(state.get('attempts'), dict)):
            raise ValueError('Invalid saved scheduler state')
        if key in state['attempts']:
            return False
        state['attempts'][key] = 'claimed'
        atomic_json(path, state)
        return True


def execute(stop):
    process = subprocess.Popen([sys.executable, str(Path(__file__).with_name('bot.py')), 'run'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 540
    while process.poll() is None:
        if stop.wait(1) or time.monotonic() > deadline:
            process.terminate()
            try:
                process.wait(timeout=40)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            return False
    return process.returncode == 0


def step(now, path, stop, run=execute, ready=gateway_ready):
    key = due_key(now)
    if key is None or stop.is_set():
        return False
    try:
        if ready() is not True:
            return False
    except Exception:
        return False
    # Only readiness reads may be repeated. Once claimed, even a failed or
    # ambiguous write requires review and must never release the weekly claim.
    if stop.is_set() or not claim(path, key):
        return False
    if not run(stop):
        print('Scheduled bin operation needs review. No automatic retry will run.', flush=True)
    return True


def gateway_available():
    # getMe is gateway-cached. Health must fit the one-second probe budget
    # without an upstream Telegram request; claims also need pin permissions.
    return gateway_ready(check_permissions=False)


def health(ready=gateway_available):
    try:
        if not 0 <= time.time() - float(HEARTBEAT.read_text()) < 90:
            return 1
        return 0 if ready() is True else 1
    except Exception:
        return 1


def heartbeat(stop):
    while not stop.is_set():
        HEARTBEAT.write_text(str(time.time()))
        stop.wait(10)


def main():
    if sys.argv[1:] == ['health']:
        return health()
    if sys.argv[1:]:
        return 1
    os.umask(0o077)
    # A deployed worker must use its scoped socket. Never read a bot token here.
    if not os.environ.get('TELEGRAM_GATEWAY_SOCKET', '').startswith('/'):
        return 1
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    path = Path(os.environ.get('BINS_RUNNER_STATE_PATH', '/data/runner-attempts.json'))
    thread = threading.Thread(target=heartbeat, args=(stop,), daemon=True)
    thread.start()
    try:
        while not stop.is_set():
            step(datetime.now(timezone.utc), path, stop)
            stop.wait(20)
    except Exception:
        print('Bin scheduler stopped. Inspect its saved state offline.', flush=True)
        return 1
    finally:
        stop.set()
        thread.join(timeout=15)
    return 0


if __name__ == '__main__':
    sys.exit(main())
