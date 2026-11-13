"""Opt-in live test of one self-created fixture; never touches other items."""
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import recycle_experimental


@unittest.skipUnless(os.name == 'nt' and os.environ.get('DISK_CLEANUP_LIVE_RECYCLE_TEST') == '1',
                     'set DISK_CLEANUP_LIVE_RECYCLE_TEST=1 on Windows to run live recycle test')
class LiveRecycleTest(unittest.TestCase):
    def test_one_fixture_can_be_restored(self):
        try:
            import win32com.client
        except ImportError:
            self.skipTest('pywin32 is required for the opt-in live test')

        root = Path(tempfile.mkdtemp(prefix='disk-cleanup-owned-fixture-'))
        target = root / ('fixture-' + uuid.uuid4().hex + '.txt')
        content = os.urandom(64)
        target.write_bytes(content)
        folder = win32com.client.Dispatch('Shell.Application').NameSpace(10)
        self.assertIsNotNone(folder, f'No Recycle Bin; fixture retained at {root}')
        result = recycle_experimental.run_experiment(target, recycle_experimental.NativeAdapter())
        self.assertEqual(result['status'], 'UNKNOWN', f'Unexpected result {result}; fixture {root}')

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
            if target.exists():
                break
            time.sleep(0.2)
        self.assertTrue(target.exists(), f'Fixture not restored; directory retained: {root}')
        self.assertEqual(target.read_bytes(), content, f'Fixture content differs; retained at {target}')
        target.unlink()
        root.rmdir()


if __name__ == '__main__':
    unittest.main()
