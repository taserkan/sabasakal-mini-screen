"""Read-only MSFS 2024 flight telemetry via the game's own SimConnect client.

Only official, documented SimConnect simulation variables are requested. The
client never writes events or data back to the simulator.
"""

from __future__ import annotations

import collections
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, replace
import json
import math
import os
from pathlib import Path
import threading
import time

import psutil

from app_log import log_exception


SIMCONNECT_RECV_ID_SIMOBJECT_DATA = 8
SIMCONNECT_DATATYPE_FLOAT64 = 4
SIMCONNECT_DATATYPE_STRING32 = 6
SIMCONNECT_DATATYPE_STRING128 = 8
SIMCONNECT_OBJECT_ID_USER = 0
SIMCONNECT_PERIOD_VISUAL_FRAME = 2
SIMCONNECT_PERIOD_SIM_FRAME = 3
SIMCONNECT_PERIOD_SECOND = 4
SIMCONNECT_DATA_REQUEST_FLAG_CHANGED = 0x1
SIMCONNECT_UNUSED = 0xFFFFFFFF
DEFINITION_FLIGHT = 1
DEFINITION_SLOW = 2
DEFINITION_TEXT = 3
REQUEST_FLIGHT = 1
REQUEST_SLOW = 2
REQUEST_TEXT = 3
PROCESS_NAME = "flightsimulator2024.exe"
PROCESS_CHECK_INTERVAL = 1.0
APP_DATA = Path(os.environ.get("APPDATA", Path.home())) / "CS2Screen"
LANDING_HISTORY_PATH = APP_DATA / "msfs-landings.json"
LANDING_HISTORY_SIZE = 5


@dataclass(frozen=True)
class FlightTelemetry:
    altitude_ft: float | None = None
    airspeed_kt: float | None = None
    vertical_speed_fpm: float | None = None
    heading_deg: float | None = None
    connected: bool = False
    updated_at: float = 0.0
    error: str = ""
    # Extended, phase-aware data (None when the simulator does not publish it).
    on_ground: bool | None = None
    altitude_agl_ft: float | None = None
    ground_speed_kt: float | None = None
    g_force: float | None = None
    gear_down: bool | None = None
    flaps_index: int | None = None
    spoilers_armed: bool | None = None
    parking_brake: bool | None = None
    strobe_on: bool | None = None
    fuel_lbs: float | None = None
    total_weight_lbs: float | None = None
    com1_active_mhz: float | None = None
    com1_standby_mhz: float | None = None
    nav1_active_mhz: float | None = None
    nav1_standby_mhz: float | None = None
    transponder_code: str = ""
    transponder_state: int | None = None
    next_wp_id: str = ""
    next_wp_distance_nm: float | None = None
    destination_ete_s: float | None = None
    flight_plan_active: bool | None = None
    headwind_kt: float | None = None
    vapp_kt: float | None = None
    aircraft_title: str = ""

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

    @property
    def full_signature(self) -> tuple:
        def rounded(value: float | None, step: float) -> int | None:
            return None if value is None else round(value / step)

        return self.signature + (
            self.on_ground, rounded(self.altitude_agl_ft, 10.0),
            rounded(self.ground_speed_kt, 1.0), self.gear_down, self.flaps_index,
            self.spoilers_armed, self.parking_brake, self.strobe_on,
            rounded(self.fuel_lbs, 50.0), rounded(self.com1_active_mhz, 0.005),
            rounded(self.com1_standby_mhz, 0.005), rounded(self.nav1_active_mhz, 0.005),
            rounded(self.nav1_standby_mhz, 0.005), self.transponder_code,
            self.next_wp_id, rounded(self.next_wp_distance_nm, 0.1),
            rounded(self.destination_ete_s, 60.0),
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
            if (process.info.get("name") or "").lower() == PROCESS_NAME:
                return process
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def _trusted_install_roots() -> tuple[str, ...]:
    roots = []
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        value = os.environ.get(variable)
        if value:
            roots.append(os.path.normcase(os.path.abspath(value)) + os.sep)
    return tuple(dict.fromkeys(roots))


_UNTRUSTED_WELL_KNOWN_SIDS = frozenset({
    "S-1-1-0",       # Everyone
    "S-1-5-11",      # Authenticated Users
    "S-1-5-32-545",  # BUILTIN\Users
    "S-1-5-4",       # INTERACTIVE
})
# Only these principals may hold write rights. Anything else (OWNER RIGHTS,
# per-user SIDs, custom groups, ...) makes the location untrusted.
_ADMIN_ONLY_SIDS = frozenset({
    "S-1-5-18",      # SYSTEM
    "S-1-5-19",      # LOCAL SERVICE
    "S-1-5-20",      # NETWORK SERVICE
    "S-1-5-32-544",  # BUILTIN\Administrators
    "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464",  # TrustedInstaller
})
# Rights that let a principal replace or plant files (write/append data = add
# file/subdirectory on folders), delete them, or rewrite the ACL/owner.
_WRITE_MASK = (
    0x00000002 | 0x00000004 | 0x00010000 | 0x00040000 | 0x00080000
    | 0x40000000 | 0x10000000
)


def _is_xbox_games_path(resolved: str) -> bool:
    parts = Path(resolved).parts
    return (
        len(parts) >= 2 and len(parts[0]) >= 2 and parts[0][1] == ":"
        and parts[1].lower() == "xboxgames"
    )


def _is_trusted_root(resolved: str) -> bool:
    if f"{os.sep}windowsapps{os.sep}" in resolved:
        return True
    if _is_xbox_games_path(resolved):
        return True
    return any(resolved.startswith(root) for root in _trusted_install_roots())


def _local_free(pointer) -> None:
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree(ctypes.cast(pointer, ctypes.c_void_p))


def _sid_to_string(advapi32, sid) -> str | None:
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    text = wintypes.LPWSTR()
    if not advapi32.ConvertSidToStringSidW(ctypes.c_void_p(sid), ctypes.byref(text)):
        return None
    try:
        return text.value
    finally:
        _local_free(text)


def _current_user_sid() -> str | None:
    if os.name != "nt":
        return None
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        return None
    try:
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            return None
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        return _sid_to_string(advapi32, sid)
    finally:
        kernel32.CloseHandle(token)


class _AclSizeInfo(ctypes.Structure):
    _fields_ = [
        ("AceCount", wintypes.DWORD),
        ("AclBytesInUse", wintypes.DWORD),
        ("AclBytesFree", wintypes.DWORD),
    ]


def _writable_by_untrusted(path: Path, untrusted: frozenset[str]) -> bool | None:
    """True if an allow-ACE grants write rights to an untrusted SID.

    None means the ACL could not be read; callers treat that as untrusted.
    """
    if os.name != "nt":
        return None
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetAclInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_int]
    advapi32.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    # SE_FILE_OBJECT = 1, DACL_SECURITY_INFORMATION = 4
    result = advapi32.GetNamedSecurityInfoW(
        str(path), 1, 4, None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor),
    )
    if result != 0:
        return None
    try:
        if not dacl.value:
            return True  # NULL DACL grants everyone full access.
        info = _AclSizeInfo()
        if not advapi32.GetAclInformation(dacl, ctypes.byref(info), ctypes.sizeof(info), 2):
            return None
        for index in range(info.AceCount):
            ace = ctypes.c_void_p()
            if not advapi32.GetAce(dacl, index, ctypes.byref(ace)):
                return None
            header = ctypes.cast(ace, ctypes.POINTER(ctypes.c_ubyte))
            ace_type, ace_flags = header[0], header[1]
            if ace_type != 0 or ace_flags & 0x08:  # allow-ACE that applies here
                continue
            mask = ctypes.cast(ace.value + 4, ctypes.POINTER(wintypes.DWORD))[0]
            if not mask & _WRITE_MASK:
                continue
            sid = _sid_to_string(advapi32, ace.value + 8)
            if sid is None or sid in untrusted or sid not in _ADMIN_ONLY_SIDS:
                return True
        return False
    finally:
        _local_free(descriptor)


def _acl_protected(dll: Path) -> tuple[bool, str]:
    untrusted = set(_UNTRUSTED_WELL_KNOWN_SIDS)
    user_sid = _current_user_sid()
    if user_sid:
        untrusted.add(user_sid)
    frozen = frozenset(untrusted)
    for target in (dll.parent, dll):
        writable = _writable_by_untrusted(target, frozen)
        if writable is None:
            return False, f"ACL could not be read: {target}"
        if writable:
            return False, f"writable by non-admin principals: {target}"
    return True, "folder and DLL ACL are admin-only"


def simconnect_trust_decision(library: str | os.PathLike) -> tuple[bool, str]:
    """Decide whether SimConnect_internal.dll may be loaded (the app runs elevated).

    Accepted: Program Files, WindowsApps, <drive>:\\XboxGames (Xbox app
    managed), or any folder where neither the folder nor the DLL is writable
    by Users, Authenticated Users, Everyone, INTERACTIVE or the current user.
    """
    try:
        path = Path(library).resolve()
    except (OSError, ValueError) as error:
        return False, f"path could not be resolved: {error}"
    if _is_trusted_root(os.path.normcase(str(path))):
        return True, "trusted install root"
    if not path.is_file():
        return False, f"DLL not found: {path}"
    return _acl_protected(path)


def is_trusted_simconnect_location(executable: str | os.PathLike) -> bool:
    """Path-only check: Program Files, WindowsApps or <drive>:\\XboxGames."""
    try:
        resolved = os.path.normcase(str(Path(executable).resolve()))
    except (OSError, ValueError):
        return False
    return _is_trusted_root(resolved)


_last_refusal: str | None = None


def find_simconnect_library(process: psutil.Process | None = None) -> Path | None:
    global _last_refusal
    process = process or _flight_simulator_process()
    if process is None:
        return None
    try:
        executable = (getattr(process, "info", None) or {}).get("exe") or process.exe()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
    if not executable:
        return None
    candidate = Path(executable).resolve().parent / "SimConnect_internal.dll"
    if not candidate.is_file():
        trusted, reason = False, f"MSFS skipped: SimConnect_internal.dll not found next to {executable}"
    else:
        trusted, why = simconnect_trust_decision(candidate)
        reason = f"MSFS skipped: SimConnect DLL refused ({why}): {candidate}"
    if not trusted:
        if reason != _last_refusal:
            _last_refusal = reason
            log_exception("msfs", reason)
        return None
    return candidate


class _ProcessWatch:
    """Cache the simulator process; re-check liveness at most once a second."""

    def __init__(self) -> None:
        self._process: psutil.Process | None = None
        self._checked_at = 0.0
        self._alive = False

    def alive(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if now - self._checked_at < PROCESS_CHECK_INTERVAL:
            return self._alive
        self._checked_at = now
        try:
            if self._process is not None and self._process.is_running():
                self._alive = True
                return True
        except psutil.Error:
            pass
        self._process = _flight_simulator_process()
        self._alive = self._process is not None
        return self._alive

    @property
    def process(self) -> psutil.Process | None:
        return self._process


def _decode_bcd(value: float) -> str:
    try:
        return f"{int(value) & 0xFFFF:04X}"
    except (TypeError, ValueError, OverflowError):
        return ""


class MSFSTelemetryMonitor:
    """Background, read-only client that reconnects as MSFS starts and stops."""

    # Fast block: needed for phase detection and touchdown capture.
    VARIABLES = (
        (b"INDICATED ALTITUDE", b"feet"),
        (b"AIRSPEED INDICATED", b"knots"),
        (b"VERTICAL SPEED", b"feet per minute"),
        (b"PLANE HEADING DEGREES MAGNETIC", b"degrees"),
        (b"SIM ON GROUND", b"bool"),
        (b"PLANE ALT ABOVE GROUND", b"feet"),
        (b"GROUND VELOCITY", b"knots"),
        (b"G FORCE", b"GForce"),
        (b"GEAR HANDLE POSITION", b"bool"),
        (b"FLAPS HANDLE INDEX", b"number"),
        (b"SPOILERS ARMED", b"bool"),
        (b"BRAKE PARKING POSITION", b"bool"),
        (b"PLANE HEADING DEGREES TRUE", b"degrees"),
    )
    # Slow block: once per second, only when something changed.
    SLOW_VARIABLES = (
        (b"FUEL TOTAL QUANTITY WEIGHT", b"pounds"),
        (b"TOTAL WEIGHT", b"pounds"),
        (b"LIGHT STROBE", b"bool"),
        (b"COM ACTIVE FREQUENCY:1", b"MHz"),
        (b"COM STANDBY FREQUENCY:1", b"MHz"),
        (b"NAV ACTIVE FREQUENCY:1", b"MHz"),
        (b"NAV STANDBY FREQUENCY:1", b"MHz"),
        (b"TRANSPONDER CODE:1", b"Bco16"),
        (b"TRANSPONDER STATE:1", b"number"),
        (b"GPS WP DISTANCE", b"meters"),
        (b"GPS ETE", b"seconds"),
        (b"GPS IS ACTIVE FLIGHT PLAN", b"bool"),
        (b"AMBIENT WIND VELOCITY", b"knots"),
        (b"AMBIENT WIND DIRECTION", b"degrees"),
        (b"DESIGN SPEED VS0", b"knots"),
    )
    TEXT_VARIABLES = (
        (b"TITLE", SIMCONNECT_DATATYPE_STRING128, 128),
        (b"GPS WP NEXT ID", SIMCONNECT_DATATYPE_STRING32, 32),
    )

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._snapshot = FlightTelemetry()
        self._slow: dict[str, object] = {}
        self._text: dict[str, str] = {}
        self._watch = _ProcessWatch()

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

    def _apply_slow(self, values: list[float]) -> None:
        (fuel, weight, strobe, com_a, com_s, nav_a, nav_s, xpdr, xpdr_state,
         wp_distance_m, ete, plan_active, wind_kt, wind_dir, vs0) = values
        self._slow = {
            "fuel_lbs": max(0.0, fuel),
            "total_weight_lbs": max(0.0, weight),
            "strobe_on": strobe >= 0.5,
            "com1_active_mhz": com_a or None,
            "com1_standby_mhz": com_s or None,
            "nav1_active_mhz": nav_a or None,
            "nav1_standby_mhz": nav_s or None,
            "transponder_code": _decode_bcd(xpdr),
            "transponder_state": int(xpdr_state),
            "next_wp_distance_nm": (wp_distance_m / 1852.0) if wp_distance_m > 0 else None,
            "destination_ete_s": ete if ete > 0 else None,
            "flight_plan_active": plan_active >= 0.5,
            "_wind_kt": wind_kt,
            "_wind_dir": wind_dir,
            "vapp_kt": (vs0 * 1.3 + 5.0) if vs0 > 20 else None,
        }

    def _build(self, values: list[float]) -> FlightTelemetry:
        (altitude, airspeed, vertical_speed, heading, on_ground, agl, ground_speed,
         g_force, gear, flaps, spoilers, park, heading_true) = values
        slow = {key: value for key, value in self._slow.items() if not key.startswith("_")}
        headwind = None
        wind_kt = self._slow.get("_wind_kt")
        wind_dir = self._slow.get("_wind_dir")
        if isinstance(wind_kt, float) and isinstance(wind_dir, float):
            # Positive = headwind; shown as a negative ground-speed effect.
            headwind = wind_kt * math.cos(math.radians(wind_dir - heading_true))
        return FlightTelemetry(
            altitude_ft=altitude,
            airspeed_kt=max(0.0, airspeed),
            vertical_speed_fpm=vertical_speed,
            heading_deg=heading % 360.0,
            connected=True,
            updated_at=time.monotonic(),
            on_ground=on_ground >= 0.5,
            altitude_agl_ft=agl,
            ground_speed_kt=max(0.0, ground_speed),
            g_force=g_force,
            gear_down=gear >= 0.5,
            flaps_index=int(round(flaps)),
            spoilers_armed=spoilers >= 0.5,
            parking_brake=park >= 0.5,
            headwind_kt=headwind,
            next_wp_id=self._text.get("next_wp_id", ""),
            aircraft_title=self._text.get("title", ""),
            **slow,
        )

    def _connect_and_read(self, library_path: Path) -> None:
        # A new session (MSFS restart, other aircraft) must not mix in the
        # previous session's fuel/radio/title values.
        self._slow = {}
        self._text = {}
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
            for definition, variables in (
                (DEFINITION_FLIGHT, self.VARIABLES), (DEFINITION_SLOW, self.SLOW_VARIABLES),
            ):
                for name, unit in variables:
                    self._check(
                        dll.SimConnect_AddToDataDefinition(
                            connection, definition, name, unit,
                            SIMCONNECT_DATATYPE_FLOAT64, 0.0, SIMCONNECT_UNUSED,
                        ),
                        f"define {name.decode('ascii')}",
                    )
            for name, datatype, _size in self.TEXT_VARIABLES:
                self._check(
                    dll.SimConnect_AddToDataDefinition(
                        connection, DEFINITION_TEXT, name, None,
                        datatype, 0.0, SIMCONNECT_UNUSED,
                    ),
                    f"define {name.decode('ascii')}",
                )
            # Fast block every second visual frame, only when changed; the
            # rest once per second when changed. This replaces the previous
            # every-sim-frame request that woke the client constantly.
            for request, definition, period, interval in (
                (REQUEST_FLIGHT, DEFINITION_FLIGHT, SIMCONNECT_PERIOD_VISUAL_FRAME, 1),
                (REQUEST_SLOW, DEFINITION_SLOW, SIMCONNECT_PERIOD_SECOND, 0),
                (REQUEST_TEXT, DEFINITION_TEXT, SIMCONNECT_PERIOD_SECOND, 4),
            ):
                self._check(
                    dll.SimConnect_RequestDataOnSimObject(
                        connection, request, definition,
                        SIMCONNECT_OBJECT_ID_USER, period,
                        SIMCONNECT_DATA_REQUEST_FLAG_CHANGED, 0, interval, 0,
                    ),
                    "request flight data",
                )

            def receive(data, _size, _context) -> None:
                try:
                    if not data or data.contents.dwID != SIMCONNECT_RECV_ID_SIMOBJECT_DATA:
                        return
                    packet = ctypes.cast(
                        data, ctypes.POINTER(_RecvSimObjectData),
                    ).contents
                    address = ctypes.addressof(packet) + _RecvSimObjectData.dwData.offset
                    if packet.dwRequestID == REQUEST_FLIGHT:
                        count = len(self.VARIABLES)
                        if packet.dwDefineCount < count:
                            return
                        values = list(map(float, (ctypes.c_double * count).from_address(address)))
                        self._publish(self._build(values))
                    elif packet.dwRequestID == REQUEST_SLOW:
                        count = len(self.SLOW_VARIABLES)
                        if packet.dwDefineCount < count:
                            return
                        values = list(map(float, (ctypes.c_double * count).from_address(address)))
                        self._apply_slow(values)
                    elif packet.dwRequestID == REQUEST_TEXT:
                        offset = 0
                        text = {}
                        for (name, _datatype, size), key in zip(
                            self.TEXT_VARIABLES, ("title", "next_wp_id"),
                        ):
                            raw = ctypes.string_at(address + offset, size)
                            text[key] = raw.split(b"\0", 1)[0].decode("utf-8", "ignore").strip()
                            offset += size
                        self._text = text
                except Exception as exc:  # never raise through the C callback
                    log_exception("msfs", "SimConnect packet decode failed", exc)

            callback = _DISPATCH_PROC(receive)
            self._publish(FlightTelemetry(connected=True))
            while not self._stop.is_set() and self._watch.alive():
                self._check(
                    dll.SimConnect_CallDispatch(connection, callback, None),
                    "SimConnect_CallDispatch",
                )
                self._stop.wait(0.02)
        finally:
            dll.SimConnect_Close(connection)

    def _run(self) -> None:
        while not self._stop.is_set():
            library_path = (
                find_simconnect_library(self._watch.process) if self._watch.alive() else None
            )
            if library_path is None:
                self._publish(FlightTelemetry())
                self._stop.wait(1.0)
                continue
            try:
                self._connect_and_read(library_path)
            except Exception as exc:
                log_exception("msfs", "SimConnect session failed", exc)
                self._publish(FlightTelemetry(error=f"{type(exc).__name__}: {exc}"))
                self._stop.wait(1.0)


# ---------------------------------------------------------------------------
# Phase detection and landing scoring (pure logic, unit-testable)
# ---------------------------------------------------------------------------

PHASE_GROUND = "ground"
PHASE_CRUISE = "cruise"
PHASE_APPROACH = "approach"
PHASE_LANDING = "landing"
PHASE_RADIO = "radio"
LANDING_SCREEN_SECONDS = 60.0
RADIO_SCREEN_SECONDS = 10.0


@dataclass(frozen=True)
class LandingRecord:
    vertical_speed_fpm: float
    g_force: float | None
    bounced: bool
    timestamp: float

    @property
    def rating(self) -> str:
        rate = abs(self.vertical_speed_fpm)
        if rate < 200:
            return "soft"
        if rate < 400:
            return "firm"
        return "hard"


@dataclass(frozen=True)
class FlightPhaseState:
    phase: str = PHASE_GROUND
    landing: LandingRecord | None = None
    landing_average_fpm: float | None = None
    route_total_nm: float | None = None
    route_remaining_nm: float | None = None
    fuel_at_destination_lbs: float | None = None
    fuel_flow_pph: float | None = None
    landing_count: int = 0


class FlightPhaseTracker:
    """Derive the display phase from SimConnect variables.

    - SIM ON GROUND and no recent touchdown  -> ground/taxi
    - airborne, below 3000 ft AGL and descending or gear down -> approach
    - airborne otherwise -> climb/cruise
    - touchdown (airborne -> on ground)       -> landing score for 60 s
    - a COM/NAV/transponder change in cruise  -> radio screen for 10 s
    """

    def __init__(self, history_path: Path | None = LANDING_HISTORY_PATH) -> None:
        self._history_path = history_path
        self._history: collections.deque[LandingRecord] = collections.deque(
            maxlen=LANDING_HISTORY_SIZE,
        )
        self._load_history()
        self._last_on_ground: bool | None = None
        self._last_airborne_vs: float | None = None
        self._recent_g: collections.deque[tuple[float, float]] = collections.deque(maxlen=120)
        self._touchdown_at: float | None = None
        self._pending: LandingRecord | None = None
        self._landing: LandingRecord | None = None
        self._radio_signature: tuple | None = None
        self._radio_until = 0.0
        self._route_total: float | None = None
        self._fuel_samples: collections.deque[tuple[float, float]] = collections.deque(maxlen=180)

    def _load_history(self) -> None:
        if self._history_path is None:
            return
        try:
            rows = json.loads(self._history_path.read_text(encoding="utf-8"))
            if isinstance(rows, list):
                for row in rows[-LANDING_HISTORY_SIZE:]:
                    if isinstance(row, dict):
                        self._history.append(LandingRecord(
                            float(row["vertical_speed_fpm"]),
                            float(row["g_force"]) if row.get("g_force") is not None else None,
                            bool(row.get("bounced", False)),
                            float(row.get("timestamp", 0.0)),
                        ))
        except (OSError, ValueError, TypeError, KeyError):
            return

    def _save_history(self) -> None:
        if self._history_path is None:
            return
        try:
            self._history_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._history_path.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(json.dumps([
                {"vertical_speed_fpm": item.vertical_speed_fpm, "g_force": item.g_force,
                 "bounced": item.bounced, "timestamp": item.timestamp}
                for item in self._history
            ]), encoding="utf-8")
            os.replace(temporary, self._history_path)
        except OSError as exc:
            log_exception("msfs", "landing history save failed", exc)

    def reset(self) -> None:
        """Forget the live flight (sim closed/disconnected); keep saved landings.

        Without this an MSFS session that ended in the air leaves
        ``_last_on_ground=False`` behind and the first on-ground packet of the
        next session would be recorded as a (fake) touchdown."""
        self._last_on_ground = None
        self._last_airborne_vs = None
        self._recent_g.clear()
        self._touchdown_at = None
        self._pending = None
        self._landing = None
        self._radio_signature = None
        self._radio_until = 0.0
        self._route_total = None
        self._fuel_samples.clear()

    @property
    def history(self) -> tuple[LandingRecord, ...]:
        return tuple(self._history)

    def update(self, telemetry: FlightTelemetry, now: float | None = None) -> FlightPhaseState:
        now = time.monotonic() if now is None else now
        on_ground = telemetry.on_ground
        if on_ground is None and self._last_on_ground is not None:
            # Telemetry lost: never bridge two sessions with one "touchdown".
            self.reset()
        if telemetry.g_force is not None:
            self._recent_g.append((now, telemetry.g_force))
        if on_ground is False and telemetry.vertical_speed_fpm is not None:
            self._last_airborne_vs = telemetry.vertical_speed_fpm
        # Touchdown edge.
        if self._last_on_ground is False and on_ground is True:
            peak_g = max((g for t, g in self._recent_g if now - t <= 1.5), default=None)
            if self._pending is not None and self._touchdown_at and now - self._touchdown_at < 6.0:
                # Second contact shortly after the first: a bounce.
                self._pending = replace(self._pending, bounced=True)
            else:
                self._pending = LandingRecord(
                    self._last_airborne_vs if self._last_airborne_vs is not None
                    else (telemetry.vertical_speed_fpm or 0.0),
                    peak_g, False, time.time(),
                )
            self._touchdown_at = now
            self._landing = self._pending
        # Commit the landing once the aircraft stayed on the ground.
        if (
            self._pending is not None and on_ground and self._touchdown_at is not None
            and now - self._touchdown_at >= 6.0
        ):
            self._history.append(self._pending)
            self._save_history()
            self._pending = None
        if on_ground is not None:
            self._last_on_ground = on_ground

        # Route / fuel estimates.
        remaining = None
        if telemetry.destination_ete_s and telemetry.ground_speed_kt and telemetry.ground_speed_kt > 30:
            remaining = telemetry.destination_ete_s / 3600.0 * telemetry.ground_speed_kt
            if self._route_total is None or remaining > self._route_total:
                self._route_total = remaining
        landing_window_over = (
            self._landing is None or self._touchdown_at is None
            or now - self._touchdown_at >= LANDING_SCREEN_SECONDS
        )
        if on_ground and (telemetry.ground_speed_kt or 0.0) < 1.0 and landing_window_over:
            self._route_total = None
        if telemetry.fuel_lbs is not None:
            if self._fuel_samples and telemetry.fuel_lbs > self._fuel_samples[-1][1] + 50:
                self._fuel_samples.clear()  # refuelled
            if not self._fuel_samples or now - self._fuel_samples[-1][0] >= 1.0:
                self._fuel_samples.append((now, telemetry.fuel_lbs))
        flow = None
        if len(self._fuel_samples) >= 10:
            (t0, f0), (t1, f1) = self._fuel_samples[0], self._fuel_samples[-1]
            if t1 - t0 >= 10 and f0 > f1:
                flow = (f0 - f1) / (t1 - t0) * 3600.0
        fuel_at_destination = None
        if flow and telemetry.fuel_lbs is not None and telemetry.destination_ete_s:
            fuel_at_destination = telemetry.fuel_lbs - flow * telemetry.destination_ete_s / 3600.0

        radio_signature = (
            telemetry.com1_active_mhz, telemetry.com1_standby_mhz,
            telemetry.nav1_active_mhz, telemetry.nav1_standby_mhz, telemetry.transponder_code,
        )
        if self._radio_signature is not None and radio_signature != self._radio_signature:
            self._radio_until = now + RADIO_SCREEN_SECONDS
        self._radio_signature = radio_signature

        agl = telemetry.altitude_agl_ft
        vs = telemetry.vertical_speed_fpm or 0.0
        if self._landing is not None and self._touchdown_at is not None and (
            now - self._touchdown_at < LANDING_SCREEN_SECONDS
        ):
            phase = PHASE_LANDING
        elif on_ground or on_ground is None:
            phase = PHASE_GROUND
        elif now < self._radio_until:
            phase = PHASE_RADIO
        elif agl is not None and agl < 3000 and (vs < -300 or telemetry.gear_down):
            phase = PHASE_APPROACH
        else:
            phase = PHASE_CRUISE
        if phase != PHASE_LANDING and on_ground is False:
            self._landing = None
        average = (
            sum(item.vertical_speed_fpm for item in self._history) / len(self._history)
            if self._history else None
        )
        return FlightPhaseState(
            phase=phase,
            landing=self._landing,
            landing_average_fpm=average,
            route_total_nm=self._route_total,
            route_remaining_nm=remaining,
            fuel_at_destination_lbs=fuel_at_destination,
            fuel_flow_pph=flow,
            landing_count=len(self._history),
        )


__all__ = [
    "FlightPhaseState", "FlightPhaseTracker", "FlightTelemetry", "LandingRecord",
    "MSFSTelemetryMonitor", "find_simconnect_library", "is_trusted_simconnect_location",
    "simconnect_trust_decision",
    "PHASE_APPROACH", "PHASE_CRUISE", "PHASE_GROUND", "PHASE_LANDING", "PHASE_RADIO",
]
