import os
from pathlib import Path
import sys
import tempfile
import subprocess
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import transaction_experimental
import windows_identity
import journal_experimental


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

    def test_journal_sync_failure_prevents_staging(self):
        with patch.object(journal_experimental.os, 'fsync', side_effect=OSError('disk error')):
            result = transaction_experimental.run_fixture(self.source, self.identity, lambda path: self.fail('backend ran'))
        self.assertEqual(result['status'], 'JOURNAL_FAILED')
        self.assertEqual(self.source.read_bytes(), b'original fixture')

    def test_process_exit_leaves_inspectable_identity_and_paths(self):
        journal = self.root / 'crash.jsonl'
        code = '''import os,sys
sys.path.insert(0, sys.argv[1])
import transaction_experimental,windows_identity
transaction_experimental.run_fixture(sys.argv[2],windows_identity.capture(sys.argv[2]),lambda path: os._exit(71),journal_path=sys.argv[3])
'''
        proc = subprocess.run([sys.executable, '-c', code,
                               str(Path(__file__).resolve().parents[1] / 'scripts'),
                               str(self.source), str(journal)], capture_output=True)
        self.assertEqual(proc.returncode, 71, proc.stderr)
        report = journal_experimental.inspect(journal)
        self.assertTrue(report['readable'])
        self.assertEqual(report['last_event'], 'BACKEND_INTENT')
        self.assertTrue(report['observed']['staged']['identity_matches'])
        self.assertFalse(report['recovery_performed'])
        self.assertEqual(Path(report['intent']['staged']).read_bytes(), b'original fixture')

    def test_torn_journal_is_incomplete(self):
        journal = self.root / 'torn.jsonl'
        journal.write_bytes(b'{"sequence":0,"event":"INTENT"')
        self.assertFalse(journal_experimental.inspect(journal)['readable'])

    def test_parents_remain_pinned_during_backend_and_are_released_after(self):
        parent = self.root / 'parent'
        parent.mkdir()
        source = parent / 'fixture'
        source.write_bytes(b'owned')
        def attempt_parent_rename(path):
            with self.assertRaises(OSError):
                parent.rename(self.root / 'moved')
        result = transaction_experimental.run_fixture(source, windows_identity.capture(source), attempt_parent_rename)
        self.assertEqual(result['status'], 'RESTORED')
        parent.rename(self.root / 'moved')
        self.assertEqual((self.root / 'moved' / 'fixture').read_bytes(), b'owned')


if __name__ == '__main__':
    unittest.main()
