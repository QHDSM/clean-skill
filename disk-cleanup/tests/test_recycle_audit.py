"""Unit tests for recycle_audit and audit_volume on synthetic bins only.

Nothing here reads or writes the real Recycle Bin: every fixture is a temporary
directory that the scanner is pointed at by patching recycle_root.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import recycle_audit
import recycle_bin

FILETIME_EPOCH_OFFSET = 11_644_473_600
DAY = 86_400


def filetime_for(unix):
    return int(round((unix + FILETIME_EPOCH_OFFSET) * 10_000_000))


def build_v2(original_path, *, size_bytes=1234, deleted_unix=1_700_000_000.0):
    encoded = original_path.encode('utf-16-le') + b'\x00\x00'
    return (struct.pack('<QQQ', 2, size_bytes, filetime_for(deleted_unix))
            + struct.pack('<I', len(original_path) + 1) + encoded)


class FakeBinCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fake = self.root / 'fakebin'
        self.sid = self.fake / 'S-1-5-21-1-2-3-1001'
        self.sid.mkdir(parents=True)

    def add_pair(self, suffix, original_path, *, size_bytes=100,
                 deleted_unix=1_700_000_000.0, content=b'payload'):
        (self.sid / f'$I{suffix}').write_bytes(
            build_v2(original_path, size_bytes=size_bytes, deleted_unix=deleted_unix))
        (self.sid / f'$R{suffix}').write_bytes(content)
        return self.sid / f'$I{suffix}'

    def audit(self, **kwargs):
        with mock.patch.object(recycle_bin, 'recycle_root', return_value=str(self.fake)):
            return recycle_bin.audit_volume('Z', **kwargs)

    def journal(self, rows):
        path = self.root / 'audit-20260101-000000-abcdef.jsonl'
        path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n', encoding='utf-8')
        return path


class AuditVolumeTests(FakeBinCase):
    def test_totals_and_order(self):
        self.add_pair('A1.txt', r'C:\Temp\a.txt', size_bytes=100, deleted_unix=1_700_000_000.0)
        self.add_pair('B2.txt', r'C:\Temp\b.txt', size_bytes=400, deleted_unix=1_700_050_000.0)
        audit = self.audit(check_conflicts=False)
        self.assertEqual(audit['schema'], 'disk-cleanup-recycle-audit/v1')
        self.assertEqual(audit['drive'], 'Z:')
        self.assertTrue(audit['complete'], audit['issues'])
        self.assertEqual(audit['totals']['entries'], 2)
        self.assertEqual(audit['totals']['bytes'], 500)
        self.assertEqual(audit['totals']['oldest_deleted_unix'], 1_700_000_000.0)
        self.assertEqual(audit['totals']['newest_deleted_unix'], 1_700_050_000.0)
        self.assertEqual([item['original_path'] for item in audit['entries']],
                         [r'C:\Temp\b.txt', r'C:\Temp\a.txt'])
        self.assertEqual(audit['entries'][0]['sid'], self.sid.name)

    def test_every_kind_of_orphan_is_counted(self):
        self.add_pair('OK.txt', r'C:\Temp\ok.txt')
        (self.sid / '$IGONE.txt').write_bytes(build_v2(r'C:\Temp\gone.txt'))
        (self.sid / '$RLONE.bin').write_bytes(b'payload with no record')
        (self.sid / '$IBROKEN.txt').write_bytes(b'not a record')
        (self.sid / '$IDIR').mkdir()
        audit = self.audit(check_conflicts=False)
        self.assertEqual(audit['orphan_counts'],
                         {'i_without_r': 1, 'r_without_i': 1, 'invalid_i': 1,
                          'i_named_directory': 1})
        self.assertEqual(audit['totals']['orphans'], 4)
        kinds = {item['kind'] for item in audit['orphans']}
        self.assertEqual(kinds, {'i_without_r', 'r_without_i', 'invalid_i',
                                 'i_named_directory'})
        self.assertEqual(audit['totals']['entries'], 1)

    def test_conflict_when_the_original_path_is_occupied_again(self):
        live = self.root / 'live.txt'
        live.write_bytes(b'new object')
        self.add_pair('A1.txt', str(live))
        audit = self.audit()
        self.assertEqual(len(audit['conflicts']), 1)
        self.assertIn('occupied again', audit['conflicts'][0]['reason'])
        self.assertEqual(audit['conflicts'][0]['original_path'], str(live))
        # The conflict is advisory, not an incomplete scan.
        self.assertTrue(audit['complete'])

    def test_conflict_check_can_be_skipped(self):
        live = self.root / 'live.txt'
        live.write_bytes(b'new object')
        self.add_pair('A1.txt', str(live))
        self.assertEqual(self.audit(check_conflicts=False)['conflicts'], [])

    def test_missing_root_is_incomplete_not_empty_success(self):
        with mock.patch.object(recycle_bin, 'recycle_root',
                               return_value=str(self.fake / 'absent')):
            audit = recycle_bin.audit_volume('Z')
        self.assertFalse(audit['complete'])
        self.assertEqual(audit['entries'], [])
        self.assertEqual(audit['totals']['entries'], 0)
        self.assertTrue(any('not found' in issue for issue in audit['issues']))


class JournalReferenceTests(FakeBinCase):
    def test_present_and_missing_are_split_by_i_path(self):
        i_path = self.add_pair('A1.txt', r'C:\Temp\a.txt')
        log = self.journal([
            {'result': 'attempted', 'path': r'C:\Temp\a.txt'},
            {'result': 'trashed', 'path': r'C:\Temp\a.txt',
             'recycle_bin': {'i_path': str(i_path)}},
            {'result': 'trashed', 'path': r'C:\Temp\emptied.txt',
             'recycle_bin': {'i_path': str(self.sid / '$IVANISHED.txt')}},
        ])
        with mock.patch.object(recycle_bin, 'recycle_root', return_value=str(self.fake)):
            link = recycle_audit.journal_reference(log)
        self.assertEqual(link['records'], 2)
        self.assertEqual(link['present'], 1)
        self.assertEqual(link['missing'], [r'C:\Temp\emptied.txt'])
        self.assertEqual(link['issues'], [])

    def test_truncated_journal_is_reported_not_raised(self):
        i_path = self.add_pair('A1.txt', r'C:\Temp\a.txt')
        log = self.root / 'audit-broken.jsonl'
        log.write_text(json.dumps({'result': 'trashed', 'path': r'C:\Temp\a.txt',
                                   'recycle_bin': {'i_path': str(i_path)}}) + '\n{"res',
                        encoding='utf-8')
        with mock.patch.object(recycle_bin, 'recycle_root', return_value=str(self.fake)):
            link = recycle_audit.journal_reference(log)
        self.assertEqual(link['present'], 1)
        self.assertTrue(any('not JSON' in issue for issue in link['issues']))

    def test_unreadable_journal_reports_an_issue(self):
        link = recycle_audit.journal_reference(self.root / 'absent.jsonl')
        self.assertEqual(link['records'], 0)
        self.assertTrue(any('cannot read' in issue for issue in link['issues']))


class RenderAndCliTests(FakeBinCase):
    def patched(self, target):
        return mock.patch.object(recycle_bin, 'recycle_root', return_value=str(self.fake))

    def test_render_reports_orphans_conflicts_and_result(self):
        live = self.root / 'live.txt'
        live.write_bytes(b'x')
        self.add_pair('A1.txt', str(live), size_bytes=2048)
        (self.sid / '$IGONE.txt').write_bytes(build_v2(r'C:\Temp\gone.txt'))
        with self.patched(self.fake):
            report = recycle_audit.run(['Z'])
        text = recycle_audit.render(report)
        self.assertIn('RECYCLE BIN AUDIT', text)
        self.assertIn('entries 1', text)
        self.assertIn('orphans 1', text)
        self.assertIn('conflicts 1', text)
        self.assertIn('result: OK', text)

    def test_cli_returns_one_when_the_scan_is_incomplete(self):
        with mock.patch.object(recycle_bin, 'recycle_root',
                               return_value=str(self.fake / 'absent')), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = recycle_audit.main(['--drive', 'Z', '--json'])
        self.assertEqual(code, 1)
        payload = json.loads(out.getvalue())
        self.assertFalse(payload['complete'])

    def test_cli_exit_zero_on_a_healthy_bin(self):
        self.add_pair('A1.txt', r'C:\Temp\a.txt')
        with self.patched(self.fake), contextlib.redirect_stdout(io.StringIO()) as out:
            code = recycle_audit.main(['--drive', 'Z'])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn('result: OK', out.getvalue())


if __name__ == '__main__':
    unittest.main()
