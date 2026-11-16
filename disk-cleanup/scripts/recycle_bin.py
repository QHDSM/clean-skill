#!/usr/bin/env python3
"""Read Windows Recycle Bin $I records and restore $R payloads without overwriting.

What this module does not prove: a parse that matches the documented $I layout
does not prove the item belongs to this user, that its deletion was authorized,
that the $R payload is unchanged, or that a restore will succeed. The Recycle
Bin is an unauthenticated on-disk structure that any process with write access
can alter, so an entry is evidence that must be re-checked, not permission.

The module never permanently deletes user data. The only removal it performs is
of the $I metadata file, and only after $R was moved back to its original path.
restore() refuses to overwrite an existing target and never recreates missing
parent directories; callers decide whether rebuilding the directory tree is
acceptable. On non-Windows platforms the move is a best-effort rename whose
no-overwrite guarantee is not atomic, because only Windows rename refuses an
existing destination.
"""
import os

SCHEMA = 'disk-cleanup-recycle-bin/v1'
AUDIT_SCHEMA = 'disk-cleanup-recycle-audit/v1'
RECYCLE_DIR = '$RECYCLE.BIN'
_HEADER_SIZE = 0x18
_V1_PATH_BYTES = 520
_V2_COUNT_SIZE = 4
# Windows paths are at most 32767 UTF-16 units, so a larger $I is never genuine.
# The cap keeps an untrusted directory from forcing an unbounded read.
_MAX_I_BYTES = 256 * 1024
_FILETIME_TICKS_PER_SECOND = 10_000_000
_FILETIME_EPOCH_OFFSET = 11_644_473_600.0


def _pair_path(i_path):
    """Return the sibling $R path for a $I path; '' when the name is unusable.

    The documented pairing is positional, not a lookup: only the leading two
    characters differ, so this stays correct for multi-dot and non-ASCII names.
    """
    try:
        text = os.fspath(i_path)
        if isinstance(text, bytes):
            text = os.fsdecode(text)
        directory, name = os.path.split(text)
        if len(name) < 2:
            return ''
        return os.path.join(directory, '$R' + name[2:])
    except (TypeError, ValueError):
        return ''


def _new_entry(i_path):
    return {
        'schema': SCHEMA,
        'i_path': str(i_path),
        'r_path': _pair_path(i_path),
        'valid': False,
        'version': 0,
        'size_bytes': 0,
        'deleted_unix': 0.0,
        'original_path': '',
        'issues': [],
    }


def _invalid(result, message):
    result['issues'].append(message)
    return result


def _decode_path(raw):
    """Decode a NUL-terminated UTF-16LE path and drop the terminator.

    Raises UnicodeDecodeError for odd or malformed encodings; callers convert
    that into an issue so a damaged entry never escapes as an exception.
    """
    text = raw.decode('utf-16-le')
    terminator = text.find('\x00')
    return text if terminator < 0 else text[:terminator]


def _parse_entry(i_path):
    result = _new_entry(i_path)
    try:
        with open(i_path, 'rb') as handle:
            data = handle.read(_MAX_I_BYTES + 1)
    except OSError as exc:
        return _invalid(result, f'cannot read $I file: {exc}')

    if not data:
        return _invalid(result, '$I file is empty')
    if len(data) > _MAX_I_BYTES:
        return _invalid(result, f'$I file is implausibly large (> {_MAX_I_BYTES} bytes)')
    if len(data) < _HEADER_SIZE:
        return _invalid(result, f'$I header is truncated: {len(data)} of {_HEADER_SIZE} bytes')

    version = int.from_bytes(data[0:8], 'little')
    result['version'] = version
    result['size_bytes'] = int.from_bytes(data[8:16], 'little')
    filetime = int.from_bytes(data[16:24], 'little')

    if version not in (1, 2):
        return _invalid(result, f'unsupported $I version: {version}')
    # A zero FILETIME is structurally valid but cannot be matched by time; treat
    # it as unusable instead of reporting a 1601 deletion date to the caller.
    if filetime == 0:
        return _invalid(result, 'deleted timestamp is zero; entry has no usable time')
    result['deleted_unix'] = (filetime / _FILETIME_TICKS_PER_SECOND
                              - _FILETIME_EPOCH_OFFSET)

    if version == 1:
        if len(data) < _HEADER_SIZE + _V1_PATH_BYTES:
            return _invalid(result, f'version 1 path is truncated: {len(data)} of '
                                    f'{_HEADER_SIZE + _V1_PATH_BYTES} bytes')
        raw = data[_HEADER_SIZE:_HEADER_SIZE + _V1_PATH_BYTES]
    else:
        if len(data) < _HEADER_SIZE + _V2_COUNT_SIZE:
            return _invalid(result, 'version 2 path length is truncated')
        characters = int.from_bytes(
            data[_HEADER_SIZE:_HEADER_SIZE + _V2_COUNT_SIZE], 'little')
        if characters == 0:
            return _invalid(result, 'version 2 path length is zero')
        start = _HEADER_SIZE + _V2_COUNT_SIZE
        end = start + characters * 2
        if len(data) < end:
            return _invalid(result, f'version 2 path is truncated: {len(data)} of {end} bytes')
        raw = data[start:end]

    try:
        original_path = _decode_path(raw)
    except UnicodeDecodeError as exc:
        return _invalid(result, f'original path is not valid UTF-16LE: {exc}')
    if not original_path:
        return _invalid(result, 'original path is empty')

    result['original_path'] = original_path
    result['valid'] = True
    return result


def parse_entry(i_path):
    """Parse one $I file into a dict; never raises, valid only on full success.

    'valid' means the version, size, deletion timestamp and original path were
    all readable. It says nothing about $R, user ownership or restore success.
    """
    try:
        return _parse_entry(os.fspath(i_path))
    except Exception as exc:  # a malformed argument must not escape as an exception
        result = _new_entry(i_path)
        return _invalid(result, f'unexpected parser error: {type(exc).__name__}: {exc}')


def _directory_entries(path):
    """Materialise one directory listing as (name, path, is_dir) tuples.

    Entries are read eagerly so a permission failure is attributable to this
    directory instead of appearing halfway through a lazy iteration. The helper
    is also the seam tests use to simulate other users' unreadable SID folders.
    """
    with os.scandir(path) as iterator:
        return [(entry.name, entry.path, entry.is_dir()) for entry in iterator]


def recycle_root(drive):
    """Return '<letter>:\\$RECYCLE.BIN' for a drive letter, else None.

    Accepts 'D', 'D:', 'D:\\' and 'D:/'. Rejects anything with extra path
    segments so a caller cannot smuggle an arbitrary directory into the scan.
    """
    try:
        text = os.fspath(drive)
    except TypeError:
        return None
    if isinstance(text, bytes):
        text = text.decode('ascii', 'replace')
    if not isinstance(text, str):
        return None
    text = text.strip()
    if len(text) >= 2 and text[1] == ':':
        letter, remainder = text[0], text[2:]
    else:
        letter, remainder = text[:1], text[1:]
    if (len(letter) != 1 or not letter.isascii() or not letter.isalpha()
            or remainder.strip('\\/') != ''):
        return None
    return f'{letter.upper()}:\\{RECYCLE_DIR}'


def _collect(drive):
    """Walk one volume's recycle bin and return entries, orphans and issues."""
    result = {'schema': SCHEMA, 'root': '', 'entries': [], 'orphans': [],
              'issues': [], 'complete': True}
    root = recycle_root(drive)
    if root is None:
        result['complete'] = False
        result['issues'].append(f'not a drive letter: {drive!r}')
        return result
    result['root'] = root

    try:
        children = _directory_entries(root)
    except FileNotFoundError:
        result['complete'] = False
        result['issues'].append(f'recycle bin root not found: {root}')
        return result
    except OSError as exc:
        result['complete'] = False
        result['issues'].append(f'cannot list recycle bin root {root}: {exc}')
        return result

    for sid_name, sid_path, sid_is_dir in sorted(children):
        if not sid_is_dir:
            continue
        try:
            items = _directory_entries(sid_path)
        except PermissionError as exc:
            result['complete'] = False
            result['issues'].append(f'permission denied for {sid_name}: {exc}')
            continue
        except OSError as exc:
            result['complete'] = False
            result['issues'].append(f'cannot list {sid_path}: {exc}')
            continue

        r_names = {}
        i_items = []
        for name, child_path, child_is_dir in items:
            head = name[:2].upper()
            if head == '$I':
                if child_is_dir:
                    result['issues'].append(f'skipping $I-named directory: {child_path}')
                    result['orphans'].append({'kind': 'i_named_directory',
                                              'i_path': child_path,
                                              'detail': 'a directory named $I is not a record'})
                else:
                    i_items.append((name, child_path))
            elif head == '$R':
                # $R may be a file or a directory: recycled folders are real
                # payloads. Compare suffixes case-insensitively because the
                # pairing is positional and some filesystems keep the case.
                r_names[name[2:].casefold()] = name

        # A payload whose $I is missing cannot be identified, so it is an orphan
        # in its own right: no metadata means no original path and no restore.
        i_suffixes = {name[2:].casefold() for name, _ in i_items}
        for suffix, r_name in sorted(r_names.items()):
            if suffix in i_suffixes:
                continue
            orphan_r = os.path.join(sid_path, r_name)
            result['issues'].append(f'orphan $R without $I: {orphan_r}')
            result['orphans'].append({
                'kind': 'r_without_i', 'r_path': orphan_r,
                'detail': 'payload has no $I metadata; it cannot be identified or restored'})

        for name, child_path in sorted(i_items):
            entry = parse_entry(child_path)
            if not entry['valid']:
                detail = '; '.join(entry['issues'])
                result['issues'].append(f'skipping invalid $I {name}: {detail}')
                result['orphans'].append({'kind': 'invalid_i', 'i_path': child_path,
                                          'detail': detail})
                continue
            actual_r_name = r_names.get(name[2:].casefold())
            if actual_r_name is None:
                result['issues'].append(f'orphan $I without $R: {child_path}')
                result['orphans'].append({
                    'kind': 'i_without_r', 'i_path': child_path,
                    'detail': 'the record survived but its payload is gone, so there is '
                              'nothing to restore'})
                continue
            actual_r_path = os.path.join(os.path.dirname(child_path), actual_r_name)
            if os.path.normcase(actual_r_path) != os.path.normcase(entry['r_path']):
                entry['r_path'] = actual_r_path
            entry['sid'] = sid_name
            result['entries'].append(entry)

    result['entries'].sort(key=lambda entry: (os.path.normcase(entry['original_path']),
                                              entry['i_path']))
    result['orphans'].sort(key=lambda item: (item['kind'],
                                             item.get('i_path') or item.get('r_path') or ''))
    return result


def entries_for_volume(drive):
    """List recoverable entries under one volume's recycle bin.

    'complete' is False only when a location could not be examined (permissions
    or I/O). A malformed or orphaned record is reported in 'issues' and 'orphans'
    and skipped: it is not recoverable data, so it does not by itself make the
    scan partial.
    """
    return _collect(drive)



def bin_usage(drive):
    """How much of this volume's Recycle Bin is already spent.

    The Recycle Bin budget is per volume and shared with everything already in
    it, so a batch check has to start from this number instead of assuming an
    empty bin. Only usable when the whole bin could be read: 'complete' False
    means the total is unknown, and callers must not treat unknown as zero.
    Directory payloads contribute the size recorded in their $I metadata; that
    is what the bin accounts for them, not the sum of their descendants.
    """
    collected = _collect(drive)
    total = sum(entry.get('size_bytes') or 0 for entry in collected['entries'])
    return {'drive': (drive or '').rstrip('\\/').upper(),
            'root': collected['root'],
            'bytes': total,
            'entries': len(collected['entries']),
            'complete': collected['complete'],
            'issues': list(collected['issues'])}

def find_entry(drive, original_path, deleted_unix=None, tolerance=120):
    """Return the best entry for a path, or None when none matches.

    Matching is an exact os.path.normcase comparison, not a prefix or glob, so
    'C:\\Temp' never matches 'C:\\Temp2'. When deleted_unix is None and a path
    was deleted repeatedly, the most recent deletion wins; None therefore does
    not prove the item is absent, only that this scan found no candidate.
    """
    scan = entries_for_volume(drive)
    wanted = os.path.normcase(original_path)
    candidates = [entry for entry in scan['entries']
                  if os.path.normcase(entry['original_path']) == wanted]
    if not candidates:
        return None
    if deleted_unix is None:
        return max(candidates, key=lambda entry: (entry['deleted_unix'], entry['i_path']))
    within = [entry for entry in candidates
              if abs(entry['deleted_unix'] - deleted_unix) <= tolerance]
    if not within:
        return None
    return min(within, key=lambda entry: (abs(entry['deleted_unix'] - deleted_unix),
                                          entry['i_path']))


def restore(entry):
    """Move $R back to its original path, then remove $I; never overwrite.

    Requires an entry produced by parse_entry with valid=True. A missing parent
    directory or an existing target is a refusal, not a failure to repair: the
    caller decides whether to recreate the tree or resolve the conflict. Once
    $R has moved, a $I removal failure only degrades metadata cleanup; the file
    is already back, so ok stays True with i_removed=False and a detail.
    """
    result = {'ok': False, 'restored_to': None, 'detail': '', 'i_removed': False}
    if not isinstance(entry, dict):
        result['detail'] = 'entry is not a parse_entry result'
        return result
    if entry.get('valid') is not True:
        result['detail'] = 'entry is not valid; refusing to restore'
        return result

    r_path = entry.get('r_path')
    i_path = entry.get('i_path')
    original_path = entry.get('original_path')
    if not (isinstance(r_path, str) and r_path
            and isinstance(i_path, str) and i_path
            and isinstance(original_path, str) and original_path):
        result['detail'] = 'entry is missing i_path, r_path or original_path'
        return result
    if not os.path.exists(r_path):
        result['detail'] = f'$R data file is missing: {r_path}'
        return result

    parent = os.path.dirname(original_path)
    if not parent or not os.path.isdir(parent):
        result['detail'] = f'original parent directory does not exist: {parent or original_path}'
        return result
    # lexists also catches a dangling link at the target name. Windows rename
    # refuses an existing destination; the pre-check keeps POSIX honest too.
    if os.path.lexists(original_path):
        result['detail'] = f'target already exists: {original_path}'
        return result

    try:
        os.rename(r_path, original_path)
    except OSError as exc:
        result['detail'] = f'cannot move $R back to original path: {exc}'
        return result

    result['ok'] = True
    result['restored_to'] = original_path
    try:
        os.remove(i_path)
        result['i_removed'] = True
        result['detail'] = 'restored and removed $I metadata'
    except OSError as exc:
        result['i_removed'] = False
        result['detail'] = f'restored, but could not remove $I metadata: {exc}'
    return result


def _audit_drive_label(drive, root):
    """Label the audit with the drive the caller asked for, not a temp path's drive."""
    text = drive.strip() if isinstance(drive, str) else ''
    if text and len(text.rstrip('\\:/')) == 1 and text[:1].isalpha():
        return text[:1].upper() + ':'
    return os.path.splitdrive(root)[0].upper()


def audit_volume(drive, *, check_conflicts=True):
    """Summarise one volume's Recycle Bin read-only: pairs, orphans, conflicts.

    A conflict is an entry whose original path exists again. restore() will
    refuse to overwrite it, so the caller has to move or rename the new object
    first; reporting the count early is cheaper than discovering it one restore
    at a time. This function never writes, prunes or restores anything.
    """
    data = _collect(drive)
    entries = data['entries']
    conflicts = []
    if check_conflicts:
        for entry in entries:
            try:
                present = os.path.lexists(entry['original_path'])
            except (OSError, ValueError) as exc:
                conflicts.append({'i_path': entry['i_path'],
                                  'original_path': entry['original_path'],
                                  'reason': f'cannot check the original path: {exc}'})
                continue
            if present:
                conflicts.append({
                    'i_path': entry['i_path'],
                    'original_path': entry['original_path'],
                    'reason': 'original path is occupied again; restore would refuse to '
                              'overwrite'})

    times = [entry['deleted_unix'] for entry in entries
             if isinstance(entry.get('deleted_unix'), float)]
    total_bytes = sum(entry.get('size_bytes') or 0 for entry in entries)
    listed = sorted(entries, key=lambda entry: (entry['deleted_unix'], entry['i_path']),
                    reverse=True)
    summaries = [{'original_path': entry['original_path'],
                  'size_bytes': entry['size_bytes'],
                  'deleted_unix': entry['deleted_unix'],
                  'sid': entry.get('sid'),
                  'i_path': entry['i_path'],
                  'r_path': entry['r_path']} for entry in listed]
    by_kind = {}
    for orphan in data['orphans']:
        by_kind[orphan['kind']] = by_kind.get(orphan['kind'], 0) + 1
    return {
        'schema': AUDIT_SCHEMA,
        'drive': _audit_drive_label(drive, data['root']),
        'root': data['root'],
        'complete': data['complete'],
        'issues': data['issues'],
        'orphans': data['orphans'],
        'orphan_counts': by_kind,
        'conflicts': conflicts,
        'totals': {
            'entries': len(entries),
            'bytes': total_bytes,
            'orphans': len(data['orphans']),
            'oldest_deleted_unix': min(times) if times else None,
            'newest_deleted_unix': max(times) if times else None,
        },
        'entries': summaries,
    }
