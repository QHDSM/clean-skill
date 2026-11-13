"""Opt-in live tests of self-created fixtures; never touches other items."""
import os
import ctypes
from ctypes import wintypes
from pathlib import Path
import sys
import tempfile
import time
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import recycle_experimental
import transaction_experimental
import windows_identity
import windows_parents
import journal_experimental


@unittest.skipUnless(os.name == 'nt' and os.environ.get('DISK_CLEANUP_LIVE_RECYCLE_TEST') == '1',
                     'set DISK_CLEANUP_LIVE_RECYCLE_TEST=1 on Windows to run live recycle test')
class LiveRecycleTest(unittest.TestCase):
    def roundtrip(self, directory):
        try:
            import win32com.client
        except ImportError:
            self.skipTest('pywin32 is required for the opt-in live test')

        parent = os.environ.get('DISK_CLEANUP_LIVE_TEST_PARENT') or None
        root = Path(tempfile.mkdtemp(prefix='disk-cleanup-owned-fixture-', dir=parent))
        target = root / ('fixture-' + uuid.uuid4().hex + ('' if directory else '.txt'))
        content = os.urandom(64)
        if directory:
            (target / 'nested').mkdir(parents=True)
            payload = target / 'nested' / 'payload.bin'
        else:
            payload = target
        payload.write_bytes(content)
        callbacks = self.recycle_and_restore(target, payload, content)
        payload.unlink()
        if directory:
            (target / 'nested').rmdir()
            target.rmdir()
        root.rmdir()
        self.assert_callback_evidence(callbacks)

    def recycle_and_restore(self, target, payload, content):
        import win32com.client
        root = target.parent
        folder = win32com.client.Dispatch('Shell.Application').NameSpace(10)
        self.assertIsNotNone(folder, f'No Recycle Bin; fixture retained at {root}')
        result = recycle_experimental.run_experiment(target, recycle_experimental.NativeAdapter(observe=True))
        self.assertEqual(result['status'], 'UNKNOWN', f'Unexpected result {result}; fixture {root}')
        callbacks = result.get('delete_callbacks', [])

        matches = []
        for _ in range(30):
            matches = [item for item in folder.Items() if item.Name == target.name]
            if matches:
                break
            time.sleep(0.2)
        self.assertEqual(len(matches), 1, f'Fixture not uniquely located; directory retained: {root}')
        item = matches[0]
        origin = item.ExtendedProperty('System.Recycle.DeletedFrom')
        self.assertEqual(os.path.normcase(os.path.normpath(str(origin))),
                         os.path.normcase(os.path.normpath(str(root))),
                         f'Unexpected original directory; fixture retained: {root}')
        verbs = [verb for verb in item.Verbs() if '还原' in verb.Name or 'restore' in verb.Name.lower()]
        self.assertEqual(len(verbs), 1, f'No unique restore verb; fixture retained: {root}')
        self.assertFalse(target.exists(), f'Original still exists; refusing restore: {target}')
        verbs[0].DoIt()
        for _ in range(50):
            if payload.exists():
                break
            time.sleep(0.2)
        self.assertTrue(payload.exists(), f'Fixture not restored; directory retained: {root}')
        self.assertEqual(payload.read_bytes(), content, f'Fixture content differs; retained at {target}')
        return callbacks

    def assert_callback_evidence(self, callbacks):
        posts = [row for row in callbacks if row['event'] == 'post_delete']
        self.assertTrue(posts, 'No deletion callbacks observed')
        self.assertTrue(any(not recycle_experimental._failed(row['hresult'])
                            and row['recycle_item_provided'] for row in posts),
                        'No successful recycle item callback evidence')

    def test_one_file_can_be_restored(self):
        self.roundtrip(directory=False)

    def test_nested_directory_can_be_restored(self):
        self.roundtrip(directory=True)

    def transaction_roundtrip(self, directory):
        try:
            import win32com.client
        except ImportError:
            self.skipTest('pywin32 is required for the opt-in live test')
        parent = os.environ.get('DISK_CLEANUP_LIVE_TEST_PARENT') or None
        root = Path(tempfile.mkdtemp(prefix='disk-cleanup-owned-transaction-', dir=parent))
        source = root / ('fixture-' + uuid.uuid4().hex)
        if directory:
            (source / 'nested').mkdir(parents=True)
            payload = source / 'nested' / 'payload.bin'
        else:
            payload = source
        content = os.urandom(64)
        payload.write_bytes(content)
        expected = windows_identity.capture(source)
        parents = windows_parents.capture_parent_chain(source)
        confirmed = []
        def backend(staged):
            staged_payload = staged / 'nested' / 'payload.bin' if directory else staged
            confirmed.append(self.recycle_and_restore(staged, staged_payload, content))
        result = transaction_experimental.run_fixture(source, expected, backend, parent_chain=parents)
        self.assertEqual(result['status'], 'RESTORED', f'{result}; fixture retained at {root}')
        self.assertEqual(len(confirmed), 1, f'Live roundtrip failed: {result}; retained at {root}')
        self.assertEqual(payload.read_bytes(), content, f'Content differs; retained at {root}')
        actual = windows_identity.capture(source)
        self.assertEqual((actual['volume_serial'], actual['file_id']),
                         (expected['volume_serial'], expected['file_id']))
        journal = journal_experimental.inspect(result['journal'])
        self.assertTrue(journal['readable'], f'Unreadable journal; retained at {root}')
        self.assertEqual(journal['last_event'], 'RESTORED')
        self.assertTrue(journal['observed']['source']['identity_matches'])
        payload.unlink()
        if directory:
            (source / 'nested').rmdir()
            source.rmdir()
        Path(result['journal']).unlink()
        root.rmdir()
        self.assert_callback_evidence(confirmed[0])

    def test_file_transaction_recovers_after_real_recycle(self):
        self.transaction_roundtrip(directory=False)

    def test_directory_transaction_recovers_after_real_recycle(self):
        self.transaction_roundtrip(directory=True)

    def canceled_transaction(self, directory):
        parent = os.environ.get('DISK_CLEANUP_LIVE_TEST_PARENT') or None
        root = Path(tempfile.mkdtemp(prefix='disk-cleanup-owned-cancel-', dir=parent))
        source = root / ('fixture-' + uuid.uuid4().hex)
        if directory:
            (source / 'nested').mkdir(parents=True)
            payload = source / 'nested' / 'payload.bin'
        else:
            payload = source
        content = os.urandom(64)
        payload.write_bytes(content)
        identity = windows_identity.capture(source)
        parents = windows_parents.capture_parent_chain(source)
        outcomes = []
        def backend(staged):
            outcomes.append(recycle_experimental.run_experiment(staged,
                recycle_experimental.NativeAdapter(permit_delete=False)))
        result = transaction_experimental.run_fixture(source, identity, backend, parent_chain=parents)
        self.assertEqual(result['status'], 'RESTORED', f'{result}; fixture retained at {root}')
        self.assertEqual(payload.read_bytes(), content, f'Content differs; retained at {root}')
        actual = windows_identity.capture(source)
        self.assertEqual((actual['volume_serial'], actual['file_id']),
                         (identity['volume_serial'], identity['file_id']))
        payload.unlink()
        if directory:
            (source / 'nested').rmdir()
            source.rmdir()
        Path(result['journal']).unlink()
        root.rmdir()
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]['status'], 'UNKNOWN')
        callbacks = outcomes[0].get('delete_callbacks', [])
        pre = [row for row in callbacks if row['event'] == 'pre_delete']
        self.assertTrue(pre)
        self.assertTrue(all(row['allowed'] is False for row in pre))
        posts = [row for row in callbacks if row['event'] == 'post_delete']
        self.assertFalse(any(not recycle_experimental._failed(row['hresult'])
                             and row['recycle_item_provided'] for row in posts))

    def test_canceled_file_transaction_preserves_original(self):
        self.canceled_transaction(directory=False)

    def test_canceled_directory_transaction_preserves_original(self):
        self.canceled_transaction(directory=True)

    def test_exclusively_locked_file_remains_present(self):
        parent = os.environ.get('DISK_CLEANUP_LIVE_TEST_PARENT') or None
        root = Path(tempfile.mkdtemp(prefix='disk-cleanup-owned-fixture-', dir=parent))
        target = root / ('locked-' + uuid.uuid4().hex + '.txt')
        content = os.urandom(64)
        target.write_bytes(content)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                       ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                       wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateFileW(str(target), 0x80000000, 0, None, 3, 0x80, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value, f'Cannot lock fixture; retained at {root}')
        try:
            result = recycle_experimental.run_experiment(target, recycle_experimental.NativeAdapter())
        finally:
            kernel.CloseHandle(handle)
        self.assertIn(result['status'], {'UNKNOWN', 'FAILED_BEFORE_PERFORM'})
        self.assertTrue(target.exists(), f'Locked fixture disappeared; directory retained: {root}')
        self.assertEqual(target.read_bytes(), content, f'Locked fixture changed; retained at {target}')
        target.unlink()
        root.rmdir()


if __name__ == '__main__':
    unittest.main()
