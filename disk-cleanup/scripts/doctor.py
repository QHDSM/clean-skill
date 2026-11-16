#!/usr/bin/env python3
"""Read-only health check: can this machine clean safely, and did it?

``clean.py --go`` only keeps its promises where the volume can prove a recycle
and the executor can open the object. This script answers that question before a
real run, in one screen, without changing anything: no plan is executed, no file
is deleted, no audit log is written. The only write is a temporary directory
probe in the environment section, which asks Windows whether DELETE access can be
opened on an ordinary temp path and closes the handle at once.

Exit codes follow the same convention as the other read-only tools: 0 when every
check passes, 1 when something merely deserves attention, 2 when a guarantee the
executor relies on does not hold here. A failing check never stops the report; the
remaining sections still run so one broken probe cannot hide the others.

Usage:
    python doctor.py
    python doctor.py --format json
    python doctor.py --drive D: --drive E:
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import clean            # noqa: E402
import git_audit        # noqa: E402
import locks            # noqa: E402
import recycle_backend  # noqa: E402
import recycle_bin      # noqa: E402
import scan             # noqa: E402
import usn              # noqa: E402
import version          # noqa: E402

SCHEMA = 'disk-cleanup-doctor/v1'

VERDICTS = {0: 'READY', 1: 'READY WITH WARNINGS', 2: 'NOT READY'}
LABELS = {'ok': '[ok  ]', 'warn': '[warn]', 'fail': '[FAIL]'}


def _brief(text, limit=160):
    text = ' '.join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + '\u2026'


def _check(section, name, status, detail, **data):
    row = {'section': section, 'name': name, 'status': status, 'detail': detail}
    if data:
        row['data'] = data
    return row


def _guarded(section, name, func):
    """Run one probe, turning an unexpected exception into a failing check.

    A doctor that dies on the first broken probe is useless exactly when it is
    needed, so every check is isolated and the failure keeps its message.
    """
    try:
        return func()
    except Exception as exc:  # noqa: BLE001 - the message is the point
        return [_check(section, name, 'fail',
                       '%s: %s' % (type(exc).__name__, exc))]


def _environment():
    rows = []
    raw = sys.version.split()[0]
    ok = sys.version_info >= (3, 10)
    rows.append(_check('environment', 'python', 'ok' if ok else 'warn',
                       'Python %s' % raw, minimum='3.10'))
    rows.append(_check('environment', 'platform',
                       'ok' if sys.platform == 'win32' else 'fail',
                       sys.platform,
                       note='this tool reads NTFS recycle metadata; other '
                            'platforms are unsupported'))

    # The probe writes one empty file into the temp directory and removes it
    # again: asking DELETE about a directory that other processes hold open would
    # report a sharing violation that says nothing about the executor.
    probe = {'deletable': None, 'reason': 'the temp directory is not writable'}
    try:
        handle, name = tempfile.mkstemp(prefix='disk-cleanup-doctor-')
        os.close(handle)
    except OSError as exc:
        rows.append(_check('environment', 'temp-deletable', 'warn',
                           'could not create a probe file: %s' % exc))
    else:
        try:
            probe = locks.probe_deletable(name)
        finally:
            try:
                os.unlink(name)
            except OSError:
                pass
        if probe.get('deletable') is None:
            rows.append(_check('environment', 'temp-deletable', 'warn',
                               _brief(probe.get('reason') or 'could not be probed')))
        elif probe.get('deletable'):
            rows.append(_check('environment', 'temp-deletable', 'ok',
                               'DELETE access opens on a fresh temp file'))
        else:
            rows.append(_check('environment', 'temp-deletable', 'fail',
                               _brief(probe.get('reason') or 'DELETE access was refused'),
                               winerror=probe.get('winerror')))
    return rows


def _backend():
    avail = recycle_backend.availability()
    if avail.get('available'):
        return [_check('backend', 'recycle-bin', 'ok',
                       'the shell Recycle Bin is available')]
    return [_check('backend', 'recycle-bin', 'fail',
                   str(avail.get('reason') or 'the Recycle Bin is unavailable'),
                   note='without a working Recycle Bin nothing can be recycled')]


def _volume_rows(drive):
    rows = []
    profile = recycle_backend.volume_profile(drive)
    issues = list(profile.get('issues') or [])
    facts = {'drive': profile.get('drive'),
             'filesystem': profile.get('filesystem'),
             'verified_capable': bool(profile.get('verified_capable'))}
    if not profile.get('supported_filesystem'):
        status, detail = 'fail', 'filesystem %s cannot hold a Recycle Bin' % (
            profile.get('filesystem') or 'unknown')
    elif profile.get('read_only_volume'):
        status, detail = 'warn', 'the volume is read-only; nothing can be cleaned'
    elif profile.get('verified_capable'):
        status, detail = 'ok', '%s on %s can prove a recycle' % (
            profile.get('drive'), profile.get('filesystem'))
    else:
        status, detail = 'fail', '; '.join(issues) or 'recycle capability is unproven'
    rows.append(_check('volumes', 'capability', status, detail, **facts))

    usage = recycle_bin.bin_usage(drive)
    issues = [str(issue) for issue in (usage.get('issues') or [])]
    # Other accounts' bin directories are unreadable without elevation and that is
    # normal; only unreadable leftovers inside the current user's bin mean the
    # accounting below is incomplete, so the two are reported differently.
    hidden = [issue for issue in issues if issue.lower().startswith('permission denied')]
    leftover = [issue for issue in issues if issue not in hidden]
    facts = {'volume': profile.get('drive'), 'entries': usage.get('entries', 0),
             'bytes': usage.get('bytes', 0), 'complete': bool(usage.get('complete'))}
    counted = '%d entries, %d bytes' % (facts['entries'], facts['bytes'])
    if leftover:
        rows.append(_check('volumes', 'bin-usage', 'warn',
                           '%s; %d unreadable leftover(s), first: %s' % (
                               counted, len(leftover), _brief(leftover[0])),
                           **facts))
    elif hidden:
        rows.append(_check('volumes', 'bin-usage', 'ok',
                           '%s; %d other account(s) hidden, normal without elevation' % (
                               counted, len(hidden)),
                           **facts))
    else:
        rows.append(_check('volumes', 'bin-usage', 'ok', counted, **facts))
    return rows


def _volumes(drives):
    rows = []
    if not drives:
        try:
            drives = [entry.get('path') for entry in scan.drives()]
        except Exception as exc:  # noqa: BLE001
            return [_check('volumes', 'discover', 'fail',
                           '%s: %s' % (type(exc).__name__, exc))]
    if not drives:
        return [_check('volumes', 'discover', 'fail', 'no fixed drive was found')]
    for drive in drives:
        rows.extend(_volume_rows(str(drive)))
    return rows


def _usn():
    if usn.elevated():
        return [_check('usn', 'elevated', 'ok', 'the shell is elevated',
                       note='USN history checkpoints can be read')]
    return [_check('usn', 'elevated', 'warn',
                   'the shell is not elevated',
                   note='scan, plan, clean and undo work without elevation; '
                        'only the USN change history needs it')]


def _history():
    run_dir = Path(clean.RUN_DIR)
    try:
        logs = sorted(run_dir.glob('audit-*.jsonl'))
    except OSError as exc:
        return [_check('history', 'audit-logs', 'warn',
                       'could not list %s: %s' % (run_dir, exc))]
    if not logs:
        return [_check('history', 'audit-logs', 'ok',
                       'no run has been recorded yet')]
    rows = [_check('history', 'audit-logs', 'ok',
                   '%d run log(s) in %s' % (len(logs), run_dir))]
    records, issues = clean._read_audit_log(logs[-1])
    if issues:
        rows.append(_check('history', 'last-log', 'warn',
                           '%d row(s) of %s do not parse: %s' % (
                               len(issues), logs[-1].name, '; '.join(issues[:3]))))
    else:
        rows.append(_check('history', 'last-log', 'ok',
                           '%s holds %d row(s)' % (logs[-1].name, len(records))))
    return rows


def _git():
    exe = shutil.which('git')
    if not exe:
        return [_check('git', 'command', 'warn', 'git is not on PATH',
                       note='Git risk auditing is skipped, the rest still works')]
    code, out, err = git_audit.run_git(str(HERE), ['--version'])
    if code != 0:
        return [_check('git', 'command', 'warn',
                       (err or out or 'git --version failed').strip())]
    return [_check('git', 'command', 'ok', out.strip())]


def run(drives=None):
    checks = []
    checks.extend(_guarded('environment', 'python', _environment))
    checks.extend(_guarded('backend', 'recycle-bin', _backend))
    checks.extend(_guarded('volumes', 'capability', lambda: _volumes(drives)))
    checks.extend(_guarded('usn', 'elevated', _usn))
    checks.extend(_guarded('history', 'audit-logs', _history))
    checks.extend(_guarded('git', 'command', _git))

    counts = {'ok': 0, 'warn': 0, 'fail': 0}
    for row in checks:
        counts[row['status']] += 1
    if counts['fail']:
        exit_code = 2
    elif counts['warn']:
        exit_code = 1
    else:
        exit_code = 0
    return {
        'schema': SCHEMA,
        'tool_version': version.TOOL_VERSION,
        'verdict': VERDICTS[exit_code],
        'exit_code': exit_code,
        'counts': counts,
        'checks': checks,
        'notes': [row['detail'] for row in checks if row['status'] == 'fail'],
    }


def render(report):
    head = 'disk-cleanup %s doctor: %s' % (report['tool_version'], report['verdict'])
    lines = [head, '=' * len(head)]
    order = []
    for row in report['checks']:
        if row['section'] not in order:
            order.append(row['section'])
    for section in order:
        lines.append('')
        lines.append(section)
        for row in report['checks']:
            if row['section'] != section:
                continue
            lines.append('  %s %-18s %s' % (LABELS[row['status']], row['name'],
                                            row['detail']))
    counts = report['counts']
    lines.append('')
    lines.append('%d ok, %d warn, %d fail' % (counts['ok'], counts['warn'],
                                              counts['fail']))
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Read-only health check for disk-cleanup.',
        epilog='Exit codes: 0 every check passed, 1 something deserves '
               'attention, 2 a guarantee the executor relies on does not hold.')
    parser.add_argument('--format', choices=('text', 'json'), default='text',
                        help='output format (default: text)')
    parser.add_argument('--drive', action='append', metavar='DRIVE',
                        help='volume to inspect, e.g. C: (default: every fixed drive)')
    parser.add_argument('--version', action='version',
                        version='disk-cleanup %s' % version.TOOL_VERSION)
    args = parser.parse_args(argv)

    report = run(args.drive)
    if args.format == 'json':
        print(json.dumps(report, indent=2))
    else:
        print(render(report))
    return report['exit_code']


if __name__ == '__main__':
    try:
        code = main()
    except BrokenPipeError:
        code = 0
    sys.exit(code)
