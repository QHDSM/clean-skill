#!/usr/bin/env python3
"""Read-only disk inventory. Python 3.10+, no external dependencies."""
import argparse
import collections
import ctypes
import hashlib
import heapq
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import time

REPARSE = 0x400
OFFLINE = 0x1000
RECALL = 0x40000 | 0x400000


def normalized(path):
    return os.path.normcase(os.path.abspath(path))


def within(path, root):
    try:
        return os.path.commonpath([normalized(path), normalized(root)]) == normalized(root)
    except ValueError:
        return False


def unsafe(st):
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, 'st_file_attributes', 0) & (REPARSE | OFFLINE | RECALL))


INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _WIN32_FIND_STREAM_DATA(ctypes.Structure):
    _fields_ = [('StreamSize', ctypes.c_longlong),
                ('cStreamName', ctypes.c_wchar * (260 + 36))]


def _kernel32():
    return ctypes.WinDLL('kernel32', use_last_error=True)


def alternate_streams(path):
    """Return [(name, size)] for NTFS alternate data streams, without the default one.

    A file's visible size can hide gigabytes in a named stream; neither logical
    bytes nor allocation queries count them. Only NTFS has streams, and this runs
    one enumeration per file, so it stays behind an explicit flag.
    """
    k32 = _kernel32()
    k32.FindFirstStreamW.restype = ctypes.c_void_p
    k32.FindFirstStreamW.argtypes = [ctypes.c_wchar_p, ctypes.c_int,
                                     ctypes.POINTER(_WIN32_FIND_STREAM_DATA), ctypes.c_uint]
    k32.FindNextStreamW.restype = ctypes.c_bool
    k32.FindNextStreamW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_WIN32_FIND_STREAM_DATA)]
    k32.FindClose.argtypes = [ctypes.c_void_p]
    data = _WIN32_FIND_STREAM_DATA()
    handle = k32.FindFirstStreamW(str(path), 0, ctypes.byref(data), 0)
    if not handle or handle == INVALID_HANDLE_VALUE:
        return []
    found = []
    try:
        while True:
            name = data.cStreamName
            if name and name != '::$DATA':
                found.append((name, int(data.StreamSize)))
            if not k32.FindNextStreamW(handle, ctypes.byref(data)):
                break
    finally:
        k32.FindClose(handle)
    return found


def check_path(path):
    """Reject unsafe ancestors as well as unsafe leaf targets."""
    p = Path(os.path.abspath(path))
    for part in reversed((p, *p.parents)):
        if unsafe(os.lstat(part)):
            raise ValueError('link, reparse point or offline ancestor: ' + str(part))
    return p


def drives():
    if os.name != 'nt':
        raise ValueError('--list-drives is supported on Windows only; specify a directory elsewhere')
    result = []
    kernel = ctypes.windll.kernel32
    kernel.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
    mask = kernel.GetLogicalDrives()
    for index in range(26):
        path = chr(65 + index) + ':\\'
        if mask & (1 << index) and kernel.GetDriveTypeW(path) == 3:
            item = {'path': path, 'type': 'fixed'}
            try:
                total, used, free = shutil.disk_usage(path)
                item.update(total_bytes=total, used_bytes=used, free_bytes=free)
            except OSError as exc:
                item['error'] = str(exc)
            result.append(item)
    return result


def size_arg(value):
    match = re.fullmatch(r'(\d+(?:\.\d+)?)\s*(B|KiB|MiB|GiB|TiB|KB|MB|GB|TB)?', value, re.I)
    if not match:
        raise argparse.ArgumentTypeError('use bytes or a unit such as 10MiB')
    unit = (match[2] or 'B').upper()
    powers = {'B': 1, **{u: 1024 ** i for i, u in enumerate(['KIB','MIB','GIB','TIB'], 1)},
              **{u: 1000 ** i for i, u in enumerate(['KB','MB','GB','TB'], 1)}}
    return int(float(match[1]) * powers[unit])


def fingerprint(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def content_identity(st):
    """Fields that stay comparable across lstat and fstat and survive a read.

    st_ino/st_dev can differ between a path-based stat and a handle-based stat on
    Windows, and st_ctime (NTFS ChangeTime) is bumped when reading a file updates
    its last-access time. Neither is evidence that the content changed, so a hash
    guard must not use them. Size plus mtime are the fields that actually move
    when content changes.
    """
    return (st.st_size, st.st_mtime_ns)


def hash_file(path, expected):
    check_path(path)
    before = os.lstat(path)
    if unsafe(before) or fingerprint(before)[:4] != expected[:4]:
        raise OSError('file changed before hashing')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if content_identity(opened) != content_identity(before):
            raise OSError('file changed while opening')
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
        if content_identity(os.fstat(stream.fileno())) != content_identity(before):
            raise OSError('file changed while hashing')
    after = os.lstat(path)
    if unsafe(after) or fingerprint(after)[:4] != expected[:4]:
        raise OSError('file changed after hashing')
    return digest.hexdigest()


class Scanner:
    def __init__(self, roots, *, depth=None, top=20, min_size=0, excludes=(), duplicates=False, progress=True, allocation=False, streams=False):
        if allocation and os.name != 'nt':
            raise ValueError('--allocation is Windows-only')
        if streams and os.name != 'nt':
            raise ValueError('--streams is Windows-only')
        self.roots = [str(check_path(p)) for p in roots]
        for i, root in enumerate(self.roots):
            if not Path(root).is_dir():
                raise ValueError('target must be a directory: ' + root)
            if any(within(root, other) or within(other, root) for other in self.roots[:i]):
                raise ValueError('overlapping scan roots are not allowed')
        self.depth, self.top, self.min_size = depth, top, min_size
        self.excludes = [normalized(p) for p in excludes]
        self.duplicates, self.progress = duplicates, progress
        self.files = self.dirs = self.total = self.error_count = self.skip_count = 0
        self.errors, self.skipped = [], []
        self.big_files, self.big_dirs = [], []
        self.types = collections.defaultdict(lambda: [0, 0])
        self.candidates = collections.defaultdict(list)
        self.limited = self.interrupted = False
        self.last_progress = 0
        self.allocation = allocation
        self.streams = streams
        self.stream_files = 0
        self.stream_bytes = 0
        self.stream_samples = []
        self.changed_dirs = 0
        self.changed_samples = []
        self.allocation_total = self.allocation_paths = self.allocation_links = 0
        self.allocation_ids = set()

    def observe_allocation(self, path, info):
        import windows_identity
        observed = windows_identity.capture(path, allocation=True)
        if observed['end_of_file'] != info.st_size or fingerprint(os.lstat(path))[:4] != fingerprint(info)[:4]:
            raise OSError('file changed while querying allocation')
        self.allocation_paths += 1
        identity = (observed['volume_serial'], observed['file_id'])
        if identity in self.allocation_ids:
            self.allocation_links += 1
        else:
            self.allocation_ids.add(identity)
            self.allocation_total += observed['allocation_bytes']

    def note(self, path, reason, error=False):
        bucket = self.errors if error else self.skipped
        if error:
            self.error_count += 1
        else:
            self.skip_count += 1
        if len(bucket) < 200:
            bucket.append({'path': str(path), 'reason': reason})

    def _settled(self, path, before):
        """Re-stat a directory after walking it; a changed mtime means the report
        mixes two states. Members appearing or disappearing move the directory's
        mtime; files edited in place do not, so this catches the former only."""
        try:
            after = os.lstat(path)
        except OSError:
            return
        if fingerprint(after)[3] != before[3]:
            self.changed_dirs += 1
            if len(self.changed_samples) < 20:
                self.changed_samples.append({
                    'path': str(path),
                    'note': 'directory metadata changed while it was being scanned'})

    def keep(self, heap, size, path, identity):
        if size < self.min_size:
            return
        value = (size, str(path), identity)
        if len(heap) < self.top:
            heapq.heappush(heap, value)
        elif value > heap[0]:
            heapq.heapreplace(heap, value)

    def tick(self):
        now = time.monotonic()
        if self.progress and now - self.last_progress >= 2:
            print(f'scanning: {self.files} files, {self.dirs} directories, {self.total} logical bytes', file=sys.stderr)
            self.last_progress = now

    def walk(self, root):
        # Explicit iterator stack keeps memory bounded by directory depth, not file count.
        stack = []
        root_device = os.lstat(root).st_dev
        def enter(path, level):
            # Roots were validated by check_path() in __init__; every directory
            # below them got here only because its own lstat was free of reparse
            # points, so re-validating the whole ancestor chain per directory is
            # both redundant and the most expensive part of a walk.
            identity = fingerprint(os.lstat(path))
            iterator = os.scandir(path)
            stack.append([path, level, iterator, 0, identity])
            self.dirs += 1
        try:
            enter(root, 0)
            while stack:
                self.tick()
                frame = stack[-1]
                path, level, iterator, _, _identity = frame
                try:
                    entry = next(iterator)
                except StopIteration:
                    iterator.close()
                    self._settled(path, frame[4])
                    self.keep(self.big_dirs, frame[3], path, frame[4])
                    amount = frame[3]
                    stack.pop()
                    if stack:
                        stack[-1][3] += amount
                    continue
                except OSError as exc:
                    self.note(path, str(exc), True)
                    iterator.close()
                    self._settled(path, frame[4])
                    amount = frame[3]
                    stack.pop()
                    if stack:
                        stack[-1][3] += amount
                    continue
                target = entry.path
                if any(within(target, ex) for ex in self.excludes):
                    self.note(target, 'excluded')
                    self.limited = True
                    continue
                try:
                    # Windows DirEntry.stat can omit st_dev/st_ino; lstat is needed
                    # for volume boundaries and independent duplicate identities.
                    info = os.lstat(target)
                    if unsafe(info):
                        self.note(target, 'link/reparse/offline/recall point')
                        continue
                    if info.st_dev != root_device:
                        self.note(target, 'different filesystem')
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        if self.depth is not None and level >= self.depth:
                            self.note(target, 'depth limit')
                            self.limited = True
                        else:
                            enter(target, level + 1)
                    elif stat.S_ISREG(info.st_mode):
                        amount = info.st_size
                        self.files += 1
                        self.total += amount
                        frame[3] += amount
                        ext = os.path.splitext(entry.name)[1].lower() or '[no extension]'
                        self.types[ext][0] += 1
                        self.types[ext][1] += amount
                        self.keep(self.big_files, amount, target, fingerprint(info))
                        if self.allocation:
                            self.observe_allocation(target, info)
                        if self.streams:
                            named = alternate_streams(target)
                            if named:
                                self.stream_files += 1
                                self.stream_bytes += sum(size for _name, size in named)
                                if len(self.stream_samples) < 20:
                                    self.stream_samples.append({
                                        'path': target,
                                        'streams': [{'name': name, 'bytes': size}
                                                    for name, size in named]})
                        if self.duplicates and amount >= self.min_size and amount > 0:
                            self.candidates[amount].append((target, fingerprint(info)))
                    else:
                        self.note(target, 'non-regular file')
                except (OSError, ValueError) as exc:
                    self.note(target, str(exc), True)
        except (OSError, ValueError) as exc:
            self.note(root, str(exc), True)
        finally:
            for frame in stack:
                frame[2].close()

    def _allocation_complete(self):
        return not(self.interrupted or self.error_count or self.skip_count)

    def run(self):
        started = time.time()
        volumes, groups = [], []
        try:
            for root in self.roots:
                try:
                    total, used, free = shutil.disk_usage(root)
                    volumes.append(dict(path=root, total_bytes=total, used_bytes=used, free_bytes=free))
                except OSError as exc:
                    self.note(root, str(exc), True)
                if any(within(root, ex) for ex in self.excludes):
                    self.note(root, 'excluded root')
                    self.limited = True
                    continue
                self.walk(root)
            if self.duplicates:
                for size, candidates in self.candidates.items():
                    if len(candidates) < 2:
                        continue
                    hashes = collections.defaultdict(list)
                    identities = set()
                    for path, expected in candidates:
                        self.tick()
                        identity = expected[:2] if expected[0] != 0 and expected[1] != 0 else None
                        if identity is not None and identity in identities:
                            continue  # Hard links are one file, not independent copies.
                        try:
                            digest = hash_file(path, expected)
                        except (OSError, ValueError) as exc:
                            self.note(path, str(exc), True)
                            continue
                        if identity is not None:
                            identities.add(identity)
                        hashes[digest].append((path, identity is not None))
                    for digest, matches in hashes.items():
                        if len(matches) > 1:
                            groups.append(dict(sha256=digest, file_size_bytes=size,
                                               paths=[path for path, _ in matches],
                                               distinct_file_ids_observed=all(known for _, known in matches)))
        except KeyboardInterrupt:
            self.interrupted = True
        rows = lambda heap: [dict(path=p, logical_bytes=s, fingerprint=list(fp))
                             for s, p, fp in sorted(heap, reverse=True)]
        report = dict(schema_version=1, roots=self.roots, exclusions=self.excludes,
                    started_at_unix=started, elapsed_seconds=round(time.time()-started, 3),
                    complete=not(self.interrupted or self.error_count or self.skip_count),
                    coverage_limited=self.limited, interrupted=self.interrupted,
                    scope=dict(depth=self.depth, top=self.top, min_size=self.min_size, duplicates=self.duplicates, allocation=self.allocation),
                    files=self.files, directories=self.dirs, logical_bytes=self.total,
                    allocated_bytes=(self.allocation_total
                                     if self.allocation and self._allocation_complete() else None),
                    volumes=volumes, largest_files=rows(self.big_files),
                    concurrent_change=dict(directories_changed=self.changed_dirs,
                        samples=self.changed_samples,
                        note='directories re-checked after walking them; a change means members'
                             ' appeared or disappeared mid-scan (files edited in place are not detected)'),
                    alternate_streams=dict(files_with_streams=self.stream_files,
                        alternate_bytes=self.stream_bytes,
                        samples=self.stream_samples,
                        note='NTFS named streams; not included in logical or allocated bytes')
                    if self.streams else None,
                    allocation_observation=(dict(reported_file_allocation_bytes=self.allocation_total,
                        paths_queried=self.allocation_paths, distinct_file_ids=len(self.allocation_ids),
                        repeated_hardlink_paths=self.allocation_links,
                        complete=self._allocation_complete(),
                        upper_bound_of_reclaimable_bytes=True,
                        excludes=['directories', 'filesystem metadata', 'alternate data streams'],
                        reclaimable_bytes=None) if self.allocation else None),
                    largest_directories=rows(self.big_dirs),
                    extensions=[dict(extension=e, files=n, logical_bytes=s) for e,(n,s) in sorted(self.types.items(), key=lambda x:x[1][1], reverse=True)],
                    duplicate_groups=groups, error_count=self.error_count, errors=self.errors,
                    skipped_count=self.skip_count, skipped=self.skipped,
                    notes=[('Logical sizes may count hard links multiple times. Allocated bytes sum the allocation of '
                            'distinct file IDs, exclude directories and filesystem metadata, and are an upper bound: '
                            'deleting a file does not guarantee its blocks are freed, so reclaimable bytes stay unknown.')
                           if self.allocation and self._allocation_complete() else
                           ('Logical sizes may count hard links multiple times; allocated/reclaimable bytes are unknown.'),
                           'Directory totals overlap. Errors/skips are sampled up to 200 each.',
                           'Fingerprints capture the object metadata, not directory descendants or file contents.',
                           'Duplicate groups are hash matches, not permission to delete; identity can be unavailable and files can change.',
                           'Metadata is not a filesystem snapshot; concurrent changes may affect totals.'])
        if self.changed_dirs:
            report['notes'].append(
                '%d director%s changed while being scanned; their totals mix two states.'
                % (self.changed_dirs, 'y' if self.changed_dirs == 1 else 'ies'))
        if self.streams:
            report['notes'].append(
                'Alternate data streams are enumerated but not counted in logical or allocated bytes.')
        return report


def render(report):
    lines = [f"Complete: {report['complete']} | Interrupted: {report['interrupted']}",
             f"Files: {report['files']} | Directories: {report['directories']} | Logical bytes: {report['logical_bytes']}"]
    if report.get('allocated_bytes') is not None:
        lines.append(f"Allocated bytes (distinct file IDs): {report['allocated_bytes']} | "
                     'Reclaimable bytes: unknown (deletion does not guarantee blocks are freed)')
    else:
        lines.append('Allocated/reclaimable bytes: unknown')
    change = report.get('concurrent_change') or {}
    if change.get('directories_changed'):
        lines.append('Directories that changed while being scanned: %d (totals are approximate)'
                     % change['directories_changed'])
    if report.get('alternate_streams') is not None:
        streams = report['alternate_streams']
        lines.append('Alternate data streams: %d file(s), %d bytes outside logical/allocated totals'
                     % (streams['files_with_streams'], streams['alternate_bytes']))
    if report.get('allocation_observation') is not None:
        lines.append('File allocation observation: ' + json.dumps(report['allocation_observation']))
        lines.append('Allocation excludes directory/metadata overhead and is not reclaimable space.')
    for key in ('volumes', 'largest_directories', 'largest_files', 'extensions', 'duplicate_groups', 'errors', 'skipped'):
        lines.append(key + ':')
        lines.extend('  ' + json.dumps(item, ensure_ascii=False) for item in report[key])
    lines.extend(report['notes'])
    lines.append(f"Errors: {report['error_count']} | Skips: {report['skipped_count']} | Seconds: {report['elapsed_seconds']}")
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('paths', nargs='*')
    parser.add_argument('--list-drives', action='store_true')
    parser.add_argument('--depth', type=int)
    parser.add_argument('--top', type=int, default=20)
    parser.add_argument('--min-size', type=size_arg, default=0)
    parser.add_argument('--exclude', action='append', default=[])
    parser.add_argument('--format', choices=['text', 'json'], default='text')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--duplicates', action='store_true')
    parser.add_argument('--allocation', action='store_true', help='query Windows file allocation metadata and deduplicate file IDs')
    parser.add_argument('--streams', action='store_true',
                        help='enumerate NTFS alternate data streams (one call per file; slower)')
    parser.add_argument('--no-progress', action='store_true')
    args = parser.parse_args(argv)
    if args.top < 1 or (args.depth is not None and args.depth < 0):
        parser.error('--top must be positive; --depth must be nonnegative')
    try:
        if args.list_drives and (args.paths or args.output):
            parser.error('--list-drives cannot be combined with paths or --output')
        if args.list_drives:
            print(json.dumps(drives(), ensure_ascii=False, indent=2))
            return 0
        if not args.paths:
            parser.error('specify paths explicitly; use --list-drives to discover fixed volumes')
        if args.output:
            check_path(args.output.absolute().parent)
            if os.path.lexists(args.output):
                raise ValueError('output already exists; refusing to overwrite')
        scanner = Scanner(args.paths, depth=args.depth, top=args.top, min_size=args.min_size,
                          excludes=args.exclude, duplicates=args.duplicates, progress=not args.no_progress, allocation=args.allocation,
                          streams=args.streams)
        report = scanner.run()
        content = json.dumps(report, ensure_ascii=False, indent=2) if args.format == 'json' else render(report)
        if args.output:
            with args.output.open('x', encoding='utf-8') as stream:
                stream.write(content + '\n')
        else:
            print(content)
        return 130 if report['interrupted'] else (2 if not report['complete'] else 0)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    # Keep redirected JSON portable across Windows code pages.
    sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
    sys.stderr.reconfigure(encoding='utf-8', errors='backslashreplace')
    sys.exit(main())
