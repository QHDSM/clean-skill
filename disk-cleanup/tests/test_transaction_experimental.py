import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import transaction_experimental
import windows_identity


@unittest.skipUnless(os.name == 'nt', 'Windows handle staging test')
class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='disk-cleanup-transaction-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'fixture'
        self.source.write_bytes(b'original fixture')
        self.identity = windows_identity.capture(self.source)

    def test_backend_failure_restores_original(self):
        def fail(path):
            self.assertEqual(path.read_bytes(), b'original fixture')
            raise OSError('simulated failure')
        result = transaction_experimental.run_fixture(self.source, self.identity, fail)
        self.assertEqual(result['status'], 'RESTORED')
        self.assertEqual(self.source.read_bytes(), b'original fixture')
        self.assertFalse(Path(result['staged']).exists())

    def test_existing_original_is_never_overwritten(self):
        def create_collision(path):
            self.source.write_bytes(b'new object')
            raise OSError('simulated failure')
        result = transaction_experimental.run_fixture(self.source, self.identity, create_collision)
        self.assertEqual(result['status'], 'RECOVERY_REQUIRED')
        self.assertEqual(self.source.read_bytes(), b'new object')
        self.assertEqual(Path(result['staged']).read_bytes(), b'original fixture')

    def test_replaced_stage_is_not_restored(self):
        def replace_stage(path):
            path.rename(self.root / 'preserved-original')
            path.write_bytes(b'replacement')
        result = transaction_experimental.run_fixture(self.source, self.identity, replace_stage)
        self.assertEqual(result['status'], 'RECOVERY_REQUIRED')
        self.assertFalse(self.source.exists())
        self.assertEqual(Path(result['staged']).read_bytes(), b'replacement')
        self.assertEqual((self.root / 'preserved-original').read_bytes(), b'original fixture')

    def test_missing_stage_is_unknown(self):
        def move_elsewhere(path):
            path.rename(self.root / 'elsewhere')
        result = transaction_experimental.run_fixture(self.source, self.identity, move_elsewhere)
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertEqual((self.root / 'elsewhere').read_bytes(), b'original fixture')

    def test_post_restore_identity_error_is_unknown(self):
        with patch.object(windows_identity, 'capture', side_effect=OSError('unavailable')):
            result = transaction_experimental.run_fixture(self.source, self.identity, lambda path: None)
        self.assertEqual(result['status'], 'RECOVERY_UNKNOWN')
        self.assertEqual(self.source.read_bytes(), b'original fixture')


if __name__ == '__main__':
    unittest.main()
