"""Exercise startup with a Kubernetes-like volume root, without network calls."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import gateway
from gateway_client import encode
from test_gateway import FakeUpstream, config_at


class EntrypointTests(unittest.TestCase):
    def test_default_pause_directory_is_created_before_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            volume = root / 'gateway-volume'
            volume.mkdir(mode=0o2775)
            volume.chmod(0o2775)
            config = config_at(root)
            for index, worker in enumerate(config['workers'], 1):
                worker['uid'] = os.geteuid() + index
                worker['socket_path'] = str(root / (worker['id'] + '-socket') / 'private/api.sock')
            config['state_path'] = str(volume / 'private/state.sqlite3')
            config_file = root / 'registry.json'
            config_file.write_bytes(encode(config))
            observed = []

            def start(instance, **_):
                observed.append(instance.pause_path)
                self.assertTrue(instance.pause_path.parent.is_dir())
                self.assertEqual(instance.pause_path.parent.stat().st_uid, os.geteuid())
                self.assertFalse(instance.pause_path.parent.stat().st_mode & 0o022)
                self.assertFalse(instance.paused())
                instance.pause_path.touch(mode=0o600)
                self.assertTrue(instance.paused())
                self.assertTrue(instance.drained())
                instance.stop.set()
                return instance

            with patch.dict(os.environ, {}, clear=True), \
                    patch('sys.argv', ['gateway.py', '--config', str(config_file)]), \
                    patch.object(gateway.os, 'umask'), \
                    patch.object(gateway.signal, 'signal'), \
                    patch.object(gateway, 'read_token', return_value='OFFLINE_ONLY'), \
                    patch.object(gateway, 'LimitedUpstream', return_value=FakeUpstream()), \
                    patch.object(gateway.Gateway, 'start', start):
                self.assertEqual(gateway.main(), 1)
            self.assertEqual(observed, [volume / 'private/pause-polling'])
            self.assertTrue((volume / 'private/state.sqlite3').exists())


if __name__ == '__main__':
    unittest.main()
