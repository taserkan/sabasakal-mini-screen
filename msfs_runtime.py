"""Read-only MSFS 2024 flight telemetry via the game's own SimConnect client."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import os
from pathlib import Path
import threading
import time

import psutil


SIMCONNECT_RECV_ID_SIMOBJECT_DATA = 8
SIMCONNECT_DATATYPE_FLOAT64 = 4
SIMCONNECT_OBJECT_ID_USER = 0
SIMCONNECT_PERIOD_SIM_FRAME = 3
SIMCONNECT_UNUSED = 0xFFFFFFFF
DEFINITION_FLIGHT = 1
REQUEST_FLIGHT = 1


@dataclass(frozen=True)
class FlightTelemetry:
    altitude_ft: float | None = None
    airspeed_kt: float | None = None
    vertical_speed_fpm: float | None = None
    heading_deg: float | None = None
    connected: bool = False
    updated_at: float = 0.0
    error: str = ""

    @property
    def signature(self) -> tuple[int | None, ...]:
        def rounded(value: float | None, step: float) -> int | None:
            return None if value is None else round(value / step)

        return (
            rounded(self.altitude_ft, 10.0),
            rounded(self.airspeed_kt, 1.0),
            rounded(self.vertical_speed_fpm, 10.0),
            rounded(self.heading_deg, 1.0),
        )


class _Recv(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("dwVersion", wintypes.DWORD),
        ("dwID", wintypes.DWORD),
    ]


class _RecvSimObjectData(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("dwVersion", wintypes.DWORD),
        ("dwID", wintypes.DWORD),
        ("dwRequestID", wintypes.DWORD),
        ("dwObjectID", wintypes.DWORD),
        ("dwDefineID", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("dwentrynumber", wintypes.DWORD),
        ("dwoutof", wintypes.DWORD),
        ("dwDefineCount", wintypes.DWORD),
        ("dwData", wintypes.DWORD),
    ]


_DISPATCH_PROC = ctypes.WINFUNCTYPE(
    None, ctypes.POINTER(_Recv), wintypes.DWORD, ctypes.c_void_p,
)


def _flight_simulator_process() -> psutil.Process | None:
    for process in psutil.process_iter(["name", "exe"]):
        try:
            if (process.info.get("name") or "").lower() == "flightsimulator2024.exe":
                return process
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def find_simconnect_library() -> Path | None:
    process = _flight_simulator_process()
    if process is None:
        return None
    try:
        executable = process.info.get("exe") or process.exe()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
    if not executable:
        return None
    candidate = Path(executable).resolve().parent / "SimConnect_internal.dll"
    return candidate if candidate.is_file() else None


class MSFSTelemetryMonitor:
    """Background, read-only client that reconnects as MSFS starts and stops."""

    VARIABLES = (
        (b"INDICATED ALTITUDE", b"feet"),
        (b"AIRSPEED INDICATED", b"knots"),
        (b"VERTICAL SPEED", b"feet per minute"),
        (b"PLANE HEADING DEGREES MAGNETIC", b"degrees"),
    )

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._snapshot = FlightTelemetry()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="MSFS-SimConnect", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def snapshot(self) -> FlightTelemetry:
        with self._lock:
            return self._snapshot

    def _publish(self, snapshot: FlightTelemetry) -> None:
        with self._lock:
            self._snapshot = snapshot

    @staticmethod
    def _configure(dll: ctypes.WinDLL) -> None:
        handle = wintypes.HANDLE
        hresult = ctypes.c_long
        dll.SimConnect_Open.argtypes = [
            ctypes.POINTER(handle), ctypes.c_char_p, wintypes.HWND,
            wintypes.DWORD, handle, wintypes.DWORD,
        ]
        dll.SimConnect_Open.restype = hresult
        dll.SimConnect_Close.argtypes = [handle]
        dll.SimConnect_Close.restype = hresult
        dll.SimConnect_AddToDataDefinition.argtypes = [
            handle, wintypes.DWORD, ctypes.c_char_p, ctypes.c_char_p,
            wintypes.DWORD, ctypes.c_float, wintypes.DWORD,
        ]
        dll.SimConnect_AddToDataDefinition.restype = hresult
        dll.SimConnect_RequestDataOnSimObject.argtypes = [
            handle, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
            wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
            wintypes.DWORD, wintypes.DWORD,
        ]
        dll.SimConnect_RequestDataOnSimObject.restype = hresult
        dll.SimConnect_CallDispatch.argtypes = [
            handle, _DISPATCH_PROC, ctypes.c_void_p,
        ]
        dll.SimConnect_CallDispatch.restype = hresult

    @staticmethod
    def _check(result: int, operation: str) -> None:
        if int(result) < 0:
            raise OSError(f"{operation} failed: HRESULT 0x{int(result) & 0xFFFFFFFF:08X}")

    def _connect_and_read(self, library_path: Path) -> None:
        dll = ctypes.WinDLL(str(library_path))
        self._configure(dll)
        connection = wintypes.HANDLE()
        self._check(
            dll.SimConnect_Open(
                ctypes.byref(connection), b"CS2 Screen MSFS Telemetry",
                None, 0, None, 0,
            ),
            "SimConnect_Open",
        )

        try:
            for name, unit in self.VARIABLES:
                self._check(
                    dll.SimConnect_AddToDataDefinition(
                        connection, DEFINITION_FLIGHT, name, unit,
                        SIMCONNECT_DATATYPE_FLOAT64, 0.0, SIMCONNECT_UNUSED,
                    ),
                    f"define {name.decode('ascii')}",
                )
            self._check(
                dll.SimConnect_RequestDataOnSimObject(
                    connection, REQUEST_FLIGHT, DEFINITION_FLIGHT,
                    SIMCONNECT_OBJECT_ID_USER, SIMCONNECT_PERIOD_SIM_FRAME,
                    0, 0, 1, 0,
                ),
                "request flight data",
            )

            def receive(data, _size, _context) -> None:
                if not data or data.contents.dwID != SIMCONNECT_RECV_ID_SIMOBJECT_DATA:
                    return
                packet = ctypes.cast(
                    data, ctypes.POINTER(_RecvSimObjectData),
                ).contents
                if packet.dwRequestID != REQUEST_FLIGHT or packet.dwDefineCount < 4:
                    return
                values_type = ctypes.c_double * 4
                values = values_type.from_address(
                    ctypes.addressof(packet) + _RecvSimObjectData.dwData.offset,
                )
                altitude, airspeed, vertical_speed, heading = map(float, values)
                self._publish(FlightTelemetry(
                    altitude_ft=altitude,
                    airspeed_kt=max(0.0, airspeed),
                    vertical_speed_fpm=vertical_speed,
                    heading_deg=heading % 360.0,
                    connected=True,
                    updated_at=time.monotonic(),
                ))

            callback = _DISPATCH_PROC(receive)
            self._publish(FlightTelemetry(connected=True))
            while not self._stop.is_set() and _flight_simulator_process() is not None:
                self._check(
                    dll.SimConnect_CallDispatch(connection, callback, None),
                    "SimConnect_CallDispatch",
                )
                self._stop.wait(0.01)
        finally:
            dll.SimConnect_Close(connection)

    def _run(self) -> None:
        while not self._stop.is_set():
            library_path = find_simconnect_library()
            if library_path is None:
                self._publish(FlightTelemetry())
                self._stop.wait(1.0)
                continue
            try:
                self._connect_and_read(library_path)
            except Exception as exc:
                self._publish(FlightTelemetry(error=f"{type(exc).__name__}: {exc}"))
                self._stop.wait(1.0)


__all__ = ["FlightTelemetry", "MSFSTelemetryMonitor", "find_simconnect_library"]
