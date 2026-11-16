"""Prove that this machine can recycle, end to end, without touching user data.

The skill's guarantee is "deleted objects land in the Recycle Bin and can be put
back". That guarantee depends on the volume, the policy and the Shell, so it is
worth proving on the machine that will actually run a cleanup instead of
assuming it. This module creates a throwaway directory under %TEMP%, recycles a
probe file, reads the proof back from the held handle, restores the file and
compares the bytes.

It never touches a user path: the probe lives in a fresh temporary directory,
and every stage is skipped once an earlier stage failed. The one residue a
failure can leave is the probe itself in the Recycle Bin (named
``disk-cleanup-selfcheck-*``) when the restore stage is what failed; nothing
else is ever moved. ``keep_evidence`` keeps the probe directory so a human can
inspect it.
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import locks  # noqa: E402
import recycle_backend  # noqa: E402
import recycle_bin  # noqa: E402

SCHEMA = 'disk-cleanup-recycle-selfcheck/v1'
PAYLOAD = b'disk-cleanup self-check payload\n' * 64


def _drive_of(path):
    return os.path.splitdrive(os.path.abspath(str(path)))[0].upper()


def _stage(results, name, ok, detail, **extra):
    row = {'stage': name, 'ok': bool(ok), 'detail': detail}
    row.update(extra)
    results.append(row)
    return row


def run(drive=None, keep_evidence=False):
    """Run every stage in order and return a report dict.

    ``ok`` is True only when the probe was recycled *and* restored with
    identical bytes. Any earlier failure short-circuits the later stages, which
    is the point: a report must never claim a restore that was never attempted.
    """
    results = []
    report = {'schema': SCHEMA, 'drive': drive, 'ok': False,
              'stages': results, 'probe_dir': None, 'issues': []}

    backend = recycle_backend.availability()
    _stage(results, 'availability', backend['available'], backend['reason'])
    if not backend['available']:
        return report

    probe_root = Path(tempfile.gettempdir())
    drive = (drive or _drive_of(probe_root) or '').upper()
    report['drive'] = drive
    if not drive:
        _stage(results, 'volume', False, 'no drive to test')
        return report

    profile = recycle_backend.volume_profile(drive)
    _stage(results, 'volume', profile['verified_capable'],
           'filesystem={filesystem} recycle_bin={recycle_bin_root_exists} '
           'max_capacity_mb={max_capacity_mb} nuke_on_delete={nuke_on_delete}'.format(**profile),
           volume={key: profile[key] for key in
                   ('drive', 'filesystem', 'recycle_bin_root', 'max_capacity_mb',
                    'nuke_on_delete', 'verified_capable', 'issues')})
    if not profile['verified_capable']:
        report['issues'].extend(profile['issues'])
        return report

    if drive != _drive_of(probe_root):
        # A probe must live on the volume under test, so keep the directory on
        # that drive instead of %TEMP% when the caller asked for another one.
        base = Path(drive + '\\')
        if not base.is_dir():
            _stage(results, 'probe', False, f'{drive} is not a directory')
            return report
        probe_dir = Path(tempfile.mkdtemp(prefix='disk-cleanup-selfcheck-', dir=str(base)))
    else:
        probe_dir = Path(tempfile.mkdtemp(prefix='disk-cleanup-selfcheck-'))
    report['probe_dir'] = str(probe_dir)

    probe = probe_dir / 'probe.txt'
    restored_ok = False
    try:
        probe.write_bytes(PAYLOAD)
        _stage(results, 'probe', True, f'wrote {len(PAYLOAD)} bytes to {probe}')

        held = locks.probe_deletable(str(probe))
        _stage(results, 'deletable', held['deletable'] is True, held['reason'])
        if held['deletable'] is not True:
            return report

        outcome = recycle_backend.recycle(str(probe), size_bytes=len(PAYLOAD))
        _stage(results, 'recycle', outcome['status'] == 'RECYCLED', outcome['status'],
               proof=outcome.get('proof'), recycle_bin=outcome.get('recycle_bin'))
        if outcome['status'] != 'RECYCLED':
            report['issues'].append(outcome['detail'])
            return report

        entry = outcome.get('recycle_bin') or {}
        if not entry.get('i_exists'):
            _stage(results, 'metadata', False, 'no $I record was found for the probe')
            return report
        parsed = recycle_bin.parse_entry(entry['i_path'])
        # The Shell records the path as it saw it, which is not always the
        # spelling this script has. Compare canonically, or a machine that
        # stores a short name or a \\?\ prefix fails a check that passed.
        matches = recycle_bin.same_path(parsed['original_path'], str(probe))
        _stage(results, 'metadata', matches,
               'original_path={}'.format(parsed['original_path']),
               metadata={key: parsed[key] for key in
                         ('version', 'size_bytes', 'deleted_unix', 'original_path')})
        if not matches:
            # Spell both sides out. Two paths that look identical in a log can
            # differ in a prefix, a short name or a trailing character, and the
            # diff is the only thing that identifies which.
            _stage(results, 'path_diff', False,
                   'recorded={!r} expected={!r} canonical_recorded={!r} '
                   'canonical_expected={!r}'.format(
                       parsed['original_path'], str(probe),
                       recycle_bin.canonical_path(parsed['original_path']),
                       recycle_bin.canonical_path(str(probe))))
            return report

        restored = recycle_bin.restore(parsed)
        _stage(results, 'restore', restored['ok'], restored['detail'], restore=restored)
        if not restored['ok']:
            return report

        data = probe.read_bytes() if probe.exists() else b''
        restored_ok = data == PAYLOAD
        _stage(results, 'content', restored_ok,
               'restored bytes match' if restored_ok else 'restored bytes differ')
    except OSError as exc:
        report['issues'].append(f'{type(exc).__name__}: {exc}')
        _stage(results, 'error', False, str(exc))
    finally:
        report['ok'] = bool(restored_ok)
        # Keep the directory only when the caller asked for evidence *and* the
        # check failed: a passing check leaves nothing behind.
        if not (keep_evidence and not report['ok']):
            shutil.rmtree(probe_dir, ignore_errors=True)
            report['probe_dir'] = None
    return report


def render(report):
    lines = [f"recycle self-check on {report['drive'] or '?'}",
             f"result: {'OK' if report['ok'] else 'FAILED'}"]
    for row in report['stages']:
        mark = 'ok  ' if row['ok'] else 'FAIL'
        lines.append(f"  [{mark}] {row['stage']}: {row['detail']}")
    if report['probe_dir']:
        lines.append('probe retained: ' + report['probe_dir'])
    for issue in report['issues']:
        lines.append('issue: ' + issue)
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Prove that this machine recycles and restores, using a throwaway probe.')
    parser.add_argument('--drive', help='volume to test, e.g. Z: (default: the %TEMP% volume)')
    parser.add_argument('--format', choices=('text', 'json'), default='text')
    parser.add_argument('--keep-evidence', action='store_true',
                        help='keep the probe directory when the check fails')
    args = parser.parse_args(argv)
    report = run(args.drive, keep_evidence=args.keep_evidence)
    if args.format == 'json':
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render(report))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
