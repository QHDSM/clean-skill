#!/usr/bin/env python3
"""Incremental volume changes through the NTFS USN change journal.

Why this exists: scan.py walks every directory every time, so "what grew since
last time" costs a second full walk and still mixes two points in time. The
journal answers that from a saved USN, touching only what changed.

What this deliberately does NOT do: parse the raw $MFT. That is how WizTree-class
tools get sizes without opening files, and how the reverse-engineered competitor
does it, but a $MFT parser is a filesystem implementation of its own and cannot
be verified on a machine without elevation. USN records carry names, parent
references and attributes -- no size. So this module reports *which paths
changed*, and sizes still come from stat on the changed paths only.

Everything here needs a volume handle opened for read, which needs elevation.
Without it the tool exits 3 and says so; it never falls back to guessing.
"""
import argparse
import ctypes
import ctypes.wintypes as wintypes
import gzip
import json
import os
import struct
import sys
import time

SCHEMA_STATE = 'disk-cleanup-usn-state/v1'
SCHEMA_CHANGES = 'disk-cleanup-usn-changes/v1'

FSCTL_ENUM_USN_DATA = 0x000900B3
FSCTL_READ_USN_JOURNAL = 0x000900BB
FSCTL_QUERY_USN_JOURNAL = 0x000900F4

FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
GENERIC_READ = 0x80000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ERROR_HANDLE_EOF = 38
ERROR_MORE_DATA = 234
ERROR_ACCESS_DENIED = 5
ROOT_FRN = 5

USN_REASON = {
    0x00000001: 'data_overwrite', 0x00000002: 'data_extend',
    0x00000004: 'data_truncation', 0x00000010: 'named_data_overwrite',
    0x00000020: 'named_data_extend', 0x00000040: 'named_data_truncation',
    0x00000100: 'file_create', 0x00000200: 'file_delete',
    0x00000400: 'ea_change', 0x00000800: 'security_change',
    0x00001000: 'rename_old_name', 0x00002000: 'rename_new_name',
    0x00004000: 'indexable_change', 0x00008000: 'basic_info_change',
    0x00010000: 'hard_link_change', 0x00020000: 'compression_change',
    0x00040000: 'encryption_change', 0x00080000: 'object_id_change',
    0x00100000: 'reparse_point_change', 0x00200000: 'stream_change',
    0x80000000: 'close',
}

RECORD = struct.Struct('<IHHQQqqIIIIHH')  # USN_RECORD_V2 header, 60 bytes


class Unsupported(Exception):
    """The volume cannot answer: not NTFS, no journal, or no elevation."""


class Truncated(Exception):
    """The index exceeded its memory budget; a partial map would lie."""


def _decode_reasons(mask):
    return sorted(name for bit, name in USN_REASON.items() if mask & bit)


def parse_records(buffer, offset=0):
    """Parse USN_RECORD_V2 entries out of a DeviceIoControl output buffer."""
    records = []
    limit = len(buffer)
    while offset + RECORD.size <= limit:
        (length, major, minor, frn, parent, usn, stamp, reason, source,
         security, attributes, name_length, name_offset) = RECORD.unpack_from(buffer, offset)
        if length < RECORD.size or offset + length > limit:
            break
        if major != 2:
            # USN_RECORD_V3 exists on newer volumes; refusing beats misparsing.
            raise Unsupported(f'USN record version {major}.{minor} is not supported')
        raw = bytes(buffer[offset + name_offset:offset + name_offset + name_length])
        records.append({
            'frn': frn, 'parent': parent, 'usn': usn, 'timestamp': stamp,
            'reason': reason, 'attributes': attributes,
            'name': raw.decode('utf-16-le', 'replace'),
        })
        offset += length
    return records


def parse_enum_output(buffer):
    """FSCTL_ENUM_USN_DATA returns the next start FRN followed by records."""
    if len(buffer) < 8:
        return 0, []
    return struct.unpack_from('<Q', buffer, 0)[0], parse_records(buffer, 8)


def parse_read_output(buffer):
    """FSCTL_READ_USN_JOURNAL returns the next USN followed by records."""
    if len(buffer) < 8:
        return 0, []
    return struct.unpack_from('<q', buffer, 0)[0], parse_records(buffer, 8)


def parse_journal_data(buffer):
    if len(buffer) < 56:
        raise Unsupported('USN journal data is shorter than one version-0 structure')
    journal_id, first, next_usn, lowest, max_usn, size, delta = struct.unpack_from(
        '<QqqqqQQ', buffer, 0)
    return {'journal_id': journal_id, 'first_usn': first, 'next_usn': next_usn,
            'lowest_valid_usn': lowest, 'max_usn': max_usn,
            'maximum_size': size, 'allocation_delta': delta}


def _escape(name):
    return name.replace('\\', '\\\\').replace('\t', '\\t').replace('\n', '\\n')


def _unescape(name):
    out, index = [], 0
    while index < len(name):
        char = name[index]
        if char == '\\' and index + 1 < len(name):
            nxt = name[index + 1]
            out.append({'\\': '\\', 't': '\t', 'n': '\n'}.get(nxt, '\\' + nxt))
            index += 2
        else:
            out.append(char)
            index += 1
    return ''.join(out)


def save_state(path, state, entries):
    """Write the map as tab-separated lines inside gzip; 1M entries stay around 20MB."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    header = dict(state, schema=SCHEMA_STATE, entries=len(entries), saved=time.time())
    tmp = path + '.tmp'
    with gzip.open(tmp, 'wt', encoding='utf-8', newline='\n') as fh:
        fh.write(json.dumps(header, ensure_ascii=False) + '\n')
        for frn, (parent, name) in entries.items():
            fh.write(f'{frn}\t{parent}\t{_escape(name)}\n')
    os.replace(tmp, path)
    return header


def load_state(path):
    with gzip.open(path, 'rt', encoding='utf-8') as fh:
        header = json.loads(fh.readline())
        if header.get('schema') != SCHEMA_STATE:
            raise ValueError('state file schema mismatch')
        entries = {}
        for line in fh:
            line = line.rstrip('\n')
            if not line:
                continue
            frn, parent, name = line.split('\t', 2)
            entries[int(frn)] = (int(parent), _unescape(name))
    return header, entries


def resolve(frn, entries, cache=None, depth=0):
    """Turn a file reference number into a full path, or None when the chain breaks."""
    if frn == ROOT_FRN:
        return ''
    cache = {} if cache is None else cache
    if frn in cache:
        return cache[frn]
    if depth > 512:
        return None
    node = entries.get(frn)
    if node is None:
        return None
    parent, name = node
    base = resolve(parent, entries, cache, depth + 1)
    if base is None:
        return None
    result = (base + '\\' + name) if base else name
    cache[frn] = result
    return result


def classify(record):
    """Collapse a reason bitmask into the one verb a report can carry."""
    names = _decode_reasons(record['reason'])
    if 'file_delete' in names:
        return 'deleted'
    if 'rename_new_name' in names:
        return 'renamed'
    if 'rename_old_name' in names:
        return 'renamed-away'
    if 'file_create' in names:
        return 'created'
    if any(name.startswith(('data_', 'named_data_', 'stream_', 'basic_info_')) for name in names):
        return 'modified'
    return 'touched'


# ---------------------------------------------------------------- Windows I/O

def elevated():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


class Volume:
    def __init__(self, drive):
        if os.name != 'nt':
            raise Unsupported('USN journal is Windows-only')
        letter = drive.rstrip('\\:').upper()
        if len(letter) != 1 or not letter.isalpha():
            raise ValueError('drive must look like C:')
        self.drive = letter + ':'
        self.path = '\\\\.\\' + self.drive
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateFileW.restype = ctypes.c_void_p
        self.kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                            ctypes.c_void_p]
        self.kernel.DeviceIoControl.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                                wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                                ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        self.kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self.handle = self.kernel.CreateFileW(
            self.path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, None,
            OPEN_EXISTING, 0, None)
        if self.handle in (None, INVALID_HANDLE_VALUE):
            error = ctypes.get_last_error()
            if error == ERROR_ACCESS_DENIED:
                raise Unsupported('opening the volume needs elevation; '
                                  'run this command from an elevated shell')
            raise Unsupported(f'could not open {self.path}: error {error}')

    def close(self):
        if getattr(self, 'handle', None):
            self.kernel.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def ioctl(self, code, payload, size=1 << 20):
        out = ctypes.create_string_buffer(size)
        returned = wintypes.DWORD(0)
        ok = self.kernel.DeviceIoControl(
            self.handle, code, payload, len(payload) if payload else 0,
            out, size, ctypes.byref(returned), None)
        if not ok:
            error = ctypes.get_last_error()
            if error == ERROR_HANDLE_EOF:
                return b'', 0
            if error == ERROR_MORE_DATA:
                return out.raw[:returned.value], returned.value
            raise Unsupported(f'DeviceIoControl {code:#010x} failed: error {error}')
        return out.raw[:returned.value], returned.value

    def journal(self):
        body, _ = self.ioctl(FSCTL_QUERY_USN_JOURNAL, None, 256)
        return parse_journal_data(body)

    def enumerate(self, max_entries=2_000_000):
        entries, start, calls = {}, 0, 0
        while True:
            payload = struct.pack('<Qqq', start, 0, 0x7FFFFFFFFFFFFFFF)
            body, returned = self.ioctl(FSCTL_ENUM_USN_DATA, payload)
            calls += 1
            if returned < 8:
                break
            start, records = parse_enum_output(body)
            for record in records:
                entries[record['frn']] = (record['parent'], record['name'])
            if len(entries) > max_entries:
                raise Truncated(f'index exceeded {max_entries} entries; '
                                'raise --max-entries or keep using the full scan')
            if start == 0 or not records or calls > 100000:
                break
        return entries

    def changes(self, since_usn, journal_id, limit=200_000):
        records, cursor, calls = [], since_usn, 0
        while True:
            payload = struct.pack('<qIIQQQ', cursor, 0xFFFFFFFF, 0, 0, 0, journal_id)
            body, returned = self.ioctl(FSCTL_READ_USN_JOURNAL, payload)
            calls += 1
            if returned < 8:
                break
            cursor, batch = parse_read_output(body)
            records.extend(batch)
            if len(records) > limit:
                raise Truncated(f'more than {limit} records since USN {since_usn}; '
                                'the journal covers a long gap, run a full scan instead')
            if cursor == since_usn or not batch or calls > 100000:
                break
            since_usn = cursor
        return records, cursor


# ------------------------------------------------------------------- commands

def default_state(drive):
    home = os.path.expanduser('~')
    return os.path.join(home, '.disk-cleanup', 'usn', drive.rstrip('\\:').upper() + '.json.gz')


def cmd_probe(args):
    with Volume(args.drive) as volume:
        data = volume.journal()
    data.update(ok=True, drive=volume.drive, elevated=True)
    print(json.dumps(data, ensure_ascii=False))
    return 0


def cmd_baseline(args):
    volume = Volume(args.drive)
    try:
        journal = volume.journal()
        entries = volume.enumerate(max_entries=args.max_entries)
    finally:
        volume.close()
    state = {'volume': volume.drive, 'journal_id': journal['journal_id'],
             'next_usn': journal['next_usn'], 'max_entries': args.max_entries}
    path = args.state or default_state(args.drive)
    header = save_state(path, state, entries)
    print(json.dumps({'ok': True, 'state': path, 'entries': header['entries'],
                      'journal_id': journal['journal_id'], 'next_usn': journal['next_usn'],
                      'caveats': ['Names and parent references only: USN records carry no size.',
                                  'A truncated index is refused, not saved.']},
                     ensure_ascii=False))
    return 0


def cmd_since(args):
    path = args.state or default_state(args.drive)
    if not os.path.exists(path):
        raise Unsupported(f'no baseline at {path}; run "usn.py baseline --drive '
                          f'{args.drive}" first (needs elevation)')
    header, entries = load_state(path)
    volume = Volume(args.drive)
    try:
        journal = volume.journal()
        if journal['journal_id'] != header['journal_id']:
            raise Unsupported('the journal was deleted or recreated since the baseline; '
                              'incremental is impossible, run a full scan')
        records, cursor = volume.changes(header['next_usn'], journal['journal_id'])
    finally:
        volume.close()

    cache, changes = {}, []
    for record in records:
        if record['reason'] & 0x80000000 and record['reason'] & 0x00000200 == 0:
            continue  # pure close events say nothing about content
        parent = resolve(record['parent'], entries, cache)
        if parent is None:
            changes.append({'action': classify(record), 'path': None,
                            'name': record['name'], 'unresolved_parent': record['parent'],
                            'reasons': _decode_reasons(record['reason'])})
            continue
        full = (parent + '\\' + record['name']) if parent else (volume.drive + '\\' + record['name'])
        changes.append({'action': classify(record), 'path': full,
                        'reasons': _decode_reasons(record['reason']),
                        'usn': record['usn']})
        if 'file_delete' in _decode_reasons(record['reason']):
            entries.pop(record['frn'], None)
        elif record['name']:
            entries[record['frn']] = (record['parent'], record['name'])

    if not args.dry_run:
        save_state(path, {'volume': volume.drive, 'journal_id': journal['journal_id'],
                          'next_usn': cursor, 'max_entries': header.get('max_entries', 0)},
                   entries)
    counts = {}
    for change in changes:
        counts[change['action']] = counts.get(change['action'], 0) + 1
    result = {'schema': SCHEMA_CHANGES, 'ok': True, 'drive': volume.drive,
              'from_usn': header['next_usn'], 'to_usn': cursor,
              'counts': counts, 'changes': changes,
              'complete': all(change['path'] for change in changes),
              'caveats': [
                  'USN records give names, parents and reasons, not sizes; stat the listed paths for bytes.',
                  'A rename appears as two records (old name, then new name).',
                  'Close-only events are dropped; they do not indicate new content.',
                  'The map is a point-in-time index: if the journal wrapped, this refuses instead of guessing.',
              ]}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for change in changes:
            print(f"{change['action']:<14} {change['path'] or '(unresolved) ' + str(change.get('name'))}")
        print(f"\n{len(changes)} change(s): " +
              ', '.join(f'{k}={v}' for k, v in sorted(counts.items())))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='NTFS USN journal: incremental changes (read-only).')
    sub = parser.add_subparsers(dest='command', required=True)

    probe = sub.add_parser('probe', help='report journal state; needs elevation')
    probe.add_argument('--drive', required=True)

    baseline = sub.add_parser('baseline', help='index the volume and save a USN checkpoint')
    baseline.add_argument('--drive', required=True)
    baseline.add_argument('--state')
    baseline.add_argument('--max-entries', type=int, default=2_000_000)

    since = sub.add_parser('since', help='list what changed since the saved checkpoint')
    since.add_argument('--drive', required=True)
    since.add_argument('--state')
    since.add_argument('--json', action='store_true')
    since.add_argument('--dry-run', action='store_true', help='do not advance the checkpoint')

    args = parser.parse_args(argv)
    handlers = {'probe': cmd_probe, 'baseline': cmd_baseline, 'since': cmd_since}
    try:
        return handlers[args.command](args)
    except Unsupported as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 3
    except (ValueError, OSError, Truncated) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
