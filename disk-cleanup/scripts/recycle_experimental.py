"""Experimental Windows Shell adapter. Not connected to clean.py or a CLI.

No result from this module proves an item is recoverable. It has not passed
real recycle-bin failure testing and must not be enabled for user cleanup.
"""
import ctypes
from ctypes import wintypes
import os
import sys
import uuid

FOF_NOERRORUI = 0x0400
FOF_WANTNUKEWARNING = 0x4000
FOFX_RECYCLEONDELETE = 0x00080000
FOFX_EARLYFAILURE = 0x00100000
FLAGS = FOF_NOERRORUI | FOF_WANTNUKEWARNING | FOFX_RECYCLEONDELETE | FOFX_EARLYFAILURE


class BackendError(Exception):
    pass


def _failed(hr):
    return bool(int(hr) & 0x80000000)


def _check(hr, stage):
    if _failed(hr):
        raise BackendError(f'{stage} failed: HRESULT 0x{int(hr) & 0xffffffff:08X}')


def run_experiment(path, adapter):
    """Testable sequencing; result is unknown even when Shell reports success.

    The caller must isolate test data and manage authorization; this is not an
    execution gate. No permanent-delete fallback is permitted.
    """
    attempted = False
    result = {'status': 'FAILED_BEFORE_PERFORM', 'detail': 'interrupted'}
    try:
        adapter.initialize()
        adapter.create_operation()
        adapter.set_flags(FLAGS)
        adapter.create_item(path)
        adapter.queue_delete()
        attempted = True
        perform_hr = adapter.perform()
        abort_hr, aborted = adapter.aborted()
        if _failed(perform_hr) or _failed(abort_hr) or aborted:
            result = {'status': 'UNKNOWN', 'detail': 'Shell reported failure or abort; inspect source and recovery'}
        else:
            result = {'status': 'UNKNOWN', 'detail': 'Shell returned success; recoverability is not verified'}
    except Exception as exc:
        result = {'status': 'UNKNOWN' if attempted else 'FAILED_BEFORE_PERFORM', 'detail': str(exc)}
    finally:
        observer = getattr(adapter, 'observer', None)
        if observer is not None:
            result['delete_callbacks'] = list(observer.events)
        try:
            adapter.close()
        except Exception as exc:
            result = {'status': 'UNKNOWN' if attempted else 'FAILED_BEFORE_PERFORM',
                      'detail': f'{result["detail"]}; cleanup failed: {exc}'}
    return result


class NativeAdapter:
    """Thin COM adapter for an isolated experiment, not a cleanup safety gate."""
    def __init__(self, *, observe=False):
        self.operation = ctypes.c_void_p()
        self.item = ctypes.c_void_p()
        self.initialized = False
        self.ole = self.shell = None
        self.observe = observe
        self.observer = None

    def initialize(self):
        if os.name != 'nt' or sys.getwindowsversion() < (6, 2):
            raise BackendError('Windows 8 or later is required')
        # Use WinDLL so HRESULT failures remain explicit values, not exceptions.
        self.ole, self.shell = ctypes.WinDLL('ole32'), ctypes.WinDLL('shell32')
        self.ole.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        self.ole.CoInitializeEx.restype = ctypes.c_long
        hr = self.ole.CoInitializeEx(None, 2)  # apartment-threaded
        _check(hr, 'CoInitializeEx')
        self.initialized = True

    @staticmethod
    def _guid(value):
        class GUID(ctypes.Structure):
            _fields_ = [('data1', ctypes.c_uint32), ('data2', ctypes.c_uint16),
                        ('data3', ctypes.c_uint16), ('data4', ctypes.c_ubyte * 8)]
        return GUID.from_buffer_copy(uuid.UUID(value).bytes_le)

    @staticmethod
    def _method(pointer, index, restype, *types):
        vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *types)(vtable[index])

    def create_operation(self):
        clsid = self._guid('3ad05575-8857-4850-9277-11b85bdb8e09')
        iid = self._guid('947aab5f-0a5c-4c13-b4d6-4bf7836fc9f8')
        self.ole.CoCreateInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                                              ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        self.ole.CoCreateInstance.restype = ctypes.c_long
        _check(self.ole.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid),
                                         ctypes.byref(self.operation)), 'CoCreateInstance')
        if not self.operation:
            raise BackendError('CoCreateInstance returned an empty interface')

    def set_flags(self, flags):
        _check(self._method(self.operation, 5, ctypes.c_long, wintypes.DWORD)(self.operation, flags),
               'SetOperationFlags')

    def create_item(self, path):
        iid = self._guid('43826d1e-e718-42ee-bc55-a1e261c37bfe')
        self.shell.SHCreateItemFromParsingName.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p,
                                                           ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        self.shell.SHCreateItemFromParsingName.restype = ctypes.c_long
        _check(self.shell.SHCreateItemFromParsingName(str(path), None, ctypes.byref(iid),
                                                      ctypes.byref(self.item)), 'SHCreateItemFromParsingName')
        if not self.item:
            raise BackendError('SHCreateItemFromParsingName returned an empty interface')

    def queue_delete(self):
        if self.observe:
            from recycle_sink_experimental import DeleteObserver
            self.observer = DeleteObserver()
        _check(self._method(self.operation, 18, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)
               (self.operation, self.item, self.observer.pointer if self.observer else None), 'DeleteItem')

    def perform(self):
        return self._method(self.operation, 21, ctypes.c_long)(self.operation)

    def aborted(self):
        aborted = wintypes.BOOL()
        hr = self._method(self.operation, 22, ctypes.c_long, ctypes.POINTER(wintypes.BOOL))(
            self.operation, ctypes.byref(aborted))
        return hr, bool(aborted.value)

    def close(self):
        for pointer in (self.item, self.operation):
            if pointer:
                self._method(pointer, 2, wintypes.ULONG)(pointer)
                pointer.value = None
        if self.initialized:
            self.ole.CoUninitialize()
            self.initialized = False
