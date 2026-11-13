#!/usr/bin/env python3
"""Collect read-only Windows target evidence for a plan preview, not authorization."""
import argparse
import json
import os
from pathlib import Path
import stat
import sys
import time

import git_discovery
import scan
import tree_evidence
import windows_identity
import windows_parents
import windows_paths


def collect(path, *, tree=False):
    started = time.time()
    result = {'schema': 'disk-cleanup-inspection/v1', 'path': str(path),
              'started_at_unix': started, 'complete': False, 'evidence': None,
              'errors': [], 'git_discovery': None,
              'notes': ['Collection is not an atomic snapshot, safety verdict or user authorization.',
                        'Parent handles are released before return; paths and contents may change.',
                        'Git markers require a separate audit; absence of markers does not prove absence of Git data.']}
    try:
        if os.name != 'nt' or not Path(path).is_absolute():
            raise ValueError('an absolute Windows path is required')
        windows_paths.require(str(path))
        path = scan.check_path(path)
        result['path'] = str(path)
        with windows_parents.PinnedParents(path.parent) as parents:
            before = os.lstat(path)
            if scan.unsafe(before) or not (stat.S_ISREG(before.st_mode) or stat.S_ISDIR(before.st_mode)):
                raise ValueError('unsupported target type')
            first_id = windows_identity.capture(path)
            evidence = {'fingerprint': list(scan.fingerprint(before)),
                        'windows_file_id': first_id,
                        'windows_parent_chain': {'schema': windows_parents.SCHEMA,
                            'path': str(path), 'parents': [dict(row) for row in parents.identities]},
                        'collected_at_unix': started}
            if stat.S_ISREG(before.st_mode):
                evidence['size_bytes'] = before.st_size
            if tree:
                if not stat.S_ISDIR(before.st_mode):
                    raise ValueError('--tree requires a directory')
                metadata = tree_evidence.capture(path)
                if not metadata['complete']:
                    raise ValueError('tree collection incomplete: ' + '; '.join(metadata['issues']))
                evidence['tree_metadata'] = metadata
            discovery = result['git_discovery'] = git_discovery.discover(path)
            if not discovery['complete']:
                raise ValueError('Git discovery incomplete: ' + '; '.join(discovery['issues']))
            after = os.lstat(path)
            last_id = windows_identity.capture(path)
            if (scan.unsafe(after) or scan.fingerprint(before)[:4] != scan.fingerprint(after)[:4]
                    or (first_id['volume_serial'], first_id['file_id']) !=
                       (last_id['volume_serial'], last_id['file_id'])):
                raise ValueError('target changed during collection')
            result.update(complete=True, evidence=evidence)
    except (OSError, ValueError) as exc:
        result['errors'].append(str(exc))
    result['finished_at_unix'] = time.time()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path')
    parser.add_argument('--tree', action='store_true', help='include bounded directory-tree metadata')
    args = parser.parse_args()
    result = collect(args.path, tree=args.tree)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result['complete'] else 2


if __name__ == '__main__':
    sys.exit(main())
