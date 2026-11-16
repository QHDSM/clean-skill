#!/usr/bin/env python3
"""Tests for version.py: one version string, stamped into every artefact.

The point of the module is that a stored report or audit log can be traced back
to the code that wrote it, so these tests check the stamp at the places a reader
would look: the report, each audit row, and ``--version`` on the command line.
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
import version  # noqa: E402


class VersionStringTests(unittest.TestCase):
    def test_the_tool_version_is_semver(self):
        self.assertRegex(version.TOOL_VERSION, r'^\d+\.\d+\.\d+$')

    def test_every_schema_carries_a_version_and_none_collide(self):
        names = [version.PLAN_SCHEMA, version.GOAL_SCHEMA,
                 version.REPORT_SCHEMA, version.AUDIT_SCHEMA]
        self.assertEqual(len(names), len(set(names)))
        for name in names:
            self.assertRegex(name, r'^[a-z0-9-]+/v\d+$')

    def test_clean_uses_the_shared_schema_constants(self):
        self.assertEqual(clean.SCHEMA, version.PLAN_SCHEMA)
        self.assertEqual(clean.GOAL_SCHEMA, version.GOAL_SCHEMA)


class ReportStampTests(unittest.TestCase):
    def test_a_dry_run_report_carries_schema_and_tool_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / 'cache'
            cache.mkdir()
            args = clean.argparse.Namespace(plan=None, path=str(cache),
                                            confirm_path=str(cache), risk='caution',
                                            reason=None, go=False)
            report = clean.run(args)
        self.assertEqual(report['schema'], version.REPORT_SCHEMA)
        self.assertEqual(report['tool_version'], version.TOOL_VERSION)


class AuditStampTests(unittest.TestCase):
    def test_each_audit_row_records_the_tool_version(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(clean, 'RUN_DIR', Path(tmp)):
            journal = clean.AuditJournal.create(None, None)
            self.assertIsNotNone(journal)
            try:
                self.assertTrue(journal.append({'event': 'probe'}))
            finally:
                journal.close()
            rows = [json.loads(line) for line in
                    Path(journal.path).read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['tool_version'], version.TOOL_VERSION)
        self.assertEqual(rows[0]['event'], 'probe')


class CommandLineTests(unittest.TestCase):
    def test_version_flag_prints_the_version_and_exits_clean(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            clean.main(['--version'])
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(out.getvalue().strip(),
                         'disk-cleanup %s' % version.TOOL_VERSION)


if __name__ == '__main__':
    unittest.main()
