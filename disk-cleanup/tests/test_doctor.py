#!/usr/bin/env python3
"""Tests for doctor.py: a consistent verdict, and nothing written along the way.

The checks themselves read the live machine, so these tests stay away from the
real Recycle Bin and audit directory: they either stub the probes or point the
run directory at a temporary path.
"""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'scripts'))

import clean    # noqa: E402
import doctor   # noqa: E402
import version  # noqa: E402

SECTIONS = ('_environment', '_backend', '_volumes', '_usn', '_history', '_git')


def capability(**over):
    base = {'schema': 'disk-cleanup-volume/v1', 'drive': 'C:', 'filesystem': 'NTFS',
            'supported_filesystem': True, 'read_only_volume': False,
            'sid': 'S-1-5-21-0', 'recycle_bin_root': 'C:\\$RECYCLE.BIN\\S-1-5-21-0',
            'recycle_bin_root_exists': True, 'volume_guid': '{x}',
            'volume_guid_resolved': True, 'max_capacity_mb': 10240,
            'nuke_on_delete': False, 'no_recycle_policy': False,
            'free_bytes': 1 << 30, 'verified_capable': True, 'issues': []}
    base.update(over)
    return base


def usage(**over):
    base = {'drive': 'C:', 'root': 'C:\\$RECYCLE.BIN\\S-1-5-21-0', 'bytes': 10,
            'entries': 2, 'complete': True, 'issues': []}
    base.update(over)
    return base


class StubSectionMixin:
    def stub_all(self, stack, **sections):
        """Replace every section check so only the given rows decide the verdict."""
        for name in SECTIONS:
            rows = sections.get(name, [])
            stack.enter_context(patch.object(doctor, name, return_value=rows))


class ReportShapeTests(unittest.TestCase):
    def test_counts_verdict_and_exit_code_agree_on_the_live_machine(self):
        report = doctor.run(drives=['C:'])
        counts = report['counts']
        self.assertEqual(sum(counts.values()), len(report['checks']))
        self.assertEqual(report['schema'], doctor.SCHEMA)
        self.assertEqual(report['tool_version'], version.TOOL_VERSION)
        if counts['fail']:
            expected = 2
        elif counts['warn']:
            expected = 1
        else:
            expected = 0
        self.assertEqual(report['exit_code'], expected)
        self.assertEqual(report['verdict'], doctor.VERDICTS[expected])
        for row in report['checks']:
            self.assertIn(row['status'], doctor.LABELS)
            self.assertTrue(row['section'])
            self.assertTrue(row['name'])
            self.assertTrue(row['detail'])

    def test_render_names_every_check_section_and_the_verdict(self):
        report = doctor.run(drives=['C:'])
        text = doctor.render(report)
        self.assertIn(report['verdict'], text)
        for row in report['checks']:
            self.assertIn(row['name'], text)
            self.assertIn(row['section'], text)


class VerdictTests(StubSectionMixin, unittest.TestCase):
    def test_a_warning_without_a_failure_is_still_ready_with_warnings(self):
        with contextlib.ExitStack() as stack:
            self.stub_all(stack, _volumes=[
                doctor._check('volumes', 'capability', 'warn', 'slow but usable')])
            report = doctor.run(drives=['C:'])
        self.assertEqual(report['counts']['warn'], 1)
        self.assertEqual(report['exit_code'], 1)
        self.assertEqual(report['verdict'], 'READY WITH WARNINGS')

    def test_a_failed_check_makes_the_report_not_ready(self):
        with contextlib.ExitStack() as stack:
            self.stub_all(stack, _backend=[
                doctor._check('backend', 'recycle-bin', 'fail', 'no bin here')])
            report = doctor.run(drives=['C:'])
        self.assertEqual(report['exit_code'], 2)
        self.assertEqual(report['verdict'], 'NOT READY')
        self.assertEqual(report['notes'], ['no bin here'])

    def test_an_unexpected_exception_becomes_a_failing_row(self):
        with contextlib.ExitStack() as stack:
            for name in SECTIONS:
                stack.enter_context(patch.object(doctor, name, return_value=[]))
            stack.enter_context(patch.object(doctor, '_backend',
                                             side_effect=RuntimeError('probe exploded')))
            report = doctor.run(drives=['C:'])
        rows = [row for row in report['checks'] if row['status'] == 'fail']
        self.assertEqual(len(rows), 1)
        self.assertIn('probe exploded', rows[0]['detail'])
        self.assertEqual(report['exit_code'], 2)


class VolumeTests(unittest.TestCase):
    def test_a_volume_that_cannot_prove_a_recycle_fails(self):
        with patch.object(doctor.recycle_backend, 'volume_profile',
                          return_value=capability(verified_capable=False,
                                                  issues=['recycling is disabled by policy'])), \
                patch.object(doctor.recycle_bin, 'bin_usage', return_value=usage()):
            report = doctor.run(drives=['C:'])
        rows = [row for row in report['checks']
                if row['section'] == 'volumes' and row['name'] == 'capability']
        self.assertEqual(rows[0]['status'], 'fail')
        self.assertIn('policy', rows[0]['detail'])
        self.assertEqual(report['exit_code'], 2)

    def test_an_unsupported_filesystem_fails_before_capability_is_trusted(self):
        with patch.object(doctor.recycle_backend, 'volume_profile',
                          return_value=capability(filesystem='exFAT',
                                                  supported_filesystem=False,
                                                  verified_capable=True)), \
                patch.object(doctor.recycle_bin, 'bin_usage', return_value=usage()):
            report = doctor.run(drives=['C:'])
        rows = [row for row in report['checks'] if row['name'] == 'capability']
        self.assertEqual(rows[0]['status'], 'fail')

    def test_other_accounts_bins_are_not_reported_as_trouble(self):
        noisy = usage(complete=False, issues=[
            'permission denied for S-1-5-18: [WinError 5] Access is denied',
            'permission denied for S-1-5-19: [WinError 5] Access is denied'])
        with patch.object(doctor.recycle_backend, 'volume_profile',
                          return_value=capability()), \
                patch.object(doctor.recycle_bin, 'bin_usage', return_value=noisy):
            report = doctor.run(drives=['C:'])
        rows = [row for row in report['checks'] if row['name'] == 'bin-usage']
        self.assertEqual(rows[0]['status'], 'ok')
        self.assertIn('normal without elevation', rows[0]['detail'])

    def test_an_unreadable_leftover_is_reported_but_kept_short(self):
        noisy = usage(complete=False, issues=[
            'orphan $I without $R: C:\\$RECYCLE.BIN\\S-1\\$I' + 'x' * 400])
        with patch.object(doctor.recycle_backend, 'volume_profile',
                          return_value=capability()), \
                patch.object(doctor.recycle_bin, 'bin_usage', return_value=noisy):
            report = doctor.run(drives=['C:'])
        rows = [row for row in report['checks'] if row['name'] == 'bin-usage']
        self.assertEqual(rows[0]['status'], 'warn')
        self.assertLess(len(rows[0]['detail']), 300)


class ReadOnlyTests(unittest.TestCase):
    def test_a_run_does_not_create_the_audit_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / 'runs'
            with patch.object(clean, 'RUN_DIR', runs):
                before = sorted(p.name for p in Path(tmp).iterdir())
                doctor.run(drives=['C:'])
                after = sorted(p.name for p in Path(tmp).iterdir())
            self.assertEqual(before, after)
            self.assertFalse(runs.exists())


class CommandLineTests(unittest.TestCase):
    def test_json_payload_matches_the_exit_code(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = doctor.main(['--format', 'json', '--drive', 'C:'])
        payload = json.loads(out.getvalue())
        self.assertEqual(payload['exit_code'], code)
        self.assertEqual(payload['counts'],
                         {'ok': sum(1 for r in payload['checks'] if r['status'] == 'ok'),
                          'warn': sum(1 for r in payload['checks'] if r['status'] == 'warn'),
                          'fail': sum(1 for r in payload['checks'] if r['status'] == 'fail')})

    def test_version_flag_prints_the_version_and_exits_clean(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            doctor.main(['--version'])
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(out.getvalue().strip(),
                         'disk-cleanup %s' % version.TOOL_VERSION)


if __name__ == '__main__':
    unittest.main()
