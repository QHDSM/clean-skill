import gzip
import importlib.util
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'usn.py'
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location('usn', SCRIPT)
usn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(usn)


def record(frn=100, parent=5, name='Temp', reason=0x00000100, version=2, length=None):
    encoded = name.encode('utf-16-le')
    head = usn.RECORD.pack(
        (length or 60 + len(encoded)), version, 0, frn, parent, 12345, 0,
        reason, 0, 0, 0x10, len(encoded), 60)
    return head + encoded


class ParseTests(unittest.TestCase):
    def test_single_record(self):
        parsed = usn.parse_records(record())
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]['frn'], 100)
        self.assertEqual(parsed[0]['parent'], 5)
        self.assertEqual(parsed[0]['name'], 'Temp')
        self.assertEqual(parsed[0]['reason'], 0x00000100)

    def test_two_records_in_sequence(self):
        buffer = record(frn=1, name='a') + record(frn=2, name='b')
        self.assertEqual([r['name'] for r in usn.parse_records(buffer)], ['a', 'b'])

    def test_version_three_is_refused_not_misparsed(self):
        with self.assertRaises(usn.Unsupported):
            usn.parse_records(record(version=3))

    def test_truncated_tail_is_ignored(self):
        buffer = record()[:-4]
        self.assertEqual(usn.parse_records(buffer), [])

    def test_enum_output_skips_the_leading_start_frn(self):
        buffer = struct.pack('<Q', 999) + record()
        start, records = usn.parse_enum_output(buffer)
        self.assertEqual(start, 999)
        self.assertEqual(len(records), 1)

    def test_read_output_skips_the_leading_next_usn(self):
        buffer = struct.pack('<q', 424242) + record()
        cursor, records = usn.parse_read_output(buffer)
        self.assertEqual(cursor, 424242)
        self.assertEqual(len(records), 1)

    def test_journal_data(self):
        body = struct.pack('<QqqqqQQ', 7, 1, 2, 3, 4, 1 << 20, 1 << 16)
        data = usn.parse_journal_data(body)
        self.assertEqual(data['journal_id'], 7)
        self.assertEqual(data['next_usn'], 2)
        self.assertEqual(data['maximum_size'], 1 << 20)

    def test_short_journal_data_is_refused(self):
        with self.assertRaises(usn.Unsupported):
            usn.parse_journal_data(b'\x00' * 10)


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.entries = {
            5: (5, ''),            # root
            100: (5, 'Windows'),
            101: (100, 'Temp'),
            102: (101, 'a.log'),
        }

    def test_nested_path(self):
        self.assertEqual(usn.resolve(102, self.entries), 'Windows\\Temp\\a.log')

    def test_root_is_empty(self):
        self.assertEqual(usn.resolve(5, self.entries), '')

    def test_missing_parent_returns_none_instead_of_guessing(self):
        self.entries[103] = (777, 'orphan.bin')
        self.assertIsNone(usn.resolve(103, self.entries))

    def test_cycle_is_bounded(self):
        self.entries[200] = (201, 'a')
        self.entries[201] = (200, 'b')
        self.assertIsNone(usn.resolve(200, self.entries))

    def test_cache_is_shared(self):
        cache = {}
        first = usn.resolve(102, self.entries, cache)
        second = usn.resolve(102, self.entries, cache)
        self.assertEqual(first, second)
        self.assertIn(101, cache)


class ClassifyTests(unittest.TestCase):
    def check(self, reason, expected):
        self.assertEqual(usn.classify({'reason': reason}), expected)

    def test_delete_wins_over_everything(self):
        self.check(0x00000200 | 0x00000100, 'deleted')

    def test_create(self):
        self.check(0x00000100, 'created')

    def test_rename_pair(self):
        self.check(0x00001000, 'renamed-away')
        self.check(0x00002000, 'renamed')

    def test_data_change(self):
        self.check(0x00000001, 'modified')

    def test_close_only_is_not_a_content_change(self):
        self.check(0x80000000, 'touched')


class StateTests(unittest.TestCase):
    def test_roundtrip_keeps_awkward_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'state.json.gz')
            entries = {5: (5, ''), 100: (5, 'tab\there'),
                       101: (100, 'back\\slash'), 102: (101, 'line\nbreak')}
            header = usn.save_state(path, {'volume': 'C:', 'journal_id': 9,
                                           'next_usn': 1234}, entries)
            self.assertEqual(header['entries'], 4)
            loaded, restored = usn.load_state(path)
            self.assertEqual(loaded['journal_id'], 9)
            self.assertEqual(restored, entries)

    def test_state_is_gzip_and_has_a_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'state.json.gz')
            usn.save_state(path, {'volume': 'C:', 'journal_id': 1, 'next_usn': 1},
                           {5: (5, '')})
            with gzip.open(path, 'rt', encoding='utf-8') as fh:
                header = json.loads(fh.readline())
            self.assertEqual(header['schema'], usn.SCHEMA_STATE)

    def test_wrong_schema_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'state.json.gz')
            with gzip.open(path, 'wt', encoding='utf-8') as fh:
                fh.write(json.dumps({'schema': 'nope'}) + '\n')
            with self.assertRaises(ValueError):
                usn.load_state(path)

    def test_parent_directories_are_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'a', 'b', 'state.json.gz')
            usn.save_state(path, {'volume': 'C:', 'journal_id': 1, 'next_usn': 1},
                           {5: (5, '')})
            self.assertTrue(os.path.exists(path))


@unittest.skipUnless(os.name == 'nt' and not usn.elevated(),
                     'only meaningful without elevation')
class UnelevatedTests(unittest.TestCase):
    def test_opening_the_volume_refuses_and_names_the_reason(self):
        with self.assertRaises(usn.Unsupported) as caught:
            usn.Volume('C:')
        self.assertIn('elevation', str(caught.exception))

    def test_since_without_a_baseline_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv = ['since', '--drive', 'C:', '--state', os.path.join(tmp, 'absent.gz')]
            self.assertEqual(usn.main(argv), 3)


@unittest.skipUnless(os.name == 'nt' and usn.elevated(), 'needs an elevated Windows shell')
class LiveTests(unittest.TestCase):
    def test_journal_reports_a_usable_checkpoint(self):
        with usn.Volume('C:') as volume:
            data = volume.journal()
        self.assertGreater(data['journal_id'], 0)
        self.assertGreater(data['next_usn'], 0)

    def test_a_small_enumeration_returns_entries(self):
        with usn.Volume('C:') as volume:
            entries = volume.enumerate(max_entries=50000)
        self.assertGreater(len(entries), 0)
        self.assertIn(usn.ROOT_FRN, entries)


if __name__ == '__main__':
    unittest.main()
