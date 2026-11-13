import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import compare_scans
import scan


class CompareScansTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.file = self.root / 'file'
        self.file.write_bytes(b'old')
        self.before = scan.Scanner([self.root], progress=False).run()
        self.file.write_bytes(b'larger content')
        self.after = scan.Scanner([self.root], progress=False).run()

    def test_real_scan_growth_and_cli(self):
        result = compare_scans.compare(self.before, self.after)
        self.assertTrue(result['comparable'])
        self.assertEqual(result['logical_bytes_delta'], len(b'larger content') - 3)
        before, after = self.root / 'before.json', self.root / 'after.json'
        before.write_text(json.dumps(self.before), encoding='utf-8')
        after.write_text(json.dumps(self.after), encoding='utf-8')
        proc = subprocess.run([sys.executable, str(SCRIPTS / 'compare_scans.py'), str(before), str(after)],
                               capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)['logical_bytes_delta'], result['logical_bytes_delta'])
        self.assertEqual(self.file.read_bytes(), b'larger content')

    def test_missing_top_entry_is_not_zero_or_deleted(self):
        after = copy.deepcopy(self.after)
        after['largest_files'] = []
        result = compare_scans.compare(self.before, after)
        row = result['largest_files'][0]
        self.assertEqual(row['observation'], 'only_in_baseline_top')
        self.assertIsNone(row['current_logical_bytes'])
        self.assertIsNone(row['observed_size_delta'])
        reverse = compare_scans.compare(after, after)
        self.assertEqual(reverse['largest_files'], [])

    def test_scope_and_coverage_differences_suppress_deltas(self):
        for change in ({'complete': False}, {'error_count': 1}, {'coverage_limited': True},
                       {'roots': [str(self.root / 'other')]}, {'scope': {'depth': 0}},
                       {'started_at_unix': self.before['started_at_unix'] - 1}):
            after = copy.deepcopy(self.after)
            after.update(change)
            result = compare_scans.compare(self.before, after)
            self.assertFalse(result['comparable'])
            self.assertIsNone(result['logical_bytes_delta'])
            self.assertTrue(all(row['observed_size_delta'] is None for row in result['largest_files']))

    def test_invalid_report_is_rejected(self):
        for key, value in (('logical_bytes', True), ('files', -1), ('complete', 'true'),
                            ('started_at_unix', float('nan')), ('scope', {})):
            after = copy.deepcopy(self.after)
            after[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                compare_scans.compare(self.before, after)


if __name__ == '__main__':
    unittest.main()
