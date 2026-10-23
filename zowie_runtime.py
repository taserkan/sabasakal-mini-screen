"""Zowie add-on: switch a ZOWIE XL monitor's picture mode when CS2 starts/exits.

The only CS2 touch point is the shared process-name check (cs2.exe running or
not). The monitor is driven over DDC/CI (VESA MCCS, VCP 0xDC "display mode"),
the same channel the monitor's own buttons and ZOWIE's tools use.

Behaviour is edge-triggered on purpose:
  * cs2.exe appears    -> one switch to the game mode (default GAMER 2)
  * cs2.exe disappears -> one switch back to the desktop mode (default GAMER 3)
  * while CS2 runs nothing is enforced, so the player can freely pick
    GAMER 1 / GAMER 2 on the monitor (alt-tab does not matter either: the
    process keeps running).

No handle is kept between switches: every switch opens the physical monitor
handles, sends one VCP command and destroys them again, so nothing can pile
up over a long session.
"""
from __future__ import annotations

import ctypes
import re
import threading
import time
from ctypes import wintypes
from typing import Callable

VCP_DISPLAY_MODE = 0xDC
# Verified on ZOWIE XL2546X+ (GAMER 1/2/3 read back as 24/25/26).
ZOWIE_MODES = {"GAMER1": 0x18, "GAMER2": 0x19, "GAMER3": 0x1A}
ZOWIE_MODE_LABELS = {"GAMER1": "GAMER 1", "GAMER2": "GAMER 2", "GAMER3": "GAMER 3"}
DEFAULT_GAME_MODE = "GAMER2"
DEFAULT_DESKTOP_MODE = "GAMER3"
POLL_SECONDS = 1.0
# cs2.exe must be missing in this many consecutive polls before the monitor
# is switched back, so one odd process scan can never flip the mode mid-game.
EXIT_CONFIRMATIONS = 3
# How often the control panel's "current mode" is refreshed while it is open.
STATUS_SECONDS = 3.0
_MODEL_RE = re.compile(r"model\(([^)]*)\)", re.IGNORECASE)


def normalise_mode(value: object, fallback: str) -> str:
    text = str(value or "").replace(" ", "").upper()
    return text if text in ZOWIE_MODES else fallback


def is_zowie_capabilities(caps: str) -> bool:
    """True for a ZOWIE XL monitor that exposes the display-mode VCP code."""
    match = _MODEL_RE.search(caps or "")
    if not match or not match.group(1).strip().upper().startswith("XL"):
        return False
    return re.search(r"\bDC\(", caps, re.IGNORECASE) is not None


# --- DDC/CI (dxva2) -------------------------------------------------------

class _PhysicalMonitor(ctypes.Structure):
    _fields_ = [("handle", wintypes.HANDLE), ("description", wintypes.WCHAR * 128)]


_MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM,
)


_DXVA2 = None
_USER32 = None


def _user32():
    # Loaded once: a fresh WinDLL per call would rebuild its function cache.
    global _USER32
    if _USER32 is None:
        lib = ctypes.WinDLL("user32", use_last_error=True)
        lib.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.c_void_p, _MONITORENUMPROC, wintypes.LPARAM]
        lib.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.c_void_p]
        lib.EnumDisplayDevicesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        _USER32 = lib
    return _USER32


def _dxva2():
    global _DXVA2
    if _DXVA2 is not None:
        return _DXVA2
    lib = ctypes.WinDLL("dxva2", use_last_error=True)
    lib.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = [wintypes.HMONITOR, ctypes.POINTER(wintypes.DWORD)]
    lib.GetPhysicalMonitorsFromHMONITOR.argtypes = [wintypes.HMONITOR, wintypes.DWORD, ctypes.POINTER(_PhysicalMonitor)]
    lib.DestroyPhysicalMonitors.argtypes = [wintypes.DWORD, ctypes.POINTER(_PhysicalMonitor)]
    lib.GetCapabilitiesStringLength.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    lib.CapabilitiesRequestAndCapabilitiesReply.argtypes = [wintypes.HANDLE, ctypes.c_char_p, wintypes.DWORD]
    lib.SetVCPFeature.argtypes = [wintypes.HANDLE, wintypes.BYTE, wintypes.DWORD]
    lib.GetVCPFeatureAndVCPFeatureReply.argtypes = [
        wintypes.HANDLE, wintypes.BYTE, ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
    ]
    _DXVA2 = lib
    return lib


class _MonitorInfoEx(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32),
    ]


class _DisplayDevice(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32),
        ("DeviceString", wintypes.WCHAR * 128), ("StateFlags", wintypes.DWORD),
        ("DeviceID", wintypes.WCHAR * 128), ("DeviceKey", wintypes.WCHAR * 128),
    ]


_enum_lock = threading.Lock()
_enum_found: list[int] = []


def _collect_monitor(hmonitor, _hdc, _rect, _data):
    _enum_found.append(hmonitor)
    return True


# One callback thunk for the whole process instead of a new one per call.
_COLLECT_MONITOR = _MONITORENUMPROC(_collect_monitor)


def _monitor_handles() -> list[int]:
    with _enum_lock:
        _enum_found.clear()
        _user32().EnumDisplayMonitors(None, None, _COLLECT_MONITOR, 0)
        found = list(_enum_found)
        _enum_found.clear()
    return found


def _monitor_identity(hmonitor) -> str:
    """Stable EDID-based id (e.g. MONITOR\\BNQ7FEA\\...) of a display; cheap, no DDC."""
    user32 = _user32()
    info = _MonitorInfoEx()
    info.cbSize = ctypes.sizeof(info)
    if not user32.GetMonitorInfoW(wintypes.HMONITOR(hmonitor), ctypes.byref(info)):
        return ""
    device = _DisplayDevice()
    device.cb = ctypes.sizeof(device)
    if not user32.EnumDisplayDevicesW(info.szDevice, 0, ctypes.byref(device), 0):
        return ""
    return device.DeviceID


# Identity of the monitor that answered as a ZOWIE XL last time. Reading the
# DDC capabilities string takes seconds per monitor; the identity check is
# instant and EDID based, so a re-ordered desktop can never hit the wrong one.
_known_zowie: dict[str, str] = {}
_known_other: set[str] = set()


def _capabilities(lib, handle) -> str:
    length = wintypes.DWORD(0)
    if not lib.GetCapabilitiesStringLength(handle, ctypes.byref(length)) or not length.value:
        return ""
    buffer = ctypes.create_string_buffer(length.value)
    if not lib.CapabilitiesRequestAndCapabilitiesReply(handle, buffer, length):
        return ""
    return buffer.value.decode("ascii", "replace")


_CODE_TO_MODE = {code: mode for mode, code in ZOWIE_MODES.items()}


def mode_from_code(code: int) -> str:
    return _CODE_TO_MODE.get(code, f"0x{code:02X}")


def set_zowie_mode(mode: str) -> str:
    """Send the display-mode command to every ZOWIE XL monitor.

    Returns the matched model name(s); raises RuntimeError when no ZOWIE
    monitor answered over DDC/CI.
    """
    code = ZOWIE_MODES[mode]

    def command(lib, handle) -> bool:
        return bool(lib.SetVCPFeature(handle, VCP_DISPLAY_MODE, code))

    return ", ".join(model for model, _ok in _on_zowie(command))


def read_zowie_mode() -> str:
    """Current picture mode of the first ZOWIE XL monitor (e.g. "GAMER2")."""

    def query(lib, handle):
        current, maximum = wintypes.DWORD(0), wintypes.DWORD(0)
        if not lib.GetVCPFeatureAndVCPFeatureReply(
            handle, VCP_DISPLAY_MODE, None, ctypes.byref(current), ctypes.byref(maximum),
        ):
            return None
        return mode_from_code(current.value)

    return _on_zowie(query)[0][1]


def _on_zowie(action) -> list[tuple[str, object]]:
    """Run ``action(lib, handle)`` on every ZOWIE XL monitor.

    ``action`` returns a truthy result on success. Handles are destroyed
    before returning. Raises RuntimeError when no ZOWIE monitor answered.
    """
    lib = _dxva2()
    switched: list[tuple[str, object]] = []
    for hmonitor in _monitor_handles():
        count = wintypes.DWORD(0)
        if not lib.GetNumberOfPhysicalMonitorsFromHMONITOR(hmonitor, ctypes.byref(count)) or not count.value:
            continue
        monitors = (_PhysicalMonitor * count.value)()
        if not lib.GetPhysicalMonitorsFromHMONITOR(hmonitor, count.value, monitors):
            continue
        try:
            identity = _monitor_identity(hmonitor) if count.value == 1 else ""
            if identity and identity in _known_zowie:
                result = action(lib, monitors[0].handle)
                if result:
                    switched.append((_known_zowie[identity], result))
                    continue
                _known_zowie.pop(identity, None)  # fall back to a full check
            if identity and identity in _known_other:
                continue
            for monitor in monitors:
                caps = _capabilities(lib, monitor.handle)
                if not is_zowie_capabilities(caps):
                    if identity and caps:  # only remember a definite answer
                        _known_other.add(identity)
                    continue
                result = action(lib, monitor.handle)
                if result:
                    match = _MODEL_RE.search(caps)
                    model = match.group(1) if match else "ZOWIE"
                    switched.append((model, result))
                    if identity:
                        _known_zowie[identity] = model
        finally:
            lib.DestroyPhysicalMonitors(count.value, monitors)
    if not switched:
        raise RuntimeError("no ZOWIE XL monitor answered over DDC/CI")
    return switched


# --- Watcher --------------------------------------------------------------

class ZowieModeMonitor:
    """Small daemon thread that reacts to cs2.exe starting and exiting."""

    def __init__(
        self,
        game_running: Callable[[], bool],
        switch: Callable[[str], str] = set_zowie_mode,
        log: Callable[[str], None] | None = None,
        poll_seconds: float = POLL_SECONDS,
        exit_confirmations: int = EXIT_CONFIRMATIONS,
        read_mode: Callable[[], str] = read_zowie_mode,
        status_seconds: float = STATUS_SECONDS,
    ) -> None:
        self._game_running = game_running
        self._switch = switch
        self._read_mode = read_mode
        self.status_seconds = status_seconds
        # Monitor mode shown in the control panel. Read over DDC/CI only while
        # ``status_wanted`` is set (window visible), never from the tray.
        self.status_wanted = False
        self.current_mode: str | None = None
        self.monitor_found: bool | None = None
        self._status_read_at = -1e9
        self._log = log or (lambda _message: None)
        self.poll_seconds = poll_seconds
        self.exit_confirmations = max(1, exit_confirmations)
        self._lock = threading.Lock()
        self._enabled = False
        self._game_mode = DEFAULT_GAME_MODE
        self._desktop_mode = DEFAULT_DESKTOP_MODE
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._in_game: bool | None = None
        self._missing = 0

    def configure(self, enabled: bool, game_mode: str, desktop_mode: str) -> None:
        with self._lock:
            self._enabled = bool(enabled)
            self._game_mode = normalise_mode(game_mode, DEFAULT_GAME_MODE)
            self._desktop_mode = normalise_mode(desktop_mode, DEFAULT_DESKTOP_MODE)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="zowie-mode", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        self._thread = None

    def poll_once(self) -> str | None:
        """One watcher step; returns the mode that was applied, if any."""
        try:
            running = bool(self._game_running())
        except Exception as exc:  # a failed scan must never look like an exit
            self._log(f"zowie: process check failed: {type(exc).__name__}: {exc}")
            return None
        if self._in_game is None:
            # First look: remember the state, never override what is on screen.
            self._in_game = running
            return None
        if running:
            self._missing = 0
            if self._in_game:
                return None
            self._in_game = True
            return self._apply("game")
        if not self._in_game:
            return None
        self._missing += 1
        if self._missing < self.exit_confirmations:
            return None
        self._missing = 0
        self._in_game = False
        return self._apply("desktop")

    def _apply(self, which: str) -> str | None:
        with self._lock:
            if not self._enabled:
                return None
            mode = self._game_mode if which == "game" else self._desktop_mode
        for attempt in range(3):
            try:
                model = self._switch(mode)
                self.current_mode = mode
                self.monitor_found = True
                self._log(f"zowie: cs2 {'started' if which == 'game' else 'exited'} -> {mode} ({model})")
                return mode
            except Exception as exc:
                if attempt == 2:
                    self._log(f"zowie: switch to {mode} failed: {type(exc).__name__}: {exc}")
                elif self._stop.wait(0.5):
                    return None
        return None

    def refresh_status(self, now: float) -> None:
        """Read the monitor's current mode (DDC/CI), at most every status_seconds."""
        if not self.status_wanted or now - self._status_read_at < self.status_seconds:
            return
        self._status_read_at = now
        try:
            self.current_mode = self._read_mode()
            self.monitor_found = True
        except Exception:
            self.current_mode = None
            self.monitor_found = False

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self.refresh_status(time.monotonic())
            self._stop.wait(self.poll_seconds)
