import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import git_discovery


class GitDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_finds_worktree_file_in_ancestor(self):
        (self.root / '.git').write_text('gitdir: elsewhere', encoding='utf-8')
        child = self.root / 'cache'
        child.mkdir()
        result = git_discovery.discover(child)
        self.assertEqual(result['markers'], [str(self.root / '.git')])
        self.assertTrue(result['complete'])

    def test_finds_nested_repository_without_entering_git_directory(self):
        marker = self.root / 'nested' / '.git'
        marker.mkdir(parents=True)
        (marker / 'hidden').mkdir()
        result = git_discovery.discover(self.root)
        self.assertEqual(result['markers'], [str(marker)])
        self.assertEqual(result['entries_checked'], 2)

    def test_reports_incomplete_on_limit(self):
        (self.root / 'child').mkdir()
        result = git_discovery.discover(self.root, max_entries=0)
        self.assertFalse(result['complete'])
        self.assertIn('limit', result['issues'][0])

    def test_reports_unreadable_directory(self):
        with patch.object(git_discovery.os, 'scandir', side_effect=PermissionError('denied')):
            result = git_discovery.discover(self.root)
        self.assertFalse(result['complete'])
        self.assertIn('denied', result['issues'][0])


if __name__ == '__main__':
    unittest.main()
