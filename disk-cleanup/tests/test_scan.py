import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'scan.py'
spec = importlib.util.spec_from_file_location('scan', SCRIPT)
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / '中文').mkdir()
        (self.root / 'empty').mkdir()
        (self.root / 'a.txt').write_bytes(b'abcd')
        (self.root / '中文' / 'b.txt').write_bytes(b'abcd')
        (self.root / '中文' / 'c.bin').write_bytes(b'zzzz')

    def run_scan(self, **kwargs):
        return scan.Scanner([self.root], progress=False, **kwargs).run()

    def test_totals_and_read_only(self):
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob('*') if p.is_file()}
        result = self.run_scan()
        self.assertEqual((result['files'], result['directories'], result['logical_bytes']), (3, 3, 12))
        self.assertTrue(result['complete'])
        self.assertIsNone(result['allocated_bytes'])
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before})

    def test_top_rows_include_observed_fingerprints(self):
        result = self.run_scan()
        for item in result['largest_files'] + result['largest_directories']:
            self.assertEqual(item['fingerprint'], list(scan.fingerprint(os.lstat(item['path']))))

    def test_depth_exclusion_and_filters(self):
        result = self.run_scan(depth=0)
        self.assertEqual(result['logical_bytes'], 4)
        self.assertFalse(result['complete'])
        self.assertTrue(result['coverage_limited'])
        result = self.run_scan(excludes=[self.root / '中文'], min_size=5, top=1)
        self.assertEqual(result['files'], 1)
        self.assertEqual(result['largest_files'], [])
        self.assertFalse(result['complete'])
        result = self.run_scan(top=1)
        self.assertEqual(len(result['largest_files']), 1)
        self.assertEqual(result['logical_bytes'], 12)

    def test_duplicates_and_no_content_read_by_default(self):
        with patch.object(scan, 'hash_file', side_effect=AssertionError('unexpected content read')):
            self.run_scan()
        result = self.run_scan(duplicates=True)
        self.assertEqual(len(result['duplicate_groups']), 1)
        self.assertEqual(len(result['duplicate_groups'][0]['paths']), 2)

    def test_changed_hash_candidate(self):
        path = self.root / 'a.txt'
        expected = scan.fingerprint(path.stat())
        path.write_bytes(b'changed')
        with self.assertRaises(OSError):
            scan.hash_file(path, expected)

    def test_hash_error_is_reported(self):
        with patch.object(scan, 'hash_file', side_effect=PermissionError('locked')):
            result = self.run_scan(duplicates=True)
        self.assertGreater(result['error_count'], 0)
        self.assertFalse(result['complete'])

    def test_scandir_failure(self):
        real = os.scandir
        def denied(path):
            if Path(path).name == '中文':
                raise PermissionError('denied')
            return real(path)
        with patch.object(scan.os, 'scandir', side_effect=denied):
            result = self.run_scan()
        self.assertEqual(result['files'], 1)
        self.assertEqual(result['error_count'], 1)
        self.assertFalse(result['complete'])

    def test_interruption_returns_partial(self):
        runner = scan.Scanner([self.root], progress=False)
        with patch.object(runner, 'tick', side_effect=KeyboardInterrupt):
            result = runner.run()
        self.assertTrue(result['interrupted'])
        self.assertFalse(result['complete'])

    def test_overlapping_roots(self):
        with self.assertRaises(ValueError):
            scan.Scanner([self.root, self.root / '中文'])

    def test_symlink_cycle_and_escape(self):
        link = self.root / 'cycle'
        try:
            link.symlink_to(self.root, target_is_directory=True)
        except OSError:
            self.skipTest('symlink creation is unavailable')
        result = self.run_scan()
        self.assertEqual(result['files'], 3)
        self.assertGreater(result['skipped_count'], 0)
        with self.assertRaises(ValueError):
            scan.Scanner([link / '中文'])

    @unittest.skipUnless(os.name == 'nt', 'Windows junction test')
    def test_junction_escape(self):
        with tempfile.TemporaryDirectory() as outside:
            Path(outside, 'private').write_bytes(b'not scanned')
            link = self.root / 'junction'
            proc = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), outside], capture_output=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            try:
                result = self.run_scan()
                self.assertEqual(result['files'], 3)
                self.assertGreater(result['skipped_count'], 0)
                with self.assertRaises(ValueError):
                    scan.Scanner([link])
            finally:
                link.rmdir()  # Remove only the test junction itself, never its target.

    def test_hardlinks_not_duplicate_copies(self):
        try:
            os.link(self.root / 'c-source', self.root / 'hard')
        except FileNotFoundError:
            (self.root / 'c-source').write_bytes(b'unique')
            try:
                os.link(self.root / 'c-source', self.root / 'hard')
            except OSError:
                self.skipTest('hardlinks unavailable')
        result = self.run_scan(duplicates=True)
        self.assertFalse(any('c-source' in p for group in result['duplicate_groups'] for p in group['paths']))

    def test_cli_json_and_output_protection(self):
        proc = subprocess.run([sys.executable, str(SCRIPT), str(self.root), '--format', 'json', '--no-progress'], capture_output=True, encoding='utf-8')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result['files'], 3)
        self.assertIn('Logical bytes: 12', scan.render(result))
        report = self.root / 'report.json'
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(scan.main([str(self.root), '--output', str(report), '--format', 'json', '--no-progress']), 0)
        saved = report.read_bytes()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            scan.main([str(self.root), '--output', str(report)])
        self.assertEqual(saved, report.read_bytes())

    def test_incomplete_exit_code(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(scan.main([str(self.root), '--depth', '0', '--no-progress']), 2)

    def test_units_and_boundaries(self):
        self.assertEqual(scan.size_arg('1MiB'), 1048576)
        self.assertEqual(scan.size_arg('1MB'), 1000000)
        self.assertFalse(scan.within(str(self.root) + '-other', self.root))


if __name__ == '__main__':
    unittest.main()
