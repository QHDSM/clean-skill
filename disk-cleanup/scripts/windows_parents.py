#!/usr/bin/env python3
"""Read-only Windows parent identity evidence; handles are released after capture."""
import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys

import scan
import windows_identity

SCHEMA = 'disk-cleanup-parent-chain/v1'


class PinnedParents:
    """Hold checked directory ancestors without delete sharing during inspection.

    An expected chain, when supplied, must match every current parent identity.
    Handles are released on context exit, including when preparation fails.
    """
    def __init__(self, parent, expected_chain=None):
        self.parent = Path(os.path.abspath(parent))
        self.handles = []
        self.kernel = None
        self.identities = []
        self.expected_chain = None
        if expected_chain is not None:
            if not isinstance(expected_chain, list) or not expected_chain:
                raise ValueError('parent chain must be a non-empty list')
            self.expected_chain = []
            for row in expected_chain:
                if (not isinstance(row, dict) or set(row) != {'path', 'volume_serial', 'file_id'}
                        or not isinstance(row['path'], str) or not Path(row['path']).is_absolute()
                        or type(row['volume_serial']) is not int or not 0 < row['volume_serial'] < (1 << 64)
                        or not isinstance(row['file_id'], str) or len(row['file_id']) != 32
                        or any(c not in '0123456789abcdef' for c in row['file_id'])
                        or row['file_id'] == '0' * 32):
                    raise ValueError('invalid parent identity evidence')
                self.expected_chain.append({**row, 'path': scan.normalized(row['path'])})

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
                identity = windows_identity.FileIdInfo()
                if not kernel.GetFileInformationByHandleEx(handle, 18, ctypes.byref(identity), ctypes.sizeof(identity)):
                    raise ctypes.WinError(ctypes.get_last_error())
                if not identity.volume_serial or not any(identity.file_id):
                    raise ValueError('parent identity unavailable')
                self.identities.append({'path': scan.normalized(directory),
                                        'volume_serial': identity.volume_serial,
                                        'file_id': bytes(identity.file_id).hex()})
            if self.expected_chain is not None and self.identities != self.expected_chain:
                raise ValueError('parent chain identity changed')
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        while self.handles:
            self.kernel.CloseHandle(self.handles.pop())


def capture_parent_chain(source):
    """Capture current pinned parent identities for later comparison."""
    with PinnedParents(Path(os.path.abspath(source)).parent) as parents:
        return [dict(row) for row in parents.identities]


def capture(source):
    source = Path(os.path.abspath(source))
    return {'schema': SCHEMA, 'path': str(source), 'parents': capture_parent_chain(source)}


def validate(source, evidence):
    if (not isinstance(evidence, dict) or evidence.get('schema') != SCHEMA
            or not isinstance(evidence.get('path'), str)
            or not Path(evidence['path']).is_absolute()
            or scan.normalized(evidence['path']) != scan.normalized(source)):
        raise ValueError('parent evidence must match target path')
    parents = PinnedParents(Path(os.path.abspath(source)).parent, evidence.get('parents'))
    if parents.expected_chain is None:
        raise ValueError('parent evidence is required')
    paths = [scan.normalized(p) for p in reversed((parents.parent, *parents.parent.parents))]
    if [row['path'] for row in parents.expected_chain] != paths:
        raise ValueError('parent evidence must include the complete ordered chain')
    return parents.expected_chain


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path')
    try:
        result = capture(parser.parse_args().path)
    except (OSError, ValueError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=True), file=sys.stderr)
        sys.exit(2)
    print(json.dumps(result, ensure_ascii=True, indent=2))
