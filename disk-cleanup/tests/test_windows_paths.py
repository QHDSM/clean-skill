import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import clean
import inspect_target
import windows_paths


class WindowsPathTests(unittest.TestCase):
    def test_ordinary_unicode_and_directory_paths_are_supported(self):
        for value in ('C:\\', 'D:/Projects/中文 cache', 'D:\\build\\.cache\\'):
            self.assertTrue(windows_paths.supported(value), value)

    def test_ambiguous_and_special_paths_are_rejected(self):
        for value in ('C:\\Windows.', 'C:\\Windows \\Temp', 'C:\\file:stream',
                      'C:\\NUL.txt', 'C:\\COM¹', 'C:\\LPT9.log', 'C:\\folder\\..\\file',
                      'C:\\folder\\.\\file', 'C:\\file*', 'C:\\folder\\\\child',
                      'C:\\bad\x00name', '\\\\?\\C:\\Windows', '\\\\server\\share'):
            with self.subTest(path=value):
                self.assertFalse(windows_paths.supported(value))

    @unittest.skipUnless(os.name == 'nt', 'Windows target path test')
    def test_single_path_and_inspection_do_not_normalize_ambiguous_input(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'owned.txt'
            target.write_bytes(b'preserve')
            alias = str(target) + '.'
            args = clean.argparse.Namespace(plan=None, path=alias, confirm_path=alias, go=False)
            with self.assertRaisesRegex(clean.GateError, 'supported absolute drive path'):
                clean.run(args)
            result = inspect_target.collect(alias)
            self.assertFalse(result['complete'])
            self.assertIsNone(result['evidence'])
            self.assertEqual(target.read_bytes(), b'preserve')
