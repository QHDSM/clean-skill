import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import tree_evidence


class TreeEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.child = self.root / 'nested'
        self.child.mkdir()
        self.file = self.child / 'data.txt'
        self.file.write_bytes(b'original')

    def test_stable_and_detects_nested_modification(self):
        first = tree_evidence.capture(self.root)
        self.assertTrue(first['complete'])
        self.assertEqual(first['digest'], tree_evidence.capture(self.root)['digest'])
        self.file.write_bytes(b'a different size')
        self.assertNotEqual(first['digest'], tree_evidence.capture(self.root)['digest'])

    def test_detects_addition_and_removal(self):
        first = tree_evidence.capture(self.root)
        new = self.child / 'new'
        new.write_bytes(b'x')
        self.assertNotEqual(first['digest'], tree_evidence.capture(self.root)['digest'])
        second = tree_evidence.capture(self.root)
        new.unlink()
        self.assertNotEqual(second['digest'], tree_evidence.capture(self.root)['digest'])

    def test_limit_and_permission_errors_have_no_digest(self):
        limited = tree_evidence.capture(self.root, max_entries=1)
        self.assertFalse(limited['complete'])
        self.assertIsNone(limited['digest'])
        with patch.object(tree_evidence.os, 'scandir', side_effect=PermissionError('denied')):
            denied = tree_evidence.capture(self.root)
        self.assertFalse(denied['complete'])
        self.assertIsNone(denied['digest'])

    @unittest.skipUnless(os.name == 'nt', 'Windows junction test')
    def test_junction_is_incomplete_without_traversing_target(self):
        with tempfile.TemporaryDirectory() as outside:
            Path(outside, 'private').write_bytes(b'outside')
            junction = self.root / 'junction'
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), outside],
                                    capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            try:
                evidence = tree_evidence.capture(self.root)
                self.assertFalse(evidence['complete'])
                self.assertIsNone(evidence['digest'])
                self.assertTrue(Path(outside, 'private').exists())
            finally:
                junction.rmdir()


if __name__ == '__main__':
    unittest.main()
