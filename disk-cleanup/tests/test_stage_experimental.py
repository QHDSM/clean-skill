import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import stage_experimental
import windows_identity


@unittest.skipUnless(os.name == 'nt', 'Windows handle rename test')
class StageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='disk-cleanup-stage-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_rename_preserves_identity_and_content(self):
        source = self.root / '源文件.txt'
        source.write_bytes(b'owned fixture')
        expected = windows_identity.capture(source)
        destination = stage_experimental.stage_fixture(source, expected, '暂存.txt')
        self.assertFalse(source.exists())
        self.assertEqual(destination.read_bytes(), b'owned fixture')
        actual = windows_identity.capture(destination)
        self.assertEqual((actual['volume_serial'], actual['file_id']),
                         (expected['volume_serial'], expected['file_id']))

    def test_existing_destination_is_not_overwritten(self):
        source, destination = self.root / 'source', self.root / 'destination'
        source.write_bytes(b'source')
        destination.write_bytes(b'preserve')
        with self.assertRaises(OSError):
            stage_experimental.stage_fixture(source, windows_identity.capture(source), destination.name)
        self.assertEqual(source.read_bytes(), b'source')
        self.assertEqual(destination.read_bytes(), b'preserve')

    def test_replaced_path_is_rejected(self):
        source = self.root / 'source'
        source.write_bytes(b'original')
        expected = windows_identity.capture(source)
        source.rename(self.root / 'old')
        source.write_bytes(b'replacement')
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            stage_experimental.stage_fixture(source, expected, 'staged')
        self.assertEqual(source.read_bytes(), b'replacement')
        self.assertEqual((self.root / 'old').read_bytes(), b'original')

    def test_directory_rename_and_invalid_name(self):
        source = self.root / 'directory'
        source.mkdir()
        (source / 'child').write_bytes(b'data')
        expected = windows_identity.capture(source)
        with self.assertRaises(ValueError):
            stage_experimental.stage_fixture(source, expected, '../other')
        staged = stage_experimental.stage_fixture(source, expected, 'staged')
        self.assertEqual((staged / 'child').read_bytes(), b'data')


if __name__ == '__main__':
    unittest.main()
