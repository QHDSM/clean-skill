"""Experimental handle-based rename; NOT connected to the cleanup executor.

This primitive does not implement authorization, rollback, parent-chain safety,
or crash recovery. It is for tests using self-created fixtures only.
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


def stage_fixture(source, expected, new_name):
    """Rename the opened object within its parent; never replace a destination.

    The returned path must be retained by a caller if later steps fail. This is
    not a safety gate: parent directories can still change and no rollback occurs.
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
