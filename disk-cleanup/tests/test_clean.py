#!/usr/bin/env python3
"""Tests for clean.py: the gate must reject bad plans before anything executes.

These tests never touch a real Recycle Bin. They exercise plan validation, path
preflight, fingerprint re-verification and dry-run behaviour. Actual Recycle Bin
behaviour needs a real Windows machine; see the note in the test module docstring
of test_scan.py for the same limitation.
"""
import json
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'scripts'))

import clean  # noqa: E402
import scan   # noqa: E402
import tree_evidence  # noqa: E402
import windows_identity  # noqa: E402
import windows_parents  # noqa: E402
import recycle_bin  # noqa: E402


def fingerprint_of(path):
    return list(scan.fingerprint(os.lstat(path)))


def recycled_verdict(path):
    """A backend verdict for one object, shaped like recycle_backend.recycle()."""
    path = str(path)
    return {'status': 'RECYCLED', 'detail': 'the object is inside the Recycle Bin',
            'proof': {'method': 'handle-final-path',
                      'final_path': '\\\\?\\C:\\$RECYCLE.BIN\\S-1\\$RABC.bin'},
            'recycle_bin': {'i_path': 'C:\\$RECYCLE.BIN\\S-1\\$IABC.bin',
                            'r_path': 'C:\\$RECYCLE.BIN\\S-1\\$RABC.bin',
                            'i_exists': True,
                            'metadata': {'version': 2, 'size_bytes': 256,
                                         'deleted_unix': 1.0, 'original_path': path},
                            'original_path_matches': True},
            'volume': {'drive': 'C:', 'filesystem': 'NTFS', 'verified_capable': True,
                       'issues': []}}


class PlanFactory:
    """Build plan dicts with sane defaults so each test overrides one thing."""

    def __init__(self, root):
        self.root = str(root)

    def target(self, obj, **over):
        st = os.lstat(obj)
        base = {
            'path': str(obj),
            'action': 'trash',
            'risk': 'safe',
            'reason': 'unit test target',
            'evidence': {'size_bytes': st.st_size, 'fingerprint': list(scan.fingerprint(st))},
        }
        base.update(over)
        return base
    def plan(self, targets, **over):
        base = {
            'schema': clean.SCHEMA,
            'generated_unix': 1730000000,
            'authorization': {'scope': [self.root], 'excluded': [], 'permanent_delete': False},
            'targets': targets,
        }
        auth_over = over.pop('authorization', None)
        if auth_over:
            base['authorization'].update(auth_over)
        base.update(over)
        return base


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.victim = self.root / 'cache'
        self.victim.mkdir()
        (self.victim / 'blob.bin').write_bytes(b'x' * 128)
        self.factory = PlanFactory(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, plan, name='plan.json'):
        path = self.root / name
        path.write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8')
        return path

    def assert_gate(self, plan, needle):
        path = self.write(plan)
        with self.assertRaises(clean.GateError) as ctx:
            clean.load_plan(path)
        self.assertIn(needle, str(ctx.exception))

    # --- schema -------------------------------------------------------------

    def test_rejects_wrong_schema(self):
        plan = self.factory.plan([self.factory.target(self.victim)], schema='disk-cleanup-plan/v0')
        self.assert_gate(plan, 'schema must be')

    def test_rejects_missing_scope(self):
        plan = self.factory.plan([self.factory.target(self.victim)])
        plan['authorization']['scope'] = []
        self.assert_gate(plan, 'scope must be a non-empty array')

    def test_rejects_scope_without_drive(self):
        plan = self.factory.plan([self.factory.target(self.victim)])
        plan['authorization']['scope'] = ['relative/path']
        self.assert_gate(plan, 'not an absolute drive path')

    def test_rejects_permanent_delete(self):
        plan = self.factory.plan([self.factory.target(self.victim)],
                                 authorization={'permanent_delete': True})
        self.assert_gate(plan, 'never performs permanent deletion')

    def test_rejects_false_like_invalid_authorization(self):
        plan = self.factory.plan([self.factory.target(self.victim)])
        for key, value in [('permanent_delete', 0), ('git_acknowledged', 'false'),
                           ('excluded', None), ('excluded', ['relative'])]:
            with self.subTest(key=key, value=value):
                modified = json.loads(json.dumps(plan))
                modified['authorization'][key] = value
                with self.assertRaises(clean.GateError):
                    clean.load_plan(self.write(modified))

    def test_rejects_duplicate_json_keys(self):
        path = self.root / 'duplicate.json'
        path.write_text('{"schema":"disk-cleanup-plan/v1","schema":"other"}', encoding='utf-8')
        with self.assertRaisesRegex(clean.GateError, 'duplicate JSON key'):
            clean.load_plan(path)

    def test_rejects_non_string_action_and_risk(self):
        for key in ('action', 'risk'):
            target = self.factory.target(self.victim, **{key: []})
            self.assert_gate(self.factory.plan([target]), key + ' must be')

    def test_rejects_empty_targets(self):
        plan = self.factory.plan([])
        self.assert_gate(plan, 'targets must be a non-empty array')

    def test_rejects_batch_over_limit(self):
        target = self.factory.target(self.victim)
        plan = self.factory.plan([target] * (clean.BATCH_LIMIT + 1))
        self.assert_gate(plan, 'batch limit')

    # --- per-target fields --------------------------------------------------

    def test_rejects_missing_fingerprint(self):
        target = self.factory.target(self.victim)
        del target['evidence']['fingerprint']
        self.assert_gate(self.factory.plan([target]), 'fingerprint must be 5 integers')

    def test_rejects_short_fingerprint(self):
        target = self.factory.target(self.victim)
        target['evidence']['fingerprint'] = [1, 2, 3]
        self.assert_gate(self.factory.plan([target]), 'fingerprint must be 5 integers')

    def test_rejects_non_integer_fingerprint(self):
        target = self.factory.target(self.victim)
        target['evidence']['fingerprint'] = [1, 2, 3, 4, 'x']
        self.assert_gate(self.factory.plan([target]), 'fingerprint must be 5 integers')

    def test_rejects_bool_fingerprint(self):
        target = self.factory.target(self.victim)
        target['evidence']['fingerprint'] = [1, 2, 3, 4, True]
        self.assert_gate(self.factory.plan([target]), 'fingerprint must be 5 integers')

    def test_rejects_relative_path(self):
        target = self.factory.target(self.victim, path='cache/blob.bin')
        self.assert_gate(self.factory.plan([target]), 'must be an absolute drive path')

    def test_rejects_special_windows_spelling_in_scope_exclusion_and_target(self):
        target = self.factory.target(self.victim)
        for scope in ['C:\\Windows.', 'C:\\folder\\..']:
            self.assert_gate(self.factory.plan([target], authorization={'scope': [scope]}), 'absolute drive path')
        self.assert_gate(self.factory.plan([target], authorization={'excluded': ['C:\\private.']}),
                         'absolute drive paths')
        target['path'] = 'C:\\cache:stream'
        self.assert_gate(self.factory.plan([target]), 'absolute drive path')

    def test_rejects_unknown_action(self):
        target = self.factory.target(self.victim, action='delete')
        self.assert_gate(self.factory.plan([target]), 'action must be one of')

    def test_rejects_unknown_risk(self):
        target = self.factory.target(self.victim, risk='low')
        self.assert_gate(self.factory.plan([target]), 'risk must be one of')

    def test_rejects_empty_reason(self):
        target = self.factory.target(self.victim, reason='   ')
        self.assert_gate(self.factory.plan([target]), 'reason must be a non-empty string')

    # --- containment --------------------------------------------------------

    def test_rejects_outside_scope(self):
        other = Path(tempfile.mkdtemp())
        try:
            stray = other / 'x'
            stray.mkdir()
            target = self.factory.target(stray)
            self.assert_gate(self.factory.plan([target]), 'outside every authorized scope')
        finally:
            stray.rmdir()
            other.rmdir()

    def test_rejects_inside_excluded(self):
        excluded_dir = self.root / 'private'
        excluded_dir.mkdir()
        target_path = excluded_dir / 'thing'
        target_path.mkdir()
        plan = self.factory.plan([self.factory.target(target_path)],
                                 authorization={'excluded': [str(excluded_dir)]})
        self.assert_gate(plan, 'overlaps an excluded root')

    def test_rejects_parent_of_excluded(self):
        private = self.victim / 'private'
        private.mkdir()
        plan = self.factory.plan([self.factory.target(self.victim)],
                                 authorization={'excluded': [str(private)]})
        self.assert_gate(plan, 'overlaps an excluded root')

    def test_rejects_the_same_target_twice(self):
        target = self.factory.target(self.victim)
        duplicate = json.loads(json.dumps(target))
        plan = self.factory.plan([target, duplicate])
        self.assert_gate(plan, 'repeats an earlier target')

    def test_rejects_a_target_inside_another_target(self):
        inner = self.victim / 'inner'
        inner.mkdir()
        plan = self.factory.plan([self.factory.target(self.victim),
                                  self.factory.target(inner)])
        self.assert_gate(plan, 'is inside another target')

    def test_allows_sibling_directories(self):
        first = self.root / 'one'
        second = self.root / 'one-sibling'
        first.mkdir()
        second.mkdir()
        plan = self.factory.plan([self.factory.target(first),
                                  self.factory.target(second)])
        loaded, _digest = clean.load_plan_with_digest(self.write(plan))
        self.assertEqual(len(loaded['targets']), 2)

    def test_execution_lock_refuses_a_second_holder_and_releases(self):
        clean.release_execution_lock()
        self.addCleanup(clean.release_execution_lock)
        self.assertEqual(clean.acquire_execution_lock(), (True, ''))
        ok, reason = clean.acquire_execution_lock()
        self.assertFalse(ok)
        self.assertIn('another cleanup run is in progress', reason)
        clean.release_execution_lock()
        self.assertEqual(clean.acquire_execution_lock(), (True, ''))

    def test_a_locked_run_is_rejected_before_anything_moves(self):
        plan = self.factory.plan([self.factory.target(self.victim)])
        path = self.write(plan)
        with patch.object(clean, 'acquire_execution_lock',
                          return_value=(False, 'another cleanup run is in progress')):
            code = clean.main(['--plan', str(path), '--go'])
        self.assertEqual(code, 2)
        self.assertTrue(self.victim.exists())

    def test_dry_run_does_not_take_the_lock(self):
        plan = self.factory.plan([self.factory.target(self.victim)])
        path = self.write(plan)
        with patch.object(clean, 'acquire_execution_lock',
                          side_effect=AssertionError('dry-run must not lock')):
            self.assertEqual(clean.main(['--plan', str(path)]), 0)

    # --- git gate -----------------------------------------------------------

    def test_rejects_unacknowledged_git_risk(self):
        target = self.factory.target(self.victim)
        target['evidence']['git'] = {'verdict': 'LOCAL_ONLY_DATA_PRESENT'}
        self.assert_gate(self.factory.plan([target]), 'git_acknowledged is not true')

    def test_accepts_acknowledged_git_risk(self):
        target = self.factory.target(self.victim)
        target['evidence']['git'] = {'verdict': 'LOCAL_ONLY_DATA_PRESENT'}
        plan = self.factory.plan([target], authorization={'git_acknowledged': True})
        loaded = clean.load_plan(self.write(plan))
        self.assertEqual(len(loaded['targets']), 1)

    def test_caution_does_not_bypass_git_acknowledgment(self):
        target = self.factory.target(self.victim, risk='caution')
        target['evidence']['git'] = {'verdict': 'LOCAL_ONLY_DATA_PRESENT'}
        plan = self.factory.plan([target])
        self.assert_gate(plan, 'git_acknowledged is not true')

    def test_missing_git_verdict_is_rejected(self):
        target = self.factory.target(self.victim)
        target['evidence']['git'] = {}
        self.assert_gate(self.factory.plan([target]), 'recognized verdict')

    def test_safe_label_with_unknown_evidence_needs_acknowledgment(self):
        target = self.factory.target(self.victim)
        target['evidence']['git'] = {'verdict': 'SAFE_TO_CONSIDER', 'all_content_synced': True,
                                     'unknown': ['not verified'], 'risks': []}
        self.assert_gate(self.factory.plan([target]), 'git_acknowledged is not true')

    def test_report_only_git_risk_does_not_require_cleanup_acknowledgment(self):
        target = self.factory.target(self.victim, action='report')
        target['evidence']['git'] = {'verdict': 'LOCAL_ONLY_DATA_PRESENT'}
        self.assertEqual(len(clean.load_plan(self.write(self.factory.plan([target])))['targets']), 1)

    def test_acknowledged_caution_git_risk_is_accepted(self):
        target = self.factory.target(self.victim, risk='caution')
        target['evidence']['git'] = {'verdict': 'UNVERIFIED'}
        plan = self.factory.plan([target], authorization={'git_acknowledged': True})
        self.assertEqual(len(clean.load_plan(self.write(plan))['targets']), 1)


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.plan = {'authorization': {'scope': [str(self.root)]}}

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, path, risk='safe'):
        target = {'path': str(path), 'risk': risk, 'evidence': {'fingerprint': [0] * 5}}
        return clean.preflight(target, self.plan)

    def test_accepts_normal_cache(self):
        cache = self.root / 'cache'
        cache.mkdir()
        ok, why = self.check(cache)
        self.assertTrue(ok, why)

    def test_refuses_missing_path(self):
        ok, why = self.check(self.root / 'nope')
        self.assertFalse(ok)
        self.assertIn('does not exist', why)

    def test_refuses_risk_never(self):
        cache = self.root / 'cache'
        cache.mkdir()
        ok, why = self.check(cache, risk='never')
        self.assertFalse(ok)
        self.assertIn('risk=never', why)

    def test_refuses_volume_root(self):
        self.assertIn('volume root', clean.is_denied(Path('C:\\')))

    def test_refuses_windows_dir_itself(self):
        self.assertIn('protected system tree', clean.is_denied(Path('C:\\Windows')))

    def test_refuses_windows_subdir(self):
        # The whole Windows tree is off limits, not just the root.
        self.assertIn('protected system tree', clean.is_denied(Path('C:\\Windows\\Temp')))

    def test_allows_cache_under_programdata(self):
        # ProgramData itself is protected; a named cache below it is not.
        self.assertIsNone(clean.is_denied(Path('C:\\ProgramData\\Package Cache')))

    def test_refuses_programdata_root(self):
        self.assertIn('protected system location', clean.is_denied(Path('C:\\ProgramData')))

    def test_refuses_user_profile_root(self):
        self.assertIn('user profile root', clean.is_denied(Path('C:\\Users\\someone')))

    def test_refuses_personal_toplevel(self):
        self.assertIn('top-level personal folder',
                      clean.is_denied(Path('C:\\Users\\someone\\Documents')))

    def test_allows_cache_inside_appdata(self):
        self.assertIsNone(clean.is_denied(
            Path('C:\\Users\\someone\\AppData\\Local\\Temp\\build')))

    def test_refuses_inside_dot_git(self):
        self.assertIn('inside a .git directory',
                      clean.is_denied(Path('C:\\repo\\.git\\objects')))


class ReverifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.file = self.root / 'data.bin'
        self.file.write_bytes(b'a' * 64)

    def tearDown(self):
        self.tmp.cleanup()

    def target(self, fp=None):
        return {'path': str(self.file),
                'evidence': {'fingerprint': fp if fp is not None else fingerprint_of(self.file)}}

    def test_accepts_unchanged(self):
        ok, why = clean.reverify(self.target())
        self.assertTrue(ok, why)

    def test_detects_size_change(self):
        fp = fingerprint_of(self.file)          # capture BEFORE mutating
        self.file.write_bytes(b'b' * 128)
        ok, why = clean.reverify(self.target(fp))
        self.assertFalse(ok)
        self.assertIn('changed since the scan', why)

    def test_detects_mtime_change(self):
        fp = fingerprint_of(self.file)
        os.utime(self.file, (1, 1))
        ok, why = clean.reverify(self.target(fp))
        self.assertFalse(ok)

    def test_detects_removal(self):
        fp = fingerprint_of(self.file)
        self.file.unlink()
        ok, why = clean.reverify(self.target(fp))
        self.assertFalse(ok)
        self.assertIn('gone since the scan', why)

    def test_zero_inode_falls_back_to_four_fields(self):
        fp = fingerprint_of(self.file)
        fp[1] = 0                                  # pretend st_ino is unavailable
        ok, why = clean.reverify(self.target(fp))
        self.assertTrue(ok, why)
        self.assertIn('identity confidence reduced', why)

    def test_reading_file_does_not_invalidate(self):
        """Reading must not count as a change.

        On Windows a read can update last-access time, which bumps NTFS ChangeTime
        (st_ctime). If reverify compared st_ctime, an agent that inspected a file
        to judge it would then be told the file had changed.
        """
        fp = fingerprint_of(self.file)
        self.file.read_bytes()                     # simulate an inspection
        ok, why = clean.reverify(self.target(fp))
        self.assertTrue(ok, why)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cache = self.root / 'cache'
        self.cache.mkdir()
        (self.cache / 'blob.bin').write_bytes(b'x' * 256)
        self.factory = PlanFactory(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def run_plan(self, plan, go=False):
        path = self.root / 'plan.json'
        path.write_text(json.dumps(plan), encoding='utf-8')
        args = clean.argparse.Namespace(plan=str(path), path=None, confirm_path=None,
                                        risk='caution', reason=None, go=go)
        return clean.run(args)

    def test_dry_run_does_not_move(self):
        report = self.run_plan(self.factory.plan([self.factory.target(self.cache)]), go=False)
        self.assertEqual(report['summary']['dry_run'], 1)
        self.assertEqual(report['summary']['trashed'], 0)
        self.assertTrue(self.cache.exists())


    def budget_plan(self, sizes):
        targets = []
        paths = []
        for index, size in enumerate(sizes):
            folder = self.root / ('budget-%d' % index)
            folder.mkdir()
            (folder / 'blob.bin').write_bytes(b'x' * 16)
            target = self.factory.target(folder)
            target['evidence']['size_bytes'] = size
            targets.append(target)
            paths.append(folder)
        return self.factory.plan(targets), paths

    def run_with_budget(self, plan, usage, go=False, capacity_mb=1):
        profile = {'drive': 'Z:', 'filesystem': 'NTFS', 'verified_capable': True,
                   'issues': [], 'max_capacity_mb': capacity_mb, 'nuke_on_delete': 0}
        with patch.object(clean.recycle_backend, 'volume_profile', return_value=profile):
            with patch.object(clean.recycle_bin, 'bin_usage', return_value=usage):
                return self.run_plan(plan, go=go)

    def usage(self, used, complete=True):
        return {'drive': 'Z:', 'root': 'Z:\\$RECYCLE.BIN', 'bytes': used, 'entries': 1,
                'complete': complete,
                'issues': [] if complete else ['permission denied for S-1-5-18']}

    def test_execution_refuses_a_target_that_would_overflow_the_bin(self):
        plan, paths = self.budget_plan([400_000, 400_000])
        with patch.object(clean.recycle_backend, 'recycle',
                          side_effect=lambda path, **kwargs: recycled_verdict(path)):
            report = self.run_with_budget(plan, self.usage(400_000), go=True)
        results = report['results']
        self.assertEqual(results[0]['result'], 'trashed')
        self.assertEqual(results[1]['result'], 'refused')
        self.assertIn('would exceed the Recycle Bin capacity', results[1]['detail'])
        self.assertEqual(report['summary']['capacity_warnings'], 1)

    def test_preview_shows_the_same_overflow_before_anything_moves(self):
        plan, paths = self.budget_plan([400_000, 400_000])
        report = self.run_with_budget(plan, self.usage(400_000), go=False)
        first, second = report['results']
        self.assertIs(first['bin_fit']['fits'], True)
        self.assertIs(second['bin_fit']['fits'], False)
        self.assertIn('would exceed', second['bin_fit']['reason'])
        self.assertEqual(report['summary']['capacity_warnings'], 1)
        self.assertTrue(all(path.exists() for path in paths))
        text = clean.render(report)
        self.assertIn('Recycle Bin: 0 MiB of 1 MiB used', text)

    def test_unknown_usage_is_reported_not_treated_as_empty(self):
        plan, paths = self.budget_plan([400_000, 400_000])
        report = self.run_with_budget(plan, self.usage(400_000, complete=False), go=False)
        first = report['results'][0]
        self.assertIs(first['bin_fit']['fits'], True)
        self.assertIs(first['bin_fit']['bin_usage_complete'], False)
        self.assertIn('current usage unknown', clean.render(report))
        self.assertEqual(report['summary']['capacity_warnings'], 0)

    def test_dry_run_reports_objects_that_cannot_fit_in_the_bin(self):
        target = self.factory.target(self.cache)
        target['evidence']['size_bytes'] = 8 * 1024 * 1024
        profile = {'drive': 'Z:', 'filesystem': 'NTFS', 'verified_capable': True,
                   'issues': [], 'max_capacity_mb': 1, 'nuke_on_delete': 0}
        with patch.object(clean.recycle_backend, 'volume_profile', return_value=profile):
            report = self.run_plan(self.factory.plan([target]), go=False)
        entry = report['results'][0]
        self.assertIs(entry['bin_fit']['fits'], False)
        self.assertEqual(entry['bin_fit']['max_capacity_mb'], 1)
        self.assertEqual(report['summary']['capacity_warnings'], 1)
        self.assertIn('Recycle Bin capacity', clean.render(report))

    def test_dry_run_reports_volumes_that_cannot_be_proven(self):
        profile = {'drive': 'Z:', 'filesystem': 'FAT32', 'verified_capable': False,
                   'issues': ['filesystem FAT32 is not NTFS or ReFS'], 'max_capacity_mb': None,
                   'nuke_on_delete': None}
        with patch.object(clean.recycle_backend, 'volume_profile', return_value=profile):
            report = self.run_plan(self.factory.plan([self.factory.target(self.cache)]), go=False)
        self.assertEqual(report['summary']['volume_warnings'], 1)
        self.assertIn('cannot be proven recycle-capable', clean.render(report))

    def test_dry_run_survives_a_volume_probe_that_fails(self):
        with patch.object(clean.recycle_backend, 'volume_profile',
                          side_effect=clean.recycle_backend.BackendUnavailable('no volume access')):
            report = self.run_plan(self.factory.plan([self.factory.target(self.cache)]), go=False)
        entry = report['results'][0]
        self.assertEqual(entry['result'], 'dry_run')
        self.assertIsNone(entry['bin_fit']['fits'])
        self.assertIn('no volume access', entry['bin_fit']['reason'])
        self.assertEqual(report['summary']['capacity_warnings'], 0)

    def test_plan_digest_hashes_the_exact_bytes_validated(self):
        plan = self.factory.plan([self.factory.target(self.cache)])
        raw = json.dumps(plan).encode('utf-8')
        args = clean.argparse.Namespace(plan=str(self.root / 'plan.json'), path=None,
                                        confirm_path=None, risk='caution', reason=None, go=False)
        with patch.object(Path, 'read_bytes', side_effect=[raw, b'different']) as read:
            report = clean.run(args)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(report['plan_digest'], hashlib.sha256(raw).hexdigest())

    def test_preview_shows_nested_git_and_directory_limit(self):
        marker = self.cache / 'project' / '.git'
        marker.mkdir(parents=True)
        report = self.run_plan(self.factory.plan([self.factory.target(self.cache)]))
        entry = report['results'][0]
        self.assertIn(str(marker), entry['git_discovery']['markers'])
        self.assertFalse(entry['directory_contents_verified'])
        self.assertIn('Git marker:', clean.render(report))
        self.assertEqual(report['summary']['refused'], 1)
        self.assertIn('audit evidence missing', report['results'][0]['detail'])

    def test_incomplete_git_discovery_is_not_a_clean_preview(self):
        target = self.factory.target(self.cache)
        discovery = {'markers': [], 'complete': False, 'issues': ['limit reached'], 'entries_checked': 1}
        with patch.object(clean.git_discovery, 'discover', return_value=discovery):
            report = self.run_plan(self.factory.plan([target]))
        self.assertEqual(report['summary']['refused'], 1)
        self.assertIn('incomplete', report['results'][0]['detail'])

    def test_acknowledgment_without_audit_does_not_cover_discovered_repo(self):
        (self.cache / '.git').mkdir()
        target = self.factory.target(self.cache, risk='caution')
        report = self.run_plan(self.factory.plan([target], authorization={'git_acknowledged': True}))
        self.assertEqual(report['summary']['refused'], 1)
        target['evidence']['git'] = {'verdict': 'UNVERIFIED'}
        report = self.run_plan(self.factory.plan([target], authorization={'git_acknowledged': True}))
        self.assertEqual(report['summary']['dry_run'], 1)

    def test_execution_without_a_proven_recycle_is_reported_as_failed(self):
        # The backend is mocked so this never touches a real Recycle Bin; what is
        # under test is that an unproven verdict is never upgraded to success.
        verdict = {'status': 'PERMANENTLY_DELETED', 'detail': 'the object entered the deleted store',
                   'proof': {'method': 'handle-final-path', 'final_path': 'X'}, 'recycle_bin': None,
                   'volume': {'drive': 'C:', 'filesystem': 'NTFS', 'verified_capable': True, 'issues': []}}
        with patch.object(clean.recycle_backend, 'recycle', return_value=verdict):
            report = self.run_plan(self.factory.plan([self.factory.target(self.cache)]), go=True)
        self.assertEqual(report['summary']['trashed'], 0)
        self.assertEqual(report['summary']['failed'], 1)
        self.assertIn('deleted store', report['results'][0]['detail'])
        self.assertEqual(report['results'][0]['recycle']['status'], 'PERMANENTLY_DELETED')
        self.assertTrue((self.cache / 'blob.bin').exists())

    def test_a_permanent_deletion_stops_the_rest_of_the_batch(self):
        other = self.root / 'cache2'
        other.mkdir()
        (other / 'blob.bin').write_bytes(b'y' * 256)
        verdict = {'status': 'PERMANENTLY_DELETED',
                   'detail': 'the object entered the deleted store',
                   'proof': {'method': 'handle-final-path', 'final_path': 'X'},
                   'recycle_bin': None,
                   'volume': {'drive': 'C:', 'filesystem': 'NTFS',
                              'verified_capable': True, 'issues': []}}
        plan = self.factory.plan([self.factory.target(self.cache),
                                  self.factory.target(other)])
        with patch.object(clean.recycle_backend, 'recycle', return_value=verdict) as recycle:
            report = self.run_plan(plan, go=True)
        # The Shell must not be asked to delete anything else after the proof
        # failed: the next item could be permanently deleted too.
        self.assertEqual(recycle.call_count, 1)
        self.assertEqual([row['result'] for row in report['results']], ['failed', 'skipped'])
        self.assertEqual(report['aborted_after'], str(self.cache))
        self.assertTrue(other.exists())
        self.assertIn('ABORTED', clean.render(report))

    def test_execution_records_the_undo_evidence(self):
        verdict = recycled_verdict(self.cache)
        with tempfile.TemporaryDirectory() as runs:
            with patch.object(clean, 'RUN_DIR', Path(runs)):
                with patch.object(clean.recycle_backend, 'recycle', return_value=verdict):
                    report = self.run_plan(self.factory.plan([self.factory.target(self.cache)]),
                                           go=True)
            log = report['audit_log']
            self.assertTrue(log)
            self.assertTrue(os.path.isfile(log))
            lines = Path(log).read_text(encoding='utf-8').splitlines()
        self.assertEqual(report['summary']['trashed'], 1)
        self.assertEqual(report['results'][0]['recycle']['recycle_bin']['i_path'],
                         verdict['recycle_bin']['i_path'])
        # Two lines per object: the intent written before the move, then the
        # record that carries the $I/$R pair once the move has been verified.
        self.assertEqual([json.loads(line)['result'] for line in lines],
                         ['attempted', 'trashed'])
        record = json.loads(lines[1])
        self.assertEqual(record['result'], 'trashed')
        self.assertEqual(record['recycle_bin']['r_path'], verdict['recycle_bin']['r_path'])
        self.assertEqual(record['proof']['final_path'], verdict['proof']['final_path'])
        self.assertEqual(record['path'], str(self.cache))

    def test_each_object_is_on_disk_before_the_next_one_runs(self):
        # The manifest exists so that a crash still leaves an undo path for
        # everything already moved; that only holds if each record is flushed
        # before the next target is touched.
        second = self.root / 'second'
        second.mkdir()
        (second / 'blob.bin').write_bytes(b'y' * 128)
        seen = []

        def recycle(path, size_bytes=0, expected=None):
            if path == str(second):
                logs = sorted(Path(clean.RUN_DIR).glob('audit-*.jsonl'))
                seen.append([(json.loads(line)['result'], json.loads(line)['path'])
                             for line in logs[0].read_text(encoding='utf-8').splitlines()])
            return recycled_verdict(Path(path))

        with tempfile.TemporaryDirectory() as runs:
            with patch.object(clean, 'RUN_DIR', Path(runs)):
                with patch.object(clean.recycle_backend, 'recycle', side_effect=recycle):
                    report = self.run_plan(self.factory.plan([
                        self.factory.target(self.cache),
                        self.factory.target(second)]), go=True)
        self.assertEqual(report['summary']['trashed'], 2)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0], [('attempted', str(self.cache)),
                                   ('trashed', str(self.cache)),
                                   ('attempted', str(second))])

    def test_a_failed_manifest_write_is_reported_not_hidden(self):
        verdict = recycled_verdict(self.cache)

        def refuse_fsync(fd):
            raise OSError(28, 'No space left on device')

        with tempfile.TemporaryDirectory() as runs:
            with patch.object(clean, 'RUN_DIR', Path(runs)):
                with patch.object(clean.recycle_backend, 'recycle', return_value=verdict):
                    with patch.object(clean.os, 'fsync', side_effect=refuse_fsync):
                        report = self.run_plan(self.factory.plan([self.factory.target(self.cache)]),
                                               go=True)
        self.assertEqual(report['summary']['trashed'], 1)
        self.assertIsNone(report['audit_log'])
        self.assertIn('could not be written', report['audit_warning'])
        self.assertIn('WARNING:', clean.render(report))

    def test_a_missing_run_directory_is_reported_not_fatal(self):
        verdict = recycled_verdict(self.cache)
        with patch.object(clean, 'RUN_DIR', self.root / 'no' / 'such' / 'dir'):
            with patch.object(Path, 'mkdir', side_effect=OSError(13, 'Permission denied')):
                with patch.object(clean.recycle_backend, 'recycle', return_value=verdict):
                    report = self.run_plan(self.factory.plan([self.factory.target(self.cache)]),
                                           go=True)
        self.assertEqual(report['summary']['trashed'], 1)
        self.assertIsNone(report['audit_log'])
        self.assertIn('no undo manifest', report['audit_warning'])

    @unittest.skipUnless(os.name == 'nt', 'Windows file identity test')
    def test_execution_re_checks_the_plan_evidence_under_the_hold(self):
        # reverify() compares against the path; the backend compares against the
        # held object. It must receive the values the plan was built from, or
        # the window between the two checks stays open.
        verdict = recycled_verdict(self.cache)
        target = self.factory.target(self.cache)
        captured = windows_identity.capture(self.cache)
        target['evidence']['windows_file_id'] = captured
        with patch.object(clean, 'RUN_DIR', self.root / 'runs'):
            with patch.object(clean.recycle_backend, 'recycle',
                              return_value=verdict) as backend:
                self.run_plan(self.factory.plan([target]), go=True)
        kwargs = backend.call_args.kwargs
        self.assertEqual(kwargs['size_bytes'], target['evidence']['size_bytes'])
        self.assertEqual(kwargs['expected']['mtime_ns'],
                         target['evidence']['fingerprint'][3])
        self.assertEqual(kwargs['expected']['identity'],
                         {'volume_serial': captured['volume_serial'],
                          'file_id': captured['file_id']})

    def test_trash_helper_returns_the_backend_verdict_unchanged(self):
        verdict = {'status': 'REFUSED', 'detail': 'cannot hold the object: in use',
                   'proof': None, 'recycle_bin': None, 'volume': {}}
        with patch.object(clean.recycle_backend, 'recycle', return_value=verdict) as backend:
            outcome = clean.trash_one(str(self.cache), size_bytes=256)
        self.assertEqual(outcome, verdict)
        backend.assert_called_once_with(str(self.cache), size_bytes=256)
        self.assertTrue(self.cache.exists())

    def test_report_is_json_serialisable(self):
        report = self.run_plan(self.factory.plan([self.factory.target(self.cache)]))
        json.dumps(report)                      # must not raise

    def test_changed_target_is_skipped(self):
        # Point at the file itself: editing a child does not bump the parent's mtime.
        blob = self.cache / 'blob.bin'
        target = self.factory.target(blob)
        blob.write_bytes(b'y' * 512)
        report = self.run_plan(self.factory.plan([target]))
        self.assertEqual(report['summary']['skipped'], 1)

    def test_scan_evidence_detects_later_file_change(self):
        scan_result = scan.Scanner([self.cache], progress=False).run()
        candidate = scan_result['largest_files'][0]
        target = self.factory.target(Path(candidate['path']))
        target['evidence']['fingerprint'] = candidate['fingerprint']
        Path(candidate['path']).write_bytes(b'changed after scan')
        report = self.run_plan(self.factory.plan([target]))
        self.assertEqual(report['summary']['skipped'], 1)

    def test_tree_evidence_detects_nested_change(self):
        folder = self.cache / 'nested'
        folder.mkdir()
        item = folder / 'data'
        item.write_bytes(b'old')
        target = self.factory.target(self.cache)
        target['evidence']['tree_metadata'] = tree_evidence.capture(self.cache)
        before = self.run_plan(self.factory.plan([target]))
        self.assertTrue(before['results'][0]['tree_metadata_comparison']['matched'])
        item.write_bytes(b'changed nested content')
        after = self.run_plan(self.factory.plan([target]))
        self.assertEqual(after['summary']['skipped'], 1)
        self.assertIn('directory metadata', after['results'][0]['detail'])

    def test_incomplete_tree_evidence_rejected(self):
        target = self.factory.target(self.cache)
        target['evidence']['tree_metadata'] = tree_evidence.capture(self.cache, max_entries=1)
        with self.assertRaisesRegex(clean.GateError, 'tree_metadata'):
            self.run_plan(self.factory.plan([target]))

    @unittest.skipUnless(os.name == 'nt', 'Windows file identity test')
    def test_windows_identity_detects_path_replacement(self):
        blob = self.cache / 'blob.bin'
        target = self.factory.target(blob)
        target['evidence']['fingerprint'][1] = 0  # simulate unavailable st_ino
        target['evidence']['windows_file_id'] = windows_identity.capture(blob)
        before = self.run_plan(self.factory.plan([target]))
        self.assertEqual(before['results'][0]['windows_file_id_comparison'], 'matched')
        blob.rename(self.cache / 'old.bin')
        blob.write_bytes(b'x' * 256)
        os.utime(blob, ns=(os.stat(blob).st_atime_ns, target['evidence']['fingerprint'][3]))
        after = self.run_plan(self.factory.plan([target]))
        self.assertEqual(after['summary']['skipped'], 1)
        self.assertIn('identity changed', after['results'][0]['detail'])

    @unittest.skipUnless(os.name == 'nt', 'Windows file identity test')
    def test_windows_identity_unavailable_skips_target(self):
        blob = self.cache / 'blob.bin'
        target = self.factory.target(blob)
        target['evidence']['windows_file_id'] = windows_identity.capture(blob)
        with patch.object(windows_identity, 'capture', side_effect=OSError('unavailable')):
            report = self.run_plan(self.factory.plan([target]))
        self.assertEqual(report['summary']['skipped'], 1)
        self.assertIn('identity unavailable', report['results'][0]['detail'])

    @unittest.skipUnless(os.name == 'nt', 'Windows parent identity test')
    def test_parent_chain_rejects_replaced_parent_with_same_file(self):
        parent = self.cache / 'parent'
        parent.mkdir()
        blob = parent / 'data'
        blob.write_bytes(b'owned')
        target = self.factory.target(blob)
        target['evidence']['windows_parent_chain'] = windows_parents.capture(blob)
        report = self.run_plan(self.factory.plan([target]))
        self.assertEqual(report['results'][0]['windows_parent_chain_comparison'], 'matched_at_inspection')
        parent.rename(self.cache / 'old-parent')
        parent.mkdir()
        (self.cache / 'old-parent' / 'data').rename(blob)
        report = self.run_plan(self.factory.plan([target]))
        self.assertEqual(report['summary']['skipped'], 1)
        self.assertIn('parent chain', report['results'][0]['detail'])

    @unittest.skipUnless(os.name == 'nt', 'Windows parent identity test')
    def test_incomplete_parent_evidence_rejects_plan(self):
        target = self.factory.target(self.cache)
        evidence = windows_parents.capture(self.cache)
        evidence['parents'] = evidence['parents'][:-1]
        target['evidence']['windows_parent_chain'] = evidence
        with self.assertRaisesRegex(clean.GateError, 'complete ordered chain'):
            self.run_plan(self.factory.plan([target]))

    @unittest.skipUnless(os.name == 'nt', 'Windows parent identity test')
    def test_parent_chain_read_error_skips_target(self):
        target = self.factory.target(self.cache)
        target['evidence']['windows_parent_chain'] = windows_parents.capture(self.cache)
        with patch.object(windows_parents.PinnedParents, '__enter__', side_effect=OSError('denied')):
            report = self.run_plan(self.factory.plan([target]))
        self.assertEqual(report['summary']['skipped'], 1)
        self.assertIn('denied', report['results'][0]['detail'])

    def test_denied_target_is_refused(self):
        target = self.factory.target(self.cache)
        target['path'] = 'C:\\Windows\\System32'
        target['evidence']['fingerprint'] = [1, 2, 3, 4, 5]
        plan = self.factory.plan([target])
        plan['authorization']['scope'] = ['C:\\']
        report = self.run_plan(plan)
        self.assertEqual(report['summary']['refused'], 1)

    def test_keep_action_is_not_executed(self):
        target = self.factory.target(self.cache, action='keep')
        report = self.run_plan(self.factory.plan([target]))
        self.assertEqual(report['summary']['not_executed'], 1)
        self.assertEqual(report['summary']['trashed'], 0)


class UndoTests(unittest.TestCase):
    """--undo trusts one manifest and nothing else: no bin scan, no Shell verb."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def manifest(self, rows, name='audit-20260101-000000-abcdef.jsonl'):
        path = self.root / name
        path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows),
                        encoding='utf-8')
        return path

    def record(self, path, i_path='C:\\$RECYCLE.BIN\\S-1\\$IABC.bin', **extra):
        row = {'result': 'trashed', 'path': str(path), 'action': 'trash', 'risk': 'safe',
               'recycle_bin': {'i_path': i_path,
                               'r_path': 'C:\\$RECYCLE.BIN\\S-1\\$RABC.bin'}}
        row.update(extra)
        return row

    def parsed(self, original, valid=True):
        return {'valid': valid, 'original_path': original, 'issues': [],
                'r_path': 'C:\\$RECYCLE.BIN\\S-1\\$RABC.bin',
                'i_path': 'C:\\$RECYCLE.BIN\\S-1\\$IABC.bin',
                'metadata': {'version': 2}}

    def undo(self, path, undo_last=False):
        args = clean.argparse.Namespace(plan=None, path=None, confirm_path=None,
                                        risk='caution', reason=None, go=False,
                                        undo=str(path) if path else None,
                                        undo_last=undo_last)
        return clean.run(args)


    def undo_verified(self, rows, identity, size_bytes, content=b'hello'):
        target = self.root / 'logs'
        log = self.manifest(rows)

        def restore(parsed):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            return {'ok': True, 'detail': 'restored', 'restored_to': str(target),
                    'i_removed': True}

        with patch.object(recycle_bin, 'parse_entry', return_value=self.parsed(str(target))):
            with patch.object(recycle_bin, 'restore', side_effect=restore):
                with patch.object(clean.recycle_backend, 'identity_of',
                                  return_value=identity) as lookup:
                    return self.undo(log), lookup

    def test_undo_verifies_the_restored_object_identity(self):
        identity = {'volume_serial': 7, 'file_id': 'aa' * 16}
        rows = [self.record(self.root / 'logs', size_bytes=5, identity_before=identity)]
        report, lookup = self.undo_verified(rows, identity, 5)
        item = report['results'][0]
        self.assertTrue(item['verified'])
        self.assertIn('matches the recorded object', item['verification'])
        self.assertEqual(report['summary']['verified'], 1)
        self.assertEqual(report['summary']['unverified'], 0)
        self.assertEqual(lookup.call_count, 1)
        self.assertIn('restores verified against the recorded file identity: 1/1',
                      clean.render(report))

    def test_undo_marks_a_different_object_as_not_the_one_trashed(self):
        rows = [self.record(self.root / 'logs', size_bytes=5,
                            identity_before={'volume_serial': 7, 'file_id': 'aa' * 16})]
        report, _ = self.undo_verified(rows, {'volume_serial': 7, 'file_id': 'bb' * 16}, 5)
        item = report['results'][0]
        self.assertFalse(item['verified'])
        self.assertIn('not the one the run trashed', item['verification'])
        self.assertIn('RESTORED BUT NOT THE SAME OBJECT', clean.render(report))
        self.assertEqual(report['summary']['unverified'], 1)

    def test_undo_detects_a_size_change_while_in_the_bin(self):
        rows = [self.record(self.root / 'logs', size_bytes=5,
                            identity_before={'volume_serial': 7, 'file_id': 'aa' * 16})]
        report, _ = self.undo_verified(rows, {'volume_serial': 7, 'file_id': 'aa' * 16},
                                       5, content=b'longer than five')
        item = report['results'][0]
        self.assertFalse(item['verified'])
        self.assertIn('size changed while it was in the Recycle Bin', item['verification'])

    def test_old_records_without_an_identity_are_not_upgraded(self):
        rows = [self.record(self.root / 'logs', size_bytes=5)]
        with patch.object(recycle_bin, 'parse_entry',
                          return_value=self.parsed(str(self.root / 'logs'))):
            with patch.object(recycle_bin, 'restore',
                              return_value={'ok': True, 'detail': 'restored',
                                            'restored_to': str(self.root / 'logs'),
                                            'i_removed': True}):
                with patch.object(clean.recycle_backend, 'identity_of') as lookup:
                    report = self.undo(self.manifest(rows))
        item = report['results'][0]
        self.assertIsNone(item['verified'])
        self.assertIn('recorded no file identity', item['verification'])
        lookup.assert_not_called()
        self.assertEqual(report['summary']['unverified'], 1)

    def test_undo_restores_only_trashed_records(self):
        target = self.root / 'logs'
        log = self.manifest([
            self.record(target),
            {'result': 'refused', 'path': str(self.root / 'nope'), 'recycle_bin': None},
            {'result': 'dry_run', 'path': str(self.root / 'later')},
        ])
        with patch.object(recycle_bin, 'parse_entry', return_value=self.parsed(str(target))):
            with patch.object(recycle_bin, 'restore',
                              return_value={'ok': True, 'detail': 'restored',
                                            'restored_to': str(target), 'i_removed': True}) as restore:
                report = self.undo(log)
        self.assertEqual(report['mode'], 'UNDO')
        self.assertEqual(report['summary']['trashed'], 1)
        self.assertEqual(report['summary']['refused'], 0)
        self.assertEqual(restore.call_count, 1)
        self.assertEqual(report['results'][0]['restored_to'], str(target))
        self.assertEqual(report['audit_log'], str(log))

    def test_undo_refuses_when_the_bin_names_another_path(self):
        target = self.root / 'logs'
        log = self.manifest([self.record(target)])
        with patch.object(recycle_bin, 'parse_entry',
                          return_value=self.parsed(str(self.root / 'somewhere-else'))):
            with patch.object(recycle_bin, 'restore') as restore:
                report = self.undo(log)
        self.assertEqual(report['summary']['refused'], 1)
        self.assertIn('different original path', report['results'][0]['detail'])
        restore.assert_not_called()

    def test_undo_skips_records_without_recycle_metadata(self):
        log = self.manifest([{'result': 'trashed', 'path': str(self.root / 'x'),
                              'recycle_bin': None}])
        with patch.object(recycle_bin, 'restore') as restore:
            report = self.undo(log)
        self.assertEqual(report['summary']['skipped'], 1)
        self.assertIn('no recycle metadata', report['results'][0]['detail'])
        restore.assert_not_called()

    def test_a_truncated_final_line_does_not_stop_undo(self):
        # A power cut can leave half a record; that must cost one object, not the run.
        target = self.root / 'logs'
        log = self.manifest([self.record(target)])
        with log.open('a', encoding='utf-8') as handle:
            handle.write('{"result": "trashed", "path": "C:\\half')
        with patch.object(recycle_bin, 'parse_entry', return_value=self.parsed(str(target))):
            with patch.object(recycle_bin, 'restore',
                              return_value={'ok': True, 'detail': 'restored',
                                            'restored_to': str(target), 'i_removed': True}):
                report = self.undo(log)
        self.assertEqual(report['summary']['trashed'], 1)
        self.assertEqual(len(report['log_issues']), 1)
        self.assertIn('not JSON', report['log_issues'][0])

    def test_undo_reports_a_failed_restore_instead_of_claiming_success(self):
        target = self.root / 'logs'
        log = self.manifest([self.record(target)])
        with patch.object(recycle_bin, 'parse_entry', return_value=self.parsed(str(target))):
            with patch.object(recycle_bin, 'restore',
                              return_value={'ok': False, 'detail': 'the target path is occupied',
                                            'restored_to': None, 'i_removed': False}):
                report = self.undo(log)
        self.assertEqual(report['summary']['failed'], 1)
        self.assertIn('occupied', report['results'][0]['detail'])

    def test_undo_last_uses_the_newest_manifest(self):
        newest = self.manifest([self.record(self.root / 'logs')],
                               name='audit-20260102-000000-ffffff.jsonl')
        self.manifest([self.record(self.root / 'older')],
                      name='audit-20260101-000000-000000.jsonl')
        with patch.object(clean, 'latest_run_log', return_value=str(newest)):
            with patch.object(recycle_bin, 'parse_entry',
                              return_value=self.parsed(str(self.root / 'logs'))):
                with patch.object(recycle_bin, 'restore',
                                  return_value={'ok': True, 'detail': 'restored',
                                                'restored_to': str(self.root / 'logs'),
                                                'i_removed': True}):
                    report = self.undo(None, undo_last=True)
        self.assertEqual(report['audit_log'], str(newest))
        # The older manifest names a different path and was never read.
        self.assertEqual([row['path'] for row in report['results']],
                         [str(self.root / 'logs')])
        self.assertEqual(report['summary']['trashed'], 1)

    def test_undo_rejects_a_relative_or_missing_manifest(self):
        args = clean.argparse.Namespace(plan=None, path=None, confirm_path=None,
                                        risk='caution', reason=None, go=False,
                                        undo='audit.jsonl', undo_last=False)
        with self.assertRaises(clean.GateError):
            clean.run(args)
        args = clean.argparse.Namespace(plan=None, path=None, confirm_path=None,
                                        risk='caution', reason=None, go=False,
                                        undo=str(self.root / 'nope.jsonl'), undo_last=False)
        with self.assertRaises(clean.GateError):
            clean.run(args)


    def test_undo_recovers_an_object_recorded_only_as_attempted(self):
        # The process died between the Shell move and the undo record. The intent
        # line plus a bin lookup in a tight window is what still restores it.
        target = self.root / 'logs'
        log = self.manifest([{'result': 'attempted', 'path': str(target), 'ts': 1000}])
        entry = self.parsed(str(target))
        with patch.object(recycle_bin, 'find_entry', return_value=entry) as find:
            with patch.object(recycle_bin, 'restore',
                              return_value={'ok': True, 'detail': 'restored',
                                            'restored_to': str(target), 'i_removed': True}):
                report = self.undo(log)
        self.assertEqual(report['summary']['trashed'], 1)
        self.assertEqual(report['summary']['recovered'], 1)
        self.assertTrue(report['results'][0]['recovered'])
        self.assertIn('incomplete run', report['results'][0]['detail'])
        self.assertEqual(find.call_args[1]['tolerance'], 5)
        self.assertEqual(find.call_args[1]['deleted_unix'], 1000)

    def test_an_attempted_record_with_no_bin_entry_is_skipped_with_a_reason(self):
        target = self.root / 'logs'
        log = self.manifest([{'result': 'attempted', 'path': str(target), 'ts': 1000}])
        with patch.object(recycle_bin, 'find_entry', return_value=None):
            with patch.object(recycle_bin, 'restore') as restore:
                report = self.undo(log)
        self.assertEqual(report['summary']['skipped'], 1)
        self.assertIn('no matching Recycle Bin entry', report['results'][0]['detail'])
        restore.assert_not_called()

    def test_an_attempted_record_without_a_timestamp_uses_the_wide_window(self):
        log = self.manifest([{'result': 'attempted', 'path': str(self.root / 'logs')}])
        with patch.object(recycle_bin, 'find_entry', return_value=None) as find:
            self.undo(log)
        self.assertIsNone(find.call_args[1]['deleted_unix'])
        self.assertEqual(find.call_args[1]['tolerance'], 120)

    def test_an_attempted_record_followed_by_trashed_is_left_to_the_exact_record(self):
        target = self.root / 'logs'
        log = self.manifest([
            {'result': 'attempted', 'path': str(target), 'ts': 1000},
            self.record(target),
        ])
        with patch.object(recycle_bin, 'find_entry') as find:
            with patch.object(recycle_bin, 'parse_entry', return_value=self.parsed(str(target))):
                with patch.object(recycle_bin, 'restore',
                                  return_value={'ok': True, 'detail': 'restored',
                                                'restored_to': str(target), 'i_removed': True}) as restore:
                    report = self.undo(log)
        find.assert_not_called()
        self.assertEqual(restore.call_count, 1)
        self.assertEqual(report['summary']['trashed'], 1)
        self.assertEqual(report['summary']['recovered'], 0)

    def test_recovery_that_cannot_restore_is_reported_as_failed(self):
        target = self.root / 'logs'
        log = self.manifest([{'result': 'attempted', 'path': str(target), 'ts': 1000}])
        with patch.object(recycle_bin, 'find_entry', return_value=self.parsed(str(target))):
            with patch.object(recycle_bin, 'restore',
                              return_value={'ok': False, 'detail': 'the target path is occupied',
                                            'restored_to': None, 'i_removed': False}):
                report = self.undo(log)
        self.assertEqual(report['summary']['failed'], 1)
        self.assertTrue(report['results'][0]['recovered'])

    def test_a_bin_scan_error_during_recovery_is_reported_not_raised(self):
        log = self.manifest([{'result': 'attempted', 'path': str(self.root / 'logs'), 'ts': 1000}])
        with patch.object(recycle_bin, 'find_entry', side_effect=OSError(5, 'Access is denied')):
            report = self.undo(log)
        self.assertEqual(report['summary']['failed'], 1)
        self.assertIn('Access is denied', report['results'][0]['detail'])

class SinglePathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cache = self.root / 'cache'
        self.cache.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def args(self, **over):
        base = dict(plan=None, path=str(self.cache), confirm_path=str(self.cache),
                    risk='caution', reason=None, go=False)
        base.update(over)
        return clean.argparse.Namespace(**base)

    def test_requires_confirm_path(self):
        with self.assertRaises(clean.GateError) as ctx:
            clean.run(self.args(confirm_path=None))
        self.assertIn('requires --confirm-path', str(ctx.exception))

    def test_confirm_path_must_match(self):
        other = self.root / 'other'
        other.mkdir()
        with self.assertRaises(clean.GateError) as ctx:
            clean.run(self.args(confirm_path=str(other)))
        self.assertIn('must name the same object', str(ctx.exception))

    def test_dry_run_reports_one_target(self):
        report = clean.run(self.args())
        self.assertEqual(report['summary']['total'], 1)
        self.assertEqual(report['summary']['dry_run'], 1)

    def test_a_file_single_path_target_carries_its_size(self):
        blob = self.root / 'blob.bin'
        blob.write_bytes(b'x' * 4096)
        report = clean.run(self.args(path=str(blob), confirm_path=str(blob)))
        self.assertEqual(report['results'][0]['size_bytes'], 4096)

    def test_a_file_without_recorded_size_is_still_budgeted(self):
        blob = self.root / 'big.bin'
        blob.write_bytes(b'y' * 2048)
        info = clean.os.lstat(blob)
        target = {'path': str(blob), 'action': 'trash', 'risk': 'safe',
                  'reason': 'size fallback',
                  'evidence': {'fingerprint': list(clean.scan.fingerprint(info))}}
        plan = {'schema': clean.SCHEMA, 'generated_unix': 1730000000,
                'authorization': {'scope': [str(self.root)], 'excluded': [],
                                  'permanent_delete': False},
                'targets': [target]}
        path = self.root / 'plan.json'
        path.write_text(clean.json.dumps(plan), encoding='utf-8')
        with patch.object(clean, 'preflight', return_value=(True, 'ok')), \
                patch.object(clean.git_discovery, 'discover',
                             return_value={'complete': True, 'markers': []}):
            report = clean.run(clean.argparse.Namespace(
                plan=str(path), path=None, confirm_path=None, risk='caution',
                reason=None, go=False))
        self.assertEqual(report['results'][0]['size_bytes'], 2048)

    def test_single_path_refuses_volume_root(self):
        with self.assertRaises(clean.GateError) as ctx:
            clean.run(self.args(path='C:\\', confirm_path='C:\\'))
        self.assertIn('refused', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
