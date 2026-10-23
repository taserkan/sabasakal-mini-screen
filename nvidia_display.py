"""NVIDIA digital vibrance (the control panel's "Digital vibrance") per display.

Uses the documented-by-practice NvAPI DVC *Ex* calls only: their level is the
same 0-100 scale the NVIDIA Control Panel shows (50 = default). The legacy
NvAPI_SetDVCLevel uses a different scale and is deliberately never called.
This only changes a driver colour setting; no game is touched.
"""
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

_QI_INITIALIZE = 0x0150E828
_QI_ENUM_DISPLAY = 0x9ABDD40D
_QI_DISPLAY_NAME = 0x22A78B05
_QI_GET_DVC_EX = 0x0E45002D
_QI_SET_DVC_EX = 0x4A82C2B1

_lock = threading.Lock()
_api = None


class _DvcInfoEx(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("current", ctypes.c_int32), ("minimum", ctypes.c_int32),
                ("maximum", ctypes.c_int32), ("default", ctypes.c_int32)]


_DVC_VERSION = ctypes.sizeof(_DvcInfoEx) | (1 << 16)


class _Api:
    def __init__(self) -> None:
        dll = ctypes.CDLL("nvapi64.dll")
        query = dll.nvapi_QueryInterface
        query.restype = ctypes.c_void_p
        query.argtypes = [ctypes.c_uint32]

        def fn(identifier, *argtypes):
            address = query(identifier)
            if not address:
                raise OSError(f"NvAPI function 0x{identifier:08X} unavailable")
            return ctypes.CFUNCTYPE(ctypes.c_int, *argtypes)(address)

        self.initialize = fn(_QI_INITIALIZE)
        self.enum_display = fn(_QI_ENUM_DISPLAY, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))
        self.display_name = fn(_QI_DISPLAY_NAME, ctypes.c_void_p, ctypes.c_char_p)
        self.get_dvc = fn(_QI_GET_DVC_EX, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_DvcInfoEx))
        self.set_dvc = fn(_QI_SET_DVC_EX, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_DvcInfoEx))
        if self.initialize() != 0:
            raise OSError("NvAPI_Initialize failed")


def _nvapi() -> _Api:
    global _api
    if _api is None:
        _api = _Api()
    return _api


def primary_display_name() -> str:
    """GDI name (e.g. \\\\.\\DISPLAY1) of the primary display."""

    class DisplayDevice(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32),
                    ("DeviceString", wintypes.WCHAR * 128), ("StateFlags", wintypes.DWORD),
                    ("DeviceID", wintypes.WCHAR * 128), ("DeviceKey", wintypes.WCHAR * 128)]

    user32 = ctypes.WinDLL("user32")
    for index in range(16):
        device = DisplayDevice()
        device.cb = ctypes.sizeof(device)
        if not user32.EnumDisplayDevicesW(None, index, ctypes.byref(device), 0):
            break
        if device.StateFlags & 0x4:  # DISPLAY_DEVICE_PRIMARY_DEVICE
            return device.DeviceName
    return "\\\\.\\DISPLAY1"


def _handle(display: str):
    api = _nvapi()
    name = ctypes.create_string_buffer(64)
    for index in range(16):
        handle = ctypes.c_void_p()
        if api.enum_display(index, ctypes.byref(handle)) != 0:
            break
        if api.display_name(handle, name) == 0 and name.value.decode("ascii", "replace") == display:
            return handle
    raise OSError(f"no NVIDIA display named {display}")


def get_vibrance(display: str | None = None) -> int:
    """Current digital vibrance (0-100, 50 = default)."""
    with _lock:
        info = _DvcInfoEx(version=_DVC_VERSION)
        if _nvapi().get_dvc(_handle(display or primary_display_name()), 0, ctypes.byref(info)) != 0:
            raise OSError("NvAPI_GetDVCInfoEx failed")
        return int(info.current)


def set_vibrance(level: int, display: str | None = None) -> int:
    """Set digital vibrance (clamped to 0-100); returns the value read back."""
    level = max(0, min(100, int(level)))
    target = display or primary_display_name()
    with _lock:
        handle = _handle(target)
        info = _DvcInfoEx(version=_DVC_VERSION, current=level)
        if _nvapi().set_dvc(handle, 0, ctypes.byref(info)) != 0:
            raise OSError("NvAPI_SetDVCLevelEx failed")
        check = _DvcInfoEx(version=_DVC_VERSION)
        _nvapi().get_dvc(handle, 0, ctypes.byref(check))
        return int(check.current)
