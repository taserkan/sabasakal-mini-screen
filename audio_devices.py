"""List playback devices and switch the Windows default output.

Listing uses the documented Core Audio API (IMMDeviceEnumerator). Switching
uses IPolicyConfig, the interface Windows' own Sound control panel uses
(also used by SoundSwitch and EarTrumpet). Every COM pointer is released and
every COM-allocated string freed before returning.
"""
from __future__ import annotations

import ctypes
import uuid
from ctypes import wintypes
from dataclasses import dataclass

CLSCTX_ALL = 0x17
E_RENDER = 0
DEVICE_STATE_ACTIVE = 0x1
ROLES = (0, 1, 2)  # console, multimedia, communications
VT_LPWSTR = 31

_CLSID_ENUMERATOR = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
_IID_ENUMERATOR = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
_CLSID_POLICY_CONFIG = "{870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}"
_IID_POLICY_CONFIG = "{F8679F50-850A-41CF-9C72-430F290290C8}"
_PKEY_FRIENDLY_NAME = ("{A45C254E-DF1C-4EFD-8020-67D146A850E0}", 14)


@dataclass(frozen=True)
class AudioDevice:
    device_id: str
    name: str


class _Guid(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text: str) -> "_Guid":
        value = uuid.UUID(text)
        guid = cls()
        guid.Data1, guid.Data2, guid.Data3 = value.fields[0], value.fields[1], value.fields[2]
        for index, byte in enumerate(value.bytes[8:]):
            guid.Data4[index] = byte
        return guid


class _PropertyKey(ctypes.Structure):
    _fields_ = [("fmtid", _Guid), ("pid", wintypes.DWORD)]


class _PropVariant(ctypes.Structure):
    _fields_ = [("vt", wintypes.USHORT), ("r1", wintypes.USHORT), ("r2", wintypes.USHORT),
                ("r3", wintypes.USHORT), ("value", ctypes.c_void_p), ("pad", ctypes.c_void_p)]


_ole32 = ctypes.WinDLL("ole32")
_ole32.CoCreateInstance.argtypes = [ctypes.POINTER(_Guid), ctypes.c_void_p, wintypes.DWORD,
                                    ctypes.POINTER(_Guid), ctypes.POINTER(ctypes.c_void_p)]
_ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
_ole32.PropVariantClear.argtypes = [ctypes.POINTER(_PropVariant)]


def _method(pointer: ctypes.c_void_p, index: int, *argtypes):
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    prototype = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
    return lambda *args: prototype(vtable[index])(pointer, *args)


def _release(pointer: ctypes.c_void_p) -> None:
    if pointer:
        _method(pointer, 2)()


class _Com:
    """CoInitialize for the calling thread when needed; balanced on exit."""

    def __enter__(self):
        result = _ole32.CoInitializeEx(None, 0x2)  # apartment-threaded
        self._uninit = result in (0, 1)  # S_OK / S_FALSE; RPC_E_CHANGED_MODE needs no balance
        return self

    def __exit__(self, *_exc):
        if self._uninit:
            _ole32.CoUninitialize()


def _create(clsid: str, iid: str) -> ctypes.c_void_p:
    pointer = ctypes.c_void_p()
    result = _ole32.CoCreateInstance(
        ctypes.byref(_Guid.parse(clsid)), None, CLSCTX_ALL, ctypes.byref(_Guid.parse(iid)), ctypes.byref(pointer),
    )
    if result != 0 or not pointer:
        raise OSError(f"CoCreateInstance failed: 0x{result & 0xFFFFFFFF:08X}")
    return pointer


def _device_id(device: ctypes.c_void_p) -> str:
    text = ctypes.c_wchar_p()
    if _method(device, 5, ctypes.POINTER(ctypes.c_wchar_p))(ctypes.byref(text)) != 0:
        return ""
    try:
        return text.value or ""
    finally:
        _ole32.CoTaskMemFree(ctypes.cast(text, ctypes.c_void_p))


def _friendly_name(device: ctypes.c_void_p) -> str:
    store = ctypes.c_void_p()
    if _method(device, 4, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))(0, ctypes.byref(store)) != 0:
        return ""
    try:
        key = _PropertyKey(_Guid.parse(_PKEY_FRIENDLY_NAME[0]), _PKEY_FRIENDLY_NAME[1])
        value = _PropVariant()
        if _method(store, 5, ctypes.POINTER(_PropertyKey), ctypes.POINTER(_PropVariant))(
                ctypes.byref(key), ctypes.byref(value)) != 0:
            return ""
        try:
            return ctypes.wstring_at(value.value) if value.vt == VT_LPWSTR and value.value else ""
        finally:
            _ole32.PropVariantClear(ctypes.byref(value))
    finally:
        _release(store)


def list_output_devices() -> list[AudioDevice]:
    """Active playback devices with their Windows names."""
    devices: list[AudioDevice] = []
    with _Com():
        enumerator = _create(_CLSID_ENUMERATOR, _IID_ENUMERATOR)
        try:
            collection = ctypes.c_void_p()
            if _method(enumerator, 3, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))(
                    E_RENDER, DEVICE_STATE_ACTIVE, ctypes.byref(collection)) != 0:
                return devices
            try:
                count = wintypes.UINT()
                _method(collection, 3, ctypes.POINTER(wintypes.UINT))(ctypes.byref(count))
                for index in range(count.value):
                    device = ctypes.c_void_p()
                    if _method(collection, 4, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))(
                            index, ctypes.byref(device)) != 0:
                        continue
                    try:
                        device_id = _device_id(device)
                        if device_id:
                            devices.append(AudioDevice(device_id, _friendly_name(device) or device_id))
                    finally:
                        _release(device)
            finally:
                _release(collection)
        finally:
            _release(enumerator)
    return devices


def default_output_device_id() -> str:
    with _Com():
        enumerator = _create(_CLSID_ENUMERATOR, _IID_ENUMERATOR)
        try:
            device = ctypes.c_void_p()
            if _method(enumerator, 4, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
                    E_RENDER, 1, ctypes.byref(device)) != 0:
                return ""
            try:
                return _device_id(device)
            finally:
                _release(device)
        finally:
            _release(enumerator)


def set_default_output_device(device_id: str) -> None:
    """Make ``device_id`` the default playback device for every role."""
    if not device_id:
        raise ValueError("empty device id")
    with _Com():
        policy = _create(_CLSID_POLICY_CONFIG, _IID_POLICY_CONFIG)
        try:
            set_default = _method(policy, 13, ctypes.c_wchar_p, ctypes.c_int)
            for role in ROLES:
                result = set_default(device_id, role)
                if result != 0:
                    raise OSError(f"SetDefaultEndpoint failed: 0x{result & 0xFFFFFFFF:08X}")
        finally:
            _release(policy)
