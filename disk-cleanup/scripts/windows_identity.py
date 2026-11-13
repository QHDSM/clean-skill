#!/usr/bin/env python3
"""Read a Windows volume serial and 128-bit file ID; no cleanup operations."""
import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys

import scan

SCHEMA = 'disk-cleanup-file-id/v1'


class FileIdInfo(ctypes.Structure):
    _fields_ = [('volume_serial', ctypes.c_uint64), ('file_id', ctypes.c_ubyte * 16)]


class AttributeTagInfo(ctypes.Structure):
    _fields_ = [('attributes', ctypes.c_uint32), ('reparse_tag', ctypes.c_uint32)]


class StandardInfo(ctypes.Structure):
    _fields_ = [('allocation_size', ctypes.c_int64), ('end_of_file', ctypes.c_int64),
                ('links', ctypes.c_uint32), ('delete_pending', ctypes.c_ubyte),
                ('directory', ctypes.c_ubyte)]


def capture(path, *, allocation=False):
    """Query the opened leaf without following a leaf reparse point.

    Ancestors are checked separately and can still change concurrently. The
    handle is closed before return, so this is evidence, not a lock or snapshot.
    """
    if os.name != 'nt':
        raise OSError('Windows file identity is unavailable on this platform')
    path = scan.check_path(path)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                   ctypes.c_void_p, wintypes.DWORD]
    kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    # Metadata access, share read/write/delete, OPEN_EXISTING; open leaf itself.
    handle = kernel.CreateFileW(str(path), 0x80, 7, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        attrs, identity = AttributeTagInfo(), FileIdInfo()
        if not kernel.GetFileInformationByHandleEx(handle, 9, ctypes.byref(attrs), ctypes.sizeof(attrs)):
            raise ctypes.WinError(ctypes.get_last_error())
        if attrs.attributes & (scan.REPARSE | scan.OFFLINE | scan.RECALL):
            raise ValueError('link, reparse or offline target')
        if not kernel.GetFileInformationByHandleEx(handle, 18, ctypes.byref(identity), ctypes.sizeof(identity)):
            raise ctypes.WinError(ctypes.get_last_error())
        file_id = bytes(identity.file_id).hex()
        if identity.volume_serial == 0 or not any(identity.file_id):
            raise ValueError('filesystem returned an unavailable file identity')
        result = {'schema': SCHEMA, 'path': str(path), 'volume_serial': identity.volume_serial,
                  'file_id': file_id}
        if allocation:
            standard = StandardInfo()
            if not kernel.GetFileInformationByHandleEx(handle, 1, ctypes.byref(standard), ctypes.sizeof(standard)):
                raise ctypes.WinError(ctypes.get_last_error())
            if standard.directory or standard.delete_pending or standard.allocation_size < 0:
                raise ValueError('file allocation is unavailable for this object')
            result.update(allocation_bytes=standard.allocation_size, end_of_file=standard.end_of_file,
                          number_of_links=standard.links)
        return result
    finally:
        kernel.CloseHandle(handle)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path')
    args = parser.parse_args()
    try:
        result = capture(Path(args.path))
    except (OSError, ValueError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=True), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
