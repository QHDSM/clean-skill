#!/usr/bin/env python3
"""Tests for clean.py: the gate must reject bad plans before anything executes.

These tests never touch a real Recycle Bin. They exercise plan validation, path
preflight, fingerprint re-verification and dry-run behaviour. Actual Recycle Bin
behaviour needs a real Windows machine; see the note in the test module docstring
of test_scan.py for the same limitation.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'scripts'))

import clean  # noqa: E402
import scan   # noqa: E402


def fingerprint_of(path):
    return list(scan.fingerprint(os.lstat(path)))


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

    def test_accepts_git_verdict_on_caution(self):
        target = self.factory.target(self.victim, risk='caution')
        target['evidence']['git'] = {'verdict': 'LOCAL_ONLY_DATA_PRESENT'}
        plan = self.factory.plan([target])
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

    def test_preview_shows_nested_git_and_directory_limit(self):
        marker = self.cache / 'project' / '.git'
        marker.mkdir(parents=True)
        report = self.run_plan(self.factory.plan([self.factory.target(self.cache)]))
        entry = report['results'][0]
        self.assertIn(str(marker), entry['git_discovery']['markers'])
        self.assertFalse(entry['directory_contents_verified'])
        self.assertIn('Git marker:', clean.render(report))

    def test_execution_is_disabled_before_any_action(self):
        with self.assertRaisesRegex(clean.GateError, 'execution disabled'):
            self.run_plan(self.factory.plan([self.factory.target(self.cache)]), go=True)
        self.assertTrue((self.cache / 'blob.bin').exists())

    def test_trash_helper_is_disabled(self):
        ok, detail = clean.trash_one(str(self.cache))
        self.assertFalse(ok)
        self.assertIn('execution disabled', detail)
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

    def test_single_path_refuses_volume_root(self):
        with self.assertRaises(clean.GateError) as ctx:
            clean.run(self.args(path='C:\\', confirm_path='C:\\'))
        self.assertIn('refused', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
