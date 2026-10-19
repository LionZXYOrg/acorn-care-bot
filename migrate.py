"""Explicit offline import. Never invoked by the service. No network access."""
import argparse
import copy
import json
import os
from pathlib import Path

import care
from configuration import integer, validate_care_config


def migrate(source, operator_id, now):
    state = copy.deepcopy(source)
    validate_care_config(state['config'])
    if state['config']['timezone'] != str(care.TZ):
        raise ValueError('Set ACORN_CONFIG_PATH to a configuration with the source timezone')
    if not isinstance(state['tasks'], dict) or not isinstance(state['checklists'], dict):
        raise ValueError('Invalid care state')
    # Remove only transport state. Historical caregiver records and audit entries remain intact.
    for key in ('standalone', 'inline_panels', 'inline_receipts', 'emissions', 'last_emission_id'):
        state.pop(key, None)
    for checklist in state['checklists'].values():
        checklist['original_message_id'] = None
    state['standalone'] = {'schema': 1, 'identity': None, 'offset': 0, 'outbox': {}}
    care.audit(state, 'explicit_standalone_migration', 'telegram:' + str(operator_id), now,
               telegram_user_id=operator_id,
               note='Transport identifiers discarded explicitly. Historical care records preserved.')
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--operator-id', required=True)
    parser.add_argument('--confirm-old-scheduler-stopped', action='store_true', required=True)
    args = parser.parse_args()
    operator = integer(args.operator_id, positive=True)
    if args.source.resolve() == args.output.resolve():
        raise ValueError('The import must use a separate output file')
    state = migrate(json.loads(args.source.read_text()), operator, care.instant())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation refuses to overwrite a mounted state file, including a symlink.
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(state, out, ensure_ascii=False, indent=2)
        out.write('\n')
        out.flush()
        os.fsync(out.fileno())
    directory = os.open(args.output.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    print('Imported care state into the separate output file. Review it before startup.')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('Import failed. Output is not safe to use until reviewed.') from None
