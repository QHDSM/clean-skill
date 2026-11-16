import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import importlib.util
import sys

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'targets.py'
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location('targets', SCRIPT)
targets = importlib.util.module_from_spec(spec)
spec.loader.exec_module(targets)


def cleaner(**overrides):
    base = {
        'id': 'sample-cache',
        'label': '示例缓存',
        'description': '仅用于测试',
        'category': 'development',
        'status': 'release',
        'risk': 'safe',
        'action': 'delete',
        'paths': ['%TESTROOT%\\cache'],
    }
    base.update(overrides)
    return base


def registry(*cleaners):
    return {'schema': targets.SCHEMA, 'cleaners': list(cleaners)}


class ValidateTests(unittest.TestCase):
    def test_the_shipped_registry_is_valid(self):
        loaded = targets.load(targets.DEFAULT_REGISTRY)
        self.assertGreaterEqual(len(loaded['cleaners']), 10)
        keys = [item['id'] for item in loaded['cleaners']]
        self.assertEqual(len(keys), len(set(keys)))

    def test_wrong_schema(self):
        problems = targets.validate({'schema': 'nope', 'cleaners': [cleaner()]})
        self.assertTrue(any('schema must be' in p for p in problems))

    def test_duplicate_ids(self):
        problems = targets.validate(registry(cleaner(), cleaner()))
        self.assertTrue(any('duplicated' in p for p in problems))

    def test_unknown_risk(self):
        problems = targets.validate(registry(cleaner(risk='probably-fine')))
        self.assertTrue(any('.risk must be' in p for p in problems))

    def test_pending_needs_a_note(self):
        problems = targets.validate(registry(cleaner(status='pending')))
        self.assertTrue(any('no note' in p for p in problems))
        ok = cleaner(status='pending', note='未验证磁盘格式')
        self.assertEqual(targets.validate(registry(ok)), [])

    def test_parent_traversal_is_refused(self):
        problems = targets.validate(registry(cleaner(paths=['%TESTROOT%\\..\\secret'])))
        self.assertTrue(any('parent traversal' in p for p in problems))

    def test_relative_template_is_refused(self):
        problems = targets.validate(registry(cleaner(paths=['cache\\tmp'])))
        self.assertTrue(any('drive letter' in p for p in problems))

    def test_trailing_separator_is_refused(self):
        problems = targets.validate(registry(cleaner(paths=['%TESTROOT%\\cache\\'])))
        self.assertTrue(any('must not end' in p for p in problems))

    def test_running_probe_shape(self):
        problems = targets.validate(registry(cleaner(running=[{'type': 'pid', 'value': 4}])))
        self.assertTrue(any('running.type' in p for p in problems))


class ExpandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        (self.root / 'cache').mkdir()
        (self.root / 'profile-a').mkdir()
        (self.root / 'profile-b').mkdir()

    def test_environment_variable_and_glob(self):
        item = cleaner(paths=['%TESTROOT%\\profile-*'])
        with patch.dict(os.environ, {'TESTROOT': str(self.root)}):
            paths, missing = targets.expand(item)
        self.assertEqual(missing, [])
        self.assertEqual(sorted(os.path.basename(p) for p in paths), ['profile-a', 'profile-b'])

    def test_missing_variable_is_reported_not_guessed(self):
        item = cleaner(paths=['%NOT_SET_ANYWHERE%\\cache'])
        with patch.dict(os.environ, {}, clear=True):
            paths, missing = targets.expand(item)
        self.assertEqual(paths, [])
        self.assertEqual(missing, ['NOT_SET_ANYWHERE'])

    def test_cleaner_vars_win_over_the_environment(self):
        item = cleaner(paths=['%ROOT%\\cache'], vars={'ROOT': str(self.root)})
        with patch.dict(os.environ, {'ROOT': 'C:\\definitely-wrong'}):
            paths, _ = targets.expand(item)
        self.assertEqual(paths, [str(self.root / 'cache')])


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        (self.root / 'cache').mkdir()
        (self.root / 'cache' / 'a.bin').write_bytes(b'x' * 100)
        self.procs = {'names': [], 'paths': set()}

    def run_report(self, *cleaners, **kwargs):
        with patch.dict(os.environ, {'TESTROOT': str(self.root)}):
            return targets.report(registry(*cleaners), procs=self.procs, **kwargs)

    def test_eligible_when_the_path_exists_and_is_allowed(self):
        result = self.run_report(cleaner())
        row = result['cleaners'][0]
        self.assertTrue(row['eligible'])
        self.assertIsNone(row['reason'])
        self.assertEqual(row['paths'][0]['kind'], 'directory')

    def test_never_entries_are_never_candidates(self):
        result = self.run_report(cleaner(risk='never', category='protection'))
        row = result['cleaners'][0]
        self.assertFalse(row['eligible'])
        self.assertIn('risk=never', row['reason'])

    def test_pending_entries_are_never_candidates(self):
        result = self.run_report(cleaner(status='pending', note='未验证'))
        self.assertFalse(result['cleaners'][0]['eligible'])

    def test_missing_path_is_not_eligible(self):
        result = self.run_report(cleaner(paths=['%TESTROOT%\\absent']))
        row = result['cleaners'][0]
        self.assertFalse(row['eligible'])
        self.assertIn('no such path', row['reason'])
        self.assertEqual(row['paths'], [])

    def test_running_process_is_reported_and_does_not_flip_eligibility(self):
        self.procs = {'names': ['node.exe'], 'paths': set()}
        result = self.run_report(cleaner(running=[{'type': 'exe', 'value': 'node.exe',
                                                   'same_user': True}]))
        row = result['cleaners'][0]
        self.assertTrue(row['running']['active'])
        self.assertTrue(row['eligible'], 'a running process is a warning, not a veto')
        self.assertEqual(result['summary']['running'], 1)

    def test_absent_process_is_not_reported_as_running(self):
        result = self.run_report(cleaner(running=[{'type': 'exe', 'value': 'nonexistent.exe'}]))
        self.assertFalse(result['cleaners'][0]['running']['active'])

    def test_size_is_only_measured_on_request(self):
        plain = self.run_report(cleaner())
        self.assertIsNone(plain['cleaners'][0]['bytes'])
        measured = self.run_report(cleaner(), stat_size=True)
        self.assertEqual(measured['cleaners'][0]['bytes'], 100)

    @unittest.skipUnless(os.name == 'nt', 'Windows path rules only')
    def test_gate_refusal_is_surfaced_not_silently_dropped(self):
        result = self.run_report(cleaner(paths=['C:\\Windows\\Temp']))
        row = result['cleaners'][0]
        self.assertTrue(row['paths'][0]['exists'])
        self.assertFalse(row['eligible'])
        self.assertIn('protected system tree', row['reason'])

    def test_tree_size_skips_missing_paths(self):
        self.assertEqual(targets.tree_size(str(self.root / 'nothing-here')), 0)
        self.assertEqual(targets.tree_size(str(self.root / 'cache')), 100)


if __name__ == '__main__':
    unittest.main()
