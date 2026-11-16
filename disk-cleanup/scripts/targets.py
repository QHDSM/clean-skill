#!/usr/bin/env python3
"""Expand the declarative candidate registry into a read-only report.

The registry says what is *worth considering*; it never says what to delete.
Every candidate still has to become an explicit entry in plan.json, carry
evidence captured at scan time, and pass clean.py's gate. Nothing here touches
a file.

Structure follows BleachBit's CleanerML split (cleaner -> option -> action,
plus running/var), narrowed to what a recycle-bin-only executor can honour:
`action` is recorded for the agent's benefit, not executed.
"""
import argparse
import ctypes
import ctypes.wintypes as wintypes
import glob
import json
import os
from pathlib import Path
import re
import stat
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scan

SCHEMA = 'disk-cleanup-targets/v1'
REPORT_SCHEMA = 'disk-cleanup-targets-report/v1'
CATEGORIES = {'system', 'browser', 'development', 'application', 'protection'}
STATUSES = {'release', 'pending'}
RISKS = {'safe', 'caution', 'never'}
ACTIONS = {'delete', 'truncate', 'vacuum'}
ID = re.compile(r'^[a-z0-9][a-z0-9-]{1,40}$')
VAR = re.compile(r'%([A-Za-z_][A-Za-z0-9_]*)%')
DRIVE_ROOT = re.compile(r'^[a-z]:\\?$')

DEFAULT_REGISTRY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, 'references', 'targets.json')


def load(path=DEFAULT_REGISTRY):
    with open(path, encoding='utf-8') as fh:
        registry = json.load(fh)
    problems = validate(registry)
    if problems:
        raise ValueError('\n'.join(problems))
    return registry


def validate(registry):
    """Return a list of problems; empty means the registry is usable."""
    problems = []
    if not isinstance(registry, dict):
        return ['registry must be an object']
    if registry.get('schema') != SCHEMA:
        problems.append(f'schema must be {SCHEMA}')
    cleaners = registry.get('cleaners')
    if not isinstance(cleaners, list) or not cleaners:
        return problems + ['cleaners must be a non-empty array']

    seen = set()
    for index, item in enumerate(cleaners):
        where = f'cleaners[{index}]'
        if not isinstance(item, dict):
            problems.append(f'{where} must be an object')
            continue
        key = item.get('id')
        if not isinstance(key, str) or not ID.match(key):
            problems.append(f'{where}.id must match {ID.pattern}: {key!r}')
        elif key in seen:
            problems.append(f'{where}.id is duplicated: {key}')
        else:
            seen.add(key)
            where = f'cleaner {key}'

        for field in ('label', 'description'):
            if not isinstance(item.get(field), str) or not item[field].strip():
                problems.append(f'{where}.{field} must be a non-empty string')
        if item.get('category') not in CATEGORIES:
            problems.append(f'{where}.category must be one of {sorted(CATEGORIES)}')
        if item.get('status') not in STATUSES:
            problems.append(f'{where}.status must be one of {sorted(STATUSES)}')
        if item.get('risk') not in RISKS:
            problems.append(f'{where}.risk must be one of {sorted(RISKS)}')
        if item.get('action') not in ACTIONS:
            problems.append(f'{where}.action must be one of {sorted(ACTIONS)}')
        if item.get('status') == 'pending' and not str(item.get('note') or '').strip():
            problems.append(f'{where}.status is pending but no note explains what is unverified')
        if 'requires_admin' in item and not isinstance(item['requires_admin'], bool):
            problems.append(f'{where}.requires_admin must be a boolean')

        paths = item.get('paths')
        if not isinstance(paths, list) or not paths:
            problems.append(f'{where}.paths must be a non-empty array')
        else:
            for template in paths:
                problem = _template_problem(template)
                if problem:
                    problems.append(f'{where}.paths: {problem}')

        excludes = item.get('exclude', [])
        if not isinstance(excludes, list) or any(not isinstance(x, str) for x in excludes):
            problems.append(f'{where}.exclude must be an array of strings')

        running = item.get('running', [])
        if not isinstance(running, list):
            problems.append(f'{where}.running must be an array')
            continue
        for probe in running:
            if not isinstance(probe, dict):
                problems.append(f'{where}.running entries must be objects')
                continue
            if probe.get('type') not in ('exe', 'pathname'):
                problems.append(f'{where}.running.type must be exe or pathname')
            if not isinstance(probe.get('value'), str) or not probe['value'].strip():
                problems.append(f'{where}.running.value must be a non-empty string')
            if 'same_user' in probe and not isinstance(probe['same_user'], bool):
                problems.append(f'{where}.running.same_user must be a boolean')
    return problems


def _template_problem(template):
    if not isinstance(template, str) or not template.strip():
        return 'templates must be non-empty strings'
    if '..' in Path(template).parts:
        return f'parent traversal is not allowed: {template}'
    if not (template.startswith('%') or re.match(r'^[A-Za-z]:\\', template)):
        return f'template must start with %VAR% or a drive letter: {template}'
    if template.endswith('\\') or template.endswith('/'):
        return f'template must not end with a separator: {template}'
    return None


def _resolve_vars(template, overrides):
    """Replace %NAME% from the cleaner's own vars first, then the environment."""
    missing = []

    def swap(match):
        name = match.group(1)
        for candidate in (name, name.upper(), name.lower()):
            if candidate in overrides:
                return overrides[candidate]
            if candidate in os.environ:
                return os.environ[candidate]
        missing.append(name)
        return match.group(0)
    expanded = VAR.sub(swap, template)
    return expanded, missing


def expand(cleaner, overrides=None):
    """Return (paths, unresolved_variables) for one cleaner, no filesystem access."""
    overrides = dict(overrides or {})
    overrides.update(cleaner.get('vars') or {})
    paths, missing = [], []
    for template in cleaner['paths']:
        expanded, absent = _resolve_vars(template, overrides)
        if absent:
            missing.extend(absent)
            continue
        for hit in glob.glob(expanded):
            paths.append(os.path.normpath(hit))
    return sorted(set(paths)), sorted(set(missing))


def tree_size(path):
    """Logical bytes below a path; reparse points are skipped, never followed."""
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            info = os.lstat(current)
        except OSError:
            continue
        if scan.unsafe(info):
            continue
        if stat.S_ISDIR(info.st_mode):
            try:
                with os.scandir(current) as entries:
                    stack.extend(entry.path for entry in entries)
            except OSError:
                continue
        elif stat.S_ISREG(info.st_mode):
            total += info.st_size
    return total


def describe(path, *, stat_size=False):
    try:
        info = os.lstat(path)
    except OSError:
        return {'path': path, 'exists': False, 'kind': 'missing', 'bytes': None,
                'denied': _denied(path)}
    if stat.S_ISDIR(info.st_mode):
        kind, size = 'directory', (tree_size(path) if stat_size else None)
    elif stat.S_ISREG(info.st_mode):
        kind, size = 'file', info.st_size
    else:
        kind, size = 'other', None
    return {'path': path, 'exists': True, 'kind': kind, 'bytes': size,
            'denied': _denied(path)}


def _denied(path):
    import clean  # local import keeps path logic usable without the executor loaded
    return clean.is_denied(path)


def report(registry, *, status='all', category=None, stat_size=False, procs=None):
    running = procs if procs is not None else processes()
    rows = []
    for cleaner in registry['cleaners']:
        if status != 'all' and cleaner['status'] != status:
            continue
        if category and cleaner['category'] != category:
            continue
        paths, missing = expand(cleaner)
        entries = [describe(path, stat_size=stat_size) for path in paths]
        usable = [e for e in entries if e['exists'] and not e['denied']]
        reason = None
        if cleaner['status'] == 'pending':
            reason = 'pending: unverified'
        elif cleaner['risk'] == 'never':
            reason = 'risk=never: protected, never a bulk candidate'
        elif missing:
            reason = 'unresolved variable: ' + ', '.join(missing)
        elif not entries:
            reason = 'no such path on this machine'
        elif not usable:
            if any(e['denied'] for e in entries):
                reason = 'every existing path is refused by clean.py: ' + entries[0]['denied']
            else:
                reason = 'nothing exists on this machine'
        rows.append({
            'id': cleaner['id'],
            'label': cleaner['label'],
            'category': cleaner['category'],
            'status': cleaner['status'],
            'risk': cleaner['risk'],
            'action': cleaner['action'],
            'regenerates': cleaner.get('regenerates', ''),
            'requires_admin': bool(cleaner.get('requires_admin', False)),
            'eligible': reason is None,
            'reason': reason,
            'unresolved': missing,
            'running': _probe_running(cleaner.get('running') or [], running),
            'paths': entries,
            'bytes': sum(e['bytes'] or 0 for e in entries) if stat_size else None,
            'note': cleaner.get('note', ''),
        })
    if stat_size:
        rows.sort(key=lambda row: (row['bytes'] or 0), reverse=True)
    return {'schema': REPORT_SCHEMA, 'cleaners': rows,
            'summary': {'total': len(rows),
                        'eligible': sum(1 for row in rows if row['eligible']),
                        'running': sum(1 for row in rows if row['running']['active'])},
            'caveats': [
                'Candidates are not permission to delete; each still needs plan.json evidence and the gate.',
                'same_user is not implemented: a match means any user runs it, which is the conservative case.',
                'Sizes are logical bytes of visible files; reparse points are skipped, not followed.',
            ]}


def _probe_running(probes, running):
    if not probes:
        return {'checked': False, 'active': False, 'matches': []}
    matches = []
    for probe in probes:
        kind, value = probe.get('type'), probe.get('value', '')
        if kind == 'exe':
            hit = any(name.lower() == os.path.basename(value).lower() for name in running['names'])
        else:
            hit = scan.normalized(value) in running['paths']
        matches.append({'type': kind, 'value': value, 'detected': hit})
    return {'checked': True, 'active': any(m['detected'] for m in matches), 'matches': matches}


TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_void_p),
                ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
                ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG),
                ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260)]


def processes():
    """Return {'names': [...], 'paths': {normalized path, ...}} for live processes."""
    if os.name != 'nt':
        raise ValueError('process detection is Windows-only')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    kernel.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                                  wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]

    snapshot = kernel.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        raise OSError(ctypes.get_last_error(), 'CreateToolhelp32Snapshot failed')
    names, paths = [], set()
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    try:
        more = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            names.append(entry.szExeFile)
            handle = kernel.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False,
                                        entry.th32ProcessID)
            if handle:
                buffer = ctypes.create_unicode_buffer(32768)
                size = wintypes.DWORD(len(buffer))
                if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                    paths.add(scan.normalized(buffer.value))
                kernel.CloseHandle(handle)
            more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return {'names': names, 'paths': paths}


def render(result):
    rows = result['cleaners']
    if not rows:
        return 'no cleaner matched the filter'
    lines = []
    for row in rows:
        mark = 'OK ' if row['eligible'] else '-- '
        flags = []
        if row['running']['active']:
            flags.append('RUNNING')
        if row['requires_admin']:
            flags.append('ADMIN')
        if row['status'] == 'pending':
            flags.append('PENDING')
        if row['risk'] == 'never':
            flags.append('NEVER')
        size = ''
        if row['bytes'] is not None:
            size = f"  {row['bytes'] / (1 << 20):,.1f} MiB"
        lines.append(f"{mark}{row['id']:<26} {row['category']:<12} "
                     f"{' '.join(flags):<18}{size}")
        if row['reason']:
            lines.append(f"    {row['reason']}")
        for entry in row['paths']:
            state = entry['kind'] if entry['exists'] else 'missing'
            if entry['denied']:
                state += f" / denied: {entry['denied']}"
            lines.append(f"    - {entry['path']}  [{state}]")
    summary = result['summary']
    lines.append(f"\n{summary['total']} cleaner(s), {summary['eligible']} eligible, "
                 f"{summary['running']} with a running process")
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Expand the candidate registry (read-only).')
    parser.add_argument('command', nargs='?', default='list',
                        choices=['list', 'validate', 'running'])
    parser.add_argument('--registry', default=DEFAULT_REGISTRY)
    parser.add_argument('--status', default='all', choices=['all', 'release', 'pending'])
    parser.add_argument('--category', choices=sorted(CATEGORIES))
    parser.add_argument('--stat', action='store_true', help='measure logical size (slow)')
    parser.add_argument('--format', default='text', choices=['text', 'json'])
    args = parser.parse_args(argv)

    try:
        registry = load(args.registry)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

    if args.command == 'validate':
        print(json.dumps({'ok': True, 'cleaners': len(registry['cleaners']),
                          'schema': registry['schema']}, ensure_ascii=False))
        return 0
    if args.command == 'running':
        live = processes()
        print(json.dumps({'processes': len(live['names']),
                          'names': sorted(set(live['names']))}, ensure_ascii=False))
        return 0

    result = report(registry, status=args.status, category=args.category, stat_size=args.stat)
    if args.format == 'json':
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
