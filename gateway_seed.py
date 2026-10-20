"""Export old Acorn receipts once, before the isolated workers can start."""
import fcntl
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    def reject(_):
        raise ValueError('Non-finite JSON value')
    return json.loads(text, object_pairs_hook=pairs, parse_constant=reject)


def manifest(state, chat_id, topic_id):
    meta = state['standalone']
    identity = meta['identity']
    if (meta.get('schema') != 1 or not isinstance(identity, dict)
            or set(identity) != {'bot_id', 'chat_id', 'topic_id'}
            or identity.get('chat_id') != chat_id or identity.get('topic_id') != topic_id
            or type(identity.get('bot_id')) is not int or identity['bot_id'] <= 0
            or type(meta.get('offset')) is not int or meta['offset'] < 0):
        raise ValueError('Legacy state has a different identity')
    ids = set()
    for panel in state.get('inline_panels', {}).values():
        for page in panel.get('pages', []):
            mid = page.get('message_id')
            if mid is not None:
                if type(mid) is not int or mid <= 0:
                    raise ValueError('Invalid legacy message identifier')
                ids.add(mid)
    for job in meta.get('outbox', {}).values():
        for mid in job.get('message_ids', []):
            if type(mid) is not int or mid <= 0:
                raise ValueError('Invalid legacy message identifier')
            ids.add(mid)
    return {'schema': 1, 'worker': 'acorn', **identity,
            'offset': meta['offset'], 'message_ids': sorted(ids)}


def export(source, target, database, chat_id, topic_id):
    # Always reject a live old receiver, including restarts and rollback attempts.
    lease = Path(str(source) + '.service.lock')
    with ExitStack() as stack:
        if lease.exists():
            handle = stack.enter_context(lease.open('r'))
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ValueError('The old care service still owns its state') from None
        elif source.exists():
            raise ValueError('The legacy care lease is missing')
        # A completed gateway database is authoritative. Never import later worker edits.
        if database.exists() or target.exists() or not source.exists():
            return
        if source.stat().st_size > 32_000_000:
            raise ValueError('Legacy state exceeds the import limit')
        value = manifest(strict_json(source.read_text()), chat_id, topic_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix('.tmp')
        with temporary.open('x') as stream:
            json.dump(value, stream, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, target)
        fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def main():
    os.umask(0o027)
    try:
        export(Path(os.environ.get('GATEWAY_LEGACY_SOURCE', '/legacy/state.json')),
               Path(os.environ.get('GATEWAY_LEGACY_PATH', '/gateway/legacy.json')),
               Path(os.environ.get('GATEWAY_STATE_PATH', '/gateway/private/state.sqlite3')),
               int(os.environ['TELEGRAM_CHAT_ID']), int(os.environ['TELEGRAM_TOPIC_ID']))
        return 0
    except Exception:
        print('Gateway legacy import stopped. Keep the old receiver stopped and inspect the offline snapshot.',
              file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
