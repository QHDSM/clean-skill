"""Tests for the recycle self-check: stage ordering, short-circuits, cleanup.

The happy path is faked at the module boundary on purpose: these tests assert
that the check refuses to claim success when an earlier stage failed, which is
the property that makes the command trustworthy. The real recycle/restore path
is covered by the live tests in test_recycle_backend/test_recycle_bin.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / 'scripts'
sys.path.insert(0, str(SCRIPTS))

import recycle_selfcheck as selfcheck  # noqa: E402


def capability(ok=True, issues=None):
    return {'available': ok, 'reason': 'ok' if ok else 'not Windows'}


def profile(ok=True, issues=None):
    row = {'drive': 'C:', 'filesystem': 'NTFS', 'recycle_bin_root': 'C:\\$RECYCLE.BIN\\S-1',
           'recycle_bin_root_exists': True, 'max_capacity_mb': None, 'nuke_on_delete': None,
           'verified_capable': ok, 'issues': list(issues or [])}
    row['sid'] = 'S-1'
    row['volume_guid'] = 'guid'
    row['volume_guid_resolved'] = True
    row['supported_filesystem'] = True
    row['free_bytes'] = 1
    return row


class StageTests(unittest.TestCase):
    def test_unavailable_backend_stops_before_creating_a_probe(self):
        with patch.object(selfcheck.recycle_backend, 'availability',
                          return_value=capability(False)):
            with patch.object(selfcheck.tempfile, 'mkdtemp') as mkdtemp:
                report = selfcheck.run('C:')
        mkdtemp.assert_not_called()
        self.assertFalse(report['ok'])
        self.assertEqual([row['stage'] for row in report['stages']], ['availability'])

    def test_incapable_volume_stops_before_creating_a_probe(self):
        with patch.object(selfcheck.recycle_backend, 'availability', return_value=capability()):
            with patch.object(selfcheck.recycle_backend, 'volume_profile',
                              return_value=profile(False, ['filesystem FAT32 not supported'])):
                with patch.object(selfcheck.tempfile, 'mkdtemp') as mkdtemp:
                    report = selfcheck.run('C:')
        mkdtemp.assert_not_called()
        self.assertFalse(report['ok'])
        self.assertIn('filesystem FAT32 not supported', report['issues'])

    def test_permanent_deletion_fails_without_attempting_a_restore(self):
        verdict = {'status': 'PERMANENTLY_DELETED', 'detail': 'deleted store',
                   'proof': {'final_path': 'X'}, 'recycle_bin': None}
        with patch.object(selfcheck.recycle_backend, 'availability', return_value=capability()):
            with patch.object(selfcheck.recycle_backend, 'volume_profile', return_value=profile()):
                with patch.object(selfcheck.locks, 'probe_deletable',
                                  return_value={'deletable': True, 'reason': 'ok'}):
                    with patch.object(selfcheck.recycle_backend, 'recycle',
                                      return_value=verdict):
                        with patch.object(selfcheck.recycle_bin, 'restore') as restore:
                            report = selfcheck.run('C:')
        restore.assert_not_called()
        self.assertFalse(report['ok'])
        self.assertIn('deleted store', report['issues'])
        self.assertIsNone(report['probe_dir'])  # temporary directory removed

    def test_happy_path_reports_ok_and_removes_the_probe(self):
        seen = {}

        def fake_recycle(path, size_bytes=0):
            seen['path'] = path
            return {'status': 'RECYCLED', 'detail': 'inside the bin',
                    'proof': {'method': 'handle-final-path',
                              'final_path': 'C:\\$RECYCLE.BIN\\$R1'},
                    'recycle_bin': {'i_path': 'C:\\$RECYCLE.BIN\\$I1',
                                    'r_path': 'C:\\$RECYCLE.BIN\\$R1',
                                    'i_exists': True, 'metadata': {},
                                    'original_path_matches': True}}

        def fake_parse(i_path):
            return {'valid': True, 'version': 2, 'size_bytes': 2048, 'deleted_unix': 1.0,
                    'original_path': seen['path'], 'i_path': i_path, 'r_path': 'r'}

        def fake_restore(parsed):
            return {'ok': True, 'restored_to': parsed['original_path'],
                    'detail': 'restored and removed $I metadata', 'i_removed': True}

        with patch.object(selfcheck.recycle_backend, 'availability', return_value=capability()):
            with patch.object(selfcheck.recycle_backend, 'volume_profile', return_value=profile()):
                with patch.object(selfcheck.locks, 'probe_deletable',
                                  return_value={'deletable': True, 'reason': 'ok'}):
                    with patch.object(selfcheck.recycle_backend, 'recycle', side_effect=fake_recycle):
                        with patch.object(selfcheck.recycle_bin, 'parse_entry', side_effect=fake_parse):
                            with patch.object(selfcheck.recycle_bin, 'restore', side_effect=fake_restore):
                                report = selfcheck.run('C:')
        self.assertTrue(report['ok'], selfcheck.render(report))
        self.assertEqual([row['stage'] for row in report['stages']],
                         ['availability', 'volume', 'probe', 'deletable', 'recycle',
                          'metadata', 'restore', 'content'])
        self.assertIsNone(report['probe_dir'])
        self.assertFalse(Path(seen['path']).exists())

    def test_keep_evidence_retains_the_probe_directory(self):
        verdict = {'status': 'PERMANENTLY_DELETED', 'detail': 'deleted store',
                   'proof': {}, 'recycle_bin': None}
        with patch.object(selfcheck.recycle_backend, 'availability', return_value=capability()):
            with patch.object(selfcheck.recycle_backend, 'volume_profile', return_value=profile()):
                with patch.object(selfcheck.locks, 'probe_deletable',
                                  return_value={'deletable': True, 'reason': 'ok'}):
                    with patch.object(selfcheck.recycle_backend, 'recycle', return_value=verdict):
                        report = selfcheck.run('C:', keep_evidence=True)
        self.assertIsNotNone(report['probe_dir'], selfcheck.render(report))
        probe_dir = Path(report['probe_dir'])
        self.assertTrue(probe_dir.is_dir())
        self.assertTrue((probe_dir / 'probe.txt').is_file())
        shutil.rmtree(probe_dir)

    def test_render_names_every_stage(self):
        report = {'drive': 'C:', 'ok': False, 'probe_dir': None, 'issues': ['boom'],
                  'stages': [{'stage': 'availability', 'ok': False, 'detail': 'nope'}]}
        text = selfcheck.render(report)
        self.assertIn('FAILED', text)
        self.assertIn('[FAIL] availability: nope', text)
        self.assertIn('issue: boom', text)


class CliTests(unittest.TestCase):
    def test_cli_prints_json_and_returns_nonzero_on_failure(self):
        with patch.object(selfcheck, 'run',
                          return_value={'schema': selfcheck.SCHEMA, 'drive': None, 'ok': False,
                                        'stages': [], 'probe_dir': None, 'issues': []}):
            with patch.object(sys, 'argv', ['recycle_selfcheck.py', '--format', 'json']):
                with contextlib.redirect_stdout(io.StringIO()):
                    code = selfcheck.main()
        self.assertEqual(code, 1)

    def test_module_runs_as_a_script(self):
        proc = subprocess.run([sys.executable, str(SCRIPTS / 'recycle_selfcheck.py'),
                               '--drive', 'C:', '--format', 'json'],
                              capture_output=True, text=True, timeout=120)
        self.assertIn(proc.returncode, (0, 1), proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload['schema'], selfcheck.SCHEMA)
        self.assertIsInstance(payload['stages'], list)

    def test_help_is_rendered_without_raising(self):
        # The help text once carried a bare %T, which argparse expands as a
        # printf-style field: --help raised ValueError instead of printing.
        with patch.object(sys, 'argv', ['recycle_selfcheck.py', '--help']):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                with self.assertRaises(SystemExit) as raised:
                    selfcheck.main()
        self.assertEqual(raised.exception.code, 0)
        self.assertIn('--drive', buffer.getvalue())


@unittest.skipUnless(os.environ.get('DISK_CLEANUP_LIVE_RECYCLE_TEST') == '1',
                     'set DISK_CLEANUP_LIVE_RECYCLE_TEST=1 to move a real probe file')
class LiveSelfCheckTests(unittest.TestCase):
    def test_self_check_succeeds_on_the_real_volume(self):
        parent = os.environ.get('DISK_CLEANUP_LIVE_TEST_PARENT')
        if not parent:
            self.skipTest('DISK_CLEANUP_LIVE_TEST_PARENT is not set')
        drive = os.path.splitdrive(os.path.abspath(parent))[0]
        report = selfcheck.run(drive)
        self.assertTrue(report['ok'], selfcheck.render(report))
