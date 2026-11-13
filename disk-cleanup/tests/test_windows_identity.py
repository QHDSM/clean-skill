import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import windows_identity


@unittest.skipUnless(os.name == 'nt', 'Windows file identity test')
class WindowsIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_rename_and_replacement(self):
        original = self.root / 'original'
        moved = self.root / 'moved'
        original.write_bytes(b'first')
        first = windows_identity.capture(original)
        original.rename(moved)
        moved_id = windows_identity.capture(moved)
        self.assertEqual((first['volume_serial'], first['file_id']),
                         (moved_id['volume_serial'], moved_id['file_id']))
        original.write_bytes(b'second')
        replacement = windows_identity.capture(original)
        self.assertNotEqual(first['file_id'], replacement['file_id'])

    def test_hardlinks_share_file_id(self):
        file = self.root / 'file'
        link = self.root / 'link'
        file.write_bytes(b'data')
        try:
            os.link(file, link)
        except OSError:
            self.skipTest('hardlinks unavailable')
        first, second = windows_identity.capture(file), windows_identity.capture(link)
        self.assertEqual((first['volume_serial'], first['file_id']),
                         (second['volume_serial'], second['file_id']))

    def test_directory_and_junction(self):
        directory = self.root / 'directory'
        directory.mkdir()
        self.assertEqual(len(windows_identity.capture(directory)['file_id']), 32)
        junction = self.root / 'junction'
        proc = subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(directory)], capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        try:
            with self.assertRaises(ValueError):
                windows_identity.capture(junction)
        finally:
            junction.rmdir()


if __name__ == '__main__':
    unittest.main()
