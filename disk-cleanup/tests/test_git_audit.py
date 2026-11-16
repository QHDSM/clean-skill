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

class CoverageTests(unittest.TestCase):
    """一个已推送、干净的仓库必须能拿到 SAFE_TO_CONSIDER。

    这条路径此前被两条全局范围限制堵死：它们被塞进 unknown，而
    tracked_history_covered 又要求 not unknown，于是判定永远到不了
    SAFE_TO_CONSIDER。范围限制现在单独进 caveats。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'work'
        self.root.mkdir()
        self.remote = Path(self.tmp.name) / 'remote.git'
        self.git('init', '-q')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        (self.root / 'file.txt').write_text('original', encoding='utf-8')
        self.git('add', 'file.txt')
        self.git('commit', '-qm', 'initial')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def publish(self):
        subprocess.run(['git', 'init', '-q', '--bare', str(self.remote)], check=True,
                       capture_output=True)
        self.git('remote', 'add', 'origin', str(self.remote))
        self.git('push', '-q', '-u', 'origin', 'HEAD')

    def test_pushed_clean_repository_is_safe_to_consider(self):
        self.publish()
        result = git_audit.audit(str(self.root))
        self.assertEqual(result['verdict'], 'SAFE_TO_CONSIDER')
        self.assertTrue(result['tracked_history_covered'])
        self.assertTrue(result['all_content_synced'])
        self.assertEqual(result['local_commits_not_on_a_remote'], 0)
        self.assertEqual(result['unknown'], [])
        self.assertEqual(result['risks'], [])
        self.assertFalse(result['needs_confirmation'])
        # 范围限制仍然要说出口：它们不是零风险，只是不参与覆盖判定。
        self.assertTrue(result['caveats'])
        self.assertTrue(any('CACHED' in caveat for caveat in result['caveats']))

    def test_unpushed_commit_is_counted_and_not_covered(self):
        self.publish()
        (self.root / 'file.txt').write_text('changed', encoding='utf-8')
        self.git('commit', '-qam', 'local only')
        result = git_audit.audit(str(self.root))
        self.assertEqual(result['local_commits_not_on_a_remote'], 1)
        self.assertFalse(result['tracked_history_covered'])
        self.assertFalse(result['all_content_synced'])
        self.assertEqual(result['verdict'], 'LOCAL_ONLY_DATA_PRESENT')
        self.assertTrue(any('not on any remote-tracking ref' in risk for risk in result['risks']))

    def test_no_remote_means_coverage_is_unknown_not_zero(self):
        result = git_audit.audit(str(self.root))
        self.assertIsNone(result['local_commits_not_on_a_remote'])
        self.assertFalse(result['tracked_history_covered'])
        self.assertIn('no remote configured', result['risks'])

    def test_bare_repository_is_audited_but_never_synced(self):
        self.publish()
        bare = Path(self.tmp.name) / 'copy.git'
        subprocess.run(['git', 'clone', '-q', '--bare', str(self.remote), str(bare)],
                       check=True, capture_output=True)
        result = git_audit.audit(str(bare))
        self.assertTrue(result['is_git_repo'])
        self.assertTrue(result['bare'])
        self.assertFalse(result['all_content_synced'])
        self.assertTrue(any('bare repository' in risk for risk in result['risks']))

    def test_repository_shape_fields_are_present(self):
        result = git_audit.audit(str(self.root))
        for key in ('git_dir', 'branch', 'detached', 'bare', 'remote_verified',
                    'checked_unix', 'local_commits_not_on_a_remote', 'caveats'):
            self.assertIn(key, result)
        self.assertFalse(result['remote_verified'])
        self.assertFalse(result['detached'])
        self.assertTrue(result['git_dir'].endswith('.git') or result['git_dir'].endswith('.git' + os.sep))
