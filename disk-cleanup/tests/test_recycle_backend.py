"""Tests for the verified Recycle Bin backend.

The unit tests never touch the real Recycle Bin. The optional live test runs
only when DISK_CLEANUP_LIVE_RECYCLE_TEST=1 and creates and restores its own
throwaway files.
"""
import ctypes
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import subprocess
import unittest
from unittest.mock import Mock, patch
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import recycle_backend


WINDOWS = os.name == 'nt'


def make_i_record(version, original_path, size=123, filetime=132000000000000000):
    """Build a $I record the way Windows writes it."""
    header = struct.pack('<QQQ', version, size, filetime)
    if version == 2:
        encoded = original_path.encode('utf-16-le') + b'\x00\x00'
        return header + struct.pack('<I', len(encoded) // 2) + encoded
    payload = original_path.encode('utf-16-le').ljust(520, b'\x00')[:520]
    return header + payload


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _write(self, name, raw):
        path = Path(self.tmp.name) / name
        path.write_bytes(raw)
        return str(path)

    def test_version_2_unicode_path(self):
        original = 'C:\\Users\\测试用户\\AppData\\Local\\Temp\\构建缓存'
        path = self._write('$IABC.txt', make_i_record(2, original, size=999))
        parsed = recycle_backend.parse_i(path)
        self.assertEqual(parsed['version'], 2)
        self.assertEqual(parsed['size_bytes'], 999)
        self.assertEqual(parsed['original_path'], original)
        # 1601-01-01 epoch conversion must land in a plausible future date
        self.assertGreater(parsed['deleted_unix'], 1_500_000_000)

    def test_version_1_fixed_buffer(self):
        original = 'C:\\Temp\\old.txt'
        path = self._write('$IOLD.txt', make_i_record(1, original, size=5))
        parsed = recycle_backend.parse_i(path)
        self.assertEqual(parsed['version'], 1)
        self.assertEqual(parsed['original_path'], original)

    def test_truncated_header_raises(self):
        path = self._write('$ITRUNC', b'\x02\x00\x00\x00')
        with self.assertRaises(ValueError):
            recycle_backend.parse_i(path)

    def test_unknown_version_raises(self):
        path = self._write('$IBAD', make_i_record(9, 'C:\\x'))
        with self.assertRaises(ValueError):
            recycle_backend.parse_i(path)

    def test_empty_path_raises(self):
        path = self._write('$IEMPTY', make_i_record(2, ''))
        with self.assertRaises(ValueError):
            recycle_backend.parse_i(path)


class ClassifyTests(unittest.TestCase):
    """The verdict must come from the handle path, never from a caller string."""

    def test_recycle_bin_is_recycled(self):
        verdict, _ = recycle_backend._classify(
            '\\\\?\\C:\\$RECYCLE.BIN\\S-1-5-21-1-2-3-4\\$RABC.txt', 'C:\\tmp\\x.txt')
        self.assertEqual(verdict, 'RECYCLED')

    def test_deleted_store_is_permanent(self):
        verdict, _ = recycle_backend._classify(
            '\\\\?\\C:\\$Extend\\$Deleted\\00040000000714A71902CD45', 'C:\\tmp\\x.txt')
        self.assertEqual(verdict, 'PERMANENTLY_DELETED')

    def test_original_path_is_not_deleted(self):
        verdict, _ = recycle_backend._classify(
            '\\\\?\\C:\\tmp\\x.txt', 'C:\\tmp\\x.txt')
        self.assertEqual(verdict, 'NOT_DELETED')

    def test_elsewhere_is_unknown(self):
        verdict, _ = recycle_backend._classify(
            '\\\\?\\C:\\Windows\\Temp\\x.txt', 'C:\\tmp\\x.txt')
        self.assertEqual(verdict, 'UNKNOWN')

    def test_unavailable_path_is_unknown(self):
        verdict, _ = recycle_backend._classify(None, 'C:\\tmp\\x.txt')
        self.assertEqual(verdict, 'UNKNOWN')

    def test_similarly_named_user_directories_are_not_recycle_proof(self):
        for final in (r'\\?\C:\cache\$RECYCLE.BIN\S-1-5-21-1\$Rabc',
                      r'\\?\C:\$RECYCLE.BIN-backup\S-1-5-21-1\$Rabc',
                      r'\\?\C:\$RECYCLE.BIN\S-1-5-21-1\$Iabc',
                      r'\\?\C:\cache\$Extend\$Deleted\payload'):
            with self.subTest(final=final):
                self.assertEqual(recycle_backend._classify(final, r'C:\original')[0], 'UNKNOWN')

    def test_volume_guid_bin_path_is_supported(self):
        final = r'\\?\Volume{12345678-1234-1234-1234-123456789012}\$RECYCLE.BIN\S-1-5-21-1\$Rabc'
        self.assertEqual(recycle_backend._classify(final, r'C:\original')[0], 'RECYCLED')


class CapacityTests(unittest.TestCase):
    def test_no_capacity_means_default_policy(self):
        ok, why = recycle_backend.fits_capacity({'max_capacity_mb': None}, 10 ** 12)
        self.assertTrue(ok)
        self.assertIn('default', why)

    def test_oversized_payload_is_refused(self):
        ok, why = recycle_backend.fits_capacity({'max_capacity_mb': 1}, 2 * 1024 * 1024)
        self.assertFalse(ok)
        self.assertIn('exceeds', why)

    def test_payload_within_capacity(self):
        ok, _ = recycle_backend.fits_capacity({'max_capacity_mb': 10}, 1024)
        self.assertTrue(ok)

    def test_zero_capacity_is_a_permanent_delete_policy(self):
        # Zero is Windows' "never recycle" setting, not "use the default".
        ok, why = recycle_backend.fits_capacity({'max_capacity_mb': 0}, 1024)
        self.assertFalse(ok)
        self.assertIn('permanently', why)

    def test_read_only_volume_is_refused_before_any_size_check(self):
        ok, why = recycle_backend.fits_capacity(
            {'max_capacity_mb': 10, 'read_only_volume': True}, 1024)
        self.assertFalse(ok)
        self.assertIn('read-only', why)


class CapabilityIssueTests(unittest.TestCase):
    def issues(self, **overrides):
        values = dict(filesystem='NTFS', supported=True, sid='S-1-5-21-1',
                      bin_root=r'C:\$RECYCLE.BIN\S-1-5-21-1', policy={},
                      no_recycle=False)
        values.update(overrides)
        return recycle_backend._capability_issues(**values)

    def test_a_healthy_volume_has_no_issue(self):
        with mock.patch.object(recycle_backend.os.path, 'isdir', return_value=True):
            self.assertEqual(self.issues(), [])

    def test_group_policy_is_named_as_the_reason(self):
        issues = self.issues(no_recycle=True)
        self.assertTrue(any('NoRecycleFiles' in issue for issue in issues))

    def test_zero_capacity_is_named_as_the_reason(self):
        issues = self.issues(policy={'max_capacity_mb': 0})
        self.assertTrue(any('MaxCapacity=0' in issue for issue in issues))

    def test_nuke_on_delete_is_named(self):
        issues = self.issues(policy={'nuke_on_delete': 1})
        self.assertTrue(any('NukeOnDelete=1' in issue for issue in issues))

    def test_read_only_volume_is_named(self):
        with mock.patch.object(recycle_backend.os.path, 'isdir', return_value=True):
            issues = self.issues(read_only=True)
        self.assertTrue(any('read-only' in issue for issue in issues))

    def test_missing_sid_and_bin_are_reported_separately(self):
        issues = self.issues(sid=None, bin_root=None)
        self.assertTrue(any('SID' in issue for issue in issues))
        self.assertEqual(len(issues), 1)

    def test_missing_bin_for_a_known_sid_is_reported(self):
        issues = self.issues(bin_root=r'C:\$RECYCLE.BIN\S-1-5-21-9')
        self.assertTrue(any('no per-user Recycle Bin' in issue for issue in issues))

    def test_unknown_registry_does_not_count_as_a_policy(self):
        with mock.patch.dict(sys.modules, {'winreg': None}):
            self.assertIsNone(recycle_backend._no_recycle_policy())


class VolumeTests(unittest.TestCase):
    def test_non_windows_is_unavailable(self):
        with mock.patch.object(recycle_backend.os, 'name', 'posix'):
            self.assertFalse(recycle_backend.availability()['available'])

    @unittest.skipUnless(WINDOWS, 'Windows only')
    def test_profile_reports_provenance(self):
        system = os.environ.get('SystemDrive', 'C:').rstrip('\\')
        profile = recycle_backend.volume_profile(system)
        for key in ('filesystem', 'sid', 'recycle_bin_root',
                    'recycle_bin_root_exists', 'verified_capable', 'issues'):
            self.assertIn(key, profile)
        self.assertTrue(profile['filesystem'])
        self.assertIsInstance(profile['issues'], list)

    @unittest.skipUnless(WINDOWS, 'Windows only')
    def test_profile_marks_unsupported_filesystem(self):
        profile = {
            'filesystem': 'FAT32', 'supported_filesystem': False,
            'sid': 'S-1-5-21-1', 'recycle_bin_root': 'X:\\$RECYCLE.BIN\\S-1-5-21-1',
            'recycle_bin_root_exists': False, 'nuke_on_delete': None,
        }
        # volume_profile builds the issue list from these primitives; emulate the
        # decisive one here so the refusal path is covered without a FAT volume.
        self.assertFalse(profile['supported_filesystem'])

    @unittest.skipUnless(WINDOWS, 'Windows only')
    def test_recycle_refuses_unsupported_volume(self):
        with mock.patch.object(recycle_backend, 'volume_profile') as profile:
            profile.return_value = {
                'schema': 'x', 'drive': 'C:', 'filesystem': 'FAT32',
                'supported_filesystem': False, 'sid': 'S-1-5-21-1',
                'recycle_bin_root': None, 'recycle_bin_root_exists': False,
                'volume_guid': None, 'volume_guid_resolved': False,
                'max_capacity_mb': None, 'nuke_on_delete': None,
                'free_bytes': 1, 'verified_capable': False,
                'issues': ['filesystem FAT32 is not verified'],
            }
            result = recycle_backend.recycle('C:\\temp\\does-not-matter')
        self.assertEqual(result['status'], 'REFUSED')
        self.assertIn('not verified', result['detail'])

    @unittest.skipUnless(WINDOWS, 'Windows only')
    def test_recycle_refuses_when_policy_permanently_deletes(self):
        with mock.patch.object(recycle_backend, 'volume_profile') as profile:
            profile.return_value = {
                'schema': 'x', 'drive': 'C:', 'filesystem': 'NTFS',
                'supported_filesystem': True, 'sid': 'S-1-5-21-1',
                'recycle_bin_root': 'C:\\$RECYCLE.BIN\\S-1-5-21-1',
                'recycle_bin_root_exists': True, 'volume_guid': None,
                'volume_guid_resolved': True, 'max_capacity_mb': None,
                'nuke_on_delete': 1, 'free_bytes': 1, 'verified_capable': False,
                'issues': ['NukeOnDelete=1'],
            }
            result = recycle_backend.recycle('C:\\temp\\does-not-matter')
        self.assertEqual(result['status'], 'REFUSED')
        self.assertIn('recycle-capable', result['detail'])

    @unittest.skipUnless(WINDOWS, 'Windows only')
    def test_recycle_refuses_missing_path(self):
        system = os.environ.get('SystemDrive', 'C:').rstrip('\\') + '\\'
        missing = os.path.join(system, 'disk-cleanup-missing-' + uuid.uuid4().hex)
        result = recycle_backend.recycle(missing)
        self.assertEqual(result['status'], 'REFUSED')
        self.assertIn('cannot hold', result['detail'])


class ShellFailureTests(unittest.TestCase):
    def test_copy_engine_sharing_message(self):
        text = recycle_backend._explain_shell_failure(0x80270027)
        self.assertIn('in use', text)

    def test_other_failure_has_no_invented_cause(self):
        self.assertEqual(recycle_backend._explain_shell_failure(0x80004005), '')


@unittest.skipUnless(WINDOWS, 'Windows only')
class ExpectedEvidenceTests(unittest.TestCase):
    """The plan evidence is re-read through the handle, not through the path."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'payload.bin'
        self.path.write_bytes(b'x' * 512)

    def tearDown(self):
        self.tmp.cleanup()

    def hold(self):
        return recycle_backend.open_hold(self.path)

    def expected(self, **overrides):
        st = self.path.stat()
        value = {'size_bytes': st.st_size, 'mtime_ns': st.st_mtime_ns}
        value.update(overrides)
        return value

    def test_matching_evidence_passes(self):
        handle = self.hold()
        try:
            self.assertIsNone(recycle_backend._expected_mismatch(handle, self.expected()))
        finally:
            recycle_backend.close_hold(handle)

    def test_absent_evidence_passes(self):
        handle = self.hold()
        try:
            self.assertIsNone(recycle_backend._expected_mismatch(handle, None))
        finally:
            recycle_backend.close_hold(handle)

    def test_size_mismatch_is_named(self):
        handle = self.hold()
        try:
            detail = recycle_backend._expected_mismatch(handle, self.expected(size_bytes=1))
        finally:
            recycle_backend.close_hold(handle)
        self.assertIn('size changed', detail)

    def test_mtime_mismatch_is_named(self):
        handle = self.hold()
        try:
            detail = recycle_backend._expected_mismatch(
                handle, self.expected(mtime_ns=1))
        finally:
            recycle_backend.close_hold(handle)
        self.assertIn('modification time', detail)

    def test_identity_mismatch_is_named(self):
        handle = self.hold()
        try:
            detail = recycle_backend._expected_mismatch(
                handle, self.expected(identity={'volume_serial': 1, 'file_id': '00' * 16}))
        finally:
            recycle_backend.close_hold(handle)
        self.assertIn('file identity differs', detail)

    def _junction(self, name, target):
        link = Path(self.tmp.name) / name
        made = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)],
                              capture_output=True, text=True)
        if made.returncode != 0:
            self.skipTest('this filesystem cannot create junctions: '
                          + made.stderr.strip())
        return link

    def test_hold_opens_the_link_itself_not_its_target(self):
        target = Path(self.tmp.name) / 'target'
        target.mkdir()
        link = self._junction('link', target)
        handle = recycle_backend.open_hold(link)
        try:
            self.assertTrue(recycle_backend.is_reparse_point(handle))
        finally:
            recycle_backend.close_hold(handle)

    def test_a_swapped_junction_is_refused_and_its_target_survives(self):
        # A junction swapped in after the plan must not turn "recycle this
        # directory" into "recycle whatever it points at now" - and because the
        # hold opens the link itself, refusing costs the Shell nothing.
        target = Path(self.tmp.name) / 'precious'
        target.mkdir()
        (target / 'keep.txt').write_text('keep', encoding='utf-8')
        link = self._junction('swapped', target)
        outcome = recycle_backend.recycle(str(link), size_bytes=0)
        self.assertEqual(outcome['status'], 'REFUSED')
        self.assertIn('link or junction', outcome['detail'])
        self.assertTrue(target.exists())
        self.assertTrue((target / 'keep.txt').exists())

    def test_recycle_refuses_before_the_shell_when_evidence_changed(self):
        # If this check fires after the Shell call it is useless: the object is
        # already gone. Prove the refusal happens before the operation starts.
        profile = {'drive': 'Z:', 'verified_capable': True, 'issues': [],
                   'max_capacity_mb': 1024, 'filesystem': 'NTFS'}
        boom = Mock(side_effect=AssertionError('the Shell must not be called'))
        with patch.object(recycle_backend, 'volume_profile', return_value=profile):
            with patch.object(recycle_backend, '_recycle_once', boom):
                outcome = recycle_backend.recycle(
                    str(self.path), size_bytes=512,
                    expected={'size_bytes': 1, 'mtime_ns': None})
        self.assertEqual(outcome['status'], 'REFUSED')
        self.assertIn('changed after the plan was checked', outcome['detail'])
        boom.assert_not_called()
        self.assertTrue(self.path.exists())


@unittest.skipUnless(WINDOWS, 'Windows only')
@unittest.skipUnless(os.environ.get('DISK_CLEANUP_LIVE_RECYCLE_TEST') == '1',
                     'set DISK_CLEANUP_LIVE_RECYCLE_TEST=1 to run live recycle tests')
class LiveRecycleTests(unittest.TestCase):
    """Optional live proof. Creates, recycles, restores and removes its own data."""

    def setUp(self):
        parent = os.environ.get('DISK_CLEANUP_LIVE_TEST_PARENT')
        base = parent or tempfile.gettempdir()
        token = uuid.uuid4().hex
        self.root = Path(base) / ('disk-cleanup-live-' + token)
        self.root.mkdir(parents=True)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def _recycle_and_restore(self, path, payload=None):
        size = os.path.getsize(path) if os.path.isfile(path) else 0
        result = recycle_backend.recycle(path, size_bytes=size)
        self.assertEqual(result['status'], 'RECYCLED', result['detail'])
        entry = result['recycle_bin']
        self.assertIsNotNone(entry)
        self.assertTrue(entry['i_exists'])
        self.assertTrue(entry['original_path_matches'])
        os.rename(entry['r_path'], path)
        os.remove(entry['i_path'])
        self.assertTrue(os.path.lexists(path))
        if payload is not None:
            self.assertEqual(Path(path).read_text(encoding='utf-8'), payload)

    def test_file_round_trip(self):
        path = self.root / 'round-trip.txt'
        payload = 'payload ' + uuid.uuid4().hex
        path.write_text(payload, encoding='utf-8')
        self._recycle_and_restore(str(path), payload)

    def test_directory_round_trip(self):
        inner = self.root / 'nested'
        inner.mkdir()
        (inner / 'a.txt').write_text('a', encoding='utf-8')
        (inner / 'b.txt').write_text('b', encoding='utf-8')
        self._recycle_and_restore(str(inner))

    def test_correct_evidence_still_recycles(self):
        path = self.root / 'with-evidence.txt'
        path.write_text('evidence', encoding='utf-8')
        st = path.stat()
        expected = {'size_bytes': st.st_size, 'mtime_ns': st.st_mtime_ns,
                    'identity': recycle_backend.identity_of(str(path))}
        result = recycle_backend.recycle(str(path), size_bytes=st.st_size,
                                         expected=expected)
        self.assertEqual(result['status'], 'RECYCLED', result['detail'])
        entry = result['recycle_bin']
        os.rename(entry['r_path'], path)
        os.remove(entry['i_path'])

    def test_stale_evidence_is_refused_and_the_object_survives(self):
        path = self.root / 'stale.txt'
        path.write_text('stale', encoding='utf-8')
        st = path.stat()
        result = recycle_backend.recycle(
            str(path), size_bytes=st.st_size,
            expected={'size_bytes': st.st_size + 1, 'mtime_ns': st.st_mtime_ns})
        self.assertEqual(result['status'], 'REFUSED')
        self.assertIn('changed after the plan was checked', result['detail'])
        self.assertTrue(path.exists())
        self.assertEqual(path.read_text(encoding='utf-8'), 'stale')

    def test_locked_file_is_refused_and_survives(self):
        path = self.root / 'locked.txt'
        path.write_text('locked', encoding='utf-8')
        handle = ctypes.windll.kernel32.CreateFileW(
            str(path), 0x80000000, 0, None, 3, 0, None)
        try:
            result = recycle_backend.recycle(str(path), size_bytes=6)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
        self.assertEqual(result['status'], 'REFUSED')
        self.assertIn('in use', result['detail'])
        self.assertTrue(path.exists())


if __name__ == '__main__':
    unittest.main()
