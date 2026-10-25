# SPDX-License-Identifier: GPL-3.0-or-later
"""Turing 3.5" system monitor screen.

Uses the GPL-3.0-or-later display driver from:
https://github.com/mathoudebine/turing-smart-screen-python
"""

from __future__ import annotations

import argparse
import atexit
import collections
import contextvars
import ctypes
import ctypes.wintypes as wintypes
import functools
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

import psutil
from PIL import Image, ImageColor, ImageDraw, ImageFont

from battery_runtime import BatteryDevice
from i18n import normalize_language, translate
from media_runtime import (
    AudioSpectrumSnapshot, MediaSnapshot, media_service_name, sanitise_media_text,
)
from msfs_runtime import FlightTelemetry
import thresholds as _thresholds
from weather_runtime import WeatherReading
from app_log import log_exception, log_info


WIDTH = 480
HEIGHT = 320
# Keep the live spectrum compact: on the Rev-A serial display this region
# transfers in about 40 ms instead of the old 123 ms 309x51 rectangle.
MEDIA_VIS_REGION = (154, 266, 327, 295)
MEDIA_VIS_REGION_WITH_BATTERY = (96, 266, 269, 295)
ROOT = Path(__file__).resolve().parent
APP_DATA = Path(os.environ.get("APPDATA", Path.home())) / "CS2Screen"
SENSOR_CACHE_PATH = APP_DATA / "elevated-sensors.json"
if hasattr(sys, "_MEIPASS"):
    DRIVER_ROOT = Path(sys._MEIPASS)
elif (ROOT / "third_party" / "turing-smart-screen-python").is_dir():
    DRIVER_ROOT = ROOT / "third_party" / "turing-smart-screen-python"
else:
    DRIVER_ROOT = ROOT.parent / "turing-driver"
ICON_ROOT = ROOT / "icons" / "png"
SENSOR_ROOT = ROOT / "vendor"


MAX_HARDWARE_LABEL_LENGTH = 12
_RENDER_LANGUAGE = contextvars.ContextVar("render_language", default="tr")


def _t(text: str, **values: object) -> str:
    return translate(text, _RENDER_LANGUAGE.get(), **values)

COLORS = {
    "bg": "#0E1714",
    "panel": "#16231E",
    "panel_2": "#101B18",
    "edge": "#2E463B",
    "cpu": "#79D49F",
    "gpu": "#E99A5E",
    "ram": "#83BCE8",
    "text": "#F3F7F4",
    "muted": "#96AAA0",
    "dim": "#64796F",
    "track": "#263B32",
    "warning": "#E5C36D",
    "danger": "#E36F6F",
}

# Classic-design palette used automatically while cs2.exe runs: CS2's orange
# (T side) and blue (CT side) on a dark warm background. warning/danger stay
# yellow/red so a hot CPU or GPU is still distinguishable from the theme.
CS2_THEME_COLORS = {
    "bg": "#14100A",
    "panel": "#221A0E",
    "panel_2": "#1A140B",
    "edge": "#4A3618",
    "cpu": "#F5A524",
    "gpu": "#5EB8F0",
    "ram": "#E8CFA0",
    "text": "#FBF3E6",
    "muted": "#C9B293",
    "dim": "#8A7556",
    "track": "#3A2C16",
    "warning": "#FFD166",
    "danger": "#EF5B5B",
}

# CPU/GPU/RAM colour limits are shared with the Claude design (thresholds.py).


def load_font(
    size: int, bold: bool = False, heavy: bool = False,
) -> ImageFont.FreeTypeFont:
    candidates = [
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / (
            "segoeuib.ttf" if heavy else "seguisb.ttf" if bold else "segoeui.ttf"
        ),
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / (
            "arialbd.ttf" if bold or heavy else "arial.ttf"
        ),
    ]
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default()


FONTS = {
    "tiny": load_font(10),
    # Readable floor for captions on the physical 3.5-inch panel. Important
    # labels must never fall back to the thin 10 px face.
    "tiny_bold": load_font(10, heavy=True),
    "small": load_font(12),
    "small_bold": load_font(12, heavy=True),
    "weather_city": load_font(12, heavy=True),
    "label": load_font(14, True),
    "hardware_label": load_font(14, heavy=True),
    "value": load_font(28, True),
    "gauge_unit": load_font(17, heavy=True),
    "ping": load_font(24, True),
    "money": load_font(21, True),
    # The complete value tile is repainted for every live update, so the
    # original, wider Segoe face remains tear-free without sacrificing size.
    "sensor": load_font(21, heavy=True),
    "sensor_compact": load_font(18, heavy=True),
    "ram_sensor": load_font(17, heavy=True),
    "detail": load_font(12, heavy=True),
}


PROCESS_SCAN_TTL_SECONDS = 2.0


class ProcessScanner:
    """One psutil.process_iter pass shared by every "is X running" check.

    The screen loop used to walk the whole process table three times per
    cycle (CS2, FACEIT, MSFS). The table is now scanned at most once every
    PROCESS_SCAN_TTL_SECONDS and the result is shared.
    """

    def __init__(self, ttl: float = PROCESS_SCAN_TTL_SECONDS) -> None:
        self.ttl = ttl
        self._lock = threading.Lock()
        self._scanned_at = -1e9
        self._names: dict[str, float] = {}

    def invalidate(self) -> None:
        with self._lock:
            self._scanned_at = -1e9

    def names(self) -> dict[str, float]:
        now = time.monotonic()
        with self._lock:
            if now - self._scanned_at < self.ttl:
                return self._names
        names: dict[str, float] = {}
        for process in psutil.process_iter(["name", "create_time"]):
            try:
                name = (process.info.get("name") or "").lower()
                if name:
                    created = float(process.info.get("create_time") or 0.0)
                    names[name] = min(names.get(name, created), created) if name in names else created
            except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError, ValueError):
                continue
        with self._lock:
            self._names = names
            self._scanned_at = now
        return names

    def running(self, name: str) -> bool:
        return name.lower() in self.names()

    def created_at(self, name: str) -> float | None:
        return self.names().get(name.lower())


PROCESS_SCANNER = ProcessScanner()


def cs2_is_running() -> bool:
    return PROCESS_SCANNER.running("cs2.exe")


# FACEIT AC (C:\Program Files\FACEIT AC): the client window and its service.
FACEIT_AC_PROCESSES = ("faceitclient.exe", "faceitservice.exe")


def faceit_ac_is_running() -> bool:
    names = PROCESS_SCANNER.names()
    return any(name in names for name in FACEIT_AC_PROCESSES)


def msfs_session_seconds() -> float | None:
    """Return the current MSFS 2024 process age, or None when it is closed."""
    started_at = PROCESS_SCANNER.created_at("flightsimulator2024.exe")
    if started_at is None:
        return None
    return max(0.0, time.time() - (started_at or time.time()))


class EmbeddedSensorMonitor:
    """Read CPU sensors through an isolated helper bundled with the application."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started = False
        self._process: subprocess.Popen[str] | None = None
        self._values: dict[str, float | None] = {"cpu_temp": None, "cpu_freq": None, "cpu_power": None}

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._run, name="embedded-sensors", daemon=True).start()

    def read(self) -> dict[str, float | None]:
        cached = self._read_elevated_cache()
        if cached is not None:
            with self._lock:
                process = self._process
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                except Exception:
                    pass
            return cached
        # Never start the legacy Python/pythonnet fallback from the live UI.
        # Loading CoreCLR used to create dotnet/conhost processes whenever the
        # cache went stale; those short-lived windows could steal focus from a
        # fullscreen game.  The dedicated native WinExe sensor host owns all
        # retries now.  Missing data is safer than changing the foreground app.
        with self._lock:
            return dict(self._values)

    @staticmethod
    def _read_elevated_cache(
        path: Path = SENSOR_CACHE_PATH, max_age: float = 6.0,
    ) -> dict[str, float | None] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return None
            timestamp = float(payload.get("timestamp", 0.0))
            if timestamp <= 0 or time.time() - timestamp > max_age:
                return None
            return {
                "cpu_temp": float(payload["cpu_temp"]) if payload.get("cpu_temp") is not None else None,
                "cpu_freq": float(payload["cpu_freq"]) if payload.get("cpu_freq") is not None else None,
                "cpu_power": float(payload["cpu_power"]) if payload.get("cpu_power") is not None else None,
            }
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def close(self) -> None:
        with self._lock:
            process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except Exception:
                pass

    @staticmethod
    def _sensor_value(sensors: list[object], sensor_type: str, preferred_names: tuple[str, ...]) -> float | None:
        for preferred in preferred_names:
            for sensor in sensors:
                try:
                    if str(sensor.SensorType) != sensor_type or preferred.lower() not in str(sensor.Name).lower():
                        continue
                    value = float(sensor.Value)
                    if value > 0:
                        return value
                except (TypeError, ValueError):
                    continue
        return None

    def _run(self) -> None:
        process = None
        try:
            command = [sys.executable, "--sensor-bridge"] if getattr(sys, "frozen", False) else [
                sys.executable, str(ROOT / "app.py"), "--sensor-bridge",
            ]
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", creationflags=flags,
            )
            with self._lock:
                self._process = process
            if process.stdout is None:
                return
            for line in process.stdout:
                try:
                    values = json.loads(line)
                    with self._lock:
                        self._values = {
                            "cpu_temp": values.get("cpu_temp"),
                            "cpu_freq": values.get("cpu_freq"),
                        }
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
        except Exception:
            with self._lock:
                self._values = {"cpu_temp": None, "cpu_freq": None}
        finally:
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                except Exception:
                    pass
            with self._lock:
                self._started = False
                self._process = None


def run_sensor_bridge(
    cache_path: Path | None = None,
    continue_running: Callable[[], bool] | None = None,
) -> int:
    """Isolated sensor process; a native sensor failure cannot close the UI."""
    computer = None
    try:
        os.environ.setdefault("PYTHONNET_RUNTIME", "coreclr")
        if str(SENSOR_ROOT) not in sys.path:
            sys.path.insert(0, str(SENSOR_ROOT))
        from pythonnet import load
        load("coreclr")
        import clr
        clr.AddReference(str(SENSOR_ROOT / "LibreHardwareMonitorLib.dll"))
        from LibreHardwareMonitor.Hardware import Computer

        computer = Computer()
        computer.IsCpuEnabled = True
        computer.IsGpuEnabled = False
        computer.IsMemoryEnabled = False
        # Ryzen 9000 systems do not always expose Tctl/Tdie on the top-level
        # CPU node.  LibreHardwareMonitor can still obtain a useful CPU value
        # from the motherboard/Super-I/O subtree, so include and traverse it.
        computer.IsMotherboardEnabled = True
        computer.IsStorageEnabled = False
        computer.IsNetworkEnabled = False
        computer.Open()
        while continue_running is None or continue_running():
            sensors: list[object] = []
            pending = list(computer.Hardware)
            while pending:
                hardware = pending.pop(0)
                hardware.Update()
                sensors.extend(list(hardware.Sensors))
                pending.extend(list(hardware.SubHardware))
            cpu_temp = EmbeddedSensorMonitor._sensor_value(
                sensors, "Temperature", (
                    "Tctl/Tdie", "CPU Package", "Core (Tctl/Tdie)",
                    "CPU (Tctl/Tdie)", "CPU", "Core Average",
                ),
            )
            cpu_freq_mhz = EmbeddedSensorMonitor._sensor_value(
                sensors, "Clock", ("Cores (Average Effective)", "Cores (Average)", "Core #1"),
            )
            cpu_power = EmbeddedSensorMonitor._sensor_value(
                sensors, "Power", ("Package", "CPU Package", "CPU Cores"),
            )
            values = {
                "cpu_temp": cpu_temp,
                "cpu_power": cpu_power,
                "cpu_freq": (cpu_freq_mhz / 1000.0) if cpu_freq_mhz else None,
                "timestamp": time.time(),
            }
            line = json.dumps(values) + "\n"
            if cache_path is not None:
                try:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = cache_path.with_suffix(f".{os.getpid()}.tmp")
                    temporary.write_text(json.dumps(values), encoding="utf-8")
                    os.replace(temporary, cache_path)
                except OSError as exc:
                    # PermissionError while a reader holds the file: keep the
                    # bridge alive and retry on the next 0.5 s tick.
                    log_exception("sensor-bridge", "sensor cache write failed", exc)
            elif sys.stdout is not None:
                sys.stdout.write(line)
                sys.stdout.flush()
            elif os.name == "nt":
                payload = line.encode("utf-8")
                written = ctypes.c_ulong(0)
                handle = ctypes.windll.kernel32.GetStdHandle(-11)
                ctypes.windll.kernel32.WriteFile(handle, payload, len(payload), ctypes.byref(written), None)
            time.sleep(0.5)
    except Exception as exc:
        log_exception("sensor-bridge", "sensor bridge stopped", exc, 0)
        return 1
    finally:
        if computer is not None:
            try:
                computer.Close()
            except Exception:
                pass
    return 0


SENSOR_MONITOR = EmbeddedSensorMonitor()
atexit.register(SENSOR_MONITOR.close)


class WindowsTaskManagerMonitor:
    """Sample the same Windows performance counters used by Task Manager.

    PdhAddEnglishCounterW deliberately accepts the invariant English counter
    path on every Windows display language.  GPU engine counters are supplied
    by WDDM, so this works for NVIDIA, AMD and Intel without vendor tools.
    """

    PDH_FMT_DOUBLE = 0x00000200
    PDH_MORE_DATA = 0x800007D2
    ERROR_SUCCESS = 0

    class _CounterValue(ctypes.Structure):
        _fields_ = [("status", wintypes.DWORD), ("value", ctypes.c_double)]

    class _CounterItem(ctypes.Structure):
        pass

    _CounterItem._fields_ = [("name", wintypes.LPWSTR), ("counter", _CounterValue)]

    class _Guid(ctypes.Structure):
        _fields_ = [
            ("data1", wintypes.DWORD), ("data2", wintypes.WORD),
            ("data3", wintypes.WORD), ("data4", ctypes.c_ubyte * 8),
        ]

    class _Luid(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.LONG)]

    class _AdapterDescription(ctypes.Structure):
        pass

    _AdapterDescription._fields_ = [
        ("description", wintypes.WCHAR * 128),
        ("vendor_id", wintypes.UINT), ("device_id", wintypes.UINT),
        ("subsystem_id", wintypes.UINT), ("revision", wintypes.UINT),
        ("dedicated_video_memory", ctypes.c_size_t),
        ("dedicated_system_memory", ctypes.c_size_t),
        ("shared_system_memory", ctypes.c_size_t),
        ("luid", _Luid), ("flags", wintypes.UINT),
    ]

    # Task Manager collects its counters once per second and shows the raw
    # one-second rate; PDH already computes that rate between two collects,
    # so a 1 s collect interval with no extra averaging matches it.
    def __init__(self, sample_interval: float = 1.0) -> None:
        self.sample_interval = sample_interval
        self._lock = threading.Lock()
        self._started = False
        self._stop_event = threading.Event()
        self._cpu: float | None = None
        self._gpu: float | None = None
        self._cpu_ghz: float | None = None
        self._gpu_memory_mb: float | None = None
        self._discrete_luid: str | None = None
        window_size = max(1, round(1.0 / sample_interval))
        self._cpu_window: collections.deque[float] = collections.deque(maxlen=window_size)
        self._gpu_window: collections.deque[float] = collections.deque(maxlen=window_size)
        self._frequency_window: collections.deque[float] = collections.deque(maxlen=window_size)

    @staticmethod
    def _status(value: int) -> int:
        return ctypes.c_uint32(value).value

    @staticmethod
    def _gpu_engine_total(
        items: list[tuple[str, float]], target_luid: str | None = None,
    ) -> float | None:
        """Return Task Manager's overall GPU utilization for one adapter.

        The GPU Engine counter set exposes one row per process and engine
        (``pid_X_luid_..._phys_N_eng_M_engtype_T``).  Task Manager sums the
        per-process rows of each engine type (3D, Copy, VideoDecode, ...)
        and shows the busiest engine type as the GPU's utilization.
        """
        engines: dict[tuple[str, str], float] = {}
        for name, value in items:
            match = re.search(
                r"(?P<adapter>luid_0x[0-9a-f]+_0x[0-9a-f]+_phys_\d+)_eng_(?P<eng>\d+)"
                r"(?:_engtype_(?P<type>[A-Za-z0-9 ]*))?",
                name,
                flags=re.IGNORECASE,
            )
            if match is None or not math.isfinite(value) or value < 0:
                continue
            adapter = match.group("adapter").lower()
            if target_luid and not adapter.startswith(f"{target_luid.lower()}_phys_"):
                continue
            engine_type = (match.group("type") or "").strip().lower() or f"eng_{match.group('eng')}"
            key = (adapter, engine_type)
            engines[key] = engines.get(key, 0.0) + value
        if not engines:
            return None
        return min(100.0, max(engines.values()))

    @classmethod
    def _formatted_array(cls, pdh, counter: ctypes.c_void_p) -> list[tuple[str, float]]:
        byte_size = wintypes.DWORD(0)
        item_count = wintypes.DWORD(0)
        result = pdh.PdhGetFormattedCounterArrayW(
            counter, cls.PDH_FMT_DOUBLE, ctypes.byref(byte_size),
            ctypes.byref(item_count), None,
        )
        if cls._status(result) != cls.PDH_MORE_DATA or not byte_size.value:
            return []
        buffer = ctypes.create_string_buffer(byte_size.value)
        result = pdh.PdhGetFormattedCounterArrayW(
            counter, cls.PDH_FMT_DOUBLE, ctypes.byref(byte_size),
            ctypes.byref(item_count), buffer,
        )
        if cls._status(result) != cls.ERROR_SUCCESS:
            return []
        values: list[tuple[str, float]] = []
        items = ctypes.cast(buffer, ctypes.POINTER(cls._CounterItem))
        for index in range(item_count.value):
            item = items[index]
            if item.name and item.counter.status == 0:
                values.append((item.name, item.counter.value))
        return values

    @staticmethod
    def _dedicated_memory_mb(
        items: list[tuple[str, float]], target_luid: str | None,
    ) -> float | None:
        candidates = [
            value for name, value in items
            if value >= 0 and (
                target_luid is None or name.lower().startswith(f"{target_luid.lower()}_phys_")
            )
        ]
        if not candidates:
            return None
        return max(candidates) / (1024.0 ** 2)

    @staticmethod
    def _com_method(obj: ctypes.c_void_p, index: int, restype, *argtypes):
        table = ctypes.cast(
            obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
        ).contents
        return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(table[index])

    @classmethod
    def _find_discrete_gpu_adapter(cls) -> tuple[str | None, str | None]:
        """Use DXGI to select the hardware adapter with most dedicated VRAM."""
        if os.name != "nt":
            return None, None
        factory = ctypes.c_void_p()
        adapters: list[tuple[int, str, str]] = []
        try:
            iid = cls._Guid(
                0x770AAE78, 0xF26F, 0x4DBA,
                (ctypes.c_ubyte * 8)(0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87),
            )
            create_factory = ctypes.WinDLL("dxgi.dll").CreateDXGIFactory1
            create_factory.argtypes = [ctypes.POINTER(cls._Guid), ctypes.POINTER(ctypes.c_void_p)]
            if cls._status(create_factory(ctypes.byref(iid), ctypes.byref(factory))) != 0:
                return None, None
            enum_adapters = cls._com_method(
                factory, 12, wintypes.LONG, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
            )
            for index in range(32):
                adapter = ctypes.c_void_p()
                result = cls._status(enum_adapters(factory, index, ctypes.byref(adapter)))
                if result == 0x887A0002:  # DXGI_ERROR_NOT_FOUND
                    break
                if result != 0 or not adapter.value:
                    continue
                try:
                    description = cls._AdapterDescription()
                    get_description = cls._com_method(
                        adapter, 10, wintypes.LONG, ctypes.POINTER(cls._AdapterDescription),
                    )
                    if cls._status(get_description(adapter, ctypes.byref(description))) != 0:
                        continue
                    if description.flags & 0x2:  # DXGI_ADAPTER_FLAG_SOFTWARE
                        continue
                    high = ctypes.c_uint32(description.luid.high).value
                    luid = f"luid_0x{high:08x}_0x{description.luid.low:08x}"
                    adapters.append((
                        int(description.dedicated_video_memory), luid,
                        str(description.description).strip(),
                    ))
                finally:
                    cls._com_method(adapter, 2, wintypes.ULONG)(adapter)
            if not adapters:
                return None, None
            _memory, luid, name = max(adapters, key=lambda item: item[0])
            return luid, name
        except Exception:
            return None, None
        finally:
            if factory.value:
                try:
                    cls._com_method(factory, 2, wintypes.ULONG)(factory)
                except Exception:
                    pass

    @classmethod
    def _find_discrete_gpu_luid(cls) -> str | None:
        return cls._find_discrete_gpu_adapter()[0]

    def start(self) -> None:
        if os.name != "nt":
            return
        with self._lock:
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._run, name="task-manager-counters", daemon=True).start()

    def read(self) -> tuple[float | None, float | None, float | None, float | None]:
        self.start()
        with self._lock:
            return self._cpu, self._gpu, self._cpu_ghz, self._gpu_memory_mb

    def close(self) -> None:
        self._stop_event.set()

    def _run(self) -> None:
        query = ctypes.c_void_p()
        try:
            self._discrete_luid = self._find_discrete_gpu_adapter()[0]
            pdh = ctypes.WinDLL("pdh.dll")
            pdh.PdhOpenQueryW.argtypes = [
                wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p),
            ]
            pdh.PdhAddEnglishCounterW.argtypes = [
                ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_size_t,
                ctypes.POINTER(ctypes.c_void_p),
            ]
            pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
            pdh.PdhGetFormattedCounterValue.argtypes = [
                ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(self._CounterValue),
            ]
            pdh.PdhGetFormattedCounterArrayW.argtypes = [
                ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
            ]
            pdh.PdhCloseQuery.argtypes = [ctypes.c_void_p]
            if self._status(pdh.PdhOpenQueryW(None, 0, ctypes.byref(query))) != self.ERROR_SUCCESS:
                return
            cpu_counter = ctypes.c_void_p()
            gpu_counter = ctypes.c_void_p()
            frequency_counter = ctypes.c_void_p()
            gpu_memory_counter = ctypes.c_void_p()
            cpu_ok = self._status(pdh.PdhAddEnglishCounterW(
                query, r"\Processor Information(_Total)\% Processor Utility",
                0, ctypes.byref(cpu_counter),
            )) == self.ERROR_SUCCESS
            gpu_ok = self._status(pdh.PdhAddEnglishCounterW(
                query, r"\GPU Engine(*)\Utilization Percentage",
                0, ctypes.byref(gpu_counter),
            )) == self.ERROR_SUCCESS
            frequency_ok = self._status(pdh.PdhAddEnglishCounterW(
                query, r"\Processor Information(_Total)\Actual Frequency",
                0, ctypes.byref(frequency_counter),
            )) == self.ERROR_SUCCESS
            gpu_memory_ok = self._status(pdh.PdhAddEnglishCounterW(
                query, r"\GPU Adapter Memory(*)\Dedicated Usage",
                0, ctypes.byref(gpu_memory_counter),
            )) == self.ERROR_SUCCESS
            if not cpu_ok and not gpu_ok and not frequency_ok and not gpu_memory_ok:
                return
            if self._status(pdh.PdhCollectQueryData(query)) != self.ERROR_SUCCESS:
                return
            while not self._stop_event.wait(self.sample_interval):
                if self._status(pdh.PdhCollectQueryData(query)) != self.ERROR_SUCCESS:
                    continue
                cpu_value: float | None = None
                gpu_value: float | None = None
                frequency_ghz: float | None = None
                gpu_memory_mb: float | None = None
                if cpu_ok:
                    formatted = self._CounterValue()
                    result = pdh.PdhGetFormattedCounterValue(
                        cpu_counter, self.PDH_FMT_DOUBLE, None, ctypes.byref(formatted),
                    )
                    if self._status(result) == self.ERROR_SUCCESS and formatted.status == 0:
                        cpu_value = max(0.0, min(100.0, formatted.value))
                if gpu_ok:
                    gpu_value = self._gpu_engine_total(
                        self._formatted_array(pdh, gpu_counter), self._discrete_luid,
                    )
                if frequency_ok:
                    formatted = self._CounterValue()
                    result = pdh.PdhGetFormattedCounterValue(
                        frequency_counter, self.PDH_FMT_DOUBLE, None, ctypes.byref(formatted),
                    )
                    if self._status(result) == self.ERROR_SUCCESS and formatted.status == 0:
                        frequency_ghz = max(0.0, formatted.value / 1000.0)
                if gpu_memory_ok:
                    gpu_memory_mb = self._dedicated_memory_mb(
                        self._formatted_array(pdh, gpu_memory_counter), self._discrete_luid,
                    )
                with self._lock:
                    if cpu_value is not None:
                        self._cpu_window.append(cpu_value)
                        self._cpu = sum(self._cpu_window) / len(self._cpu_window)
                    if gpu_value is not None:
                        self._gpu_window.append(gpu_value)
                        self._gpu = sum(self._gpu_window) / len(self._gpu_window)
                    if frequency_ghz is not None:
                        self._frequency_window.append(frequency_ghz)
                        self._cpu_ghz = sum(self._frequency_window) / len(self._frequency_window)
                    if gpu_memory_mb is not None:
                        self._gpu_memory_mb = gpu_memory_mb
        except Exception:
            return
        finally:
            if query.value:
                try:
                    pdh.PdhCloseQuery(query)
                except Exception:
                    pass
            with self._lock:
                self._started = False


TASK_MANAGER_MONITOR = WindowsTaskManagerMonitor()
atexit.register(TASK_MANAGER_MONITOR.close)


def normalise_hardware_label(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:MAX_HARDWARE_LABEL_LENGTH]


def _short_cpu_name(name: str) -> str:
    patterns = (
        r"\b\d{4,5}X3D\b",
        r"\bi[3579]-\d{4,5}[A-Z]{0,3}\b",
        r"\bCore Ultra \d \d{3}[A-Z]{0,2}\b",
        r"\bRyzen \d \d{4,5}[A-Z0-9]*\b",
    )
    for pattern in patterns:
        match = re.search(pattern, name, flags=re.IGNORECASE)
        if match:
            return normalise_hardware_label(match.group(0).upper().replace("CORE ULTRA", "Ultra"))
    cleaned = re.sub(
        r"\b(AMD|Intel|Processor|CPU|\d+-Core|with Radeon Graphics)\b",
        "", name, flags=re.IGNORECASE,
    )
    return normalise_hardware_label(cleaned) or "CPU"


def _short_gpu_name(name: str) -> str:
    patterns = (
        r"\b(?:RTX|GTX)\s*\d{3,4}(?:\s*(?:Ti|SUPER))?\b",
        r"\bRX\s*\d{3,4}(?:\s*(?:XT|XTX))?\b",
        r"\bArc\s*[A-Z]\d{3}\b",
    )
    for pattern in patterns:
        match = re.search(pattern, name, flags=re.IGNORECASE)
        if match:
            label = re.sub(r"\s+", " ", match.group(0)).strip()
            label = re.sub(r"\bti\b", "Ti", label, flags=re.IGNORECASE)
            return normalise_hardware_label(label.upper().replace(" TI", " Ti"))
    cleaned = re.sub(
        r"\b(NVIDIA|AMD|Intel|GeForce|Radeon|Graphics|GPU)\b",
        "", name, flags=re.IGNORECASE,
    )
    return normalise_hardware_label(cleaned) or "GPU"


@functools.lru_cache(maxsize=1)
def get_auto_hardware_labels() -> dict[str, str]:
    cpu_name = ""
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            ) as key:
                cpu_name = str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        except OSError:
            cpu_name = ""
    _luid, gpu_name = WindowsTaskManagerMonitor._find_discrete_gpu_adapter()
    installed_gib = psutil.virtual_memory().total / (1024 ** 3)
    ram_gib = max(1, int(round(installed_gib / 4.0) * 4)) if installed_gib >= 6 else max(1, round(installed_gib))
    return {
        "cpu": _short_cpu_name(cpu_name) if cpu_name else "CPU",
        "gpu": _short_gpu_name(gpu_name or "GPU"),
        "ram": normalise_hardware_label(f"{ram_gib} GB RAM"),
    }


def resolve_hardware_labels(overrides: object = None) -> dict[str, str]:
    source = overrides if isinstance(overrides, dict) else {}
    automatic = get_auto_hardware_labels()
    return {
        key: normalise_hardware_label(source.get(key)) or automatic[key]
        for key in ("cpu", "gpu", "ram")
    }


class _NvmlUtilization(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class _NvmlMemoryInfo(ctypes.Structure):
    _fields_ = [
        ("total", ctypes.c_ulonglong),
        ("free", ctypes.c_ulonglong),
        ("used", ctypes.c_ulonglong),
    ]


class NvidiaNvmlMonitor:
    """Read the dedicated NVIDIA GPU without spawning nvidia-smi/conhost."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._dll = None
        self._handle = ctypes.c_void_p()
        self._initialised = False

    def _initialise(self) -> bool:
        if self._initialised:
            return self._dll is not None and bool(self._handle.value)
        self._initialised = True
        if os.name != "nt":
            return False
        try:
            dll = ctypes.WinDLL("nvml.dll")
            dll.nvmlInit_v2.restype = ctypes.c_int
            dll.nvmlDeviceGetCount_v2.argtypes = [ctypes.POINTER(ctypes.c_uint)]
            dll.nvmlDeviceGetCount_v2.restype = ctypes.c_int
            dll.nvmlDeviceGetHandleByIndex_v2.argtypes = [
                ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p),
            ]
            dll.nvmlDeviceGetHandleByIndex_v2.restype = ctypes.c_int
            dll.nvmlDeviceGetUtilizationRates.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(_NvmlUtilization),
            ]
            dll.nvmlDeviceGetUtilizationRates.restype = ctypes.c_int
            dll.nvmlDeviceGetTemperature.argtypes = [
                ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint),
            ]
            dll.nvmlDeviceGetTemperature.restype = ctypes.c_int
            dll.nvmlDeviceGetMemoryInfo.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(_NvmlMemoryInfo),
            ]
            dll.nvmlDeviceGetMemoryInfo.restype = ctypes.c_int
            try:
                dll.nvmlDeviceGetPowerUsage.argtypes = [
                    ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint),
                ]
                dll.nvmlDeviceGetPowerUsage.restype = ctypes.c_int
            except AttributeError:
                pass
            if dll.nvmlInit_v2() != 0:
                return False
            count = ctypes.c_uint()
            if dll.nvmlDeviceGetCount_v2(ctypes.byref(count)) != 0 or count.value == 0:
                return False
            selected = ctypes.c_void_p()
            selected_total = -1
            for index in range(count.value):
                handle = ctypes.c_void_p()
                if dll.nvmlDeviceGetHandleByIndex_v2(index, ctypes.byref(handle)) != 0:
                    continue
                memory = _NvmlMemoryInfo()
                total = 0
                if dll.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(memory)) == 0:
                    total = int(memory.total)
                if selected.value is None or total > selected_total:
                    selected = handle
                    selected_total = total
            if not selected.value:
                return False
            self._dll = dll
            self._handle = selected
            return True
        except (AttributeError, OSError, TypeError, ValueError):
            self._dll = None
            self._handle = ctypes.c_void_p()
            return False

    def read(self) -> tuple[float, float, float]:
        with self._lock:
            if not self._initialise() or self._dll is None:
                return 0.0, 0.0, 0.0
            utilization = _NvmlUtilization()
            temperature = ctypes.c_uint()
            memory = _NvmlMemoryInfo()
            load = (
                float(utilization.gpu)
                if self._dll.nvmlDeviceGetUtilizationRates(
                    self._handle, ctypes.byref(utilization),
                ) == 0 else 0.0
            )
            heat = (
                float(temperature.value)
                if self._dll.nvmlDeviceGetTemperature(
                    self._handle, 0, ctypes.byref(temperature),
                ) == 0 else 0.0
            )
            used_mb = (
                float(memory.used) / (1024.0 ** 2)
                if self._dll.nvmlDeviceGetMemoryInfo(
                    self._handle, ctypes.byref(memory),
                ) == 0 else 0.0
            )
            return load, heat, used_mb

    def read_power_watts(self) -> float | None:
        """Board power draw in watts (nvmlDeviceGetPowerUsage), if supported."""
        with self._lock:
            if not self._initialise() or self._dll is None:
                return None
            milliwatts = ctypes.c_uint()
            try:
                if self._dll.nvmlDeviceGetPowerUsage(self._handle, ctypes.byref(milliwatts)) != 0:
                    return None
            except (AttributeError, OSError):
                return None
            return milliwatts.value / 1000.0 if milliwatts.value else None


NVIDIA_MONITOR = NvidiaNvmlMonitor()


def get_gpu_metrics() -> tuple[float, float, float]:
    return NVIDIA_MONITOR.read()


RAM_SAMPLE_INTERVAL = 1.0
_RAM_SAMPLE: dict[str, object] = {"at": None, "value": None}
_RAM_SAMPLE_LOCK = threading.Lock()


def sample_virtual_memory(now: float | None = None):
    """psutil.virtual_memory(), sampled at most once per second."""
    now = time.monotonic() if now is None else now
    with _RAM_SAMPLE_LOCK:
        taken = _RAM_SAMPLE["at"]
        if taken is None or now - float(taken) >= RAM_SAMPLE_INTERVAL or _RAM_SAMPLE["value"] is None:
            _RAM_SAMPLE["value"] = psutil.virtual_memory()
            _RAM_SAMPLE["at"] = now
        return _RAM_SAMPLE["value"]


_METRIC_SOURCES_LOGGED = {"done": False, "calls": 0}


def _log_metric_sources_once(cpu: str, gpu: str, gpu_temp: str, final: bool) -> None:
    """Record once which counters feed CPU/GPU/RAM (after PDH warmed up)."""
    state = _METRIC_SOURCES_LOGGED
    if state["done"]:
        return
    state["calls"] += 1
    if not final and state["calls"] < 20:
        return
    state["done"] = True
    log_info(
        "metrics",
        f"metric sources; cpu={cpu}; gpu={gpu}; gpu_temp={gpu_temp}; "
        "ram=total-available (Task Manager 'In use'); interval=1s",
    )


def get_metrics() -> dict[str, float | None]:
    fallback_cpu_load = psutil.cpu_percent(interval=None)
    memory = sample_virtual_memory()
    fallback_gpu_load, gpu_temp, fallback_gpu_memory = get_gpu_metrics()
    task_cpu_load, task_gpu_load, task_cpu_frequency, task_gpu_memory = TASK_MANAGER_MONITOR.read()
    cpu_load = task_cpu_load if task_cpu_load is not None else fallback_cpu_load
    gpu_load = task_gpu_load if task_gpu_load is not None else fallback_gpu_load
    sensor_values = SENSOR_MONITOR.read()
    cpu_frequency = task_cpu_frequency
    if cpu_frequency is None:
        cpu_frequency = sensor_values["cpu_freq"]
    if cpu_frequency is None:
        try:
            frequency = psutil.cpu_freq()
            cpu_frequency = (float(frequency.current) / 1000.0) if frequency else None
        except Exception:
            cpu_frequency = None
    _log_metric_sources_once(
        r"PDH \Processor Information(_Total)\% Processor Utility" if task_cpu_load is not None
        else "psutil.cpu_percent",
        r"PDH \GPU Engine(*)\Utilization Percentage (max engine type)" if task_gpu_load is not None
        else "NVML utilization",
        "NVML" if gpu_temp > 0 else "none",
        task_cpu_load is not None and task_gpu_load is not None,
    )
    return {
        "cpu_load": cpu_load,
        "cpu_freq": cpu_frequency,
        "cpu_temp": sensor_values["cpu_temp"],
        "gpu_load": gpu_load,
        "gpu_temp": gpu_temp if gpu_temp > 0 else None,
        "gpu_memory": task_gpu_memory if task_gpu_memory is not None else fallback_gpu_memory,
        "gpu_power": NVIDIA_MONITOR.read_power_watts(),
        "cpu_power": sensor_values.get("cpu_power"),
        # Task Manager defines used RAM as Total - Available.  This explicitly
        # matches that formula on Windows instead of relying on platform-
        # specific psutil percentage semantics.
        "ram_load": ((memory.total - memory.available) / memory.total * 100.0) if memory.total else 0.0,
        "ram_used": (memory.total - memory.available) / (1024 ** 3),
        "ram_used_gb": (memory.total - memory.available) / (1024 ** 3),
        "ram_total_gb": memory.total / (1024 ** 3),
    }


def draw_smooth_arc(
    image: Image.Image,
    bbox: tuple[int, int, int, int],
    start: float,
    end: float,
    fill: str,
    width: int,
) -> None:
    """Draw one continuous anti-aliased arc without wrap seams or endpoint dots."""
    if end <= start:
        return
    _paste_supersampled(image, bbox, ((start, end, fill),), width)


def _paste_supersampled(
    image: Image.Image,
    bbox: tuple[int, int, int, int],
    sectors: tuple[tuple[float, float, str], ...],
    width: int,
    scale: int = 4,
) -> None:
    """Anti-alias only the ring's own bounding box instead of a 4x full frame.

    The old implementation allocated and LANCZOS-resized a 1920x1280 RGBA
    layer for every ring on every frame; the local layer is ~40x smaller.
    """
    left, top, right, bottom = bbox
    pad = 2
    x0, y0 = max(0, left - pad), max(0, top - pad)
    x1, y1 = min(image.width, right + pad + 1), min(image.height, bottom + pad + 1)
    if x1 <= x0 or y1 <= y0:
        return
    local_bbox = (left - x0, top - y0, right - x0, bottom - y0)
    layer = Image.new("RGBA", ((x1 - x0) * scale, (y1 - y0) * scale), (0, 0, 0, 0))
    layer_draw = ImageDraw.Draw(layer)
    for start, end, fill in sectors:
        if end > start:
            layer_draw.polygon(
                _annular_sector_points(local_bbox, start, end, width, scale), fill=fill,
            )
    layer = layer.resize((x1 - x0, y1 - y0), Image.Resampling.LANCZOS)
    image.paste(layer, (x0, y0), layer)


def _annular_sector_points(
    bbox: tuple[int, int, int, int],
    start: float,
    end: float,
    width: int,
    scale: int,
) -> list[tuple[float, float]]:
    left, top, right, bottom = (value * scale for value in bbox)
    cx = (left + right) / 2.0
    cy = (top + bottom) / 2.0
    outer_rx = max(1.0, (right - left) / 2.0)
    outer_ry = max(1.0, (bottom - top) / 2.0)
    inner_rx = max(0.5, outer_rx - (width * scale))
    inner_ry = max(0.5, outer_ry - (width * scale))
    span = end - start
    sample_count = max(2, int(math.ceil(span * 2.0)) + 1)
    outer_points = []
    inner_points = []
    for index in range(sample_count):
        angle = math.radians(start + (span * index / (sample_count - 1)))
        outer_points.append((
            cx + outer_rx * math.cos(angle),
            cy + outer_ry * math.sin(angle),
        ))
        inner_points.append((
            cx + inner_rx * math.cos(angle),
            cy + inner_ry * math.sin(angle),
        ))
    return [*outer_points, *reversed(inner_points)]


def draw_smooth_progress_ring(
    image: Image.Image,
    bbox: tuple[int, int, int, int],
    start: float,
    end: float,
    progress_end: float,
    track_fill: str,
    progress_fill: str,
    width: int,
) -> None:
    """Render track and progress together so their shared cap has no blend seam."""
    # Downsample only once. Two independently antialiased layers created the
    # bright triangular wedge at the common open-ring start on the LCD.
    _paste_supersampled(image, bbox, (
        (start, end, track_fill),
        (start, min(progress_end, end), progress_fill),
    ), width)


def point_on_circle(cx: int, cy: int, radius: int, angle_deg: float) -> tuple[float, float]:
    angle = math.radians(angle_deg)
    return cx + radius * math.cos(angle), cy + radius * math.sin(angle)


def mix_color(first: str, second: str, amount: float) -> str:
    amount = max(0.0, min(1.0, amount))
    a = ImageColor.getrgb(first)
    b = ImageColor.getrgb(second)
    rgb = tuple(round(a[index] + (b[index] - a[index]) * amount) for index in range(3))
    return "#%02x%02x%02x" % rgb


def _level_color(level: int, colors: dict[str, str]) -> str:
    return (colors["cpu"], colors["warning"], colors["danger"])[level]


def load_color(value: float, component: str, colors: dict[str, str]) -> str:
    """Theme-aware 3-level scale using the shared thresholds."""
    return _level_color(_thresholds.level(component, "load", value), colors)


def _critical_blink_color(
    base: str, colors: dict[str, str], animation_time: float | None,
) -> str:
    if animation_time is None:
        return colors["danger"]
    # Match the physical display's 0.5 s refresh cadence. Alternating two
    # fully bright colors is much more legible than fading toward the dark UI.
    return colors["danger"] if int(animation_time / 0.5) % 2 == 0 else colors["text"]


def load_alert_color(
    value: float,
    component: str,
    colors: dict[str, str],
    animation_time: float | None = None,
) -> str | None:
    """Blink the central load value once it reaches the critical level."""
    if _thresholds.level(component, "load", value) != _thresholds.CRITICAL:
        return None
    return _critical_blink_color(load_color(value, component, colors), colors, animation_time)


def temperature_color(
    value: float | None, component: str, colors: dict[str, str],
) -> str:
    """Theme-aware 3-level thermal scale using the shared thresholds."""
    lvl = _thresholds.level(component, "temp", value)
    if lvl is None:
        return colors["dim"]
    return _level_color(lvl, colors)


def temperature_unit_color(
    value: float | None,
    component: str,
    colors: dict[str, str],
    animation_time: float | None = None,
) -> str:
    """Animate only the °C unit when the temperature needs attention."""
    base = temperature_color(value, component, colors)
    if value is None or animation_time is None:
        return base
    lvl = _thresholds.level(component, "temp", value)
    if lvl == _thresholds.NORMAL:
        return base
    if lvl == _thresholds.WARNING:
        # A slow, soft pulse is noticeable without making the dashboard restless.
        wave = (math.sin((animation_time / 1.6) * math.tau) + 1.0) / 2.0
        return mix_color(colors["dim"], base, 0.42 + (0.58 * wave))
    # Critical temperatures use a faster, high-contrast blink.
    return _critical_blink_color(base, colors, animation_time)


def temperature_number_color(
    value: float | None,
    component: str,
    colors: dict[str, str],
    animation_time: float | None = None,
) -> str:
    """Keep the number calm until the critical level is reached."""
    if value is None:
        return colors["dim"]
    if _thresholds.level(component, "temp", value) != _thresholds.CRITICAL:
        return colors["text"]
    return temperature_unit_color(value, component, colors, animation_time)


def draw_open_ring(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    center: tuple[int, int],
    radius: int,
    value: float,
    color: str,
    label: str,
    sensor_text: str,
    detail_text: str,
    colors: dict[str, str],
    value_color: str | None = None,
    percent_color: str | None = None,
    sensor_unit_color: str | None = None,
    sensor_number_color: str | None = None,
) -> None:
    cx, cy = center
    width = 8
    start = 135.0
    span = 270.0
    end = start + span
    bbox = (cx - radius, cy - radius, cx + radius, cy + radius)
    clamped = max(0.0, min(100.0, value))
    progress_end = start + span * clamped / 100.0
    draw_smooth_progress_ring(
        image, bbox, start, end, progress_end,
        colors["track"], color, width,
    )

    value_text = f"{int(round(clamped))}"
    # Fixed slot sized for "100": the number is right-aligned in it so the
    # "%" sign never moves when the load goes 9 -> 10 or 99 -> 100.
    value_width = draw.textlength("100", font=FONTS["value"])
    percent_width = draw.textlength("%", font=FONTS["gauge_unit"])
    value_gap = 4
    value_left = cx - (value_width + value_gap + percent_width) / 2
    draw.text(
        (value_left + value_width, cy - 6), value_text,
        font=FONTS["value"], fill=value_color or colors["text"], anchor="rm",
    )
    draw.text(
        (value_left + value_width + value_gap, cy - 3), "%",
        font=FONTS["gauge_unit"],
        fill=percent_color or colors["text"], anchor="lm",
    )
    original_label = normalise_hardware_label(label)
    label = original_label
    while label and draw.textlength(label, font=FONTS["hardware_label"]) > 110:
        label = label[:-1].rstrip(" …")
    if label != original_label:
        while label and draw.textlength(label + "…", font=FONTS["hardware_label"]) > 110:
            label = label[:-1].rstrip(" …")
        label = label + "…"
    draw.text(
        (cx, cy - radius - 14), label, font=FONTS["hardware_label"],
        fill=colors["text"], anchor="mm",
    )
    if sensor_text.startswith("—"):
        pass  # unavailable sensor: hidden, never a dash placeholder
    elif sensor_text.endswith("°C"):
        number_text, unit_text = sensor_text[:-2], "°C"
        number_width = draw.textlength("100", font=FONTS["sensor"])
        unit_width = draw.textlength(unit_text, font=FONTS["sensor"])
        start_x = cx - (number_width + unit_width) / 2
        draw.text(
            (start_x + number_width, cy + 28), number_text, font=FONTS["sensor"],
            fill=sensor_number_color or colors["text"], anchor="rm",
        )
        draw.text(
            (start_x + number_width, cy + 28), unit_text, font=FONTS["sensor"],
            fill=sensor_unit_color or colors["cpu"], anchor="lm",
        )
    else:
        sensor_font = FONTS["ram_sensor"] if sensor_text.endswith(" GB") else FONTS["sensor"]
        if draw.textlength(sensor_text, font=sensor_font) > 72:
            sensor_font = FONTS["sensor_compact"]
        draw.text(
            (cx, cy + 28), sensor_text, font=sensor_font,
            fill=colors["text"], anchor="mm",
        )
    draw.text(
        (cx, cy + radius + 8), detail_text, font=FONTS["detail"],
        fill=colors["text"], anchor="mm",
    )


def _temperature(value: float | None) -> str:
    return "—°C" if value is None else f"{int(round(value))}°C"


def _frequency(value: float | None) -> str:
    return _t("Canlı hız bekleniyor") if value is None else f"{value:.2f} GHz"


@functools.lru_cache(maxsize=32)
def _loaded_icon_alpha(name: str) -> Image.Image | None:
    path = ICON_ROOT / f"{name}.png"
    try:
        return Image.open(path).convert("RGBA")
    except (OSError, ValueError):
        return None


def _paste_tinted_icon(
    image: Image.Image, name: str, box: tuple[int, int, int, int], color: str,
) -> bool:
    source = _loaded_icon_alpha(name)
    if source is None:
        return False
    width = max(1, box[2] - box[0])
    height = max(1, box[3] - box[1])
    icon = source.copy()
    icon.thumbnail((width, height), Image.Resampling.LANCZOS)
    alpha = icon.getchannel("A")
    tinted = Image.new("RGBA", icon.size, (*ImageColor.getrgb(color), 255))
    tinted.putalpha(alpha)
    left = box[0] + ((width - icon.width) // 2)
    top = box[1] + ((height - icon.height) // 2)
    image.paste(tinted, (left, top), tinted)
    return True


def weather_condition_kind(weather_code: int) -> str:
    if weather_code == 0:
        return "clear"
    if weather_code in {45, 48}:
        return "fog"
    if weather_code in {1, 2, 3}:
        return "cloud"
    if weather_code in {71, 73, 75, 77, 85, 86}:
        return "snow"
    if weather_code in {95, 96, 99}:
        return "storm"
    return "rain"


def _draw_weather_icon(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    center_x: int,
    center_y: int,
    weather_code: int,
    is_day: bool,
    colors: dict[str, str],
) -> None:
    kind = weather_condition_kind(weather_code)
    names = {
        "clear": "sun" if is_day else "moon",
        "cloud": "cloud", "rain": "cloud-rain", "snow": "snowflake",
        "storm": "cloud-lightning", "fog": "cloud-fog",
    }
    icon_colors = {
        "clear": colors["warning"] if is_day else colors["text"],
        "cloud": colors["muted"], "rain": colors["ram"],
        "snow": colors["text"], "storm": colors["warning"],
        "fog": colors["muted"],
    }
    if not _paste_tinted_icon(
        image, names[kind],
        (center_x - 17, center_y - 17, center_x + 17, center_y + 17),
        icon_colors[kind],
    ):
        draw.ellipse((center_x - 8, center_y - 8, center_x + 8, center_y + 8), outline=icon_colors[kind], width=2)


def draw_weather_panel(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    readings: tuple[WeatherReading, ...],
    colors: dict[str, str],
    battery_devices: tuple[BatteryDevice, ...] = (),
) -> None:
    content_right = 320 if battery_devices else 449
    visible = readings[:3]
    if visible:
        column_width = (content_right - 28) / len(visible)
        for index, reading in enumerate(visible):
            center_x = round(28 + column_width * (index + 0.5))
            if index:
                divider_x = round(28 + column_width * index)
                draw.line((divider_x, 184, divider_x, 282), fill=colors["edge"], width=1)
            _draw_weather_icon(
                image, draw, center_x, 213, reading.weather_code, reading.is_day, colors,
            )
            draw.text(
                (center_x, 254),
                _fit_text(draw, reading.city, FONTS["weather_city"], column_width - 8),
                font=FONTS["weather_city"], fill=colors["text"], anchor="mm",
            )
            draw.text(
                (center_x, 280), f"{int(round(reading.temperature_c))}°",
                font=FONTS["label"], fill=colors["text"], anchor="mm",
            )
        draw.text(
            (content_right, 299), "Open-Meteo",
            font=FONTS["tiny"], fill=colors["dim"], anchor="rs",
        )
    if battery_devices:
        draw_battery_devices(image, draw, battery_devices, colors)


def _fit_text(
    draw: ImageDraw.ImageDraw,
    value: str,
    font: ImageFont.FreeTypeFont,
    max_width: float,
) -> str:
    text = " ".join(value.split())
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1].rstrip()
    return text + "…" if text else ""


def draw_media_panel(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    media: MediaSnapshot,
    spectrum: AudioSpectrumSnapshot,
    colors: dict[str, str],
    battery_devices: tuple[BatteryDevice, ...] = (),
) -> None:
    has_metadata = media.visible
    platform = media_service_name(media) if has_metadata else _t("Sistem sesi")
    title = sanitise_media_text(media.title) if has_metadata else _t("Ses oynatılıyor")
    artist = sanitise_media_text(media.artist) if has_metadata else ""

    draw.text((28, 181), platform, font=FONTS["small_bold"], fill=colors["text"])
    text_width = 300 if battery_devices else 421
    draw.text(
        (28, 209), _fit_text(draw, title, FONTS["label"], text_width),
        font=FONTS["label"], fill=colors["text"], anchor="lm",
    )
    if artist:
        draw.text(
            (28, 231), _fit_text(draw, artist, FONTS["small"], text_width),
            font=FONTS["small"], fill=colors["muted"], anchor="lm",
        )

    visual_region = MEDIA_VIS_REGION_WITH_BATTERY if battery_devices else MEDIA_VIS_REGION
    draw_media_spectrum(
        draw, spectrum, colors,
        origin=(visual_region[0], visual_region[1]), region=visual_region,
    )
    if battery_devices:
        draw_battery_devices(image, draw, battery_devices, colors)


def _battery_color(percent: int, colors: dict[str, str]) -> str:
    if percent <= 20:
        return colors["danger"]
    if percent <= 40:
        return colors["warning"]
    # A healthy battery is information, not a warning. Theme accents stay
    # reserved for gauges and exceptional states so every palette is legible.
    return colors["text"]


def _draw_device_icon(
    image: Image.Image, draw: ImageDraw.ImageDraw, kind: str, x: int, y: int, color: str,
) -> None:
    icon_name = {
        "mouse": "mouse", "headset": "headphones", "headphones": "headphones",
        "keyboard": "keyboard", "controller": "gamepad-2", "gamepad": "gamepad-2",
    }.get(kind, "gamepad-2")
    if not _paste_tinted_icon(image, icon_name, (x, y, x + 19, y + 19), color):
        draw.rounded_rectangle((x + 1, y + 2, x + 17, y + 16), radius=2, outline=color, width=2)


def draw_battery_devices(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    devices: tuple[BatteryDevice, ...],
    colors: dict[str, str],
) -> None:
    devices = devices[:3]
    if not devices:
        return
    draw.rectangle((338, 176, 455, 294), fill=colors["panel"])
    draw.line((337, 176, 337, 294), fill=colors["edge"], width=1)
    # A single, centred battery glyph labels the rail without spending the
    # horizontal space needed by three peripheral rows.
    header_color = colors["muted"]
    draw.rounded_rectangle((378, 182, 409, 192), radius=3, outline=header_color, width=2)
    draw.rectangle((409, 185, 413, 189), fill=header_color)
    draw.rounded_rectangle((382, 185, 400, 189), radius=1, fill=header_color)
    labels = {"mouse": "Mouse", "headset": _t("Kulaklık"), "keyboard": _t("Klavye")}
    row_centers = {
        1: (248,),
        2: (228, 270),
        3: (215, 251, 286),
    }[len(devices)]
    for device, center_y in zip(devices, row_centers):
        color = _battery_color(device.percent, colors)
        _draw_device_icon(image, draw, device.kind, 347, center_y - 9, color)
        percent_text = f"{device.percent}%"
        label_width = 451 - 370 - 4 - draw.textlength(percent_text, font=FONTS["small_bold"])
        label_text = labels.get(device.kind) or device.name
        label_font = FONTS["small_bold"]
        if draw.textlength(label_text, font=label_font) > label_width:
            label_font = FONTS["tiny_bold"]  # e.g. "Keyboard" next to "100%"
        label = _fit_text(draw, label_text, label_font, label_width)
        draw.text(
            (370, center_y), label,
            font=label_font, fill=colors["text"], anchor="lm",
        )
        draw.text(
            (451, center_y), percent_text,
            font=FONTS["small_bold"], fill=color, anchor="rm",
        )
        dpi = getattr(device, "dpi", None)
        if dpi is not None and len(devices) < 3:
            # Rows are 42+ px apart with one or two devices: room for DPI.
            draw.text(
                (451, center_y + 15), f"{dpi} DPI", font=FONTS["tiny"],
                fill=colors["muted"],
                anchor="rm",
            )
        if device.charging:
            draw.polygon(
                ((364, center_y - 8), (360, center_y), (364, center_y),
                 (361, center_y + 8), (368, center_y - 2), (364, center_y - 2)),
                fill=color,
            )


def draw_msfs_aircraft_backdrop(
    draw: ImageDraw.ImageDraw,
    colors: dict[str, str],
    left: int,
    right: int,
) -> None:
    """Draw a quiet, theme-aware aircraft silhouette behind flight data."""
    center_x = round((left + right) / 2)
    wing_half = max(65, round((right - left) * 0.42))
    color = mix_color(colors["panel"], colors["muted"], 0.11)
    draw.polygon((
        (center_x, 188),
        (center_x + 5, 215),
        (center_x + 6, 239),
        (center_x + wing_half, 264),
        (center_x + wing_half, 269),
        (center_x + 7, 257),
        (center_x + 7, 285),
        (center_x + 29, 296),
        (center_x + 28, 300),
        (center_x + 4, 293),
        (center_x, 302),
        (center_x - 4, 293),
        (center_x - 28, 300),
        (center_x - 29, 296),
        (center_x - 7, 285),
        (center_x - 7, 257),
        (center_x - wing_half, 269),
        (center_x - wing_half, 264),
        (center_x - 6, 239),
        (center_x - 5, 215),
    ), fill=color)


def format_airspeed_kmh(knots: float) -> str:
    return f"{round(float(knots) * 1.852)} {_t('km/sa')}"


def format_altitude_km(feet: float) -> str:
    kilometres = float(feet) * 0.0003048
    rendered = f"{kilometres:.2f}"
    if _RENDER_LANGUAGE.get() == "tr":
        rendered = rendered.replace(".", ",")
    return rendered + " km"


def draw_msfs_panel(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    colors: dict[str, str],
    telemetry: FlightTelemetry,
    battery_devices: tuple[BatteryDevice, ...] = (),
    layout: str = "classic",
) -> None:
    """Show flight data using the selected compact layout."""
    right = 325 if battery_devices else 452
    draw_msfs_aircraft_backdrop(draw, colors, 28, right)
    draw.text((28, 181), "MSFS 2024", font=FONTS["small_bold"], fill=colors["text"])
    has_values = telemetry.updated_at > 0 and all(
        value is not None for value in (
            telemetry.altitude_ft, telemetry.airspeed_kt,
            telemetry.vertical_speed_fpm, telemetry.heading_deg,
        )
    )
    if not has_values:
        draw.text(
            ((28 + right) // 2, 232), _t("Uçuş verisi bekleniyor"),
            font=FONTS["label"], fill=colors["text"], anchor="mm",
        )
        draw.text(
            ((28 + right) // 2, 261), _t("Kokpit açıldığında otomatik bağlanır"),
            font=FONTS["small"], fill=colors["muted"], anchor="mm",
        )
    elif layout == "classic":
        values = (
            (
                _t("İrtifa"), f"{telemetry.altitude_ft:,.0f}" if _RENDER_LANGUAGE.get() == "en" else f"{telemetry.altitude_ft:,.0f}".replace(",", "."), "ft",
                format_altitude_km(float(telemetry.altitude_ft)),
            ),
            (
                _t("Hava hızı"), f"{telemetry.airspeed_kt:.0f}", "kt",
                format_airspeed_kmh(float(telemetry.airspeed_kt)),
            ),
            (_t("Dikey hız"), f"{telemetry.vertical_speed_fpm:+.0f}", _t("ft/dk"), ""),
            (_t("İstikamet"), f"{telemetry.heading_deg:03.0f}", "°", ""),
        )
        if battery_devices:
            positions = ((98, 209), (250, 209), (98, 261), (250, 261))
            for (label, value, unit, secondary), (center_x, label_y) in zip(values, positions):
                draw.text(
                    (center_x, label_y), label, font=FONTS["small_bold"],
                    fill=colors["text"], anchor="mm",
                )
                value_width = draw.textlength(value, font=FONTS["money"])
                unit_width = draw.textlength(unit, font=FONTS["tiny"])
                secondary_width = draw.textlength(secondary, font=FONTS["tiny"]) if secondary else 0
                total_width = value_width + 4 + unit_width + (
                    7 + secondary_width if secondary else 0
                )
                start_x = center_x - total_width / 2
                draw.text(
                    (start_x, label_y + 27), value, font=FONTS["money"],
                    fill=colors["text"], anchor="lm",
                )
                draw.text(
                    (start_x + value_width + 4, label_y + 29), unit,
                    font=FONTS["tiny"], fill=colors["cpu"], anchor="lm",
                )
                if secondary:
                    draw.text(
                        (start_x + value_width + unit_width + 11, label_y + 29),
                        secondary, font=FONTS["tiny"], fill=colors["muted"], anchor="lm",
                    )
        else:
            centers = (80, 188, 296, 404)
            for (label, value, unit, secondary), center_x in zip(values, centers):
                draw.text(
                    (center_x, 216), label, font=FONTS["small_bold"],
                    fill=colors["text"], anchor="mm",
                )
                draw.text(
                    (center_x, 254), value, font=FONTS["money"],
                    fill=colors["text"], anchor="mm",
                )
                draw.text(
                    (center_x, 279),
                    f"{unit} · {secondary}" if secondary else unit,
                    font=FONTS["tiny"],
                    fill=colors["cpu"], anchor="mm",
                )
    else:
        left = 28
        split = round((left + right) / 2)
        top_right_x = split + 13

        def draw_primary(
            label: str, value: str, unit: str, secondary: str, x: int,
        ) -> None:
            draw.text(
                (x, 200), label, font=FONTS["small_bold"],
                fill=colors["text"], anchor="la",
            )
            draw.text(
                (x, 244), value, font=FONTS["value"],
                fill=colors["text"], anchor="ls",
            )
            value_width = max(
                draw.textlength(value, font=FONTS["value"]),
                draw.textlength("000", font=FONTS["value"]),
            )
            unit_x = x + value_width + 5
            draw.text(
                (unit_x, 243), unit, font=FONTS["small_bold"],
                fill=colors["cpu"], anchor="ls",
            )
            unit_width = draw.textlength(unit, font=FONTS["small_bold"])
            draw.text(
                (unit_x + unit_width + 6, 242), secondary,
                font=FONTS["tiny"], fill=colors["muted"], anchor="ls",
            )

        draw_primary(
            _t("Hava hızı"), f"{telemetry.airspeed_kt:.0f}", "kt",
            format_airspeed_kmh(float(telemetry.airspeed_kt)), left,
        )
        draw_primary(
            _t("İrtifa"), f"{telemetry.altitude_ft:,.0f}" if _RENDER_LANGUAGE.get() == "en" else f"{telemetry.altitude_ft:,.0f}".replace(",", "."),
            "ft", format_altitude_km(float(telemetry.altitude_ft)), top_right_x,
        )
        draw.line((split, 201, split, 244), fill=colors["edge"], width=1)
        draw.line((left, 251, right, 251), fill=colors["edge"], width=1)

        # The secondary row behaves like a compact flight deck: climb rate at
        # the left, heading tape at the right.  It avoids four equal cards and
        # keeps the two primary values dominant from arm's length.
        secondary_split = left + round((right - left) * 0.43)
        vertical_speed = float(telemetry.vertical_speed_fpm)
        speed_color = colors["cpu"] if vertical_speed >= 0 else colors["warning"]
        draw.text(
            (left, 255), _t("Dikey hız"), font=FONTS["small_bold"],
            fill=colors["text"], anchor="la",
        )
        arrow_y = 285
        if vertical_speed >= 0:
            draw.line((left + 3, arrow_y + 7, left + 3, arrow_y - 6), fill=speed_color, width=2)
            draw.polygon(
                ((left - 1, arrow_y - 3), (left + 3, arrow_y - 8), (left + 7, arrow_y - 3)),
                fill=speed_color,
            )
        else:
            draw.line((left + 3, arrow_y - 7, left + 3, arrow_y + 6), fill=speed_color, width=2)
            draw.polygon(
                ((left - 1, arrow_y + 3), (left + 3, arrow_y + 8), (left + 7, arrow_y + 3)),
                fill=speed_color,
            )
        vs_value = f"{vertical_speed:+.0f}"
        draw.text(
            (left + 13, 295), vs_value, font=FONTS["money"],
            fill=colors["text"], anchor="ls",
        )
        vs_width = draw.textlength(vs_value, font=FONTS["money"])
        draw.text(
            (left + 17 + vs_width, 294), _t("ft/dk"), font=FONTS["tiny"],
            fill=speed_color, anchor="ls",
        )

        tape_left = secondary_split + 10
        tape_right = right
        tape_center = round((tape_left + tape_right) / 2)
        heading = float(telemetry.heading_deg) % 360.0
        draw.text(
            (tape_left, 255), _t("İstikamet"), font=FONTS["small_bold"],
            fill=colors["text"], anchor="la",
        )
        draw.text(
            (tape_right, 255), f"{heading:03.0f}°", font=FONTS["label"],
            fill=colors["cpu"], anchor="ra",
        )
        draw.line((tape_left, 294, tape_right, 294), fill=colors["edge"], width=1)
        for offset in (-2, -1, 0, 1, 2):
            tick_x = tape_center + (offset * 25)
            if tape_left <= tick_x <= tape_right:
                tick_height = 8 if offset == 0 else 4
                draw.line(
                    (tick_x, 294 - tick_height, tick_x, 294),
                    fill=colors["cpu"] if offset == 0 else colors["muted"], width=1,
                )
        draw.polygon(
            ((tape_center - 4, 299), (tape_center + 4, 299), (tape_center, 294)),
            fill=colors["cpu"],
        )
    if battery_devices:
        draw_battery_devices(image, draw, battery_devices, colors)


def draw_media_spectrum(
    draw: ImageDraw.ImageDraw,
    spectrum: AudioSpectrumSnapshot,
    colors: dict[str, str],
    origin: tuple[int, int] = (MEDIA_VIS_REGION[0], MEDIA_VIS_REGION[1]),
    region: tuple[int, int, int, int] = MEDIA_VIS_REGION,
) -> None:
    bars = spectrum.bars if spectrum.bars else (0.0,) * 16
    bar_width, gap = 7, 4
    total_width = (len(bars) * bar_width) + ((len(bars) - 1) * gap)
    region_width = region[2] - region[0]
    region_height = region[3] - region[1]
    left = origin[0] + ((region_width - total_width) // 2)
    bottom = origin[1] + region_height - 1
    for index, level in enumerate(bars):
        height = max(3, round(3 + (max(0.0, min(1.0, level)) * 25)))
        x = left + index * (bar_width + gap)
        color = mix_color(
            colors["cpu"], colors["gpu"], index / max(1, len(bars) - 1),
        )
        draw.rounded_rectangle(
            (x, bottom - height, x + bar_width, bottom), radius=2, fill=color,
        )


def render_media_visual_patch(
    spectrum: AudioSpectrumSnapshot,
    colors: dict[str, str],
    with_battery: bool = False,
) -> Image.Image:
    """Render only the live spectrum, avoiding the 100+ ms full-frame path."""
    region = MEDIA_VIS_REGION_WITH_BATTERY if with_battery else MEDIA_VIS_REGION
    width = region[2] - region[0]
    height = region[3] - region[1]
    patch = Image.new("RGB", (width, height), colors["panel"])
    draw_media_spectrum(
        ImageDraw.Draw(patch), spectrum, colors, origin=(0, 0), region=region,
    )
    return patch


def _vram_label(megabytes: float | None) -> str:
    """Same unit as the Claude design: GB once the value reaches 1 GB."""
    value = float(megabytes or 0)
    if value >= 1024:
        return f"{value / 1024:.1f} GB VRAM"
    return f"{int(value)} MB VRAM"


def render_screen(
    metrics: dict[str, float | None],
    colors: dict[str, str] | None = None,
    hardware_labels: dict[str, str] | None = None,
    animation_time: float | None = None,
    media_snapshot: MediaSnapshot | None = None,
    audio_spectrum: AudioSpectrumSnapshot | None = None,
    battery_devices: tuple[BatteryDevice, ...] = (),
    msfs_active: bool = False,
    flight_telemetry: FlightTelemetry | None = None,
    msfs_layout: str = "classic",
    weather_readings: tuple[WeatherReading, ...] = (),
    language: str = "tr",
    cs2_active: bool = False,
) -> Image.Image:
    _RENDER_LANGUAGE.set(normalize_language(language))
    # While CS2 runs the normal desktop screen is drawn in CS2's orange.
    colors = dict(CS2_THEME_COLORS) if cs2_active else {**COLORS, **(colors or {})}
    labels = resolve_hardware_labels(hardware_labels)
    image = Image.new("RGB", (WIDTH, HEIGHT), colors["bg"])
    draw = ImageDraw.Draw(image)
    cpu_load_color = load_color(float(metrics["cpu_load"]), "cpu", colors)
    gpu_load_color = load_color(float(metrics["gpu_load"]), "gpu", colors)
    ram_load_color = load_color(float(metrics["ram_load"]), "ram", colors)
    cpu_load_alert = load_alert_color(
        float(metrics["cpu_load"]), "cpu", colors, animation_time,
    )
    gpu_load_alert = load_alert_color(
        float(metrics["gpu_load"]), "gpu", colors, animation_time,
    )
    ram_load_alert = load_alert_color(
        float(metrics["ram_load"]), "ram", colors, animation_time,
    )
    draw_open_ring(
        image,
        draw,
        (80, 79),
        53,
        metrics["cpu_load"],
        cpu_load_color,
        labels["cpu"],
        _temperature(metrics.get("cpu_temp")),
        _frequency(metrics.get("cpu_freq")),
        colors,
        value_color=cpu_load_alert,
        percent_color=cpu_load_alert,
        sensor_unit_color=temperature_unit_color(
            metrics.get("cpu_temp"), "cpu", colors, animation_time,
        ),
        sensor_number_color=temperature_number_color(
            metrics.get("cpu_temp"), "cpu", colors, animation_time,
        ),
    )
    draw_open_ring(
        image,
        draw,
        (240, 79),
        53,
        metrics["gpu_load"],
        gpu_load_color,
        labels["gpu"],
        _temperature(metrics.get("gpu_temp")),
        _vram_label(metrics.get("gpu_memory")),
        colors,
        value_color=gpu_load_alert,
        percent_color=gpu_load_alert,
        sensor_unit_color=temperature_unit_color(
            metrics.get("gpu_temp"), "gpu", colors, animation_time,
        ),
        sensor_number_color=temperature_number_color(
            metrics.get("gpu_temp"), "gpu", colors, animation_time,
        ),
    )
    draw_open_ring(
        image,
        draw,
        (400, 79),
        53,
        metrics["ram_load"],
        ram_load_color,
        labels["ram"],
        f"{float(metrics['ram_used'] or 0):.1f} GB",
        _t("kullanımda"),
        colors,
        value_color=ram_load_alert,
        percent_color=ram_load_alert,
    )

    draw.rounded_rectangle((14, 160, 466, 306), radius=8, fill=colors["panel"], outline=colors["edge"], width=1)
    media_snapshot = media_snapshot or MediaSnapshot()
    audio_spectrum = audio_spectrum or AudioSpectrumSnapshot()
    if not cs2_active and msfs_active:
        draw_msfs_panel(
            image, draw, colors, flight_telemetry or FlightTelemetry(), battery_devices,
            msfs_layout,
        )
        return image
    if not cs2_active and (media_snapshot.visible or audio_spectrum.active):
        draw_media_panel(image, draw, media_snapshot, audio_spectrum, colors, battery_devices)
        return image
    draw_weather_panel(image, draw, weather_readings, colors, battery_devices)
    return image


SUPPORTED_ROTATIONS = ("normal", "180")


def normalise_rotation(rotation: object) -> str:
    return "180" if str(rotation) == "180" else "normal"


def _orientation_value(orientation_module: object, rotation: str):
    """Hardware orientation command sent to the panel.

    The Rev-A 3.5" firmware does not reliably honour REVERSE_LANDSCAPE (the
    command is accepted but the picture is not flipped on many units), so the
    panel is always driven in plain LANDSCAPE and 180 degrees is applied to
    the pixels in software by :class:`ScreenSession`. ``rotation`` is kept in
    the signature for API compatibility.
    """
    del rotation
    return orientation_module.LANDSCAPE


def rotate_region(
    region: tuple[int, int, int, int], size: tuple[int, int], rotation: str,
) -> tuple[int, int, int, int]:
    """Map a logical (unrotated) frame region to panel coordinates."""
    if normalise_rotation(rotation) != "180":
        return region
    width, height = size
    left, top, right, bottom = region
    return (width - right, height - bottom, width - left, height - top)


def orient_frame(image: Image.Image, rotation: str) -> Image.Image:
    """Return the frame exactly as it must appear in panel memory."""
    if normalise_rotation(rotation) == "180":
        return image.transpose(Image.Transpose.ROTATE_180)
    return image


SERIAL_WRITE_TIMEOUT_SECONDS = 6.0
SERIAL_OPEN_ATTEMPTS = 3
SERIAL_RECONNECT_ATTEMPTS = 3


class DisplayConnectionError(OSError):
    """The USB display could not be opened or stopped accepting data."""


@functools.lru_cache(maxsize=1)
def _robust_display_class():
    """Return a Rev-A driver subclass that never exits and never hides errors.

    The upstream driver calls ``sys.exit`` when the port cannot be opened,
    swallows ``SerialTimeoutException`` on writes and silently reopens the
    port on other serial errors (dropping the configured ``write_timeout``).
    For a long-running GUI those behaviours either kill the worker thread or
    leave the panel showing a torn frame. This subclass opens the port with a
    write timeout every time and lets every failure propagate so that
    :class:`ScreenSession` can rebuild the whole session cleanly.
    """
    if str(DRIVER_ROOT) not in sys.path:
        sys.path.insert(0, str(DRIVER_ROOT))
    import serial  # pyserial, used by the driver
    from library.lcd.lcd_comm_rev_a import LcdCommRevA

    class RobustLcdCommRevA(LcdCommRevA):
        def openSerial(self):  # noqa: N802 - driver API name
            last_error: Exception | None = None
            for attempt in range(SERIAL_OPEN_ATTEMPTS):
                com_port = self.com_port
                if com_port == "AUTO":
                    com_port = self.auto_detect_com_port()
                    if not com_port:
                        last_error = DisplayConnectionError("COM port not found")
                        time.sleep(0.5 * (attempt + 1))
                        continue
                try:
                    self.lcd_serial = serial.Serial(
                        com_port, 115200, timeout=1, rtscts=True,
                        write_timeout=SERIAL_WRITE_TIMEOUT_SECONDS,
                    )
                    return
                except Exception as exc:  # serial.SerialException, OSError
                    last_error = exc
                    time.sleep(0.5 * (attempt + 1))
            raise DisplayConnectionError(f"display port could not be opened: {last_error}")

        def WriteLine(self, line: bytes):  # noqa: N802 - driver API name
            # Propagate SerialTimeoutException/SerialException instead of the
            # upstream "log and continue / silently reopen" behaviour.
            self.serial_write(line)

        def ReadData(self, readSize: int):  # noqa: N802,N803 - driver API name
            return self.serial_read(readSize)

        def __del__(self):
            try:
                self.closeSerial()
            except Exception:
                pass

    return RobustLcdCommRevA


def send_to_screen(image: Image.Image, port: str, brightness: int, rotation: str = "normal") -> None:
    session = ScreenSession(port, brightness, rotation)
    try:
        session.show(image)
    finally:
        session.close()


class ScreenSession:
    """Serial display session that rebuilds itself after any write failure."""

    def __init__(self, port: str, brightness: int, rotation: str = "normal") -> None:
        if str(DRIVER_ROOT) not in sys.path:
            sys.path.insert(0, str(DRIVER_ROOT))
        from library.lcd.lcd_comm import Orientation

        self.orientation_module = Orientation
        self.port = port
        self.brightness = brightness
        self.rotation = normalise_rotation(rotation)
        self.display = None
        self.reconnects = 0
        self._open()

    def _open(self) -> None:
        display_class = _robust_display_class()
        try:
            display = display_class(com_port=self.port)
        except SystemExit as exc:  # defensive: never let the driver exit us
            raise DisplayConnectionError("display driver requested exit") from exc
        try:
            serial_connection = getattr(display, "lcd_serial", None)
            if serial_connection is not None:
                # A disconnected or contended USB display must not leave the
                # worker blocked forever inside a serial write.
                serial_connection.write_timeout = SERIAL_WRITE_TIMEOUT_SECONDS
            display.InitializeComm()
            # Stop/shutdown powers the panel down immediately. Always wake it
            # before configuring and drawing the first frame of a new session.
            display.ScreenOn()
            display.SetOrientation(_orientation_value(self.orientation_module, self.rotation))
            display.SetBrightness(self.brightness)
        except BaseException as exc:
            try:
                display.closeSerial()
            except Exception:
                pass
            if isinstance(exc, SystemExit):
                raise DisplayConnectionError("display driver requested exit") from exc
            raise
        self.display = display

    def _close_quietly(self) -> None:
        display, self.display = self.display, None
        if display is not None:
            try:
                display.closeSerial()
            except Exception:
                pass

    def _reconnect(self) -> None:
        """Close, reopen and fully reinitialise; restores orientation/brightness."""
        self._close_quietly()
        last_error: Exception | None = None
        for attempt in range(SERIAL_RECONNECT_ATTEMPTS):
            if attempt:
                time.sleep(min(4.0, 0.5 * (2 ** attempt)))
            try:
                self._open()
                self.reconnects += 1
                log_info("display", f"display session rebuilt; attempt={attempt + 1}")
                return
            except Exception as exc:
                last_error = exc
                log_exception("display", "display reconnect attempt failed", exc, 0)
        raise DisplayConnectionError(f"display reconnect failed: {last_error}")

    def _with_recovery(self, operation: Callable[[], None], full_frame: Image.Image | None) -> None:
        try:
            if self.display is None:
                raise DisplayConnectionError("display session is closed")
            operation()
            return
        except Exception as exc:
            log_exception("display", "display write failed; rebuilding session", exc, 0)
        self._reconnect()
        if full_frame is not None:
            # After a reconnect the panel content is unknown: resend all of it.
            self._display_full(full_frame)
        else:
            operation()

    def _flush(self) -> None:
        serial_connection = getattr(self.display, "lcd_serial", None)
        if serial_connection is not None:
            serial_connection.flush()

    def _display_full(self, image: Image.Image) -> None:
        self.display.DisplayPILImage(orient_frame(image.convert("RGB"), self.rotation))
        self._flush()

    def configure(self, brightness: int, rotation: str) -> bool:
        """Apply brightness/orientation; True when the panel needs a full frame."""
        rotation = normalise_rotation(rotation)
        rotation_changed = rotation != self.rotation
        self.brightness = brightness
        self.rotation = rotation

        def apply() -> None:
            self.display.SetOrientation(_orientation_value(self.orientation_module, rotation))
            self.display.SetBrightness(brightness)

        self._with_recovery(apply, None)
        return rotation_changed

    def show(self, image: Image.Image) -> None:
        self._with_recovery(lambda: self._display_full(image), image)

    def show_once(self, image: Image.Image) -> None:
        """Send one full frame without reconnecting (used while stopping:
        a reconnect would take seconds and switch the backlight back on)."""
        if self.display is None:
            raise DisplayConnectionError("display session is closed")
        self._display_full(image)

    def show_regions(self, image: Image.Image, regions: list[tuple[int, int, int, int]]) -> None:
        def send() -> None:
            rotation = self.rotation
            panel_image = orient_frame(image.convert("RGB"), rotation)
            for region in regions:
                left, top, right, bottom = rotate_region(region, image.size, rotation)
                crop = panel_image.crop((left, top, right, bottom))
                self.display.DisplayPILImage(crop, x=left, y=top)
            self._flush()

        self._with_recovery(send, image)

    def set_brightness(self, level: int) -> None:
        """Backlight only (a 6-byte command): cheap enough to animate."""
        level = max(0, min(100, int(level)))

        def apply() -> None:
            self.display.SetBrightness(level)
            self._flush()

        self._with_recovery(apply, None)

    def fade(self, start: int, end: int, seconds: float, steps: int = 8,
             sleep: Callable[[float], None] = time.sleep) -> None:
        """Ramp the backlight from ``start`` to ``end`` (ease-in-out)."""
        steps = max(1, steps)
        for index in range(1, steps + 1):
            progress = index / steps
            eased = progress * progress * (3 - 2 * progress)  # smoothstep
            self.set_brightness(round(start + (end - start) * eased))
            sleep(seconds / steps)

    def power_off(self) -> None:
        """Blank the physical panel immediately without a full-frame transfer."""
        if self.display is None:
            return
        self.display.ScreenOff()
        self._flush()

    def close(self) -> None:
        self._close_quietly()


def main() -> int:
    parser = argparse.ArgumentParser(description="System ring display for the 3.5-inch screen")
    parser.add_argument("--preview", type=Path, help="Save one rendered frame as PNG")
    parser.add_argument("--screen-once", action="store_true", help="Send one rendered frame to the 3.5-inch display")
    parser.add_argument("--run-screen", action="store_true", help="Continuously update the display")
    parser.add_argument("--force-cs2", action="store_true", help="Use the CS2 colours even if the game is closed")
    parser.add_argument("--port", default="AUTO", help="Display serial port, default: AUTO")
    parser.add_argument("--brightness", type=int, default=55, choices=range(0, 101), metavar="0-100")
    parser.add_argument("--interval", type=float, default=3.0, help="Refresh interval for continuous mode")
    parser.add_argument("--cycles", type=int, default=0, help="Stop continuous mode after N frames; 0 means unlimited")
    parser.add_argument("--sensor-bridge", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--sensor-file-bridge", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.sensor_bridge:
        return run_sensor_bridge()
    if args.sensor_file_bridge:
        return run_sensor_bridge(SENSOR_CACHE_PATH)
    metrics = get_metrics()
    frame = render_screen(metrics, cs2_active=args.force_cs2 or cs2_is_running())

    if args.preview:
        args.preview.parent.mkdir(parents=True, exist_ok=True)
        frame.save(args.preview)
        print(f"Önizleme: {args.preview}")

    if args.screen_once:
        send_to_screen(frame, args.port, args.brightness)
        print("Ekrana tek kare gönderildi.")

    if args.run_screen:
        screen = None
        frame_count = 0
        failures = 0
        try:
            while True:
                started = time.monotonic()
                try:
                    if screen is None:
                        screen = ScreenSession(args.port, args.brightness)
                    frame = render_screen(
                        get_metrics(), animation_time=started,
                        cs2_active=args.force_cs2 or cs2_is_running(),
                    )
                    screen.show(frame)
                    failures = 0
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    failures += 1
                    log_exception("cli", "frame failed", exc)
                    print(f"Kare gönderilemedi: {type(exc).__name__}: {exc}", file=sys.stderr)
                    if screen is not None:
                        screen.close()
                        screen = None
                    time.sleep(min(30.0, 1.0 * (2 ** min(failures, 5))))
                    continue
                frame_count += 1
                if args.cycles and frame_count >= args.cycles:
                    break
                elapsed = time.monotonic() - started
                time.sleep(max(0.2, args.interval - elapsed))
        finally:
            if screen is not None:
                screen.close()

    if not (args.preview or args.screen_once or args.run_screen):
        default_preview = ROOT / "preview.png"
        frame.save(default_preview)
        print(f"Önizleme: {default_preview}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
