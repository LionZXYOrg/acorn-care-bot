"""Carry message ownership across the first gateway cutover without care changes."""
import copy
import fcntl
import json
from pathlib import Path
import tempfile
import unittest

import gateway_seed


class LegacySeedTests(unittest.TestCase):
    def state(self):
        return {'standalone': {'schema': 1, 'identity': {'bot_id': 9000, 'chat_id': -1001, 'topic_id': 210},
                               'offset': 500, 'outbox': {'a': {'message_ids': [31]}}},
                'inline_panels': {'a': {'pages': [{'message_id': 30}, {'message_id': 31}, {}]}},
                'tasks': {'saved': {'status': 'done'}}, 'progression_hold': {'reason': 'saved'}}

    def test_export_limits_records_to_saved_receipts_and_preserves_source(self):
        state = self.state()
        before = copy.deepcopy(state)
        value = gateway_seed.manifest(state, -1001, 210)
        self.assertEqual(value, {'schema': 1, 'worker': 'acorn', 'bot_id': 9000,
                                 'chat_id': -1001, 'topic_id': 210, 'offset': 500, 'message_ids': [30, 31]})
        self.assertEqual(state, before)
        self.assertNotIn('tasks', value)
        self.assertNotIn('progression_hold', value)

    def test_scope_and_offset_fail_closed(self):
        for key, value in [('chat_id', -2), ('topic_id', 45), ('bot_id', True)]:
            state = self.state()
            state['standalone']['identity'][key] = value
            with self.assertRaises(ValueError):
                gateway_seed.manifest(state, -1001, 210)
        state = self.state()
        state['standalone']['offset'] = -1
        with self.assertRaises(ValueError):
            gateway_seed.manifest(state, -1001, 210)

    def test_one_time_export_does_not_overwrite_an_existing_manifest_or_database(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, target, database = root / 'state.json', root / 'legacy.json', root / 'state.sqlite3'
            source.write_text(json.dumps(self.state()))
            Path(str(source) + '.service.lock').touch()
            before = source.read_bytes()
            gateway_seed.export(source, target, database, -1001, 210)
            original = target.read_bytes()
            self.assertEqual(source.read_bytes(), before)
            source.write_text('changed after cutover')
            gateway_seed.export(source, target, database, -1001, 210)
            self.assertEqual(target.read_bytes(), original)
            target.unlink()
            database.touch()
            gateway_seed.export(source, target, database, -1001, 210)
            self.assertFalse(target.exists())

    def test_old_receiver_lease_blocks_first_import(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'state.json'
            source.write_text(json.dumps(self.state()))
            with open(str(source) + '.service.lock', 'w') as lease:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(ValueError):
                    gateway_seed.export(source, root / 'legacy.json', root / 'state.sqlite3', -1001, 210)
            self.assertFalse((root / 'legacy.json').exists())

    def test_existing_gateway_database_does_not_bypass_old_receiver_guard(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, database = root / 'state.json', root / 'state.sqlite3'
            source.write_text(json.dumps(self.state()))
            database.touch()
            with open(str(source) + '.service.lock', 'w') as lease:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(ValueError):
                    gateway_seed.export(source, root / 'legacy.json', database, -1001, 210)

    def test_duplicate_keys_and_nonfinite_values_are_rejected(self):
        for text in ('{"x":1,"x":2}', '{"x":NaN}'):
            with self.assertRaises(ValueError):
                gateway_seed.strict_json(text)


if __name__ == '__main__':
    unittest.main()
