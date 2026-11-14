"""Verified Recycle Bin backend.

What this module proves, and how
--------------------------------
The old backend could only observe "the original path is gone". That is not a
proof of recycling: a permanent delete looks identical. This module instead
keeps a handle on the object across the whole operation and then asks the
kernel *where the object is now*:

    \\\\?\\C:\\$RECYCLE.BIN\\<sid>\\$R...   -> RECYCLED (recoverable)
    \\\\?\\C:\\$Extend\\$Deleted\\...        -> PERMANENTLY_DELETED (alarm)
    the original path                      -> NOT_DELETED

Only the first outcome is reported as success. The second one is impossible to
mistake because NTFS moves permanently deleted files into $Extend\\$Deleted
while a handle is still open on them; the path is read from the handle itself,
so it cannot be spoofed by a path string the caller supplied.

Two independent guards back this up:

1. ``IFileOperationProgressSink::PreDeleteItem`` returns a failure unless the
   operation flags carry the recycle bit (0x80). ``FOFX_RECYCLEONDELETE`` asks
   the Shell to recycle, but it is documented as best-effort; without this veto
   the Shell silently falls back to permanent deletion on volumes that have no
   Recycle Bin (verified: a SUBST drive only set 0x200, not 0x280).
2. The volume is profiled before the call: the per-user ``$RECYCLE.BIN\\<sid>``
   must exist, ``NukeOnDelete`` must be 0 and the payload must fit the bin's
   configured capacity. Those are the conditions under which Windows degrades
   to permanent deletion.

What this module does NOT prove
-------------------------------
* It does not prove the *contents* of a directory were unchanged; it proves the
  directory object itself is the same one that was inspected.
* It does not prove the object will still be restorable later: anything may
  empty the Recycle Bin afterwards.
* The hold denies writers but not readers; a file may still be read (and its
  access time bumped) during the window.
* Callback evidence and the $I record are local artefacts, not authorization.
"""
import ctypes
import ntpath
from ctypes import wintypes
import os
import struct
import sys
import time
import uuid

SCHEMA = 'disk-cleanup-recycle-proof/v1'

# FILEOP and IFileOperation flags. FOF_WANTNUKEWARNING is the one that asks the
# Shell to tell us about a permanent delete; with FOF_NOERRORUI the answer is
# silence, which is why the sink veto below is the actual guard.
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_NOERRORUI = 0x0400
FOF_WANTNUKEWARNING = 0x4000
FOFX_RECYCLEONDELETE = 0x00080000
FOFX_EARLYFAILURE = 0x00100000
OPERATION_FLAGS = (FOF_NOERRORUI | FOF_WANTNUKEWARNING | FOFX_EARLYFAILURE
                   | FOFX_RECYCLEONDELETE)

# The bit the Shell sets in PreDeleteItem when the item really goes to the bin.
CALLBACK_RECYCLE_BIT = 0x80

FILE_READ_ATTRIBUTES = 0x0080
DELETE = 0x00010000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ERROR_SHARING_VIOLATION = 32
ERROR_ACCESS_DENIED = 5

CLSID_FILE_OPERATION = '3ad05575-8857-4850-9277-11b85bdb8e09'
IID_IFILE_OPERATION = '947aab5f-0a5c-4c13-b4d6-4bf7836fc9f8'
IID_ISHELL_ITEM = '43826d1e-e718-42ee-bc55-a1e261c37bfe'

SUPPORTED_FILESYSTEMS = {'NTFS', 'REFS'}


class BackendUnavailable(Exception):
    """The platform cannot produce verified evidence. Never downgraded silently."""


def _failed(hr):
    return bool(int(hr) & 0x80000000)


def _hr(hr):
    return f'0x{int(hr) & 0xffffffff:08X}'


def _explain_shell_failure(hr):
    """Translate a Copy Engine failure into the likely operator-facing cause.

    The 0x8027xxxx family is the Copy Engine; Windows uses it for the move into
    the Recycle Bin. It reports a sharing violation when any handle on the
    object refuses FILE_SHARE_DELETE, which is the single most common reason a
    cache cannot be trashed while an application is still running.
    """
    code = int(hr) & 0xffffffff
    if code == 0x80270027:
        return ' (the Shell could not move it: the object is in use or locked)'
    if code >> 16 == 0x8027:
        return ' (Copy Engine refused the move; the object may be in use, ' \
               'read-only or blocked by policy)'
    return ''


class _GUID(ctypes.Structure):
    _fields_ = [('data1', ctypes.c_uint32), ('data2', ctypes.c_uint16),
                ('data3', ctypes.c_uint16), ('data4', ctypes.c_ubyte * 8)]


def _guid(text):
    return _GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


def _method(pointer, index, restype, *types):
    """Bind a COM vtable slot. Indexes verified against the published vtables."""
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *types)(vtable[index])


class _FILE_ID_INFO(ctypes.Structure):
    _fields_ = [('volume_serial', ctypes.c_ulonglong), ('file_id', ctypes.c_ubyte * 16)]


class _FILE_STANDARD_INFO(ctypes.Structure):
    _fields_ = [('allocation_size', ctypes.c_longlong),
                ('end_of_file', ctypes.c_longlong),
                ('links', wintypes.DWORD),
                ('delete_pending', wintypes.BOOL),
                ('directory', wintypes.BOOL)]


class _FILE_BASIC_INFO(ctypes.Structure):
    _fields_ = [('creation_time', ctypes.c_longlong),
                ('last_access_time', ctypes.c_longlong),
                ('last_write_time', ctypes.c_longlong),
                ('change_time', ctypes.c_longlong),
                ('attributes', wintypes.DWORD)]


if os.name == 'nt':
    _k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _ole32 = ctypes.WinDLL('ole32')
    _shell32 = ctypes.WinDLL('shell32')

    _k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.c_void_p]
    _k32.CreateFileW.restype = ctypes.c_void_p
    _k32.CloseHandle.argtypes = [ctypes.c_void_p]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.GetFinalPathNameByHandleW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR,
                                               wintypes.DWORD, wintypes.DWORD]
    _k32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    _k32.GetFileInformationByHandleEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                                  ctypes.c_void_p, wintypes.DWORD]
    _k32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    _k32.GetVolumeInformationW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR,
                                           wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                           ctypes.POINTER(wintypes.DWORD),
                                           ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR,
                                           wintypes.DWORD]
    _k32.GetVolumeInformationW.restype = wintypes.BOOL
    _k32.GetVolumeNameForVolumeMountPointW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR,
                                                       wintypes.DWORD]
    _k32.GetVolumeNameForVolumeMountPointW.restype = wintypes.BOOL
else:  # pragma: no cover - exercised only by the non-Windows degradation test
    _k32 = _ole32 = _shell32 = None


# ------------------------------------------------------------------ handle layer

def open_hold(path):
    """Open the object without allowing writers, but allowing the Shell to move it.

    Three properties, each load-bearing:

    * ``FILE_READ_ATTRIBUTES`` is enough to read the file ID, so the handle stays
      read-only in spirit.
    * Omitting ``FILE_SHARE_WRITE`` stops the payload from changing during the
      operation (a rename by another process would still be blocked separately
      by the parent-chain pins that ``clean.py`` holds).
    * Asking for ``DELETE`` access looks odd but is what turns the open itself
      into a real "may this be deleted right now" probe: if any other handle
      holds the object without ``FILE_SHARE_DELETE``, Windows fails here with
      ERROR_SHARING_VIOLATION instead of failing after the Shell has already
      started moving things. Holding DELETE access does not delete anything and
      was verified not to block the Shell's own recycle.
    """
    if os.name != 'nt':
        raise BackendUnavailable('Windows only')
    handle = _k32.CreateFileW(str(path), DELETE | FILE_READ_ATTRIBUTES,
                              FILE_SHARE_READ | FILE_SHARE_DELETE, None,
                              OPEN_EXISTING,
                              FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
                              None)
    if handle in (INVALID_HANDLE_VALUE, None):
        raise OSError(ctypes.get_last_error(), 'CreateFileW failed')
    return handle


def close_hold(handle):
    if handle:
        _k32.CloseHandle(handle)


def final_path(handle):
    """Where is this object now? ``None`` when the kernel cannot say."""
    buffer = ctypes.create_unicode_buffer(32768)
    written = _k32.GetFinalPathNameByHandleW(handle, buffer, 32768, 0)
    if not written:
        return None
    return buffer.value


def file_identity(handle):
    info = _FILE_ID_INFO()
    if not _k32.GetFileInformationByHandleEx(handle, 18, ctypes.byref(info),
                                             ctypes.sizeof(info)):
        raise OSError(ctypes.get_last_error(), 'FileIdInfo failed')
    return {'volume_serial': info.volume_serial,
            'file_id': bytes(info.file_id).hex()}


def identity_of(path):
    """Identity of the object at ``path`` now: evidence, not a lock.

    The handle is closed before returning, so a caller cannot mistake this for
    the hold that ``recycle`` keeps across the operation.
    """
    handle = open_hold(path)
    try:
        return file_identity(handle)
    finally:
        close_hold(handle)


# FILETIME counts 100 ns ticks from 1601; Unix time starts 11644473600 s later.
_FILETIME_EPOCH = 116444736000000000


def held_size(handle):
    """Apparent size read through the held handle, not through the path."""
    info = _FILE_STANDARD_INFO()
    if not _k32.GetFileInformationByHandleEx(handle, 1, ctypes.byref(info),
                                             ctypes.sizeof(info)):
        raise OSError(ctypes.get_last_error(), 'FileStandardInfo failed')
    return {'allocation_size': info.allocation_size, 'end_of_file': info.end_of_file,
            'links': info.links, 'delete_pending': bool(info.delete_pending),
            'directory': bool(info.directory)}


def _basic_info(handle):
    info = _FILE_BASIC_INFO()
    if not _k32.GetFileInformationByHandleEx(handle, 0, ctypes.byref(info),
                                             ctypes.sizeof(info)):
        raise OSError(ctypes.get_last_error(), 'FileBasicInfo failed')
    return info


def held_mtime_ns(handle):
    """Last-write time of the held object, in nanoseconds since the Unix epoch."""
    return (_basic_info(handle).last_write_time - _FILETIME_EPOCH) * 100


def held_attributes(handle):
    """DOS attributes of the held object, read through the handle."""
    return _basic_info(handle).attributes


def is_reparse_point(handle):
    """True when the held object itself is a link/junction, not its target.

    ``open_hold`` passes ``FILE_FLAG_OPEN_REPARSE_POINT``, so the handle names
    the object the path spells. Without that flag this check would describe the
    target and a junction swapped in after the plan would read as an ordinary
    directory.
    """
    return bool(held_attributes(handle) & FILE_ATTRIBUTE_REPARSE_POINT)


def _expected_mismatch(handle, expected):
    """Compare the held object with the evidence the plan rested on.

    The path can be replaced between the last check and the hold. Everything
    compared here is read through the handle, so the answer describes the object
    that would actually be moved, not whatever the path points at now.
    """
    if not expected:
        return None
    identity = expected.get('identity')
    if identity:
        try:
            current = file_identity(handle)
        except OSError:
            return None
        pair = (identity.get('volume_serial'), identity.get('file_id'))
        if pair != (current['volume_serial'], current['file_id']):
            return 'it is not the object the plan inspected (file identity differs)'
    try:
        info = held_size(handle)
    except OSError:
        return None
    is_directory = expected.get('is_directory')
    if is_directory is None:
        is_directory = info['directory']
    size = expected.get('size_bytes')
    if isinstance(size, int) and not is_directory and info['end_of_file'] != size:
        return f'its size changed from {size} to {info["end_of_file"]} bytes'
    mtime = expected.get('mtime_ns')
    if isinstance(mtime, int) and mtime > 0:
        try:
            actual = held_mtime_ns(handle)
        except OSError:
            actual = None
        if actual is not None and actual != mtime:
            return 'its modification time changed since the plan was checked'
    return None


# ------------------------------------------------------------------ volume layer

def _current_sid():
    """Return the SID string of the current user (needed for the bin path)."""
    try:
        import win32api  # optional, only used when present
        return win32api.GetUserNameEx(3) if False else None
    except Exception:
        return None


def _sid_from_token():
    """Resolve the current user SID without pywin32.

    The Recycle Bin is per-SID, so the path we check must be ours. Using the
    token avoids depending on the localized account name.
    """
    if os.name != 'nt':
        return None
    advapi32 = ctypes.WinDLL('advapi32', use_last_error=True)
    k32 = _k32
    k32.OpenProcessToken.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    k32.OpenProcessToken.restype = wintypes.BOOL
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    TOKEN_QUERY = 0x0008
    token = ctypes.c_void_p()
    if not k32.OpenProcessToken(k32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        return None
    try:
        size = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not size.value:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            return None
        # TOKEN_USER -> SID_AND_ATTRIBUTES { PSID Sid; DWORD Attributes; }
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
        advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(text)):
            return None
        try:
            return text.value
        finally:
            k32.LocalFree(text)
    finally:
        k32.CloseHandle(token)


def _bitbucket_for_drive(drive):
    """Read the per-volume Recycle Bin policy that Windows itself consults."""
    import winreg
    guid = ctypes.create_unicode_buffer(64)
    try:
        ok = _k32.GetVolumeNameForVolumeMountPointW(drive + '\\', guid, 64)
    except Exception:
        ok = False
    if not ok:
        return {'volume_guid': None, 'resolved': False}
    volume = guid.value.rstrip('\\')
    result = {'volume_guid': volume, 'resolved': True,
              'max_capacity_mb': None, 'nuke_on_delete': None}
    key_path = (r'Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume'
                '\\' + volume)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            for name in ('MaxCapacity', 'NukeOnDelete'):
                try:
                    value, _kind = winreg.QueryValueEx(key, name)
                    result['max_capacity_mb' if name == 'MaxCapacity'
                           else 'nuke_on_delete'] = value
                except OSError:
                    pass
    except OSError:
        # Absent key means "Windows default policy", not "unresolved volume".
        pass
    return result


_EXPLORER_POLICY_KEY = r'Software\Microsoft\Windows\CurrentVersion\Policies\Explorer'


def _no_recycle_policy():
    """True when Group Policy makes every delete permanent for this user.

    Reads NoRecycleFiles from both policy hives because either can be set by an
    administrator. Returns None when the registry cannot be consulted at all, so
    callers can distinguish "policy checked, absent" from "policy unknown".
    """
    try:
        import winreg
    except ImportError:
        return None
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(root, _EXPLORER_POLICY_KEY) as key:
                value, _kind = winreg.QueryValueEx(key, 'NoRecycleFiles')
        except OSError:
            continue
        try:
            if int(value) == 1:
                return True
        except (TypeError, ValueError):
            return None
    return False


def _capability_issues(filesystem, supported, sid, bin_root, policy, no_recycle):
    """Every reason this volume cannot be promised recycle-only behavior."""
    issues = []
    if not filesystem:
        issues.append('volume information unavailable')
    elif not supported:
        issues.append(f'filesystem {filesystem} is not verified for recycle-only behavior')
    if not sid:
        issues.append('cannot resolve the current user SID')
    if sid and bin_root and not os.path.isdir(bin_root):
        issues.append(f'no per-user Recycle Bin at {bin_root}')
    if no_recycle:
        issues.append('group policy NoRecycleFiles=1: deletes bypass the Recycle Bin')
    if policy.get('nuke_on_delete') == 1:
        issues.append('NukeOnDelete=1: this volume permanently deletes on trash')
    if policy.get('max_capacity_mb') == 0:
        issues.append('MaxCapacity=0: this volume is configured to delete permanently')
    return issues


def volume_profile(drive):
    """Everything we need to know before promising recycle-only behavior."""
    if os.name != 'nt':
        raise BackendUnavailable('Windows only')
    filesystem = None
    serial = wintypes.DWORD()
    max_length = wintypes.DWORD()
    flags = wintypes.DWORD()
    name = ctypes.create_unicode_buffer(64)
    ok = _k32.GetVolumeInformationW(drive + '\\', None, 0, ctypes.byref(serial),
                                    ctypes.byref(max_length), ctypes.byref(flags), name, 64)
    if ok:
        filesystem = name.value
    sid = _sid_from_token()
    bin_root = drive + '\\$RECYCLE.BIN\\' + sid if sid else None
    policy = _bitbucket_for_drive(drive)
    capacity = policy.get('max_capacity_mb')
    free = None
    try:
        import shutil
        free = shutil.disk_usage(drive + '\\').free
    except OSError:
        free = None
    supported = filesystem in SUPPORTED_FILESYSTEMS if filesystem else False
    no_recycle = _no_recycle_policy()
    issues = _capability_issues(filesystem, supported, sid, bin_root, policy, no_recycle)
    return {
        'schema': SCHEMA,
        'drive': drive,
        'filesystem': filesystem,
        'supported_filesystem': supported,
        'sid': sid,
        'recycle_bin_root': bin_root,
        'recycle_bin_root_exists': bool(bin_root and os.path.isdir(bin_root)),
        'volume_guid': policy.get('volume_guid'),
        'volume_guid_resolved': policy.get('resolved'),
        'max_capacity_mb': capacity,
        'nuke_on_delete': policy.get('nuke_on_delete'),
        'no_recycle_policy': no_recycle,
        'free_bytes': free,
        'verified_capable': not issues,
        'issues': issues,
    }


def fits_capacity(profile, size_bytes):
    """Would the Shell have to drop the payload to fit the bin?

    Windows compares the *file size* against MaxCapacity and prompts to delete
    permanently when it does not fit. We refuse instead of letting the prompt be
    answered silently by FOF_NOERRORUI.
    """
    capacity = profile.get('max_capacity_mb')
    if capacity == 0:
        return False, 'MaxCapacity=0: this volume is configured to delete permanently'
    if capacity is None:
        # No registry override: Windows applies its own default, which we cannot
        # read without guessing. The PreDeleteItem veto is the guard here -- if
        # the Shell decides an object is too large and switches to a permanent
        # delete, the callback aborts the whole operation.
        return True, ('no explicit MaxCapacity; the Shell default applies and the callback veto blocks a permanent delete')
    if size_bytes > capacity * 1024 * 1024:
        return False, f'size {size_bytes} exceeds Recycle Bin capacity {capacity} MB'
    return True, 'fits'


# ---------------------------------------------------------------------- sink

class _Observer:
    """Minimal IFileOperationProgressSink that can veto a non-recycle delete.

    It implements only QueryInterface/AddRef/Release, PreDeleteItem and
    PostDeleteItem; every other slot returns a failure HRESULT so the Shell
    cannot quietly use a path we did not audit.
    """

    def __init__(self):
        if os.name != 'nt':
            raise BackendUnavailable('Windows only')
        self.events = []
        self.refs = 1
        self.callbacks = []
        P, D, H, W = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int32, ctypes.c_wchar_p
        iid = uuid.UUID('04b0f1a7-9490-44bc-96e1-4296a31252e2').bytes_le
        iid_unknown = uuid.UUID('00000000-0000-0000-c000-000000000046').bytes_le
        E_ABORT, E_NOINTERFACE = -2147467260, -2147467262

        def bind(types, function, result=H):
            def guarded(*args):
                try:
                    return function(*args)
                except BaseException:  # noqa: BLE001 - never let a callback abort silently
                    self.events.append({'event': 'callback_error'})
                    return E_ABORT
            callback = ctypes.WINFUNCTYPE(result, P, *types)(guarded)
            self.callbacks.append(callback)
            return ctypes.cast(callback, P).value

        def query(this, requested_iid, output):
            if not output:
                return E_NOINTERFACE
            ctypes.cast(output, ctypes.POINTER(P))[0] = None
            raw = ctypes.string_at(requested_iid, 16)
            if raw not in (iid, iid_unknown):
                return E_NOINTERFACE
            ctypes.cast(output, ctypes.POINTER(P))[0] = self.pointer
            self.refs += 1
            return 0

        def add_ref(this):
            self.refs += 1
            return self.refs

        def release(this):
            self.refs = max(0, self.refs - 1)
            return self.refs

        def pre_delete(this, flags, item):
            recycle_requested = bool(flags & CALLBACK_RECYCLE_BIT)
            self.events.append({'event': 'pre_delete', 'flags': flags,
                                'recycle_requested': recycle_requested})
            return 0 if recycle_requested else E_ABORT

        def post_delete(this, flags, item, hr, recycled_item):
            # A non-null newly created item is the documented signal that the
            # object now lives in the Recycle Bin. It is evidence, not a gate.
            self.events.append({'event': 'post_delete', 'flags': flags,
                                'hresult': hr,
                                'recycle_item_provided': bool(recycled_item)})
            return 0

        ok = lambda *a: 0
        reject = lambda *a: E_ABORT
        slots = [bind((P, P), query), bind((), add_ref, D), bind((), release, D),
                 bind((), ok), bind((H,), ok),
                 bind((D, P, W), reject), bind((D, P, W, H, P), reject),
                 bind((D, P, P, W), reject), bind((D, P, P, W, H, P), reject),
                 bind((D, P, P, W), reject), bind((D, P, P, W, H, P), reject),
                 bind((D, P), pre_delete), bind((D, P, H, P), post_delete),
                 bind((D, P, W), reject), bind((D, P, W, W, D, H, P), reject),
                 bind((D, D), ok), bind((), ok), bind((), ok), bind((), ok)]
        self.vtable = (P * len(slots))(*slots)

        class Interface(ctypes.Structure):
            _fields_ = [('vtable', ctypes.POINTER(P))]

        self.interface = Interface(self.vtable)
        self.pointer = ctypes.cast(ctypes.pointer(self.interface), P)


# ------------------------------------------------------------------- operation

def _recycle_once(path, observer):
    """Queue one DeleteItem on a fresh IFileOperation and run it."""
    initialized = _ole32.CoInitializeEx(None, 2)
    if _failed(initialized) and _hr(initialized) != '0x80010106':
        raise BackendUnavailable(f'CoInitializeEx failed: {_hr(initialized)}')
    operation = ctypes.c_void_p()
    try:
        hr = _ole32.CoCreateInstance(ctypes.byref(_guid(CLSID_FILE_OPERATION)), None, 1,
                                     ctypes.byref(_guid(IID_IFILE_OPERATION)),
                                     ctypes.byref(operation))
        if _failed(hr):
            raise BackendUnavailable(f'CoCreateInstance failed: {_hr(hr)}')
        hr = _method(operation, 5, ctypes.c_long, wintypes.DWORD)(operation, OPERATION_FLAGS)
        if _failed(hr):
            return {'status': 'FAILED', 'detail': f'SetOperationFlags failed: {_hr(hr)}'}
        item = ctypes.c_void_p()
        _shell32.SHCreateItemFromParsingName.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p,
                                                         ctypes.c_void_p,
                                                         ctypes.POINTER(ctypes.c_void_p)]
        _shell32.SHCreateItemFromParsingName.restype = ctypes.c_long
        hr = _shell32.SHCreateItemFromParsingName(str(path), None,
                                                  ctypes.byref(_guid(IID_ISHELL_ITEM)),
                                                  ctypes.byref(item))
        if _failed(hr):
            return {'status': 'FAILED', 'detail': f'SHCreateItemFromParsingName failed: {_hr(hr)}'}
        hr = _method(operation, 18, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)(
            operation, item, observer.pointer)
        if _failed(hr):
            return {'status': 'FAILED', 'detail': f'DeleteItem failed: {_hr(hr)}'}
        perform = _method(operation, 21, ctypes.c_long)(operation)
        aborted = wintypes.BOOL()
        abort_hr = _method(operation, 22, ctypes.c_long, ctypes.POINTER(wintypes.BOOL))(
            operation, ctypes.byref(aborted))
        if _failed(perform):
            return {'status': 'FAILED',
                    'detail': f'PerformOperations failed: {_hr(perform)}'
                              + _explain_shell_failure(perform)}
        if _failed(abort_hr):
            return {'status': 'UNKNOWN', 'detail': f'GetAnyOperationsAborted failed: {_hr(abort_hr)}'}
        if aborted.value:
            return {'status': 'FAILED', 'detail': 'operation was aborted by the Shell'}
        return {'status': 'QUEUED'}
    finally:
        if operation:
            _method(operation, 2, wintypes.ULONG)(operation)


def _classify(final, original):
    """Read the verdict out of the handle's current path, not out of a guess."""
    if final is None:
        return 'UNKNOWN', 'the kernel cannot report where the object went'
    lowered = final.lower().replace('/', '\\')
    _, tail = ntpath.splitdrive(lowered)
    parts = tail.strip('\\').split('\\')
    if (len(parts) >= 3 and parts[0] == '$recycle.bin'
            and parts[1].startswith('s-1-') and parts[2].startswith('$r')
            and len(parts[2]) > 2):
        return 'RECYCLED', 'the object is inside the per-user Recycle Bin'
    if len(parts) >= 3 and parts[:2] == ['$extend', '$deleted']:
        return 'PERMANENTLY_DELETED', 'NTFS moved the object into $Extend\\$Deleted'
    if os.path.normcase(final.replace('\\\\?\\', '')) == os.path.normcase(str(original)):
        return 'NOT_DELETED', 'the object still lives at its original path'
    return 'UNKNOWN', 'the object moved somewhere that is neither the bin nor $Deleted'


def recycle(path, *, size_bytes=0, expected=None):
    """Attempt a recycle and return proof-carrying evidence.

    Returns a dict with ``status`` in RECYCLED / REFUSED / FAILED / UNKNOWN.
    Only RECYCLED is a success. Every other status is reported verbatim; this
    function never upgrades an uncertain outcome.
    """
    path = os.path.abspath(str(path))
    drive = os.path.splitdrive(path)[0].upper()
    outcome = {'schema': SCHEMA, 'path': path, 'drive': drive,
               'status': 'UNKNOWN', 'detail': '', 'proof': None,
               'recycle_bin': None, 'events': [], 'started_unix': time.time()}
    if os.name != 'nt':
        outcome.update(status='REFUSED', detail='Windows only')
        return outcome
    try:
        profile = volume_profile(drive)
    except BackendUnavailable as exc:
        outcome.update(status='REFUSED', detail=str(exc))
        return outcome
    outcome['volume'] = profile
    if not profile['verified_capable']:
        outcome.update(status='REFUSED',
                       detail='volume is not verified recycle-capable: '
                              + '; '.join(profile['issues']))
        return outcome
    fits, why = fits_capacity(profile, size_bytes)
    if not fits:
        outcome.update(status='REFUSED', detail=f'Recycle Bin would not hold it: {why}')
        return outcome

    try:
        handle = open_hold(path)
    except OSError as exc:
        code = getattr(exc, 'winerror', None) or getattr(exc, 'errno', None)
        reason = 'in use' if code == ERROR_SHARING_VIOLATION else (
            'access denied' if code == ERROR_ACCESS_DENIED else str(exc))
        outcome.update(status='REFUSED', detail=f'cannot hold the object: {reason}')
        return outcome

    try:
        if is_reparse_point(handle):
            outcome.update(
                status='REFUSED',
                detail='the object at that path is now a link or junction; '
                       'the plan inspected a real object, not a redirect')
            return outcome
        before = file_identity(handle)
        outcome['identity_before'] = before
        original_final = final_path(handle)
        outcome['path_before'] = original_final
        mismatch = _expected_mismatch(handle, expected)
        if mismatch:
            outcome.update(status='REFUSED',
                           detail=f'the object changed after the plan was checked: {mismatch}')
            return outcome
        observer = _Observer()
        result = _recycle_once(path, observer)
        outcome['events'] = observer.events
        if result['status'] == 'FAILED':
            outcome.update(status='FAILED', detail=result['detail'])
            return outcome
        final = final_path(handle)
        verdict, why = _classify(final, path)
        outcome['proof'] = {
            'method': 'handle-final-path',
            'final_path': final,
            'identity_after': None,
            'recycle_item_in_callback': any(
                event.get('recycle_item_provided') for event in observer.events),
        }
        try:
            outcome['proof']['identity_after'] = file_identity(handle)
        except OSError:
            outcome['proof']['identity_after'] = None
        outcome.update(status=verdict, detail=why)
        if verdict == 'RECYCLED' and final:
            outcome['recycle_bin'] = _bin_entry(final, path)
        return outcome
    finally:
        close_hold(handle)


def _bin_entry(final, original):
    """Derive the $I/$R pair for the object we just verified."""
    native = final.replace('\\\\?\\', '')
    directory = os.path.dirname(native)
    leaf = os.path.basename(native)
    if not leaf.startswith('$R'):
        return None
    index_path = os.path.join(directory, '$I' + leaf[2:])
    entry = {'r_path': native, 'i_path': index_path,
             'i_exists': os.path.exists(index_path)}
    if entry['i_exists']:
        try:
            entry['metadata'] = parse_i(index_path)
        except (OSError, ValueError, struct.error) as exc:
            entry['metadata'] = None
            entry['metadata_error'] = str(exc)
    entry['original_path_matches'] = bool(
        entry.get('metadata') and
        os.path.normcase(entry['metadata'].get('original_path', '')) ==
        os.path.normcase(original))
    return entry


def parse_i(i_path):
    """Parse a $I metadata record.

    Layout: 8B version, 8B original size, 8B FILETIME, then the original path.
    Version 2 stores a 4-byte character count (including the terminator) at
    0x18 with the path from 0x1C; version 1 stores a fixed 520-byte buffer from
    0x18. Verified against this machine's own entries; a malformed record raises
    rather than guessing.
    """
    with open(i_path, 'rb') as handle:
        raw = handle.read()
    if len(raw) < 24:
        raise ValueError('$I record is shorter than its fixed header')
    version, size, filetime = struct.unpack('<QQQ', raw[0:24])
    if version == 2:
        if len(raw) < 28:
            raise ValueError('$I version 2 record has no path length field')
        count = struct.unpack('<I', raw[24:28])[0]
        payload = raw[28:28 + count * 2]
    elif version == 1:
        payload = raw[24:24 + 520]
    else:
        raise ValueError(f'unsupported $I version {version}')
    text = payload.decode('utf-16-le', errors='replace').split('\x00', 1)[0]
    if not text:
        raise ValueError('$I record carries no original path')
    return {
        'version': version,
        'size_bytes': size,
        'deleted_unix': filetime / 10_000_000 - 11644473600,
        'original_path': text,
    }


def availability():
    """Cheap capability check used by callers that want to fail closed early."""
    if os.name != 'nt':
        return {'available': False, 'reason': 'Windows only'}
    try:
        version = sys.getwindowsversion()
    except AttributeError:
        return {'available': False, 'reason': 'cannot determine the Windows version'}
    if (version.major, version.minor) < (6, 2):
        return {'available': False, 'reason': 'Windows 8 or later is required'}
    if _ole32 is None or _shell32 is None:
        return {'available': False, 'reason': 'COM libraries unavailable'}
    return {'available': True, 'reason': 'ok'}
