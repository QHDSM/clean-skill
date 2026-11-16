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
sys.path.insert(0, str(SCRIPT.parent))
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
        self.assertTrue(result['duplicate_groups'][0]['distinct_file_ids_observed'])

    def test_duplicate_identity_unavailable_is_not_deduplicated_or_claimed_distinct(self):
        real = scan.fingerprint
        def without_inode(info):
            values = list(real(info))
            values[1] = 0
            return tuple(values)
        with patch.object(scan, 'fingerprint', side_effect=without_inode):
            result = self.run_scan(duplicates=True)
        group = result['duplicate_groups'][0]
        self.assertEqual(len(group['paths']), 2)
        self.assertFalse(group['distinct_file_ids_observed'])

    def test_hash_ignores_change_time_only(self):
        path = self.root / 'a.txt'
        expected = list(scan.fingerprint(os.lstat(path)))
        expected[4] -= 1000
        self.assertEqual(len(scan.hash_file(path, tuple(expected))), 64)

    def test_failed_hash_does_not_suppress_other_hardlink(self):
        link = self.root / 'z-hardlink'
        try:
            os.link(self.root / 'a.txt', link)
        except OSError:
            self.skipTest('hardlinks unavailable')
        real = scan.hash_file
        first_identity = scan.fingerprint(os.lstat(link))[:2]
        failed = []
        def fail_once(path, expected):
            if expected[:2] == first_identity and not failed:
                failed.append(path)
                raise OSError('simulated transient read error')
            return real(path, expected)
        with patch.object(scan, 'hash_file', side_effect=fail_once):
            result = self.run_scan(duplicates=True)
        self.assertEqual(len(failed), 1)
        self.assertEqual(len(result['duplicate_groups']), 1)
        self.assertGreater(result['error_count'], 0)

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

    @unittest.skipUnless(os.name == 'nt', 'Windows allocation metadata test')
    def test_allocation_counts_file_identity_once(self):
        import windows_identity
        originals = [self.root / 'a.txt', self.root / '中文' / 'b.txt', self.root / '中文' / 'c.bin']
        expected = sum(windows_identity.capture(p, allocation=True)['allocation_bytes'] for p in originals)
        try:
            os.link(originals[0], self.root / 'hardlink')
        except OSError:
            self.skipTest('hardlinks unavailable')
        result = self.run_scan(allocation=True)
        observation = result['allocation_observation']
        self.assertTrue(observation['complete'])
        self.assertEqual(observation['reported_file_allocation_bytes'], expected)
        self.assertEqual(observation['paths_queried'], 4)
        self.assertEqual(observation['distinct_file_ids'], 3)
        self.assertEqual(observation['repeated_hardlink_paths'], 1)
        self.assertIsNone(observation['reclaimable_bytes'])
        # The report may state an upper bound, never a reclaimable figure.
        self.assertEqual(result['allocated_bytes'], expected)
        self.assertTrue(observation['upper_bound_of_reclaimable_bytes'])
        text = scan.render(result)
        self.assertIn('Allocated bytes (distinct file IDs):', text)
        self.assertIn('Reclaimable bytes: unknown', text)

    @unittest.skipUnless(os.name == 'nt', 'Windows allocation metadata test')
    def test_allocation_failure_reports_incomplete_observation(self):
        import windows_identity
        with patch.object(windows_identity, 'capture', side_effect=OSError('denied')):
            result = self.run_scan(allocation=True)
        self.assertFalse(result['allocation_observation']['complete'])
        self.assertGreater(result['error_count'], 0)
        self.assertEqual(result['files'], 3)
        # A partial observation must not be promoted to a number.
        self.assertIsNone(result['allocated_bytes'])

    def test_default_scan_does_not_query_native_allocation(self):
        import windows_identity
        with patch.object(windows_identity, 'capture', side_effect=AssertionError('unexpected query')):
            result = self.run_scan()
        self.assertIsNone(result['allocation_observation'])
        self.assertIsNone(result['allocated_bytes'])
        self.assertIn('Allocated/reclaimable bytes: unknown', scan.render(result))


@unittest.skipUnless(os.name == 'nt', 'NTFS alternate data streams are Windows-only')
class StreamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'plain.txt').write_bytes(b'visible')
        self.carrier = self.root / 'carrier.txt'
        self.carrier.write_bytes(b'small')
        for stream, payload in (((':hidden', 'secret payload'), (':Zone.Identifier', 'zone'))):
            made = subprocess.run(['cmd', '/c', 'echo', payload, '>', str(self.carrier) + stream],
                                  capture_output=True)
            if made.returncode != 0:
                self.skipTest('this volume has no alternate data stream support')

    def tearDown(self):
        self.tmp.cleanup()

    def test_named_streams_are_counted_outside_the_totals(self):
        result = scan.Scanner([str(self.root)], progress=False, streams=True).run()
        streams = result['alternate_streams']
        self.assertEqual(streams['files_with_streams'], 1)
        self.assertGreater(streams['alternate_bytes'], 0)
        self.assertEqual(len(streams['samples']), 1)
        names = [item['name'] for item in streams['samples'][0]['streams']]
        self.assertIn(':hidden:$DATA', names)
        self.assertEqual(result['logical_bytes'], len(b'visible') + len(b'small'))
        self.assertIn('Alternate data streams: 1 file(s)', scan.render(result))
        self.assertIn('outside logical/allocated totals', scan.render(result))

    def test_streams_stay_off_without_the_flag(self):
        result = scan.Scanner([str(self.root)], progress=False).run()
        self.assertIsNone(result['alternate_streams'])
        self.assertNotIn('Alternate data streams:', scan.render(result))


class ConcurrentChangeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.watched = self.root / 'busy'
        self.watched.mkdir()
        (self.watched / 'member.bin').write_bytes(b'x' * 16)
        (self.root / 'quiet').mkdir()
        (self.root / 'quiet' / 'member.bin').write_bytes(b'y' * 8)

    def tearDown(self):
        self.tmp.cleanup()

    class _Stat:
        def __init__(self, real, mtime_ns):
            self.st_mode = real.st_mode
            self.st_dev = real.st_dev
            self.st_ino = real.st_ino
            self.st_size = real.st_size
            self.st_mtime_ns = mtime_ns
            self.st_ctime_ns = real.st_ctime_ns
            self.st_file_attributes = getattr(real, 'st_file_attributes', 0)

    class _Drained:
        def __init__(self, inner, on_exhausted):
            self.inner, self.on_exhausted = inner, on_exhausted

        def __iter__(self):
            return self

        def __next__(self):
            try:
                return next(self.inner)
            except StopIteration:
                self.on_exhausted()
                raise

        def close(self):
            self.inner.close()

    def _move_after_walking(self, watched, offset_ns=10_000_000):
        """Simulate a directory gaining or losing a member after it has been read.

        The scanner lstats a child once in its parent's loop, again when entering
        it, and a third time after walking it. Only that last call may see the new
        timestamp, so the move is keyed on scandir exhaustion, not on call count.
        """
        state = {'walked': False}
        key = os.path.normcase(str(watched))
        real_lstat, real_scandir = os.lstat, os.scandir

        def lstat(path):
            info = real_lstat(path)
            if state['walked'] and os.path.normcase(str(path)) == key:
                return self._Stat(info, info.st_mtime_ns + offset_ns)
            return info

        def scandir(path):
            inner = real_scandir(path)
            if os.path.normcase(str(path)) != key:
                return inner
            return self._Drained(inner, lambda: state.update(walked=True))

        return lstat, scandir

    def test_a_directory_that_changes_mid_scan_is_reported(self):
        lstat, scandir = self._move_after_walking(self.watched)
        with patch.object(os, 'lstat', side_effect=lstat), \
                patch.object(os, 'scandir', side_effect=scandir):
            result = scan.Scanner([str(self.root)], progress=False).run()
        change = result['concurrent_change']
        self.assertEqual(change['directories_changed'], 1)
        self.assertEqual(len(change['samples']), 1)
        self.assertIn('busy', change['samples'][0]['path'])
        self.assertIn('Directories that changed while being scanned: 1', scan.render(result))
        self.assertTrue(any('changed while being scanned' in note for note in result['notes']))

    def test_a_quiet_tree_reports_no_change(self):
        result = scan.Scanner([str(self.root)], progress=False).run()
        self.assertEqual(result['concurrent_change']['directories_changed'], 0)
        self.assertNotIn('Directories that changed', scan.render(result))


if __name__ == '__main__':
    unittest.main()
