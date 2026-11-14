import ctypes
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import locks


GENERIC_READ = 0x80000000
_FILE_SHARE_READ = locks.FILE_SHARE_READ
_INVALID = ctypes.c_void_p(-1).value


def _open_handle(path, access, share, flags):
    """Open one handle for the tests and return it without closing it."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                   ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                   ctypes.c_void_p]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_uint32
    handle = kernel.CreateFileW(path, access, share, None, locks.OPEN_EXISTING, flags, None)
    return kernel, handle


class LayoutTests(unittest.TestCase):
    def test_rm_process_info_matches_measured_abi(self):
        # Pinned by measurement: three processes holding one file put their
        # PIDs at 0, 668 and 1336, so the row stride must stay 668 bytes.
        self.assertEqual(ctypes.sizeof(locks.RmProcessInfo), 668)
        info = locks.RmProcessInfo
        self.assertEqual(locks.RmUniqueProcess.dwProcessId.offset, 0)
        self.assertEqual(info.Process.offset, 0)
        self.assertEqual(info.strAppName.offset, 12)
        self.assertEqual(info.strServiceShortName.offset, 524)
        self.assertEqual(info.ApplicationType.offset, 652)
        self.assertEqual(info.AppStatus.offset, 656)

    def test_restartable_flag_is_advice_only(self):
        # The flag mirrors the Restart Manager classification; it is never an
        # instruction to end anything, so critical/unknown holders read False.
        row = locks.RmProcessInfo()
        row.Process.dwProcessId = 42
        row.strAppName = 'Python'
        row.ApplicationType = locks.RM_APP_CONSOLE
        self.assertTrue(locks._summarize(row)['restartable'])
        row.ApplicationType = locks.RM_APP_CRITICAL
        self.assertFalse(locks._summarize(row)['restartable'])
        row.ApplicationType = 0
        self.assertFalse(locks._summarize(row)['restartable'])


class WindowsProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.file = self.root / 'data.txt'
        self.file.write_bytes(b'payload')

    def _exclusive_handle(self, path):
        kernel, handle = _open_handle(str(path), GENERIC_READ, 0, 0)
        self.assertNotIn(handle, (None, _INVALID))
        self.addCleanup(kernel.CloseHandle, handle)
        return handle

    @unittest.skipUnless(os.name == 'nt', 'Windows probe test')
    def test_free_file_is_deletable(self):
        result = locks.probe_deletable(self.file)
        self.assertEqual(result, {'deletable': True, 'reason': 'ok', 'winerror': None})

    @unittest.skipUnless(os.name == 'nt', 'Windows probe test')
    def test_free_directory_is_deletable(self):
        self.assertTrue(locks.probe_deletable(self.root)['deletable'])

    @unittest.skipUnless(os.name == 'nt', 'Windows probe test')
    def test_exclusive_holder_is_sharing_violation(self):
        self._exclusive_handle(self.file)
        result = locks.probe_deletable(self.file)
        self.assertEqual(result['deletable'], False)
        self.assertEqual(result['winerror'], 32)
        self.assertIn('sharing violation', result['reason'])

    @unittest.skipUnless(os.name == 'nt', 'Windows probe test')
    def test_probe_is_read_only_and_leaves_no_handle(self):
        self.assertTrue(locks.probe_deletable(self.file)['deletable'])
        # A second probe still succeeds only if the first handle was closed.
        self.assertTrue(locks.probe_deletable(self.file)['deletable'])
        self.file.write_bytes(b'still writable')
        self.assertEqual(self.file.read_bytes(), b'still writable')

    @unittest.skipUnless(os.name == 'nt', 'Windows probe test')
    def test_missing_path_is_not_found(self):
        result = locks.probe_deletable(self.root / 'absent')
        self.assertEqual(result['deletable'], False)
        self.assertIn(result['winerror'], (2, 3))
        self.assertEqual(result['reason'], 'not found')

    @unittest.skipUnless(os.name == 'nt', 'Windows probe test')
    def test_ctypes_failure_is_unknown_not_safe(self):
        with patch.object(locks, '_open_for_delete', side_effect=OSError('no kernel32')):
            result = locks.probe_deletable(self.file)
        self.assertIsNone(result['deletable'])
        self.assertIn('no kernel32', result['reason'])


class WindowsRestartManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.file = self.root / 'data.txt'
        self.file.write_bytes(b'payload')

    def _holder(self, share):
        """Start a helper process that holds ``self.file`` open."""
        script = (
            'import sys, ctypes\n'
            'kernel = ctypes.windll.kernel32\n'
            'kernel.CreateFileW.restype = ctypes.c_void_p\n'
            'kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32,\n'
            '                               ctypes.c_uint32, ctypes.c_void_p,\n'
            '                               ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]\n'
            'handle = kernel.CreateFileW(sys.argv[1], 0x80000000, int(sys.argv[2]),\n'
            '                            None, 3, 0x80, None)\n'
            'ok = handle not in (None, ctypes.c_void_p(-1).value)\n'
            'print("held", ok, flush=True)\n'
            'sys.stdin.readline()\n'
        )
        child = subprocess.Popen(
            [sys.executable, '-c', script, str(self.file), str(share)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE)

        def stop():
            try:
                child.stdin.write(b'x\n')
                child.stdin.flush()
                child.wait(timeout=10)
            except Exception:
                child.kill()
                child.wait(timeout=10)
            child.stdin.close()
            child.stdout.close()

        self.addCleanup(stop)
        line = child.stdout.readline().decode('ascii', 'replace').strip()
        return child, line

    @unittest.skipUnless(os.name == 'nt', 'Windows Restart Manager test')
    def test_free_file_has_no_holders(self):
        result = locks.restart_manager_locks([self.file])
        self.assertTrue(result['ok'])
        self.assertEqual(result['processes'], [])
        self.assertEqual(result['issues'], [])
        self.assertTrue(result['complete'])

    @unittest.skipUnless(os.name == 'nt', 'Windows Restart Manager test')
    def test_empty_and_single_path_inputs(self):
        empty = locks.restart_manager_locks([])
        self.assertTrue(empty['ok'])
        self.assertTrue(empty['complete'])
        single = locks.restart_manager_locks(str(self.file))
        self.assertTrue(single['ok'])
        self.assertTrue(single['complete'])

    @unittest.skipUnless(os.name == 'nt', 'Windows Restart Manager test')
    def test_directory_is_expanded_before_registration(self):
        (self.root / 'nested').mkdir()
        (self.root / 'nested' / 'other.bin').write_bytes(b'x')
        result = locks.restart_manager_locks([self.root])
        self.assertTrue(result['ok'])
        self.assertEqual(result['processes'], [])
        self.assertTrue(result['complete'])

    @unittest.skipUnless(os.name == 'nt', 'Windows Restart Manager test')
    def test_duplicate_paths_are_registered_once(self):
        result = locks.restart_manager_locks([self.file, self.file])
        self.assertTrue(result['ok'])
        self.assertEqual(result['issues'], [])

    @unittest.skipUnless(os.name == 'nt', 'Windows Restart Manager test')
    def test_long_and_missing_paths_become_issues(self):
        long_path = 'C:\\' + 'x' * 300
        result = locks.restart_manager_locks([long_path, self.root / 'absent', self.file])
        self.assertTrue(result['ok'])
        self.assertFalse(result['complete'])
        self.assertEqual(result['processes'], [])
        joined = '\n'.join(result['issues'])
        self.assertIn('MAX_PATH', joined)
        self.assertIn('skipped missing path', joined)

    @unittest.skipUnless(os.name == 'nt', 'Windows Restart Manager test')
    def test_holding_process_is_reported_with_its_pid(self):
        child, line = self._holder(_FILE_SHARE_READ)
        if 'True' not in line:
            self.skipTest(f'helper could not hold the file: {line}')
        pids = []
        for _ in range(20):
            result = locks.restart_manager_locks([self.file])
            self.assertTrue(result['ok'], result['issues'])
            pids = [row['pid'] for row in result['processes']]
            if child.pid in pids:
                break
            time.sleep(0.1)
        self.assertIn(child.pid, pids)
        row = next(item for item in result['processes'] if item['pid'] == child.pid)
        self.assertEqual(row['service'], '')
        self.assertIsInstance(row['restartable'], bool)

    @unittest.skipUnless(os.name == 'nt', 'Windows Restart Manager test')
    def test_blockers_combines_probe_and_holders(self):
        free = locks.blockers(self.file)
        self.assertEqual(free['deletable'], True)
        self.assertEqual(free['processes'], [])
        self.assertEqual(free['issues'], [])
        self.assertTrue(free['complete'])
        self._holder(_FILE_SHARE_READ)
        held = locks.blockers(self.file)
        self.assertEqual(held['deletable'], False)
        self.assertTrue(held['processes'])
        self.assertIn('sharing violation', held['reason'])

    @unittest.skipUnless(os.name == 'nt', 'Windows Restart Manager test')
    def test_blockers_missing_path_is_complete_and_safe_to_skip(self):
        result = locks.blockers(self.root / 'absent')
        self.assertEqual(result['deletable'], False)
        self.assertEqual(result['reason'], 'not found')
        self.assertEqual(result['processes'], [])
        self.assertEqual(result['issues'], [])
        self.assertTrue(result['complete'])


class DegradedTests(unittest.TestCase):
    def test_probe_reports_unsupported_platform(self):
        with patch.object(locks, '_on_windows', lambda: False):
            result = locks.probe_deletable(tempfile.gettempdir())
        self.assertEqual(result, {'deletable': None, 'reason': 'unsupported platform',
                                  'winerror': None})

    def test_restart_manager_reports_unsupported_platform(self):
        with patch.object(locks, '_on_windows', lambda: False):
            result = locks.restart_manager_locks([tempfile.gettempdir()])
        self.assertFalse(result['ok'])
        self.assertFalse(result['complete'])
        self.assertEqual(result['issues'], ['unsupported platform'])

    def test_blockers_fails_closed_when_probe_is_unknown(self):
        with patch.object(locks, '_on_windows', lambda: False):
            result = locks.blockers(tempfile.gettempdir())
        self.assertIsNone(result['deletable'])
        self.assertFalse(result['complete'])
        self.assertEqual(result['reason'], 'unsupported platform')
        self.assertIn('DELETE probe unavailable', '\n'.join(result['issues']))

    def test_invalid_path_is_rejected_before_any_call(self):
        with patch.object(locks, '_on_windows', lambda: True):
            probe = locks.probe_deletable(None)
            locks_rm = locks.restart_manager_locks([None])
        self.assertEqual(probe['reason'], 'invalid path')
        self.assertIsNone(probe['deletable'])
        self.assertFalse(locks_rm['complete'])
        self.assertIn('skipped empty or invalid path value', locks_rm['issues'])


if __name__ == '__main__':
    unittest.main()
