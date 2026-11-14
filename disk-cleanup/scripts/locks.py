#!/usr/bin/env python3
"""Best-effort, read-only evidence that Windows objects may be in use.

Two independent heuristics:

* ``probe_deletable`` opens the object with DELETE access and full sharing.
  A sharing violation means some other handle holds an incompatible mode, so
  a delete would fail right now; success means one open happened to succeed
  one moment ago.
* ``restart_manager_locks`` asks the Restart Manager which registered
  applications and services hold the given files.

Neither result is a lock, a reservation or proof of safety. Another handle
can appear a microsecond after a successful probe, and Restart Manager only
reports holders Windows can identify inside the current session: kernel
holders, handles opened without delete sharing, and short-lived opens can be
missed. Absence of evidence is not evidence that a delete is safe, and
``deletable=None`` must be treated as unknown (fail closed) by callers.

This module is read-only: it never deletes, renames or writes targets, never
ends a process, never stops a service, never asks for privileges and never
changes ACLs. The Restart Manager session is queried and always ended; no
restart or shutdown is ever requested.
"""
import argparse
import ctypes
import functools
import json
import os
import sys

# CreateFileW access rights and sharing modes.
DELETE = 0x00010000
FILE_READ_ATTRIBUTES = 0x00000080
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
FILE_SHARE_ALL = FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
OPEN_EXISTING = 3
# Directories can only be opened with this flag, and the checklist applies to
# them too, so the same probe serves files and directories.
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

ERROR_SUCCESS = 0
ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
ERROR_ACCESS_DENIED = 5
ERROR_SHARING_VIOLATION = 32
ERROR_LOCK_VIOLATION = 33
ERROR_BAD_ARGUMENTS = 160
ERROR_MORE_DATA = 234
ERROR_FILENAME_EXCED_RANGE = 206

# Restart Manager limits and retries. The session key buffer must hold 33
# UTF-16 characters (32 + terminator): passing NULL instead returns
# ERROR_BAD_ARGUMENTS(160), so the buffer is allocated even though the key is
# not otherwise used.
RM_SESSION_KEY_LENGTH = 33
CCH_RM_MAX_APP_NAME = 255
CCH_RM_MAX_SVC_NAME = 63
RM_MAX_PATH = 260
# RM_APP_TYPE values. RmCritical (1000) marks processes Windows considers
# damaging to terminate; everything else is in the range the Restart Manager
# documents for restartable applications. These numbers only feed the advisory
# ``restartable`` flag: this module never restarts or ends anything.
RM_APP_MAIN_WINDOW = 1
RM_APP_OTHER_WINDOW = 2
RM_APP_SERVICE = 3
RM_APP_EXPLORER = 4
RM_APP_CONSOLE = 5
RM_APP_CRITICAL = 1000
# Application types the Restart Manager is prepared to stop and start again.
# RmUnknownApp (0) means it could not classify the holder (so nothing is known
# about restarting it) and RmCritical (1000) marks processes Windows itself
# treats as unsafe to terminate; both are reported as not restartable.
RM_RESTARTABLE_TYPES = (RM_APP_MAIN_WINDOW, RM_APP_OTHER_WINDOW, RM_APP_SERVICE,
                        RM_APP_EXPLORER, RM_APP_CONSOLE)
# One unreadable path must not hide locks held on every other path, so
# registration is chunked rather than all-or-nothing.
RM_REGISTER_CHUNK = 256
RM_RETRY_LIMIT = 6
RM_DEFAULT_FILE_LIMIT = 5000


class FileTime(ctypes.Structure):
    _fields_ = [('dwLowDateTime', ctypes.c_uint32), ('dwHighDateTime', ctypes.c_uint32)]


class RmUniqueProcess(ctypes.Structure):
    """RM_UNIQUE_PROCESS; RmProcessInfo embeds it, so the PID is not at offset 0."""
    _fields_ = [('dwProcessId', ctypes.c_uint32), ('ProcessStartTime', FileTime)]


class RmProcessInfo(ctypes.Structure):
    """One RM_PROCESS_INFO row.

    The shipped ABI is 668 bytes wide: with three processes holding the same
    file, their PIDs land at 0, 668 and 1336, and the fields sit at 0 (PID),
    4-12 (start time), 12-524 (app name), 524-652 (service name), 652 (type),
    656 (status) and a trailing 8 bytes (TS session id + reserved). The
    published header text describes a wider tail than the installed runtime
    actually uses, so the stride is pinned to the measured layout: a wrong
    stride would silently misread every row after the first.
    """
    _fields_ = [
        ('Process', RmUniqueProcess),
        ('strAppName', ctypes.c_wchar * (CCH_RM_MAX_APP_NAME + 1)),
        ('strServiceShortName', ctypes.c_wchar * (CCH_RM_MAX_SVC_NAME + 1)),
        ('ApplicationType', ctypes.c_uint32),
        ('AppStatus', ctypes.c_uint32),
        ('dwTSSessionId', ctypes.c_uint32),
        ('reserved', ctypes.c_uint32),
    ]


def _on_windows():
    return os.name == 'nt'


@functools.lru_cache(maxsize=1)
def _kernel32():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                   ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                   ctypes.c_void_p]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_uint32
    return kernel


@functools.lru_cache(maxsize=1)
def _restart_manager():
    rm = ctypes.WinDLL('rstrtmgr', use_last_error=True)
    rm.RmStartSession.argtypes = [ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint32,
                                  ctypes.POINTER(ctypes.c_wchar)]
    rm.RmStartSession.restype = ctypes.c_uint32
    rm.RmRegisterResources.argtypes = [ctypes.c_uint32, ctypes.c_uint32,
                                       ctypes.POINTER(ctypes.c_wchar_p),
                                       ctypes.c_uint32, ctypes.POINTER(ctypes.c_wchar_p),
                                       ctypes.c_uint32, ctypes.POINTER(ctypes.c_wchar_p)]
    rm.RmRegisterResources.restype = ctypes.c_uint32
    rm.RmGetList.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
                             ctypes.POINTER(ctypes.c_uint32),
                             ctypes.POINTER(RmProcessInfo),
                             ctypes.POINTER(ctypes.c_uint32)]
    rm.RmGetList.restype = ctypes.c_uint32
    rm.RmEndSession.argtypes = [ctypes.c_uint32]
    rm.RmEndSession.restype = ctypes.c_uint32
    return rm


def _text(path):
    """Return a str path or None; keeps ctypes from silently cutting at NUL."""
    try:
        value = os.fspath(path)
    except TypeError:
        return None
    if not isinstance(value, str) or not value or '\x00' in value:
        return None
    return value


def _open_for_delete(text):
    """Try the DELETE probe once and return the Win32 error (0 on success)."""
    kernel = _kernel32()
    handle = kernel.CreateFileW(text, DELETE | FILE_READ_ATTRIBUTES, FILE_SHARE_ALL,
                                None, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None)
    if handle in (None, INVALID_HANDLE_VALUE):
        return ctypes.get_last_error()
    try:
        return ERROR_SUCCESS
    finally:
        # The probe must not keep the object open: close before classifying so
        # the caller's own state is unchanged even when this call fails.
        kernel.CloseHandle(handle)


def _classify(error):
    if error == ERROR_SUCCESS:
        return {'deletable': True, 'reason': 'ok', 'winerror': None}
    if error == ERROR_SHARING_VIOLATION:
        return {'deletable': False, 'reason': 'in use (sharing violation)', 'winerror': error}
    if error == ERROR_LOCK_VIOLATION:
        return {'deletable': False, 'reason': 'in use (lock violation)', 'winerror': error}
    if error == ERROR_ACCESS_DENIED:
        return {'deletable': False, 'reason': 'access denied', 'winerror': error}
    if error in (ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND):
        # A missing object is not locked, but it also cannot be deleted later.
        return {'deletable': False, 'reason': 'not found', 'winerror': error}
    if error == ERROR_FILENAME_EXCED_RANGE:
        return {'deletable': None, 'reason': 'path exceeds the Win32 path limit',
                'winerror': error}
    return {'deletable': None, 'reason': f'open failed with winerror {error}',
            'winerror': error}


def probe_deletable(path):
    """Ask Windows whether DELETE access can be opened right now.

    Only FILE_READ_ATTRIBUTES and DELETE access is requested; no content is
    read or written and the handle is closed immediately. A success says the
    object was deletable at that instant, nothing about the next instant.
    """
    if not _on_windows():
        return {'deletable': None, 'reason': 'unsupported platform', 'winerror': None}
    text = _text(path)
    if text is None:
        return {'deletable': None, 'reason': 'invalid path', 'winerror': None}
    try:
        error = _open_for_delete(text)
    except OSError as exc:
        # A missing kernel32 binding or a ctypes failure is unknown, not safe.
        return {'deletable': None, 'reason': f'open failed: {exc}', 'winerror': None}
    return _classify(error)


def _expand_directory(root, budget):
    """Return (files, truncated, problems) for one directory.

    Restart Manager refuses directories with ERROR_ACCESS_DENIED(5), so the
    directory itself is probed separately and only its files are registered.
    """
    files, problems, truncated = [], [], False
    walker = os.walk(root, onerror=lambda exc: problems.append(
        f'cannot list {exc.filename}: {exc}'))
    for current, _dirs, names in walker:
        names.sort()  # Stable output makes reports and tests reproducible.
        for name in names:
            if len(files) >= budget:
                truncated = True
                break
            full = os.path.join(current, name)
            if len(full) >= RM_MAX_PATH:
                problems.append(f'skipped path at or over the Win32 MAX_PATH limit: {full}')
                continue
            files.append(full)
        if truncated:
            break
    return files, truncated, problems


def _collect_files(entries, limit, issues):
    """Flatten paths and directories into registerable files."""
    files, seen = [], set()
    remaining = limit
    for entry in entries:
        text = _text(entry)
        if text is None:
            issues.append('skipped empty or invalid path value')
            continue
        full = os.path.abspath(text)
        if len(full) >= RM_MAX_PATH:
            # Restart Manager rejects longer paths, and one bad path must not
            # poison the rest of the registration batch.
            issues.append(f'skipped path at or over the Win32 MAX_PATH limit: {full}')
            continue
        if os.path.isdir(full):
            if remaining <= 0:
                issues.append(f'skipped directory, file limit reached: {full}')
                continue
            expanded, truncated, problems = _expand_directory(full, remaining)
            remaining -= len(expanded)
            issues.extend(problems)
            if truncated:
                issues.append(f'directory expansion stopped at the {limit} file limit: {full}')
            candidates = expanded
        else:
            if not os.path.exists(full):
                issues.append(f'skipped missing path: {full}')
                continue
            candidates = [full]
        for candidate in candidates:
            key = os.path.normcase(candidate)
            if key in seen:
                continue
            seen.add(key)
            files.append(candidate)
    return files


def _register(rm, session, files, issues):
    for start in range(0, len(files), RM_REGISTER_CHUNK):
        chunk = files[start:start + RM_REGISTER_CHUNK]
        buffers = [ctypes.create_unicode_buffer(item) for item in chunk]
        array = (ctypes.c_wchar_p * len(chunk))(
            *[ctypes.cast(buffer, ctypes.c_wchar_p) for buffer in buffers])
        code = rm.RmRegisterResources(session, len(chunk), array, 0, None, 0, None)
        if code != ERROR_SUCCESS:
            issues.append(f'RmRegisterResources failed for {len(chunk)} path(s) '
                          f'with winerror {code}')


def _processes(rm, session, issues):
    """Query holders; returns None when the query failed."""
    needed = ctypes.c_uint32(0)
    count = ctypes.c_uint32(0)
    reason = ctypes.c_uint32(0)
    code = rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), None,
                        ctypes.byref(reason))
    attempts = 0
    while code == ERROR_MORE_DATA:
        if attempts >= RM_RETRY_LIMIT:
            issues.append(f'RmGetList kept reporting more data after {RM_RETRY_LIMIT} retries')
            return None
        attempts += 1
        size = max(int(needed.value), 1)
        rows = (RmProcessInfo * size)()
        count = ctypes.c_uint32(size)
        code = rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), rows,
                            ctypes.byref(reason))
        if code == ERROR_SUCCESS:
            return [_summarize(row) for row in rows[:min(int(count.value), size)]]
    if code != ERROR_SUCCESS:
        issues.append(f'RmGetList failed with winerror {code}')
        return None
    return []


def _summarize(row):
    # ``restartable`` is advisory only: it says the Restart Manager classes the
    # holder as an application or service it knows how to stop and start, not
    # that stopping it is safe. This module reports it; it never acts on it.
    restartable = int(row.ApplicationType) in RM_RESTARTABLE_TYPES
    return {'pid': int(row.Process.dwProcessId), 'app': row.strAppName,
            'service': row.strServiceShortName, 'restartable': restartable}


def restart_manager_locks(paths, *, limit=RM_DEFAULT_FILE_LIMIT):
    """List applications/services the Restart Manager reports as holding paths.

    Directories are expanded to files first. Paths at or over MAX_PATH,
    missing paths and directories over ``limit`` are recorded in ``issues``
    with ``complete=False`` instead of failing the whole call. ``ok`` only
    says the Restart Manager session ran; it does not mean no lock can exist
    after this call.
    """
    if not _on_windows():
        return {'ok': False, 'processes': [], 'issues': ['unsupported platform'],
                'complete': False}
    issues = []
    if isinstance(paths, (str, bytes)):
        paths = [paths]
    try:
        entries = list(paths)
    except TypeError:
        return {'ok': False, 'processes': [], 'issues': ['paths must be an iterable of paths'],
                'complete': False}
    files = _collect_files(entries, limit, issues)
    try:
        rm = _restart_manager()
    except (OSError, AttributeError) as exc:
        issues.append(f'Restart Manager unavailable: {exc}')
        return {'ok': False, 'processes': [], 'issues': issues, 'complete': False}
    session = ctypes.c_uint32(0)
    # RmStartSession requires a real 33-character key buffer; None fails with
    # ERROR_BAD_ARGUMENTS(160). The key itself is discarded.
    key = ctypes.create_unicode_buffer(RM_SESSION_KEY_LENGTH)
    code = rm.RmStartSession(ctypes.byref(session), 0, key)
    if code != ERROR_SUCCESS:
        issues.append(f'RmStartSession failed with winerror {code}')
        return {'ok': False, 'processes': [], 'issues': issues, 'complete': False}
    try:
        if files:
            _register(rm, session.value, files, issues)
        rows = _processes(rm, session.value, issues)
    finally:
        # Always end the session, even when registration or the query failed.
        ended = rm.RmEndSession(session.value)
        if ended != ERROR_SUCCESS:
            issues.append(f'RmEndSession failed with winerror {ended}')
    if rows is None:
        return {'ok': False, 'processes': [], 'issues': issues, 'complete': False}
    return {'ok': True, 'processes': rows, 'issues': issues, 'complete': not issues}


def blockers(path):
    """Combine the DELETE probe with Restart Manager evidence for one path.

    The probe covers the object itself (files and directories); Restart
    Manager is only asked about the files a directory expands to. A missing
    path short-circuits, because nothing can hold a handle to it. An unknown
    probe result makes ``complete`` False: the caller must not read that as
    "safe to delete".
    """
    probe = probe_deletable(path)
    if probe['deletable'] is None:
        return {'deletable': None, 'reason': probe['reason'], 'processes': [],
                'issues': [f'DELETE probe unavailable: {probe["reason"]}'],
                'complete': False}
    if probe['winerror'] in (ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND):
        return {'deletable': probe['deletable'], 'reason': probe['reason'],
                'processes': [], 'issues': [], 'complete': True}
    held = restart_manager_locks([path])
    return {'deletable': probe['deletable'], 'reason': probe['reason'],
            'processes': held['processes'], 'issues': held['issues'],
            'complete': held['complete']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('paths', nargs='+', help='files or directories to inspect')
    args = parser.parse_args()
    results = [{'path': str(path), **blockers(path)} for path in args.paths]
    print(json.dumps(results, ensure_ascii=True, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
