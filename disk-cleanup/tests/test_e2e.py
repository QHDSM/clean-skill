import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'e2e_check.py'


@unittest.skipUnless(os.name == 'nt' and os.environ.get('DISK_CLEANUP_E2E') == '1',
                     'opt-in: moves real files through the Recycle Bin and back')
class EndToEndTests(unittest.TestCase):
    def test_scan_recycle_undo_round_trip(self):
        drive = os.environ.get('DISK_CLEANUP_E2E_DRIVE', 'C:')
        proc = subprocess.run([sys.executable, str(SCRIPT), '--drive', drive, '--json'],
                              capture_output=True, text=True, timeout=900)
        self.assertEqual(proc.returncode, 0,
                         msg='stdout:\n' + proc.stdout[-3000:] + '\nstderr:\n' + proc.stderr[-2000:])
        payload = json.loads(proc.stdout)
        self.assertTrue(payload['ok'])
        names = [row['check'] for row in payload['checks']]
        for expected in ('every target reports RECYCLED',
                         'every proof points into $RECYCLE.BIN',
                         'both objects restored',
                         'restored bytes match what was written'):
            self.assertIn(expected, names)


if __name__ == '__main__':
    unittest.main()
