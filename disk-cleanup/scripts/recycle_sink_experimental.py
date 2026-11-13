"""Fixture-only COM progress sink. Callback evidence is not a recovery guarantee."""
import ctypes
import os
import uuid

IID = '04b0f1a7-9490-44bc-96e1-4296a31252e2'
E_ABORT = -2147467260
E_NOINTERFACE = -2147467262
RECYCLE_FLAG = 0x80


class DeleteObserver:
    def __init__(self, *, permit_delete=True):
        if os.name != 'nt':
            raise OSError('Windows only')
        self.events = []
        self.refs = 1
        self.callbacks = []
        self.permit_delete = permit_delete
        P, D, H, W = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int32, ctypes.c_wchar_p

        def bind(types, function, result=H):
            def guarded(*args):
                try:
                    return function(*args)
                except BaseException:
                    self.events.append({'event': 'callback_error'})
                    return E_ABORT
            callback = ctypes.WINFUNCTYPE(result, P, *types)(guarded)
            self.callbacks.append(callback)
            return ctypes.cast(callback, P).value

        def query(this, iid, output):
            if not output:
                return E_NOINTERFACE
            ctypes.cast(output, ctypes.POINTER(P))[0] = None
            requested = ctypes.string_at(iid, 16)
            if requested not in (uuid.UUID(IID).bytes_le, uuid.UUID('00000000-0000-0000-c000-000000000046').bytes_le):
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
            requested = bool(flags & RECYCLE_FLAG)
            allowed = requested and self.permit_delete is True
            self.events.append({'event': 'pre_delete', 'flags': flags, 'recycle_requested': requested,
                                'allowed': allowed})
            return 0 if allowed else E_ABORT

        def post_delete(this, flags, item, hr, recycled_item):
            self.events.append({'event': 'post_delete', 'flags': flags, 'hresult': hr,
                                'recycle_item_provided': bool(recycled_item)})
            return 0

        ok = lambda *args: 0
        reject = lambda *args: E_ABORT
        methods = [bind((P, P), query), bind((), add_ref, D), bind((), release, D),
            bind((), ok), bind((H,), ok),
            bind((D, P, W), reject), bind((D, P, W, H, P), reject),
            bind((D, P, P, W), reject), bind((D, P, P, W, H, P), reject),
            bind((D, P, P, W), reject), bind((D, P, P, W, H, P), reject),
            bind((D, P), pre_delete), bind((D, P, H, P), post_delete),
            bind((D, P, W), reject), bind((D, P, W, W, D, H, P), reject),
            bind((D, D), ok), bind((), ok), bind((), ok), bind((), ok)]
        self.vtable = (P * len(methods))(*methods)
        class Interface(ctypes.Structure):
            _fields_ = [('vtable', ctypes.POINTER(P))]
        self.interface = Interface(self.vtable)
        self.pointer = ctypes.cast(ctypes.pointer(self.interface), P)
