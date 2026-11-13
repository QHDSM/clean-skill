"""Experimental handle-based rename; NOT connected to the cleanup executor.

This primitive pins current parents but does not bind historical parent IDs,
authorization, rollback or crash recovery. Use self-created fixtures only.
"""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path

import scan
import windows_identity


class RenameInfo(ctypes.Structure):
    _fields_ = [('replace', ctypes.c_ubyte), ('root', wintypes.HANDLE),
                ('name_length', wintypes.DWORD), ('name', ctypes.c_wchar * 1)]


class PinnedParents:
    """Hold checked directory ancestors without delete sharing during a test.

    This pins current directory objects, not historical plan identities. Handles
    are released when leaving the context, including when preparation fails.
    """
    def __init__(self, parent):
        self.parent = Path(os.path.abspath(parent))
        self.handles = []
        self.kernel = None

    def __enter__(self):
        if os.name != 'nt':
            raise OSError('Windows only')
        kernel = self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                       ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                       ctypes.c_void_p, wintypes.DWORD]
        kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        try:
            for directory in reversed((self.parent, *self.parent.parents)):
                # No FILE_SHARE_DELETE: an opened ancestor cannot be renamed or
                # deleted through a new handle while this context holds it.
                # Include FILE_LIST_DIRECTORY; attribute-only opens do not
                # reliably enforce the intended sharing restriction.
                handle = kernel.CreateFileW(str(directory), 0x81, 3, None, 3, 0x02200000, None)
                if handle == ctypes.c_void_p(-1).value:
                    raise ctypes.WinError(ctypes.get_last_error())
                self.handles.append(handle)
                attrs = windows_identity.AttributeTagInfo()
                if not kernel.GetFileInformationByHandleEx(handle, 9, ctypes.byref(attrs), ctypes.sizeof(attrs)):
                    raise ctypes.WinError(ctypes.get_last_error())
                if not attrs.attributes & 0x10 or attrs.attributes & (scan.REPARSE | scan.OFFLINE | scan.RECALL):
                    raise ValueError('unsafe parent directory: ' + str(directory))
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        while self.handles:
            self.kernel.CloseHandle(self.handles.pop())


def stage_fixture(source, expected, new_name):
    source = Path(os.path.abspath(source))
    with PinnedParents(source.parent):
        return _stage_fixture_pinned(source, expected, new_name)


def _stage_fixture_pinned(source, expected, new_name):
    """Rename the opened object within its parent; never replace a destination.

    The returned path must be retained by a caller if later steps fail. Current
    parents are pinned only during this call; no rollback or authorization exists.
    """
    if os.name != 'nt':
        raise OSError('Windows only')
    if (not new_name or new_name in ('.', '..') or any(c in new_name for c in '\\/:\x00')
            or Path(new_name).name != new_name):
        raise ValueError('new name must be a single file name')
    source = scan.check_path(source)
    destination = source.with_name(new_name)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                   ctypes.c_void_p, wintypes.DWORD]
    kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                 ctypes.c_void_p, wintypes.DWORD]
    kernel.SetFileInformationByHandle.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(source), 0x10080, 7, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info, attributes = windows_identity.FileIdInfo(), windows_identity.AttributeTagInfo()
        if not kernel.GetFileInformationByHandleEx(handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)):
            raise ctypes.WinError(ctypes.get_last_error())
        if attributes.attributes & (scan.REPARSE | scan.OFFLINE | scan.RECALL):
            raise ValueError('unsafe source object')
        if not kernel.GetFileInformationByHandleEx(handle, 18, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if (info.volume_serial, bytes(info.file_id).hex()) != (expected['volume_serial'], expected['file_id']):
            raise ValueError('opened source identity changed')

        name = str(destination).encode('utf-16-le')
        offset = RenameInfo.name.offset
        buffer = ctypes.create_string_buffer(offset + len(name) + 2)
        header = RenameInfo.from_buffer(buffer)
        header.replace = 0
        header.root = None
        header.name_length = len(name)
        ctypes.memmove(ctypes.addressof(buffer) + offset, name, len(name))
        if not kernel.SetFileInformationByHandle(handle, 3, buffer, len(buffer)):
            raise ctypes.WinError(ctypes.get_last_error())
        return destination
    finally:
        kernel.CloseHandle(handle)
