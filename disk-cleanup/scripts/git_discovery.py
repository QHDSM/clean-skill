"""Bounded, read-only discovery of Git worktree markers; not a safety proof."""
import os
from pathlib import Path
import stat
import time

import scan


def discover(path, max_entries=5000, max_seconds=0.5):
    """Find .git files/directories in ancestors and descendants without following links.

    Coverage gaps are reported explicitly. This does not detect every bare repository
    or external git directory and cannot provide a race-free snapshot.
    """
    path = Path(os.path.abspath(path))
    markers, issues = [], []
    seen = set()
    start = time.monotonic()
    count = 0

    def marker_at(directory):
        marker = directory / '.git'
        try:
            os.lstat(marker)
        except FileNotFoundError:
            return
        except OSError as exc:
            issues.append(f'cannot inspect {marker}: {exc}')
            return
        key = scan.normalized(marker)
        if key not in seen:
            seen.add(key)
            markers.append(str(marker))

    try:
        mode = os.lstat(path).st_mode
    except OSError as exc:
        return {'markers': [], 'complete': False, 'issues': [str(exc)], 'entries_checked': 0}

    directory = path if stat.S_ISDIR(mode) else path.parent
    for ancestor in (directory, *directory.parents):
        marker_at(ancestor)

    pending = [path] if stat.S_ISDIR(mode) else []
    while pending:
        if count >= max_entries or time.monotonic() - start >= max_seconds:
            issues.append('discovery limit reached; descendants not fully checked')
            break
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if count >= max_entries or time.monotonic() - start >= max_seconds:
                        issues.append('discovery limit reached; descendants not fully checked')
                        pending.clear()
                        break
                    count += 1
                    if entry.name.lower() == '.git':
                        marker_at(current)
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        issues.append(f'cannot inspect {entry.path}: {exc}')
                        continue
                    if scan.unsafe(info):
                        issues.append(f'skipped link, reparse or offline entry: {entry.path}')
                    elif stat.S_ISDIR(info.st_mode):
                        pending.append(Path(entry.path))
        except OSError as exc:
            issues.append(f'cannot read {current}: {exc}')

    return {'markers': sorted(markers), 'complete': not issues,
            'issues': issues, 'entries_checked': count}
