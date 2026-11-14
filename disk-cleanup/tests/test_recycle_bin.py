"""Unit tests for recycle_bin using synthetic $I/$R files only.

Every fixture lives under a temporary directory. The real Recycle Bin is never
read, written or repaired by these tests, so they run on any machine and on a
CI host without recycle-bin permissions.
"""
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import recycle_bin

FILETIME_EPOCH_OFFSET = 11_644_473_600


def filetime_for(unix):
    return int(round((unix + FILETIME_EPOCH_OFFSET) * 10_000_000))


def build_v2(original_path, *, size_bytes=1234, deleted_unix=1_700_000_000.0,
             characters=None, extra=b''):
    """Build a version 2 $I record; characters may be overridden to fake damage."""
    encoded = original_path.encode('utf-16-le') + b'\x00\x00'
    count = len(original_path) + 1 if characters is None else characters
    return (struct.pack('<QQQ', 2, size_bytes, filetime_for(deleted_unix))
            + struct.pack('<I', count) + encoded + extra)


def build_v1(original_path, *, size_bytes=42, deleted_unix=1_700_000_000.0):
    encoded = original_path.encode('utf-16-le')
    assert len(encoded) <= 520
    return (struct.pack('<QQQ', 1, size_bytes, filetime_for(deleted_unix))
            + encoded.ljust(520, b'\x00'))


class ParseEntryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write_i(self, name, data):
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_version_2_chinese_path(self):
        original = r'C:\Users\测试用户\文档\报告 终版.docx'
        path = self.write_i('$IABC123.docx', build_v2(original, size_bytes=987654))
        entry = recycle_bin.parse_entry(path)
        self.assertTrue(entry['valid'], entry['issues'])
        self.assertEqual(entry['schema'], recycle_bin.SCHEMA)
        self.assertEqual(entry['i_path'], str(path))
        self.assertEqual(entry['r_path'], str(self.root / '$RABC123.docx'))
        self.assertEqual(entry['version'], 2)
        self.assertEqual(entry['size_bytes'], 987654)
        self.assertAlmostEqual(entry['deleted_unix'], 1_700_000_000.0, places=3)
        self.assertEqual(entry['original_path'], original)
        self.assertEqual(entry['issues'], [])

    def test_version_1_chinese_path(self):
        original = r'D:\备份\旧文件.txt'
        path = self.write_i('$IDEF456.txt', build_v1(original, size_bytes=11))
        entry = recycle_bin.parse_entry(path)
        self.assertTrue(entry['valid'], entry['issues'])
        self.assertEqual(entry['version'], 1)
        self.assertEqual(entry['size_bytes'], 11)
        self.assertEqual(entry['original_path'], original)
        self.assertEqual(entry['r_path'], str(self.root / '$RDEF456.txt'))

    def test_version_2_accepts_trailing_padding(self):
        original = r'C:\Temp\note.txt'
        data = build_v2(original, extra=b'\x00' * 160)
        entry = recycle_bin.parse_entry(self.write_i('$IPAD001.txt', data))
        self.assertTrue(entry['valid'], entry['issues'])
        self.assertEqual(entry['original_path'], original)

    def test_truncated_header_is_invalid(self):
        entry = recycle_bin.parse_entry(self.write_i('$ITRUNC1.txt', b'\x02\x00\x00'))
        self.assertFalse(entry['valid'])
        self.assertTrue(any('header is truncated' in issue for issue in entry['issues']))

    def test_truncated_version_2_path_is_invalid(self):
        data = build_v2(r'C:\Temp\note.txt')[:0x1C + 4]
        entry = recycle_bin.parse_entry(self.write_i('$ITRUNC2.txt', data))
        self.assertFalse(entry['valid'])
        self.assertTrue(any('truncated' in issue for issue in entry['issues']))

    def test_truncated_version_1_path_is_invalid(self):
        data = build_v1(r'C:\Temp\note.txt')[:0x18 + 100]
        entry = recycle_bin.parse_entry(self.write_i('$ITRUNC3.txt', data))
        self.assertFalse(entry['valid'])
        self.assertTrue(any('version 1 path is truncated' in issue
                            for issue in entry['issues']))

    def test_wrong_version_is_invalid(self):
        data = struct.pack('<QQQ', 3, 10, filetime_for(1_700_000_000.0))
        entry = recycle_bin.parse_entry(self.write_i('$IVER999.txt', data))
        self.assertFalse(entry['valid'])
        self.assertEqual(entry['version'], 3)
        self.assertTrue(any('unsupported $I version' in issue for issue in entry['issues']))

    def test_empty_file_is_invalid(self):
        path = self.write_i('$IEMPTY.txt', b'')
        entry = recycle_bin.parse_entry(path)
        self.assertFalse(entry['valid'])
        self.assertEqual(entry['i_path'], str(path))
        self.assertEqual(entry['r_path'], str(self.root / '$REMPTY.txt'))
        self.assertTrue(any('empty' in issue for issue in entry['issues']))

    def test_oversized_file_is_rejected(self):
        data = b'\x00' * (recycle_bin._MAX_I_BYTES + 1)
        entry = recycle_bin.parse_entry(self.write_i('$IHUGE.txt', data))
        self.assertFalse(entry['valid'])
        self.assertTrue(any('implausibly large' in issue for issue in entry['issues']))

    def test_zero_path_length_is_invalid(self):
        data = build_v2(r'C:\Temp\note.txt', characters=0)
        entry = recycle_bin.parse_entry(self.write_i('$IZERO.txt', data))
        self.assertFalse(entry['valid'])
        self.assertTrue(any('length is zero' in issue for issue in entry['issues']))

    def test_zero_filetime_is_invalid(self):
        encoded = r'C:\Temp\note.txt'.encode('utf-16-le') + b'\x00\x00'
        data = (struct.pack('<QQQ', 2, 5, 0) + struct.pack('<I', len(encoded) // 2)
                + encoded)
        entry = recycle_bin.parse_entry(self.write_i('$ITIME0.txt', data))
        self.assertFalse(entry['valid'])
        self.assertTrue(any('timestamp is zero' in issue for issue in entry['issues']))

    def test_malformed_utf16_is_invalid(self):
        data = struct.pack('<QQQ', 2, 5, filetime_for(1_700_000_000.0)) + struct.pack('<I', 2)
        data += b'\x00\xd8\x00\x00'  # lone high surrogate
        entry = recycle_bin.parse_entry(self.write_i('$IBADUTF.txt', data))
        self.assertFalse(entry['valid'])
        self.assertTrue(any('UTF-16LE' in issue for issue in entry['issues']))

    def test_empty_original_path_is_invalid(self):
        data = struct.pack('<QQQ', 2, 5, filetime_for(1_700_000_000.0))
        data += struct.pack('<I', 1) + b'\x00\x00'
        entry = recycle_bin.parse_entry(self.write_i('$INONAME.txt', data))
        self.assertFalse(entry['valid'])
        self.assertTrue(any('original path is empty' in issue for issue in entry['issues']))

    def test_missing_file_is_invalid_without_exception(self):
        entry = recycle_bin.parse_entry(self.root / '$IMISSING.txt')
        self.assertFalse(entry['valid'])
        self.assertTrue(any('cannot read $I file' in issue for issue in entry['issues']))

    def test_bad_argument_does_not_raise(self):
        entry = recycle_bin.parse_entry(object())
        self.assertFalse(entry['valid'])
        self.assertTrue(entry['issues'])


class FakeBinCase(unittest.TestCase):
    """Shared setup for tests that point the scanner at a temp fake bin."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fake = self.root / 'fakebin'
        self.sid1 = self.fake / 'S-1-5-21-111111111-222222222-333333333-1001'
        self.sid2 = self.fake / 'S-1-5-21-111111111-222222222-333333333-1002'
        self.sid1.mkdir(parents=True)
        self.sid2.mkdir(parents=True)

    def add_pair(self, directory, suffix, original_path, *, deleted_unix=1_700_000_000.0,
                 content=b'payload', make_r=True):
        (directory / f'$I{suffix}').write_bytes(
            build_v2(original_path, deleted_unix=deleted_unix))
        if make_r:
            (directory / f'$R{suffix}').write_bytes(content)
        return directory / f'$I{suffix}'

    def patched_scan(self, drive='Z'):
        with mock.patch.object(recycle_bin, 'recycle_root', return_value=str(self.fake)):
            return recycle_bin.entries_for_volume(drive)


class RecycleRootTests(unittest.TestCase):
    def test_accepts_drive_forms(self):
        for value in ('C', 'c', 'C:', 'C:\\', 'C:/', Path('D:'), Path('D:/')):
            expected = f'{str(value)[0].upper()}:\\$RECYCLE.BIN'
            self.assertEqual(recycle_bin.recycle_root(value), expected, value)

    def test_rejects_extra_segments_and_junk(self):
        for value in ('C:\\Temp', 'CD', '', None, 3, Path('C:/Temp')):
            self.assertIsNone(recycle_bin.recycle_root(value), value)


class EntriesForVolumeTests(FakeBinCase):
    def test_lists_pairs_and_skips_broken_records(self):
        first = r'C:\Users\测试\a.txt'
        second = r'C:\Temp\b.log'
        third = r'D:\Backup\c.bin'
        self.add_pair(self.sid1, 'A111.txt', first, deleted_unix=1_700_000_000.0)
        self.add_pair(self.sid1, 'B222.log', second, deleted_unix=1_700_000_100.0)
        self.add_pair(self.sid2, 'C333.bin', third, deleted_unix=1_700_000_200.0)
        self.add_pair(self.sid1, 'ORPHAN.txt', r'C:\Temp\orphan.txt', make_r=False)
        (self.sid1 / '$IBROKEN.txt').write_bytes(b'not a recycle record')
        (self.sid1 / '$IADIR').mkdir()
        (self.sid1 / 'desktop.ini').write_bytes(b'[.ShellClassInfo]')

        scan = self.patched_scan()
        self.assertTrue(scan['complete'], scan['issues'])
        self.assertEqual(scan['schema'], recycle_bin.SCHEMA)
        self.assertEqual(scan['root'], str(self.fake))
        self.assertEqual(sorted(entry['original_path'] for entry in scan['entries']),
                         sorted([first, second, third]))
        joined = '\n'.join(scan['issues'])
        self.assertIn('orphan', joined)
        self.assertIn('invalid', joined)
        self.assertIn('$I-named directory', joined)

    def test_directory_payload_is_a_pair_not_an_orphan(self):
        original = r'C:\Temp\recycled-folder'
        self.add_pair(self.sid1, 'DIRPAY', original, make_r=False)
        (self.sid1 / '$RDIRPAY').mkdir()
        (self.sid1 / '$RDIRPAY' / 'nested.txt').write_bytes(b'nested')

        scan = self.patched_scan()

        self.assertTrue(scan['complete'], scan['issues'])
        self.assertEqual([entry['original_path'] for entry in scan['entries']], [original])
        self.assertFalse(any('orphan' in issue for issue in scan['issues']))

    def test_permission_error_on_one_sid_keeps_other_users_entries(self):
        keep = r'C:\Temp\readable.txt'
        self.add_pair(self.sid1, 'HIDDEN.txt', r'C:\Temp\hidden.txt')
        self.add_pair(self.sid2, 'KEEP.txt', keep)
        real_listing = recycle_bin._directory_entries

        def listing(path):
            if os.path.normcase(path) == os.path.normcase(str(self.sid1)):
                raise PermissionError(13, 'Access is denied', str(self.sid1))
            return real_listing(path)

        with mock.patch.object(recycle_bin, 'recycle_root', return_value=str(self.fake)), \
                mock.patch.object(recycle_bin, '_directory_entries', side_effect=listing):
            scan = recycle_bin.entries_for_volume('Z')

        self.assertFalse(scan['complete'])
        self.assertEqual([entry['original_path'] for entry in scan['entries']], [keep])
        self.assertTrue(any(self.sid1.name in issue and 'permission denied' in issue
                            for issue in scan['issues']))

    def test_missing_root_reports_incomplete(self):
        with mock.patch.object(recycle_bin, 'recycle_root',
                               return_value=str(self.fake / 'absent')):
            scan = recycle_bin.entries_for_volume('Z')
        self.assertFalse(scan['complete'])
        self.assertEqual(scan['entries'], [])
        self.assertTrue(any('not found' in issue for issue in scan['issues']))

    def test_invalid_drive_reports_incomplete(self):
        scan = recycle_bin.entries_for_volume('nonsense')
        self.assertFalse(scan['complete'])
        self.assertEqual(scan['root'], '')
        self.assertTrue(any('not a drive letter' in issue for issue in scan['issues']))


class FindEntryTests(FakeBinCase):
    def setUp(self):
        super().setUp()
        self.target = r'C:\Users\测试\重复报告.docx'
        self.add_pair(self.sid1, 'OLD.txt', self.target, deleted_unix=1_000.0)
        self.add_pair(self.sid1, 'MID.txt', self.target, deleted_unix=1_100.0)
        self.add_pair(self.sid2, 'NEW.txt', self.target, deleted_unix=5_000.0)
        self.add_pair(self.sid2, 'OTHER.txt', r'C:\Users\测试\别的.docx',
                      deleted_unix=1_100.0)

    def test_matches_path_and_uses_time_tolerance(self):
        entry = self.patched_find(self.target, deleted_unix=1_090.0, tolerance=120)
        self.assertIsNotNone(entry)
        self.assertEqual(entry['deleted_unix'], 1_100.0)

    def test_rejects_candidates_outside_tolerance(self):
        self.assertIsNone(self.patched_find(self.target, deleted_unix=3_000.0, tolerance=120))

    def test_unknown_path_returns_none(self):
        self.assertIsNone(self.patched_find(r'C:\Users\测试\不存在.docx'))

    def test_without_timestamp_returns_most_recent(self):
        entry = self.patched_find(self.target)
        self.assertIsNotNone(entry)
        self.assertEqual(entry['deleted_unix'], 5_000.0)

    def test_tolerance_boundary_is_inclusive(self):
        entry = self.patched_find(self.target, deleted_unix=1_220.0, tolerance=120)
        self.assertIsNotNone(entry)
        self.assertEqual(entry['deleted_unix'], 1_100.0)

    @unittest.skipUnless(os.name == 'nt', 'normcase folding is Windows-specific')
    def test_windows_normcase_matches_case_and_separators(self):
        entry = self.patched_find('c:/users/测试/重复报告.DOCX')
        self.assertIsNotNone(entry)

    def test_invalid_drive_returns_none(self):
        self.assertIsNone(recycle_bin.find_entry('nonsense', self.target))

    def patched_find(self, original_path, **kwargs):
        with mock.patch.object(recycle_bin, 'recycle_root', return_value=str(self.fake)):
            return recycle_bin.find_entry('Z', original_path, **kwargs)


class RestoreTests(FakeBinCase):
    def make_entry(self, suffix, original_path, *, content=b'payload',
                   deleted_unix=1_700_000_000.0, make_r=True):
        directory = self.sid1
        i_path = self.add_pair(directory, suffix, str(original_path),
                               deleted_unix=deleted_unix, content=content,
                               make_r=make_r)
        entry = recycle_bin.parse_entry(i_path)
        self.assertTrue(entry['valid'], entry['issues'])
        return entry

    def test_restores_content_and_removes_metadata(self):
        parent = self.root / 'restore-target'
        parent.mkdir()
        original = parent / '报告 终版.txt'
        content = b'random payload \xe4\xb8\xad\xe6\x96\x87'
        entry = self.make_entry('OK001.txt', original, content=content)

        result = recycle_bin.restore(entry)

        self.assertTrue(result['ok'], result['detail'])
        self.assertEqual(result['restored_to'], str(original))
        self.assertTrue(result['i_removed'])
        self.assertEqual(original.read_bytes(), content)
        self.assertFalse(Path(entry['r_path']).exists())
        self.assertFalse(Path(entry['i_path']).exists())

    def test_refuses_existing_target_and_leaves_both_files(self):
        parent = self.root / 'restore-target'
        parent.mkdir()
        original = parent / 'conflict.txt'
        original.write_bytes(b'existing')
        entry = self.make_entry('OK002.txt', original)

        result = recycle_bin.restore(entry)

        self.assertFalse(result['ok'])
        self.assertIsNone(result['restored_to'])
        self.assertIn('already exists', result['detail'])
        self.assertEqual(original.read_bytes(), b'existing')
        self.assertEqual(Path(entry['r_path']).read_bytes(), b'payload')
        self.assertTrue(Path(entry['i_path']).exists())
        self.assertFalse(result['i_removed'])

    def test_missing_r_file_fails(self):
        parent = self.root / 'restore-target'
        parent.mkdir()
        original = parent / 'no-data.txt'
        entry = self.make_entry('OK003.txt', original, make_r=False)

        result = recycle_bin.restore(entry)

        self.assertFalse(result['ok'])
        self.assertIsNone(result['restored_to'])
        self.assertIn('missing', result['detail'])
        self.assertTrue(Path(entry['i_path']).exists())

    def test_missing_parent_directory_is_refused(self):
        original = self.root / 'missing-parent' / 'nested.txt'
        entry = self.make_entry('OK004.txt', original)

        result = recycle_bin.restore(entry)

        self.assertFalse(result['ok'])
        self.assertIn('parent directory does not exist', result['detail'])
        self.assertFalse(original.exists())
        self.assertTrue(Path(entry['r_path']).exists())
        self.assertTrue(Path(entry['i_path']).exists())

    def test_invalid_entry_is_refused(self):
        parent = self.root / 'restore-target'
        parent.mkdir()
        entry = self.make_entry('OK005.txt', parent / 'invalid.txt')
        entry['valid'] = False

        result = recycle_bin.restore(entry)

        self.assertFalse(result['ok'])
        self.assertIn('not valid', result['detail'])
        self.assertTrue(Path(entry['r_path']).exists())

    def test_non_dict_entry_is_refused(self):
        result = recycle_bin.restore('not an entry')
        self.assertFalse(result['ok'])
        self.assertIsNone(result['restored_to'])
        self.assertFalse(result['i_removed'])

    def test_i_remove_failure_keeps_restore_successful(self):
        parent = self.root / 'restore-target'
        parent.mkdir()
        original = parent / 'metadata-stuck.txt'
        entry = self.make_entry('OK006.txt', original)

        with mock.patch.object(recycle_bin.os, 'remove',
                               side_effect=PermissionError('access denied')):
            result = recycle_bin.restore(entry)

        self.assertTrue(result['ok'], result['detail'])
        self.assertFalse(result['i_removed'])
        self.assertIn('could not remove $I metadata', result['detail'])
        self.assertEqual(original.read_bytes(), b'payload')
        self.assertTrue(Path(entry['i_path']).exists())

    def test_restores_directory_payload(self):
        parent = self.root / 'restore-target'
        parent.mkdir()
        original = parent / 'recycled-folder'
        entry = self.make_entry('DIR001', original, make_r=False)
        payload = Path(entry['r_path'])
        payload.mkdir()
        (payload / 'nested.txt').write_bytes(b'nested')

        result = recycle_bin.restore(entry)

        self.assertTrue(result['ok'], result['detail'])
        self.assertEqual((original / 'nested.txt').read_bytes(), b'nested')
        self.assertFalse(payload.exists())
        self.assertFalse(Path(entry['i_path']).exists())


if __name__ == '__main__':
    unittest.main()
