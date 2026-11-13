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


def hash_file(path, expected):
    check_path(path)
    before = os.lstat(path)
    if unsafe(before) or fingerprint(before) != expected:
        raise OSError('file changed before hashing')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, 'rb') as stream:
        if fingerprint(os.fstat(stream.fileno())) != expected:
            raise OSError('file changed while opening')
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
        if fingerprint(os.fstat(stream.fileno())) != expected:
            raise OSError('file changed while hashing')
    if fingerprint(os.lstat(path)) != expected:
        raise OSError('file changed after hashing')
    return digest.hexdigest()


class Scanner:
    def __init__(self, roots, *, depth=None, top=20, min_size=0, excludes=(), duplicates=False, progress=True):
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

    def note(self, path, reason, error=False):
        bucket = self.errors if error else self.skipped
        if error:
            self.error_count += 1
        else:
            self.skip_count += 1
        if len(bucket) < 200:
            bucket.append({'path': str(path), 'reason': reason})

    def keep(self, heap, size, path):
        if size < self.min_size:
            return
        value = (size, str(path))
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
            check_path(path)
            iterator = os.scandir(path)
            stack.append([path, level, iterator, 0])
            self.dirs += 1
        try:
            enter(root, 0)
            while stack:
                self.tick()
                frame = stack[-1]
                path, level, iterator, _ = frame
                try:
                    entry = next(iterator)
                except StopIteration:
                    iterator.close()
                    self.keep(self.big_dirs, frame[3], path)
                    amount = frame[3]
                    stack.pop()
                    if stack:
                        stack[-1][3] += amount
                    continue
                except OSError as exc:
                    self.note(path, str(exc), True)
                    iterator.close()
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
                        ext = Path(entry.name).suffix.lower() or '[no extension]'
                        self.types[ext][0] += 1
                        self.types[ext][1] += amount
                        self.keep(self.big_files, amount, target)
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
                        if expected[:2] in identities:
                            continue  # Hard links are one file, not independent copies.
                        identities.add(expected[:2])
                        try:
                            hashes[hash_file(path, expected)].append(path)
                        except (OSError, ValueError) as exc:
                            self.note(path, str(exc), True)
                    for digest, paths in hashes.items():
                        if len(paths) > 1:
                            groups.append(dict(sha256=digest, file_size_bytes=size, paths=paths))
        except KeyboardInterrupt:
            self.interrupted = True
        rows = lambda heap: [dict(path=p, logical_bytes=s) for s, p in sorted(heap, reverse=True)]
        return dict(schema_version=1, roots=self.roots, exclusions=self.excludes,
                    started_at_unix=started, elapsed_seconds=round(time.time()-started, 3),
                    complete=not(self.interrupted or self.error_count or self.skip_count),
                    coverage_limited=self.limited, interrupted=self.interrupted,
                    scope=dict(depth=self.depth, top=self.top, min_size=self.min_size, duplicates=self.duplicates),
                    files=self.files, directories=self.dirs, logical_bytes=self.total,
                    allocated_bytes=None, volumes=volumes, largest_files=rows(self.big_files),
                    largest_directories=rows(self.big_dirs),
                    extensions=[dict(extension=e, files=n, logical_bytes=s) for e,(n,s) in sorted(self.types.items(), key=lambda x:x[1][1], reverse=True)],
                    duplicate_groups=groups, error_count=self.error_count, errors=self.errors,
                    skipped_count=self.skip_count, skipped=self.skipped,
                    notes=['Logical sizes may count hard links multiple times; allocated/reclaimable bytes are unknown.',
                           'Directory totals overlap. Errors/skips are sampled up to 200 each.',
                           'Metadata is not a filesystem snapshot; concurrent changes may affect totals.'])


def render(report):
    lines = [f"Complete: {report['complete']} | Interrupted: {report['interrupted']}",
             f"Files: {report['files']} | Directories: {report['directories']} | Logical bytes: {report['logical_bytes']}",
             'Allocated/reclaimable bytes: unknown']
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
                          excludes=args.exclude, duplicates=args.duplicates, progress=not args.no_progress)
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
    sys.exit(main())
