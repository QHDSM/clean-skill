#!/usr/bin/env python3
"""Capture bounded directory metadata for later comparison; never deletes files."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import time

import scan

SCHEMA = 'disk-cleanup-tree/v1'
MAX_ENTRIES = 100000
MAX_SECONDS = 30


def capture(root, max_entries=MAX_ENTRIES, max_seconds=MAX_SECONDS):
    """Hash relative names and metadata, not file contents; incomplete means unusable.

    A traversal is not an atomic snapshot and does not prevent later changes. It
    cannot detect content changes that preserve the metadata fields used here.
    """
    issues, rows = [], []
    start = time.monotonic()
    root = Path(os.path.abspath(root))
    try:
        scan.check_path(root)
        root_stat = os.lstat(root)
        if not stat.S_ISDIR(root_stat.st_mode):
            raise ValueError('target must be a directory')
    except (OSError, ValueError) as exc:
        return {'schema': SCHEMA, 'root': str(root), 'complete': False,
                'entries': 0, 'digest': None, 'issues': [str(exc)]}

    def row(path, info):
        kind = 'directory' if stat.S_ISDIR(info.st_mode) else 'file'
        return [os.path.relpath(path, root).replace(os.sep, '/'), kind,
                info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]

    rows.append(row(root, root_stat))
    pending = [root]
    while pending and not issues:
        if len(rows) >= max_entries or time.monotonic() - start >= max_seconds:
            issues.append('entry or time limit reached')
            break
        current = pending.pop()
        try:
            scan.check_path(current)
            with os.scandir(current) as entries:
                for entry in entries:
                    if len(rows) >= max_entries or time.monotonic() - start >= max_seconds:
                        issues.append('entry or time limit reached')
                        break
                    info = os.lstat(entry.path)
                    if scan.unsafe(info) or info.st_dev != root_stat.st_dev:
                        issues.append('link, reparse, offline or different filesystem: ' + entry.path)
                        break
                    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                        issues.append('unsupported object: ' + entry.path)
                        break
                    rows.append(row(entry.path, info))
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(Path(entry.path))
        except (OSError, ValueError) as exc:
            issues.append(f'cannot inspect {current}: {exc}')

    digest = None
    if not issues:
        payload = json.dumps(sorted(rows), ensure_ascii=True, separators=(',', ':')).encode('utf-8')
        digest = hashlib.sha256(payload).hexdigest()
    return {'schema': SCHEMA, 'root': str(root), 'complete': not issues,
            'entries': len(rows), 'digest': digest, 'issues': issues}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', help='directory to inspect')
    args = parser.parse_args()
    result = capture(args.path)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result['complete'] else 2


if __name__ == '__main__':
    sys.exit(main())
