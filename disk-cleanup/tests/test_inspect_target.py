import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import clean
import inspect_target


@unittest.skipUnless(os.name == 'nt', 'Windows evidence collection test')
class InspectTargetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.target = self.root / 'candidate'
        self.target.mkdir()
        self.file = self.target / 'file'
        self.file.write_bytes(b'owned')

    def test_evidence_can_be_used_in_real_plan_preview(self):
        report = inspect_target.collect(self.target, tree=True)
        self.assertTrue(report['complete'], report['errors'])
        plan = {'schema': clean.SCHEMA, 'authorization': {'scope': [str(self.root)]},
                'targets': [{'path': str(self.target), 'action': 'trash', 'risk': 'caution',
                             'reason': 'test fixture', 'evidence': report['evidence']}]}
        saved = self.root / 'plan.json'
        saved.write_text(json.dumps(plan), encoding='utf-8')
        args = clean.argparse.Namespace(plan=str(saved), path=None, go=False)
        preview = clean.run(args)
        self.assertEqual(preview['summary']['dry_run'], 1)
        self.assertEqual(self.file.read_bytes(), b'owned')

    def test_change_during_collection_withholds_evidence(self):
        real = inspect_target.git_discovery.discover
        def mutate(path):
            self.file.write_bytes(b'changed during collection')
            return real(path)
        with patch.object(inspect_target.git_discovery, 'discover', side_effect=mutate):
            result = inspect_target.collect(self.file)
        self.assertFalse(result['complete'])
        self.assertIsNone(result['evidence'])
        self.assertIn('changed during collection', result['errors'][0])

    def test_incomplete_discovery_withholds_evidence(self):
        with patch.object(inspect_target.git_discovery, 'discover', return_value={
                'complete': False, 'markers': [], 'issues': ['limit'], 'entries_checked': 1}):
            result = inspect_target.collect(self.file)
        self.assertFalse(result['complete'])
        self.assertIsNone(result['evidence'])

    def test_git_marker_does_not_fabricate_audit_or_permission(self):
        (self.target / '.git').mkdir()
        result = inspect_target.collect(self.target)
        self.assertTrue(result['complete'])
        self.assertTrue(result['git_discovery']['markers'])
        self.assertNotIn('git', result['evidence'])
        self.assertNotIn('authorization', result)

    def test_cli_and_directory_size_semantics(self):
        proc = subprocess.run([sys.executable, str(SCRIPTS / 'inspect_target.py'), str(self.target), '--tree'],
                               capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertNotIn('size_bytes', report['evidence'])
        self.assertIn('tree_metadata', report['evidence'])
        self.assertEqual(inspect_target.collect(self.file)['evidence']['size_bytes'], 5)


if __name__ == '__main__':
    unittest.main()
