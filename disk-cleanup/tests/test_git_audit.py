import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import git_audit


class GitAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.file = self.root / 'file.txt'
        self.file.write_text('original', encoding='utf-8')
        self.git('add', 'file.txt')
        self.git('commit', '-qm', 'initial')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def test_deleted_file_is_modification_not_conflict(self):
        self.file.unlink()
        counts = git_audit.porcelain_audit(str(self.root))['counts']
        self.assertEqual(counts['modified'], 1)
        self.assertEqual(counts['conflicts'], 0)
        self.git('add', '-u')
        counts = git_audit.porcelain_audit(str(self.root))['counts']
        self.assertEqual(counts['staged'], 1)
        self.assertEqual(counts['conflicts'], 0)

    def test_real_ahead_branch_is_reported(self):
        branch = self.git('branch', '--show-current')
        self.git('remote', 'add', 'origin', str(self.root / 'no-network'))
        self.git('update-ref', 'refs/remotes/origin/' + branch, self.git('rev-parse', 'HEAD'))
        self.git('config', 'branch.' + branch + '.remote', 'origin')
        self.git('config', 'branch.' + branch + '.merge', 'refs/heads/' + branch)
        self.file.write_text('changed', encoding='utf-8')
        self.git('add', 'file.txt')
        self.git('commit', '-qm', 'local')
        result = git_audit.audit(str(self.root))
        self.assertEqual(result['unpushed_commit_count'], 1)
        self.assertEqual(result['verdict'], 'LOCAL_ONLY_DATA_PRESENT')

    def test_gone_upstream_is_unknown(self):
        with patch.object(git_audit, 'run_git', return_value=(0, 'main|origin/main|[gone]\n', '')):
            result = git_audit.upstream_audit(str(self.root))
        self.assertTrue(result['unknown'])
        self.assertTrue(result['local_only_refs'])

    def test_divergent_branch_counts_both_sides(self):
        with patch.object(git_audit, 'run_git', return_value=(0, 'main|origin/main|[ahead 2, behind 3]\n', '')):
            result = git_audit.upstream_audit(str(self.root))
        self.assertEqual(result['branches'][0]['ahead'], 2)
        self.assertEqual(result['branches'][0]['behind'], 3)
        self.assertEqual(result['unpushed_commit_count'], 2)

    def test_failures_and_detached_head_remain_unknown(self):
        self.git('checkout', '--detach', '-q')
        with patch.object(git_audit, 'worktree_state', return_value={'error': 'denied', 'linked_worktrees': 0}):
            result = git_audit.audit(str(self.root))
        self.assertTrue(any('detached HEAD' in s for s in result['unknown']))
        self.assertTrue(any('linked worktree' in s for s in result['unknown']))
        self.assertFalse(result['all_content_synced'])


if __name__ == '__main__':
    unittest.main()
