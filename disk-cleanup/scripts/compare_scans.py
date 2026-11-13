#!/usr/bin/env python3
"""Compare two saved scanner reports without scanning or modifying the filesystem."""
import argparse
import json
import math
import os
from pathlib import Path
import sys


def _path(value):
    if not isinstance(value, str) or not os.path.isabs(value) or '\x00' in value:
        raise ValueError('report paths must be absolute')
    return os.path.normcase(os.path.normpath(value))


def _integer(value):
    if type(value) is not int or value < 0:
        raise ValueError('report counts and sizes must be nonnegative integers')
    return value


def _validate(report):
    if not isinstance(report, dict) or type(report.get('schema_version')) is not int or report['schema_version'] != 1:
        raise ValueError('expected scanner schema_version 1')
    for key in ('roots', 'exclusions'):
        if not isinstance(report.get(key), list) or (key == 'roots' and not report[key]):
            raise ValueError('missing report ' + key)
        if len({_path(p) for p in report[key]}) != len(report[key]):
            raise ValueError('duplicate report ' + key)
    if not isinstance(report.get('scope'), dict) or 'depth' not in report['scope']:
        raise ValueError('missing scan scope')
    depth = report['scope'].get('depth')
    if depth is not None:
        _integer(depth)
    for key in ('logical_bytes', 'files', 'directories', 'error_count', 'skipped_count'):
        _integer(report.get(key))
    for key in ('complete', 'coverage_limited', 'interrupted'):
        if type(report.get(key)) is not bool:
            raise ValueError('missing or invalid coverage flag: ' + key)
    stamp = report.get('started_at_unix')
    if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp < 0:
        raise ValueError('invalid scan time')
    for key in ('largest_files', 'largest_directories'):
        if not isinstance(report.get(key), list):
            raise ValueError('missing top list: ' + key)
        seen = set()
        for row in report[key]:
            if not isinstance(row, dict):
                raise ValueError('invalid top row')
            path = _path(row.get('path'))
            if path in seen:
                raise ValueError('duplicate top path')
            seen.add(path)
            _integer(row.get('logical_bytes'))


def compare(baseline, current):
    for report in (baseline, current):
        _validate(report)
    reasons = []
    for key in ('roots', 'exclusions'):
        if {_path(p) for p in baseline[key]} != {_path(p) for p in current[key]}:
            reasons.append(key + ' differ')
    if baseline['scope'].get('depth') != current['scope'].get('depth'):
        reasons.append('scan depths differ')
    for name, report in (('baseline', baseline), ('current', current)):
        if (not report['complete'] or report['coverage_limited'] or report['interrupted']
                or report['error_count'] or report['skipped_count']):
            reasons.append(name + ' coverage is incomplete')
    if current['started_at_unix'] < baseline['started_at_unix']:
        reasons.append('current scan predates baseline')
    comparable = not reasons
    lists = {}
    for key in ('largest_files', 'largest_directories'):
        before = {_path(row['path']): row for row in baseline[key]}
        after = {_path(row['path']): row for row in current[key]}
        rows = []
        for path in sorted(before.keys() | after.keys()):
            old, new = before.get(path), after.get(path)
            rows.append({'path': (new or old)['path'],
                'baseline_logical_bytes': old['logical_bytes'] if old else None,
                'current_logical_bytes': new['logical_bytes'] if new else None,
                'observed_size_delta': new['logical_bytes'] - old['logical_bytes'] if old and new and comparable else None,
                'observation': 'both_top_lists' if old and new else 'only_in_current_top' if new else 'only_in_baseline_top'})
        lists[key] = sorted(rows, key=lambda row: (row['observed_size_delta'] is None,
                            -(row['observed_size_delta'] or 0), row['path']))
    return {'schema': 'disk-cleanup-comparison/v1', 'comparable': comparable, 'reasons': reasons,
        'baseline_started_at_unix': baseline['started_at_unix'], 'current_started_at_unix': current['started_at_unix'],
        'logical_bytes_delta': current['logical_bytes'] - baseline['logical_bytes'] if comparable else None,
        'files_delta': current['files'] - baseline['files'] if comparable else None,
        **lists, 'notes': ['Missing from a top list does not prove creation, deletion or zero size.',
        'Directory rows overlap; do not sum their deltas.',
        'Observations are not atomic snapshots or proof of reclaimed space; same paths may refer to different objects.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('baseline', type=Path)
    parser.add_argument('current', type=Path)
    args = parser.parse_args()
    try:
        result = compare(json.loads(args.baseline.read_text(encoding='utf-8')),
                         json.loads(args.current.read_text(encoding='utf-8')))
    except (OSError, ValueError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=True), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
