"""Fixture transaction journal. Inspection never performs recovery actions."""
import json
import argparse
import os
from pathlib import Path
import sys

import windows_identity


class Journal:
    def __init__(self, path):
        self.path = Path(path)
        self.handle = self.path.open('x', encoding='utf-8', newline='\n')
        self.sequence = 0

    def append(self, event, **data):
        self.handle.write(json.dumps({'sequence': self.sequence, 'event': event, **data},
                                     ensure_ascii=True) + '\n')
        self.handle.flush()
        os.fsync(self.handle.fileno())
        self.sequence += 1

    def close(self):
        self.handle.close()


def inspect(path):
    """Report the recorded intent and current identities; no rename/delete calls.

    The journal is not authenticated and observations can race with other activity.
    Truncated or malformed records are treated as incomplete evidence.
    """
    records = []
    try:
        raw = Path(path).read_bytes()
        for index, line in enumerate(raw.splitlines()):
            row = json.loads(line)
            if not isinstance(row, dict) or row.get('sequence') != index:
                raise ValueError('journal sequence mismatch')
            records.append(row)
        if not records or not raw.endswith(b'\n') or records[0].get('event') != 'INTENT':
            raise ValueError('missing or incomplete intent')
        intent = records[0]
        expected = intent['identity']
        observed = {}
        for name in ('source', 'staged'):
            try:
                identity = windows_identity.capture(intent[name])
                observed[name] = {'identity_matches':
                    (identity['volume_serial'], identity['file_id']) ==
                    (expected['volume_serial'], expected['file_id'])}
            except (OSError, ValueError) as exc:
                observed[name] = {'identity_matches': None, 'error': str(exc)}
        return {'readable': True, 'last_event': records[-1]['event'],
                'intent': intent, 'observed': observed, 'recovery_performed': False}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {'readable': False, 'error': str(exc), 'recovery_performed': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Read-only inspection of an experimental fixture journal')
    parser.add_argument('path')
    report = inspect(parser.parse_args().path)
    print(json.dumps(report, ensure_ascii=True, indent=2))
    sys.exit(0 if report['readable'] else 2)
