#!/usr/bin/env python3
"""Read-only audit of the Windows Recycle Bin, plus a cross-check of one run log.

The audit answers three questions a restore needs: what is still in the bin,
what can no longer be restored (a `$I` record whose payload is gone, a payload
with no `$I`, a broken record, a `$I`-named directory), and which entries can no
longer go back because their original path is occupied again.  With `--journal`
it also answers whether the objects a past `--go` run moved are still there, so
"the bin was emptied behind our back" is a reported fact instead of a surprise
during `--undo`.

Nothing here writes, prunes or restores. A Recycle Bin is an unauthenticated
on-disk structure: these counts are evidence to re-check, not permission.
"""
import argparse
import json
import os
from pathlib import Path
import sys

import recycle_bin

SCHEMA = 'disk-cleanup-recycle-audit-report/v1'


def drives():
    """Every mounted drive letter, in alphabetical order."""
    found = []
    for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
        try:
            if os.path.exists(f'{letter}:\\'):
                found.append(letter)
        except OSError:
            continue
    return found


def journal_reference(path):
    """Compare the trashed records of one audit journal with the current bin."""
    result = {'log': str(path), 'records': 0, 'present': 0, 'missing': [],
              'issues': []}
    try:
        lines = Path(path).read_text(encoding='utf-8').splitlines()
    except OSError as exc:
        result['issues'].append(f'cannot read the journal: {exc}')
        return result

    trashed = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            result['issues'].append(f'line {number} is not JSON; ignored')
            continue
        if isinstance(row, dict) and row.get('result') == 'trashed':
            trashed.append(row)

    audits = {}
    for row in trashed:
        path_text = row.get('path')
        drive = os.path.splitdrive(os.path.abspath(path_text))[0].upper() if path_text else ''
        if drive not in audits:
            try:
                audits[drive] = recycle_bin.audit_volume(drive) if drive else None
            except (OSError, ValueError):
                audits[drive] = None
        audit = audits[drive]
        known = {entry['i_path']: entry for entry in (audit['entries'] if audit else [])}
        i_path = (row.get('recycle_bin') or {}).get('i_path')
        # Match on $I identity, not on the original path: the same path may have
        # been recycled twice, and only the record tells the two apart.
        if i_path and i_path in known:
            result['present'] += 1
        else:
            result['missing'].append(path_text or i_path or '(no path recorded)')
    result['records'] = len(trashed)
    return result


def run(drive_letters=None, journals=(), check_conflicts=True):
    letters = [letter.upper().rstrip(':\\') for letter in drive_letters] if drive_letters \
        else drives()
    reports = []
    for letter in letters:
        audit = recycle_bin.audit_volume(letter, check_conflicts=check_conflicts)
        reports.append(audit)
    links = [journal_reference(path) for path in journals]
    complete = all(report['complete'] for report in reports) and \
        all(not link['missing'] for link in links)
    return {
        'schema': SCHEMA,
        'reports': reports,
        'journals': links,
        'complete': complete,
    }


def _size(count):
    value = float(count)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if value < 1024 or unit == 'TiB':
            return ('%d %s' % (value, unit)) if unit == 'B' else ('%.2f %s' % (value, unit))
        value /= 1024
    return '%d B' % count


def _when(stamp):
    if not isinstance(stamp, (int, float)):
        return 'unknown'
    import datetime
    return datetime.datetime.fromtimestamp(stamp).strftime('%Y-%m-%d %H:%M')


def render(report):
    lines = ['=== RECYCLE BIN AUDIT ===']
    for audit in report['reports']:
        totals = audit['totals']
        lines.append('%s  %s' % (audit['root'] or '(no root)',
                                 'complete' if audit['complete'] else 'INCOMPLETE'))
        if totals['entries']:
            lines.append('  entries %d | %s | oldest %s | newest %s'
                         % (totals['entries'], _size(totals['bytes']),
                            _when(totals['oldest_deleted_unix']),
                            _when(totals['newest_deleted_unix'])))
        else:
            lines.append('  entries 0')
        if audit['orphan_counts']:
            lines.append('  orphans %d: %s' % (
                totals['orphans'],
                ', '.join('%s %d' % item for item in sorted(audit['orphan_counts'].items()))))
            lines.append('             these cannot be restored; their records or payloads '
                         'are incomplete')
        if audit['conflicts']:
            lines.append('  conflicts %d: the original path exists again, so restore would '
                         'refuse to overwrite' % len(audit['conflicts']))
            for item in audit['conflicts'][:5]:
                lines.append('             %s' % item['original_path'])
        for issue in audit['issues']:
            lines.append('  issue: %s' % issue)
    for link in report['journals']:
        lines.append('journal %s' % link['log'])
        lines.append('  trashed %d | present %d | missing %d'
                     % (link['records'], link['present'], len(link['missing'])))
        for path in link['missing'][:5]:
            lines.append('             no longer in the bin: %s (restored, or removed by '
                         'someone else)' % path)
        for issue in link['issues']:
            lines.append('  issue: %s' % issue)
    lines.append('result: %s' % ('OK' if report['complete'] else 'ATTENTION'))
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Read-only audit of the Recycle Bin; never restores or prunes.')
    parser.add_argument('--drive', action='append', default=None,
                        help='drive letter to audit; repeatable (default: every mounted drive)')
    parser.add_argument('--journal', action='append', default=[],
                        help='audit-*.jsonl from a --go run to cross-check against the bin')
    parser.add_argument('--no-conflicts', action='store_true',
                        help='skip the original-path conflict check (fewer filesystem calls)')
    parser.add_argument('--json', action='store_true', help='machine-readable report')
    args = parser.parse_args(argv)

    try:
        report = run(args.drive, args.journal, check_conflicts=not args.no_conflicts)
    except (OSError, ValueError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render(report))
    return 0 if report['complete'] else 1


if __name__ == '__main__':
    sys.exit(main())
