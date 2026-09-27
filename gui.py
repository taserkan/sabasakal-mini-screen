# SPDX-License-Identifier: GPL-3.0-or-later
"""Friendly Qt control panel for the 3.5-inch CS2 information screen."""

from __future__ import annotations

import argparse
from concurrent.futures import Future, ThreadPoolExecutor
import ctypes
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from PIL import Image, ImageChops
import psutil
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QCloseEvent, QIcon, QImage, QPixmap, QTransform
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QFrame, QGridLayout,
    QGraphicsDropShadowEffect, QGraphicsOpacityEffect, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton,
    QMenu, QRadioButton, QSizePolicy, QSlider, QSystemTrayIcon, QVBoxLayout, QWidget,
)

from app import (
    COLORS, CS2GameState, FACEIT_PING_MONITOR, GSIServer, GameSnapshot,
    HEIGHT,
    MAX_HARDWARE_LABEL_LENGTH, MEDIA_VIS_REGION, MEDIA_VIS_REGION_WITH_BATTERY,
    RelayPing, SENSOR_CACHE_PATH, ScreenSession,
    STEAM_PING_MONITOR, cs2_is_running, faceit_ac_is_running,
    get_auto_hardware_labels, get_metrics, install_gsi_config, normalise_hardware_label,
    msfs_session_seconds, render_media_visual_patch, render_screen,
    resolve_hardware_labels, run_sensor_bridge,
    run_steam_ping_bridge, WIDTH,
)
from battery_runtime import BatteryDevice, BatteryDeviceMonitor
from media_runtime import (
    AudioSpectrumMonitor, AudioSpectrumSnapshot, MediaSessionMonitor, MediaSnapshot,
)
from msfs_runtime import FlightTelemetry, MSFSTelemetryMonitor
from weather_runtime import (
    DEFAULT_WEATHER_CITIES, WeatherMonitor, WeatherReading, format_city_name,
    normalise_city_names,
)
from i18n import SUPPORTED_LANGUAGES, normalize_language, translate


RUN_VALUE_NAME = "CS2Screen"
STARTUP_TASK_NAME = "SabasakalMiniScreen"
STARTUP_SHORTCUT_NAME = "Sabasakal Mini Screen.lnk"
APP_DATA = Path(os.environ.get("APPDATA", Path.home())) / "CS2Screen"
SETTINGS_PATH = APP_DATA / "settings.json"
RUNTIME_LOG_PATH = APP_DATA / "runtime.log"
RUNTIME_FALLBACK_LOG_PATH = APP_DATA / "runtime-current.log"
ACTIVATE_REQUEST_PATH = APP_DATA / "show-window.request"
ACTIVATE_ACK_PATH = APP_DATA / "show-window.ack"
INSTANCE_STATE_PATH = APP_DATA / "instance.json"
SENSOR_TASK_STATE_PATH = APP_DATA / "sensor-task.json"
SENSOR_TASK_NAME = "CS2ScreenSensors"
SENSOR_HOST_ROOT = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Sabasakal Mini Ekran"
SENSOR_HOST_PATH = SENSOR_HOST_ROOT / "SabasakalSensorHost.exe"
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
APP_ICON_PATH = RESOURCE_ROOT / "sabasakal-logo.ico"
APP_LOGO_PATH = RESOURCE_ROOT / "sabasakal-logo.png"
WINDOW_BG, CARD_BG, CARD_2 = "#0D0F13", "#15181E", "#101319"
EDGE, TEXT, MUTED, ACCENT = "#2B313B", "#F3F5F7", "#9AA4B2", "#79A9F5"
CONTROL_BG, CONTROL_HOVER, CONTROL_PRESSED = "#20242C", "#2A303A", "#191D24"
COLOR_IMPACT_COPY = {
    "cpu": "Halkaların normal başlangıç rengi",
    "warning": "Yük ve sıcaklık yükselirken geçiş",
    "danger": "Son eşik ve yanıp sönme uyarısı",
    "bg": "Mini ekranın ana arka planı",
    "text": "Donanım adları, değerler ve ana başlıklar",
    "muted": "Açıklamalar ve ikincil bilgi metinleri",
}
SIMPLE_ACCENT_COLORS = (
    "#F3F5F7", "#A8B2BE", "#79A9F5", "#5F9FE8",
    "#56D6C9", "#68D391", "#D6B878", "#F0A36A",
    "#F08C78", "#E36F6F", "#E88AA8", "#B39AF6",
)
SIMPLE_BACKGROUND_COLORS = (
    "#080A0E", "#101319", "#0E1714", "#0A1020",
    "#11101A", "#17120F", "#07141A", "#151121",
    "#190D13", "#101214", "#111312", "#0D151B",
)

PALETTES: dict[str, dict[str, str]] = {
    "Kuzey": {
        "bg": "#0E1714", "panel": "#16231E", "panel_2": "#101B18", "edge": "#2E463B",
        "cpu": "#79D49F", "gpu": "#E99A5E", "ram": "#83BCE8", "text": "#F3F7F4",
        "muted": "#AABBB2", "dim": "#71867B", "track": "#263B32", "warning": "#E5C36D", "danger": "#E36F6F",
    },
    "Mor Gece": {
        "bg": "#11101A", "panel": "#1C1929", "panel_2": "#151321", "edge": "#3B3450",
        "cpu": "#A98DF2", "gpu": "#F095B4", "ram": "#7BBBEF", "text": "#F6F2FC",
        "muted": "#B7AEC7", "dim": "#7B718E", "track": "#302A41", "warning": "#F0B36F", "danger": "#EF708F",
    },
    "Bakır": {
        "bg": "#17120F", "panel": "#241B16", "panel_2": "#1D1612", "edge": "#49362B",
        "cpu": "#D8A15F", "gpu": "#E47552", "ram": "#8CB6AD", "text": "#FAF3EA",
        "muted": "#C0AD9C", "dim": "#806F62", "track": "#3B2B23", "warning": "#E7B65F", "danger": "#E7684D",
    },
    "Buz": {
        "bg": "#0D151B", "panel": "#14222C", "panel_2": "#101B23", "edge": "#294151",
        "cpu": "#71D2C4", "gpu": "#7DAEF2", "ram": "#C08FE7", "text": "#F0F6FA",
        "muted": "#A2B7C3", "dim": "#6B818D", "track": "#223744", "warning": "#C4B86C", "danger": "#E4788F",
    },
    "Tek renk": {
        "bg": "#111312", "panel": "#1B1E1C", "panel_2": "#161817", "edge": "#353A37",
        "cpu": "#E4E8E5", "gpu": "#AAB1AD", "ram": "#757E79", "text": "#F5F7F6",
        "muted": "#B2B8B4", "dim": "#777F7A", "track": "#2B302D", "warning": "#B8B2A4", "danger": "#D27777",
    },
    "Okyanus": {
        "bg": "#07141A", "panel": "#0D2028", "panel_2": "#091A21", "edge": "#24424D",
        "cpu": "#55D6C2", "gpu": "#56A8E8", "ram": "#8AC7FF", "text": "#EFFAFC",
        "muted": "#A1BBC3", "dim": "#617D86", "track": "#19333D", "warning": "#E7BD65", "danger": "#EF6F73",
    },
    "Zümrüt": {
        "bg": "#08130F", "panel": "#10231A", "panel_2": "#0B1B14", "edge": "#284638",
        "cpu": "#68D391", "gpu": "#B5D66B", "ram": "#66C8B4", "text": "#F1FAF4",
        "muted": "#A6BDAE", "dim": "#697F70", "track": "#1D3528", "warning": "#E4BC63", "danger": "#E66D6D",
    },
    "Gece Mavisi": {
        "bg": "#0A1020", "panel": "#111B31", "panel_2": "#0D1628", "edge": "#293A5B",
        "cpu": "#69A7FF", "gpu": "#8C7CF0", "ram": "#5ED3D0", "text": "#F1F5FF",
        "muted": "#A8B5D1", "dim": "#687796", "track": "#1E2B47", "warning": "#F0B96A", "danger": "#EE6F88",
    },
    "Gün Batımı": {
        "bg": "#1A1012", "panel": "#2A181A", "panel_2": "#201316", "edge": "#503033",
        "cpu": "#F0A36A", "gpu": "#E67B72", "ram": "#C596D9", "text": "#FFF4EE",
        "muted": "#CBB1AA", "dim": "#8D706B", "track": "#402327", "warning": "#F2C15F", "danger": "#EC6262",
    },
    "Lavanta": {
        "bg": "#151121", "panel": "#211B33", "panel_2": "#191529", "edge": "#40365B",
        "cpu": "#B39AF6", "gpu": "#D28BD2", "ram": "#88B8F4", "text": "#F8F4FF",
        "muted": "#BEB3D2", "dim": "#7D718F", "track": "#332A49", "warning": "#E8BB68", "danger": "#EA718F",
    },
    "Kiraz": {
        "bg": "#190D13", "panel": "#29141D", "panel_2": "#201017", "edge": "#4D2A38",
        "cpu": "#E88AA8", "gpu": "#CB6D92", "ram": "#9E93E7", "text": "#FFF2F7",
        "muted": "#C8AAB5", "dim": "#8A6975", "track": "#3D222C", "warning": "#E7B45E", "danger": "#F05F72",
    },
    "Kum Taşı": {
        "bg": "#16130F", "panel": "#262018", "panel_2": "#1D1813", "edge": "#4A4031",
        "cpu": "#D6B878", "gpu": "#C88C62", "ram": "#8CB7A5", "text": "#FAF5E9",
        "muted": "#C1B7A2", "dim": "#827866", "track": "#3A3126", "warning": "#E2B653", "danger": "#D96857",
    },
    "Orman": {
        "bg": "#09110B", "panel": "#142019", "panel_2": "#0F1812", "edge": "#2D4435",
        "cpu": "#7FCB83", "gpu": "#B6C66A", "ram": "#6DB7A0", "text": "#F2F8F3",
        "muted": "#AABAAA", "dim": "#6C7D6D", "track": "#23362A", "warning": "#E0B85F", "danger": "#DC6C65",
    },
    "Kutup Işığı": {
        "bg": "#071316", "panel": "#0D2226", "panel_2": "#091A1D", "edge": "#21444A",
        "cpu": "#66E0C1", "gpu": "#A07BEF", "ram": "#63BCEB", "text": "#EFFBFA",
        "muted": "#A1BEBD", "dim": "#617F80", "track": "#18363A", "warning": "#E6C46B", "danger": "#EF728F",
    },
    "Safir": {
        "bg": "#090F1A", "panel": "#101D2D", "panel_2": "#0C1724", "edge": "#263E5B",
        "cpu": "#5F9FE8", "gpu": "#4CC4D9", "ram": "#93A7F2", "text": "#F0F6FF",
        "muted": "#A2B3C9", "dim": "#657990", "track": "#1C3047", "warning": "#E8BE64", "danger": "#EE6D78",
    },
    "Mercan": {
        "bg": "#18100F", "panel": "#281B18", "panel_2": "#201511", "edge": "#4B352E",
        "cpu": "#F08C78", "gpu": "#E8B36A", "ram": "#79BEB1", "text": "#FFF4EF",
        "muted": "#C9B0A8", "dim": "#8A7068", "track": "#3C2924", "warning": "#F2C35E", "danger": "#EF6168",
    },
    "Göktaşı": {
        "bg": "#101214", "panel": "#1A1E22", "panel_2": "#15181B", "edge": "#353B42",
        "cpu": "#A8B2BE", "gpu": "#7FA4C7", "ram": "#9B91B7", "text": "#F4F6F8",
        "muted": "#B0B6BD", "dim": "#747B83", "track": "#292E34", "warning": "#D6B66B", "danger": "#DB6B72",
    },
    "Neon Şehir": {
        "bg": "#0C0B15", "panel": "#171327", "panel_2": "#110F1E", "edge": "#352B53",
        "cpu": "#56D6C9", "gpu": "#F06BB1", "ram": "#8F7CF0", "text": "#F8F4FF",
        "muted": "#B8AECE", "dim": "#766B91", "track": "#2A2340", "warning": "#F4C45F", "danger": "#FF5C73",
    },
}
MSFS_LAYOUTS = {
    "Klasik": "classic",
    "Kokpit şeridi": "cockpit",
}
DEFAULT_SETTINGS = {
    "palette": "Kuzey", "colors": PALETTES["Kuzey"], "brightness": 55,
    "auto_brightness": False, "rotation": "normal", "autostart": False,
    "language": "tr",
    "port": "AUTO", "interval": 0.5,
    "msfs_layout": "cockpit",
    "weather_cities": list(DEFAULT_WEATHER_CITIES),
    "custom_palettes": {}, "hardware_labels": {"cpu": "", "gpu": "", "ram": ""},
}

AUTO_BRIGHTNESS_DAY = 90
AUTO_BRIGHTNESS_NIGHT = 30
AUTO_BRIGHTNESS_DAY_START = 7
AUTO_BRIGHTNESS_NIGHT_START = 19


def automatic_brightness(now: dt.datetime | None = None) -> int:
    hour = (now or dt.datetime.now()).hour
    if AUTO_BRIGHTNESS_DAY_START <= hour < AUTO_BRIGHTNESS_NIGHT_START:
        return AUTO_BRIGHTNESS_DAY
    return AUTO_BRIGHTNESS_NIGHT


def effective_brightness(settings: dict, now: dt.datetime | None = None) -> int:
    if settings.get("auto_brightness") is True:
        return automatic_brightness(now)
    return max(0, min(100, int(settings.get("brightness", 55))))

RING_REGIONS = [(15, 0, 145, 155), (175, 0, 305, 155), (335, 0, 465, 155)]
VALUE_REGIONS = [
    [(40, 49, 128, 123), (18, 126, 143, 158)],
    [(200, 49, 288, 123), (176, 126, 305, 158)],
    [(360, 49, 448, 123), (337, 126, 466, 158)],
]
BOTTOM_REGION = (14, 160, 467, 307)
BOMB_TIMER_REGION = (20, 205, 174, 260)
BOMB_BAR_REGION = (24, 264, 455, 302)
BOMB_WORKER_INTERVAL = 0.20
LIVE_GAME_WORKER_INTERVAL = 0.20
BATTERY_PANEL_REGION = (337, 202, 456, 295)
MSFS_PANEL_REGION_WITH_BATTERY = (14, 160, 337, 307)
MSFS_TELEMETRY_REGION = (20, 198, 460, 300)
MSFS_TELEMETRY_REGION_WITH_BATTERY = (20, 198, 332, 300)
MSFS_LIVE_REGIONS = (
    (20, 218, 220, 246), (220, 218, 460, 246),
    (20, 268, 220, 300), (220, 252, 460, 300),
)
MSFS_LIVE_REGIONS_WITH_BATTERY = (
    (20, 218, 176, 246), (176, 218, 332, 246),
    (20, 268, 166, 300), (166, 252, 332, 300),
)
MSFS_TELEMETRY_REFRESH_INTERVAL = 0.15
MSFS_WORKER_INTERVAL = 0.08
TOP_REGION = (0, 0, 480, 160)
FAST_GAUGE_REGION = (48, 51, 439, 122)
# Load and sensor glyphs are separate rectangles. A single generous rectangle
# also covered the open ring's lower start cap; when a fresh value tile met an
# older ring strip on the physical LCD it could look like a triangular cut.
# These padded boxes replace every antialiased glyph pixel without ever
# touching either open-ring endpoint.
FAST_VALUE_REGION_GROUPS = (
    ((46, 63, 114, 121),),
    ((206, 63, 274, 121),),
    ((365, 63, 435, 121),),
)
FAST_VALUE_BOXES = tuple(
    region for group in FAST_VALUE_REGION_GROUPS for region in group
)


def _region_has_changed(
    current: Image.Image,
    sent: Image.Image | None,
    region: tuple[int, int, int, int],
) -> bool:
    """Avoid spending serial bandwidth on an unchanged live-value tile."""
    if sent is None:
        return True
    return ImageChops.difference(
        current.crop(region), sent.crop(region),
    ).getbbox() is not None


def _tight_changed_regions(
    current: Image.Image,
    sent: Image.Image | None,
    regions: list[tuple[int, int, int, int]] | tuple[tuple[int, int, int, int], ...],
) -> list[tuple[int, int, int, int]]:
    """Reduce requested LCD updates to the exact pixels that changed.

    Rev-A panels paint serial bitmap data as it arrives and do not expose a
    double buffer. Sending a large mostly-unchanged rectangle therefore makes
    the redraw visible. Exact dirty rectangles keep the same final pixels while
    greatly shortening the physical transfer.
    """
    if sent is None:
        return list(regions)
    changed: list[tuple[int, int, int, int]] = []
    for left, top, right, bottom in regions:
        local_box = ImageChops.difference(
            current.crop((left, top, right, bottom)),
            sent.crop((left, top, right, bottom)),
        ).getbbox()
        if local_box is None:
            continue
        candidate = (
            left + local_box[0], top + local_box[1],
            left + local_box[2], top + local_box[3],
        )
        # Partitioned regions normally do not overlap. Keep this containment
        # guard for antialias-safe media strips that overlap by a few pixels.
        if any(
            old[0] <= candidate[0] and old[1] <= candidate[1]
            and old[2] >= candidate[2] and old[3] >= candidate[3]
            for old in changed
        ):
            continue
        changed = [
            old for old in changed
            if not (
                candidate[0] <= old[0] and candidate[1] <= old[1]
                and candidate[2] >= old[2] and candidate[3] >= old[3]
            )
        ]
        changed.append(candidate)
    return changed
# In media mode, sending TOP_REGION or FAST_GAUGE_REGION in one transfer
# stalls the spectrum for 200-600 ms. Text/value tiles stay small. Each ring
# is refreshed as four overlapping strips in one batch; the overlap covers
# all diagonal pixels and prevents stale triangular wedges at arc endpoints.
MEDIA_GAUGE_REGIONS = (
    (25, 0, 136, 25), (18, 125, 143, 158),
    (185, 0, 296, 25), (176, 125, 305, 158),
    (345, 0, 456, 25), (337, 125, 466, 158),
)
MEDIA_RING_REGION_GROUPS = (
    (
        (24, 22, 137, 49), (24, 109, 137, 134),
        (23, 39, 50, 120), (111, 39, 137, 120),
    ),
    (
        (184, 22, 297, 49), (184, 109, 297, 134),
        (183, 39, 210, 120), (271, 39, 297, 120),
    ),
    (
        (344, 22, 457, 49), (344, 109, 457, 134),
        (343, 39, 370, 120), (431, 39, 457, 120),
    ),
)
# Complete non-overlapping partition of the hardware area. Normal and game
# modes request these tiles instead of repainting the old 480x160 rectangle.
# The exact-difference pass above then reduces each tile to its changed pixels.
TOP_DIRTY_TILES = tuple(
    region
    for cell_left in (0, 160, 320)
    for region in (
        (cell_left, 0, cell_left + 160, 25),
        (cell_left, 25, cell_left + 160, 50),
        (cell_left, 50, cell_left + 46, 109),
        (cell_left + 46, 50, cell_left + 114, 109),
        (cell_left + 114, 50, cell_left + 160, 109),
        (cell_left, 109, cell_left + 160, 135),
        (cell_left, 135, cell_left + 160, 160),
    )
)
_SINGLE_INSTANCE_HANDLE = None
_RECOVERED_STALE_INSTANCE = False
APP_DISPLAY_NAME = "Sabasakal Mini Ekran 3,5″"
WINDOW_TITLE = f"{APP_DISPLAY_NAME} · Ayarlar"
INSTANCE_HEARTBEAT_INTERVAL_MS = 1000
INSTANCE_STALE_SECONDS = 5.0
DISPLAY_STALL_SECONDS = 8.0


def _runtime_log(message: str) -> None:
    try:
        APP_DATA.mkdir(parents=True, exist_ok=True)
        if RUNTIME_LOG_PATH.exists() and RUNTIME_LOG_PATH.stat().st_size > 512 * 1024:
            previous = RUNTIME_LOG_PATH.with_suffix(".old.log")
            previous.unlink(missing_ok=True)
            RUNTIME_LOG_PATH.replace(previous)
        timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with RUNTIME_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"{timestamp} {message}\n")
    except OSError:
        # A diagnostic reader may temporarily keep the historical log open
        # without write sharing. Keep current diagnostics alive in a separate
        # file instead of silently losing startup/sensor errors.
        try:
            APP_DATA.mkdir(parents=True, exist_ok=True)
            timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with RUNTIME_FALLBACK_LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(f"{timestamp} {message}\n")
        except OSError:
            pass


def _hidden_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _sensor_task_action() -> tuple[str, str]:
    # The scheduled task always targets an administrator-protected host. The
    # portable control-panel EXE may then be moved, renamed or replaced
    # without invalidating the elevated sensor task, and an unprivileged
    # process cannot replace what the highest-privilege task executes.
    return str(SENSOR_HOST_PATH), "--sensor-file-bridge"


def _sensor_host_is_current(source: Path, target: Path = SENSOR_HOST_PATH) -> bool:
    try:
        source_stat = source.stat()
        target_stat = target.stat()
        return (
            source_stat.st_size == target_stat.st_size
            and source_stat.st_mtime_ns == target_stat.st_mtime_ns
        )
    except OSError:
        return False


def _install_sensor_host_copy(
    source: Path | None = None, target: Path = SENSOR_HOST_PATH,
) -> bool:
    """Atomically install the protected sensor host from an elevated process."""
    source = Path(source or sys.executable).resolve()
    target = Path(target).resolve()
    if source == target or _sensor_host_is_current(source, target):
        return True
    temporary = target.with_name(f"{target.stem}.new{target.suffix}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary.unlink(missing_ok=True)
        shutil.copy2(source, temporary)
        for _attempt in range(20):
            try:
                os.replace(temporary, target)
                return True
            except PermissionError:
                time.sleep(0.20)
        return False
    except OSError:
        return False
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def install_sensor_task() -> int:
    """Install the user-approved elevated sensor reader and start it now."""
    if os.name != "nt":
        return 0
    if not getattr(sys, "frozen", False):
        return 1
    if not _install_sensor_host_copy():
        _runtime_log("sensor host install failed: stable copy could not be created")
        return 1
    executable, arguments = _sensor_task_action()
    task_command = f'"{executable}" {arguments}'
    account = "\\".join(filter(None, (
        os.environ.get("USERDOMAIN"), os.environ.get("USERNAME"),
    )))
    command = [
        "schtasks.exe", "/Create", "/TN", SENSOR_TASK_NAME,
        "/TR", task_command, "/SC", "ONLOGON", "/RL", "HIGHEST", "/IT", "/F",
    ]
    if account:
        command.extend(["/RU", account])
    try:
        created = subprocess.run(
            command, capture_output=True, text=True, timeout=20,
            creationflags=_hidden_flags(),
        )
        if created.returncode != 0:
            _runtime_log(f"sensor task install failed: {created.stderr.strip() or created.stdout.strip()}")
            return 1
        APP_DATA.mkdir(parents=True, exist_ok=True)
        state = {"executable": str(Path(executable).resolve()), "arguments": arguments}
        temporary = SENSOR_TASK_STATE_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(state), encoding="utf-8")
        os.replace(temporary, SENSOR_TASK_STATE_PATH)
        started = subprocess.run(
            ["schtasks.exe", "/Run", "/TN", SENSOR_TASK_NAME],
            capture_output=True, text=True, timeout=10, creationflags=_hidden_flags(),
        )
        if started.returncode != 0:
            _runtime_log(f"sensor task start failed: {started.stderr.strip() or started.stdout.strip()}")
            return 1
        _runtime_log("sensor task installed and started")
        return 0
    except (OSError, subprocess.SubprocessError) as exc:
        _runtime_log(f"sensor task install exception: {type(exc).__name__}: {exc}")
        return 1


def ensure_sensor_task() -> None:
    """Keep the approved sensor task attached to the current portable EXE."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    executable, arguments = _sensor_task_action()
    expected = {"executable": str(Path(executable).resolve()), "arguments": arguments}
    matches = False
    try:
        saved = json.loads(SENSOR_TASK_STATE_PATH.read_text(encoding="utf-8"))
        query = subprocess.run(
            ["schtasks.exe", "/Query", "/TN", SENSOR_TASK_NAME],
            capture_output=True, timeout=8, creationflags=_hidden_flags(),
        )
        matches = (
            saved == expected
            and query.returncode == 0
            and Path(executable).is_file()
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
        matches = False
    if matches:
        subprocess.run(
            ["schtasks.exe", "/Run", "/TN", SENSOR_TASK_NAME],
            capture_output=True, timeout=8, creationflags=_hidden_flags(),
        )
        return
    _runtime_log("sensor task missing or EXE moved; requesting approved setup")
    shell_execute = ctypes.windll.shell32.ShellExecuteW
    shell_execute.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
        ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int,
    ]
    shell_execute.restype = ctypes.c_void_p
    result = shell_execute(
        None, "runas", sys.executable, "--install-sensor-task",
        str(Path(sys.executable).parent), 0,
    )
    result_code = int(result or 0)
    if result_code <= 32:
        _runtime_log(f"sensor task elevation was declined or failed: code={result_code}")


def nudge_sensor_task_if_stale(
    path: Path = SENSOR_CACHE_PATH, max_age: float = 5.0,
) -> bool:
    """Restart the existing sensor task when its cache stops advancing."""
    try:
        if time.time() - path.stat().st_mtime <= max_age:
            return False
    except OSError:
        pass
    try:
        result = subprocess.run(
            ["schtasks.exe", "/Run", "/TN", SENSOR_TASK_NAME],
            capture_output=True, timeout=8, creationflags=_hidden_flags(),
        )
        if result.returncode == 0:
            _runtime_log("stale sensor cache; existing task restarted")
            return True
        _runtime_log("stale sensor cache; task restart failed")
    except (OSError, subprocess.SubprocessError) as exc:
        _runtime_log(f"sensor task restart exception: {type(exc).__name__}: {exc}")
    return False


def _activate_existing_window(timeout_seconds: float = 12.0) -> bool:
    if os.name != "nt":
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p)
    user32.FindWindowW.restype = ctypes.c_void_p
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        window = None
        for candidate in (
            WINDOW_TITLE,
            f"{translate(APP_DISPLAY_NAME, 'en')} · {translate('Ayarlar', 'en')}",
        ):
            window = user32.FindWindowW(None, candidate)
            if window:
                break
        if window:
            # A second EXE launch must always bring the already-running control
            # panel back, including when Windows started it minimized at login.
            swp_flags = 0x0001 | 0x0002 | 0x0040  # NOSIZE | NOMOVE | SHOWWINDOW
            user32.ShowWindow(window, 9)  # SW_RESTORE
            user32.SetWindowPos(window, ctypes.c_void_p(-1), 0, 0, 0, 0, swp_flags)
            user32.SetWindowPos(window, ctypes.c_void_p(-2), 0, 0, 0, 0, swp_flags)
            user32.BringWindowToTop(window)
            user32.SetForegroundWindow(window)
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.15)


def _request_existing_window() -> str | None:
    """Ask a tray-only primary instance to restore its control panel."""
    try:
        APP_DATA.mkdir(parents=True, exist_ok=True)
        token = f"{os.getpid()}-{time.time_ns()}"
        ACTIVATE_ACK_PATH.unlink(missing_ok=True)
        ACTIVATE_REQUEST_PATH.write_text(token, encoding="ascii")
        return token
    except OSError:
        return None


def _wait_for_activation_ack(token: str, timeout_seconds: float = 2.0) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while time.monotonic() < deadline:
        try:
            if ACTIVATE_ACK_PATH.read_text(encoding="ascii").strip() == token:
                ACTIVATE_ACK_PATH.unlink(missing_ok=True)
                return True
        except OSError:
            pass
        time.sleep(0.10)
    return False


def _restore_existing_instance_window() -> bool:
    """Restore through Qt first so a hidden window is laid out and repainted."""
    token = _request_existing_window()
    if token and _wait_for_activation_ack(token):
        _runtime_log("duplicate launch restored tray instance")
        return True
    # Keep native activation only as a last-resort fallback for an instance
    # whose Qt event loop cannot acknowledge the request. Calling ShowWindow
    # first on a never-shown Qt widget can expose an unpainted white surface.
    if _activate_existing_window(timeout_seconds=0.8):
        _runtime_log("duplicate launch redirected to existing instance")
        return True
    return False


def _instance_state() -> dict:
    try:
        state = json.loads(INSTANCE_STATE_PATH.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_instance_state() -> None:
    """Publish a UI-thread heartbeat so a frozen tray process is recoverable."""
    try:
        APP_DATA.mkdir(parents=True, exist_ok=True)
        process = psutil.Process(os.getpid())
        payload = {
            "pid": os.getpid(),
            "created_at": process.create_time(),
            "heartbeat": time.time(),
            "executable": str(Path(sys.executable).resolve()),
        }
        temporary = INSTANCE_STATE_PATH.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temporary, INSTANCE_STATE_PATH)
    except (OSError, psutil.Error):
        pass


def _cleanup_instance_state() -> None:
    state = _instance_state()
    if state.get("pid") != os.getpid():
        return
    try:
        INSTANCE_STATE_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def _terminate_unresponsive_primary() -> bool:
    """Terminate only the verified owner of a stale single-instance heartbeat."""
    state = _instance_state()
    try:
        heartbeat = float(state.get("heartbeat", 0.0))
        pid = int(state.get("pid", 0))
        expected_created_at = float(state.get("created_at", 0.0))
    except (TypeError, ValueError):
        return False
    if pid <= 0 or pid == os.getpid() or time.time() - heartbeat <= INSTANCE_STALE_SECONDS:
        return False
    try:
        process = psutil.Process(pid)
        if abs(process.create_time() - expected_created_at) > 1.0:
            return False
        expected_executable = os.path.normcase(str(state.get("executable") or ""))
        if expected_executable and os.path.normcase(process.exe()) != expected_executable:
            return False
        process.terminate()
        try:
            process.wait(timeout=3.0)
        except psutil.TimeoutExpired:
            process.kill()
            process.wait(timeout=2.0)
        _runtime_log(f"unresponsive primary recovered; pid={pid}")
        return True
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
        return False


def _terminate_stale_instances(minimum_age_seconds: float = 30.0) -> bool:
    """Remove only old, windowless copies of this exact portable EXE."""
    if os.name != "nt":
        return False
    excluded = {os.getpid(), os.getppid()}
    try:
        executable = os.path.normcase(str(Path(sys.executable).resolve()))
    except OSError:
        executable = os.path.normcase(sys.executable)
    stale = []
    now = time.time()
    for process in psutil.process_iter(("pid", "exe", "create_time")):
        try:
            if process.pid in excluded:
                continue
            process_executable = process.info.get("exe") or ""
            if os.path.normcase(str(Path(process_executable).resolve())) != executable:
                continue
            if now - float(process.info.get("create_time") or now) < minimum_age_seconds:
                continue
            stale.append(process)
        except (OSError, psutil.Error, ValueError):
            continue
    if not stale:
        return False
    for process in stale:
        try:
            process.terminate()
        except psutil.Error:
            pass
    _gone, alive = psutil.wait_procs(stale, timeout=2.0)
    for process in alive:
        try:
            process.kill()
        except psutil.Error:
            pass
    _runtime_log(f"removed {len(stale)} stale windowless instance process(es)")
    return True


def _acquire_single_instance(
    recovery_attempt: bool = False, activate_existing: bool = True,
) -> bool:
    global _SINGLE_INSTANCE_HANDLE, _RECOVERED_STALE_INSTANCE
    if os.name != "nt":
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, "Local\\CS2ScreenControlPanel-v1")
    if not handle:
        return True
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        # Windows may launch both the Startup-folder shortcut and HKCU Run
        # fallback at nearly the same time. An autorun duplicate must exit
        # silently; only an explicit/manual launch should reveal the UI.
        if not activate_existing:
            _runtime_log("duplicate autorun ignored; primary remains in tray")
            return False
        if _restore_existing_instance_window():
            return False
        if not recovery_attempt and _terminate_unresponsive_primary():
            _RECOVERED_STALE_INSTANCE = True
            time.sleep(0.35)
            return _acquire_single_instance(
                recovery_attempt=True, activate_existing=activate_existing,
            )
        # Never create parallel control panels: they can compete for the same
        # serial display and cut the media animation frame rate. The primary
        # instance also receives the restore request when it only lives in
        # the system tray and therefore has no discoverable native window.
        _runtime_log("duplicate launch found a live or unrecoverable primary instance")
        return False
    _SINGLE_INSTANCE_HANDLE = handle
    _write_instance_state()
    try:
        ACTIVATE_REQUEST_PATH.unlink(missing_ok=True)
    except OSError:
        pass
    return True


def _is_color(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value) is not None


def _normalise_palette(colors: object, fallback: dict[str, str]) -> dict[str, str]:
    source = colors if isinstance(colors, dict) else {}
    return {
        key: source.get(key) if _is_color(source.get(key)) else fallback[key]
        for key in fallback
    }


def load_settings() -> dict:
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if not isinstance(saved, dict):
            raise ValueError("Ayar dosyasının kökü bir nesne olmalı.")
        saved_palettes = saved.get("custom_palettes", {})
        if isinstance(saved_palettes, dict):
            settings["custom_palettes"] = {
                str(name): _normalise_palette(palette, PALETTES["Kuzey"])
                for name, palette in saved_palettes.items()
                if isinstance(palette, dict)
            }
        palette = saved.get("palette", settings["palette"])
        if palette == "Mono":
            palette = "Tek renk"
        if palette not in PALETTES and palette not in settings["custom_palettes"]:
            palette = settings["palette"]
        settings["palette"] = palette
        palette_fallback = (
            settings["custom_palettes"].get(palette)
            or PALETTES.get(palette)
            or PALETTES["Kuzey"]
        )
        settings["colors"] = _normalise_palette(saved.get("colors"), palette_fallback)
        try:
            settings["brightness"] = max(0, min(100, int(saved.get("brightness", settings["brightness"]))))
        except (TypeError, ValueError):
            pass
        if isinstance(saved.get("auto_brightness"), bool):
            settings["auto_brightness"] = saved["auto_brightness"]
        rotation = saved.get("rotation")
        if rotation in ("normal", "180"):
            settings["rotation"] = rotation
        if isinstance(saved.get("autostart"), bool):
            settings["autostart"] = saved["autostart"]
        settings["language"] = normalize_language(saved.get("language"))
        port = saved.get("port")
        if isinstance(port, str) and port.strip():
            settings["port"] = port.strip()
        if saved.get("msfs_layout") in MSFS_LAYOUTS.values():
            settings["msfs_layout"] = saved["msfs_layout"]
        settings["weather_cities"] = list(normalise_city_names(saved.get("weather_cities")))
        labels = saved.get("hardware_labels")
        if isinstance(labels, dict):
            settings["hardware_labels"] = {
                key: normalise_hardware_label(labels.get(key))
                for key in ("cpu", "gpu", "ram")
            }
    except (OSError, ValueError, TypeError):
        pass
    # Older test builds used a three-second pause after every completed frame.
    # Keep the complete measurement + transfer cycle close to Task Manager's cadence.
    settings["interval"] = 0.5
    return settings


def save_settings_file(settings: dict) -> None:
    APP_DATA.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, SETTINGS_PATH)


def startup_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable)}" --autorun'
    return f'"{Path(sys.executable)}" "{Path(__file__).resolve()}" --autorun'


def _startup_action() -> tuple[str, str]:
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).resolve()), "--autorun"
    return str(Path(sys.executable).resolve()), f'"{Path(__file__).resolve()}" --autorun'


def _startup_task_xml() -> str:
    executable, arguments = _startup_action()
    account = "\\".join(filter(None, (
        os.environ.get("USERDOMAIN"), os.environ.get("USERNAME"),
    ))) or os.environ.get("USERNAME", "")
    # Task Scheduler is the primary launch path. A short delay lets Windows
    # finish enumerating the USB display and audio devices before the first
    # frame, while StartWhenAvailable and RestartOnFailure cover slow logons.
    return f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Sabasakal Mini Screen automatic startup</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><Delay>PT7S</Delay></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{xml_escape(account)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>{xml_escape(executable)}</Command><Arguments>{xml_escape(arguments)}</Arguments><WorkingDirectory>{xml_escape(str(APP_DATA))}</WorkingDirectory></Exec>
  </Actions>
</Task>'''


def _set_windows_run_fallback(enabled: bool) -> None:
    import winreg
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    # Some Windows cleanup/startup configurations remove an empty Run key.
    # CreateKey makes the portable app self-healing on the next launch.
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE_NAME)
            except FileNotFoundError:
                pass


def _windows_startup_shortcut_path() -> Path:
    return (
        Path(os.environ.get("APPDATA", APP_DATA.parent))
        / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        / STARTUP_SHORTCUT_NAME
    )


def _powershell_literal(value: str | Path) -> str:
    return str(value).replace("'", "''")


def _install_windows_startup_shortcut() -> bool:
    """Create a per-user Startup shortcut without requiring administrator rights."""
    shortcut = _windows_startup_shortcut_path()
    executable, arguments = _startup_action()
    try:
        shortcut.parent.mkdir(parents=True, exist_ok=True)
        script = (
            "$w=New-Object -ComObject WScript.Shell;"
            f"$s=$w.CreateShortcut('{_powershell_literal(shortcut)}');"
            f"$s.TargetPath='{_powershell_literal(executable)}';"
            f"$s.Arguments='{_powershell_literal(arguments)}';"
            f"$s.WorkingDirectory='{_powershell_literal(APP_DATA)}';"
            f"$s.IconLocation='{_powershell_literal(executable)},0';"
            "$s.Description='Sabasakal Mini Screen automatic startup';"
            "$s.Save()"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
             "-Command", script],
            capture_output=True, text=True, timeout=20,
            creationflags=_hidden_flags(),
        )
        if result.returncode != 0 or not shortcut.is_file():
            _runtime_log(
                "startup shortcut install failed: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
            return False
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        _runtime_log(f"startup shortcut install exception: {type(exc).__name__}: {exc}")
        return False


def _remove_windows_startup_shortcut() -> bool:
    try:
        _windows_startup_shortcut_path().unlink(missing_ok=True)
        return True
    except OSError as exc:
        _runtime_log(f"startup shortcut removal exception: {type(exc).__name__}: {exc}")
        return False


def _install_windows_startup_task() -> bool:
    APP_DATA.mkdir(parents=True, exist_ok=True)
    task_file = APP_DATA / "startup-task.xml"
    temporary = task_file.with_suffix(".tmp")
    try:
        temporary.write_text(_startup_task_xml(), encoding="utf-16")
        os.replace(temporary, task_file)
        result = subprocess.run(
            ["schtasks.exe", "/Create", "/TN", STARTUP_TASK_NAME,
             "/XML", str(task_file), "/F"],
            capture_output=True, text=True, timeout=20,
            creationflags=_hidden_flags(),
        )
        if result.returncode != 0:
            _runtime_log(
                "startup task install failed: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
            return False
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        _runtime_log(f"startup task install exception: {type(exc).__name__}: {exc}")
        return False
    finally:
        temporary.unlink(missing_ok=True)


def _remove_windows_startup_task() -> bool:
    try:
        result = subprocess.run(
            ["schtasks.exe", "/Delete", "/TN", STARTUP_TASK_NAME, "/F"],
            capture_output=True, text=True, timeout=15,
            creationflags=_hidden_flags(),
        )
        # Windows returns a non-zero code when an already absent task is
        # deleted. Absence is the requested end state, so accept that case.
        if result.returncode == 0:
            return True
        query = subprocess.run(
            ["schtasks.exe", "/Query", "/TN", STARTUP_TASK_NAME],
            capture_output=True, timeout=10, creationflags=_hidden_flags(),
        )
        return query.returncode != 0
    except (OSError, subprocess.SubprocessError) as exc:
        _runtime_log(f"startup task removal exception: {type(exc).__name__}: {exc}")
        return False


def set_windows_autostart(enabled: bool) -> None:
    """Maintain three independent per-user startup paths.

    Some managed Windows installations deny creation of scheduled tasks to a
    normal user. The Startup-folder shortcut is equally path-aware and needs no
    elevation; HKCU Run remains a final fallback. Single-instance locking makes
    simultaneous triggers harmless.
    """
    task_ok = _install_windows_startup_task() if enabled else _remove_windows_startup_task()
    shortcut_ok = (
        _install_windows_startup_shortcut()
        if enabled else _remove_windows_startup_shortcut()
    )
    registry_ok = True
    try:
        _set_windows_run_fallback(enabled)
    except Exception as exc:
        registry_ok = False
        _runtime_log(f"startup Run fallback failed: {type(exc).__name__}: {exc}")
    if not task_ok and not shortcut_ok and not registry_ok:
        raise OSError("all Windows startup methods failed")


def reconcile_windows_autostart(enabled: bool) -> bool:
    """Repair both startup paths and follow the current EXE path."""
    try:
        set_windows_autostart(enabled)
        return True
    except Exception as exc:
        _runtime_log(
            "autostart reconciliation failed; "
            f"enabled={enabled}; error={type(exc).__name__}: {exc}"
        )
        return False


def prepare_runtime_working_directory(path: Path | None = None) -> Path:
    """Keep third-party relative logs out of System32 at Windows startup."""
    runtime_path = Path(path or APP_DATA)
    runtime_path.mkdir(parents=True, exist_ok=True)
    os.chdir(runtime_path)
    return runtime_path


def pil_to_qimage(image) -> QImage:
    rgb = image.convert("RGB")
    return QImage(rgb.tobytes("raw", "RGB"), rgb.width, rgb.height, rgb.width * 3, QImage.Format.Format_RGB888).copy()


class WorkerSignals(QObject):
    preview = Signal(QImage)
    status = Signal(str)
    error = Signal(str)
    stopped = Signal()


class ControlPanel(QWidget):
    def __init__(self, autorun: bool = False, smoke_test: bool = False) -> None:
        super().__init__()
        self.settings = load_settings()
        self.language = normalize_language(self.settings.get("language"))
        self.stop_event = threading.Event()
        self.screen_connected = threading.Event()
        self.worker: threading.Thread | None = None
        self.swatches: dict[str, QPushButton] = {}
        self.edit_controls: list[QWidget] = []
        self._editing_enabled = True
        self._preview_image: QImage | None = None
        self.preview_mode = "daily"
        self._exit_requested = False
        self._session_shutdown_started = False
        self._tray_notice_shown = False
        self._tray_enabled = not smoke_test
        self._autorun = autorun
        self._tray_setup_attempts = 0
        self._smoke_test = smoke_test
        self._screen_progress_at = 0.0
        self._watchdog_restart_started = False
        self.tray_icon: QSystemTrayIcon | None = None
        self.signals = WorkerSignals()
        self.signals.preview.connect(self._show_preview)
        self.signals.status.connect(self._worker_status)
        self.signals.error.connect(self._worker_error)
        self.signals.stopped.connect(self._screen_stopped)
        self.setWindowTitle(self._window_title())
        if APP_ICON_PATH.exists():
            self.setWindowIcon(QIcon(str(APP_ICON_PATH)))
        self.resize(1200, 890)
        self.setMinimumSize(1160, 860)
        self.setStyleSheet(self._style_sheet())
        self._build_ui()
        self._setup_tray_icon()
        self._register_static_translations()
        self._apply_language(refresh_preview=False)
        self.activation_timer = QTimer(self)
        self.activation_timer.timeout.connect(self._consume_activation_request)
        self.activation_timer.start(250)
        self.liveness_timer = QTimer(self)
        self.liveness_timer.timeout.connect(self._liveness_tick)
        self.liveness_timer.start(INSTANCE_HEARTBEAT_INTERVAL_MS)
        self._refresh_preview()
        _runtime_log(f"control panel ready; autorun={autorun}; smoke_test={smoke_test}")
        if smoke_test:
            QTimer.singleShot(900, self.close)

    def _consume_activation_request(self) -> None:
        if not ACTIVATE_REQUEST_PATH.exists():
            return
        try:
            token = ACTIVATE_REQUEST_PATH.read_text(encoding="ascii").strip()
            ACTIVATE_REQUEST_PATH.unlink(missing_ok=True)
        except OSError:
            return
        self._show_from_tray()
        try:
            ACTIVATE_ACK_PATH.write_text(token, encoding="ascii")
        except OSError:
            pass

    def _liveness_tick(self) -> None:
        if self._smoke_test:
            return
        _write_instance_state()
        worker = self.worker
        if (
            self._watchdog_restart_started
            or self.stop_event.is_set()
            or worker is None
            or not worker.is_alive()
        ):
            return
        if self._screen_progress_at and time.monotonic() - self._screen_progress_at > DISPLAY_STALL_SECONDS:
            self._restart_after_display_stall()

    def _restart_after_display_stall(self) -> None:
        """Replace the process when a native USB display write blocks forever."""
        if self._watchdog_restart_started:
            return
        self._watchdog_restart_started = True
        _runtime_log("display watchdog detected a stalled frame loop; restarting application")
        command = (
            [sys.executable, "--autorun", "--recover-pid", str(os.getpid())]
            if getattr(sys, "frozen", False)
            else [
                sys.executable, str(Path(__file__).resolve()),
                "--autorun", "--recover-pid", str(os.getpid()),
            ]
        )
        try:
            subprocess.Popen(command, cwd=str(APP_DATA), creationflags=_hidden_flags())
        except OSError as exc:
            self._watchdog_restart_started = False
            _runtime_log(f"display watchdog restart failed: {type(exc).__name__}: {exc}")
            return
        self._exit_requested = True
        self.stop_event.set()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _style_sheet(self) -> str:
        return f"""
            QWidget {{ background: {WINDOW_BG}; color: {TEXT}; font-family: 'Segoe UI Variable Text', 'Segoe UI'; font-size: 13px; }}
            QLabel, QRadioButton, QCheckBox {{ background: transparent; }}
            QFrame#card {{ background: {CARD_BG}; border: 1px solid {EDGE}; border-radius: 16px; }}
            QFrame#subCard, QFrame#colorChooser {{ background: {CARD_2}; border: 1px solid {EDGE}; border-radius: 11px; }}
            QFrame#previewShell, QFrame#statusBar {{ background: {CARD_2}; border: 0; border-radius: 11px; }}
            QLabel#title {{ font-size: 30px; font-weight: 700; }}
            QLabel#subtitle, QLabel#body, QLabel#device, QLabel#status {{ color: {MUTED}; }}
            QLabel#section {{ font-size: 15px; font-weight: 700; }}
            QLabel#preview {{ background: #080A0E; border-radius: 5px; }}
            QComboBox, QLineEdit {{ background: {CONTROL_BG}; border: 1px solid {EDGE}; border-radius: 8px; padding: 9px 11px; }}
            QComboBox:focus, QLineEdit:focus {{ border-color: {ACCENT}; }}
            QComboBox::drop-down {{ border: 0; width: 28px; }}
            QComboBox QAbstractItemView {{ background: {CONTROL_BG}; selection-background-color: #303846; outline: 0; }}
            QPushButton {{ background: {CONTROL_BG}; border: 1px solid transparent; border-radius: 9px; padding: 11px 14px; font-weight: 600; outline: none; }}
            QPushButton:hover {{ background: {CONTROL_HOVER}; }} QPushButton:pressed {{ background: {CONTROL_PRESSED}; }}
            QPushButton:focus {{ border-color: {ACCENT}; }}
            QPushButton:disabled {{ color: #646C78; background: #191D24; }}
            QComboBox:disabled, QLineEdit:disabled {{ color: #555D68; background: #111419; border-color: #20252D; }}
            QRadioButton:disabled, QCheckBox:disabled {{ color: #555D68; }}
            QSlider::groove:horizontal:disabled {{ background: #191D24; }}
            QSlider::sub-page:horizontal:disabled {{ background: #343B46; }}
            QSlider::handle:horizontal:disabled {{ background: #555D68; }}
            QPushButton#accent {{ color: #0A1019; background: {ACCENT}; padding: 12px 16px; }}
            QPushButton#accent:hover {{ background: #93BAF7; }}
            QPushButton#accent:disabled {{ color: #555D68; background: #191D24; border: 1px solid #20252D; }}
            QPushButton#previewMode {{ padding: 7px 8px; color: {MUTED}; background: transparent; border: 1px solid {EDGE}; font-size: 12px; }}
            QPushButton#previewMode:hover {{ color: {TEXT}; background: {CONTROL_BG}; }}
            QPushButton#previewMode:checked {{ color: {TEXT}; background: #29364A; border-color: {ACCENT}; }}
            QPushButton#previewMode:disabled {{ color: #4E5661; background: #111419; border-color: #20252D; }}
            QSlider::groove:horizontal {{ height: 5px; background: #29303A; border-radius: 2px; }}
            QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
            QSlider::handle:horizontal {{ width: 16px; margin: -6px 0; background: {TEXT}; border-radius: 8px; }}
            QRadioButton, QCheckBox {{ spacing: 8px; }}
            QRadioButton::indicator, QCheckBox::indicator {{ width: 17px; height: 17px; }}
            QRadioButton::indicator:unchecked, QCheckBox::indicator:unchecked {{ background: #29303A; border: 1px solid #4B5563; border-radius: 8px; }}
            QRadioButton::indicator:checked, QCheckBox::indicator:checked {{ background: {ACCENT}; border: 3px solid #29303A; border-radius: 8px; }}
            QFrame#rule {{ background: {EDGE}; max-height: 1px; border: 0; }}
            QToolTip {{ background: {CONTROL_BG}; color: {TEXT}; border: 1px solid {EDGE}; padding: 6px; }}
        """

    def _label(self, text: str, name: str = "body") -> QLabel:
        label = QLabel(self._tr(text))
        label.setProperty("i18n_source", text)
        label.setObjectName(name)
        return label

    def _tr(self, text: str, **values: object) -> str:
        return translate(text, self.language, **values)

    def _window_title(self) -> str:
        return f"{self._tr(APP_DISPLAY_NAME)} · {self._tr('Ayarlar')}"

    def _rule(self) -> QFrame:
        rule = QFrame()
        rule.setObjectName("rule")
        rule.setFixedHeight(1)
        return rule

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(26, 20, 26, 20)
        outer.setSpacing(14)
        header = QHBoxLayout()
        header.setSpacing(13)
        if APP_LOGO_PATH.exists():
            brand_mark = QLabel()
            brand_mark.setFixedSize(52, 52)
            brand_mark.setPixmap(QPixmap(str(APP_LOGO_PATH)).scaled(
                52, 52, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))
            header.addWidget(brand_mark)
        header_copy = QVBoxLayout()
        header_copy.setSpacing(2)
        header_copy.addWidget(self._label(APP_DISPLAY_NAME, "title"))
        header_copy.addWidget(self._label(
            "Sistem, oyun, medya ve kablosuz cihaz bilgilerini tek yerden yönetin",
            "subtitle",
        ))
        header.addLayout(header_copy)
        header.addStretch()
        language_box = QVBoxLayout()
        language_box.setSpacing(3)
        language_box.addWidget(self._label("Dil", "device"))
        self.language_combo = QComboBox()
        for code, label in SUPPORTED_LANGUAGES.items():
            self.language_combo.addItem(label, code)
        self.language_combo.setCurrentIndex(self.language_combo.findData(self.language))
        self.language_combo.setFixedSize(122, 36)
        self.language_combo.currentIndexChanged.connect(self._language_changed)
        language_box.addWidget(self.language_combo)
        header.addLayout(language_box)
        self.edit_controls.append(self.language_combo)
        outer.addLayout(header)

        content = QHBoxLayout()
        content.setSpacing(16)
        outer.addLayout(content, 1)
        content.addStretch(1)

        left_stack = QWidget()
        left_stack.setFixedWidth(700)
        left_layout = QVBoxLayout(left_stack)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(14)
        content.addWidget(left_stack, 0, Qt.AlignmentFlag.AlignTop)

        self.quick_card = QFrame(objectName="card")
        self.quick_card.setFixedHeight(252)
        self.quick_card_effect = QGraphicsOpacityEffect(self.quick_card)
        self.quick_card_effect.setOpacity(1.0)
        self.quick_card.setGraphicsEffect(self.quick_card_effect)
        quick_layout = QVBoxLayout(self.quick_card)
        quick_layout.setContentsMargins(16, 14, 16, 14)
        quick_layout.setSpacing(9)
        quick_layout.addWidget(self._label("Ekran ve donanım", "section"))
        quick_columns = QHBoxLayout()
        quick_columns.setSpacing(10)
        quick_layout.addLayout(quick_columns, 1)

        screen_box = QFrame(objectName="subCard")
        screen_form = QVBoxLayout(screen_box)
        screen_form.setContentsMargins(13, 11, 13, 11)
        screen_form.setSpacing(7)
        screen_form.addWidget(self._label("Ekran", "section"))
        brightness_line = QHBoxLayout()
        brightness_line.addWidget(self._label("Parlaklık"))
        brightness_line.addStretch()
        self.auto_brightness_check = QCheckBox("Otomatik")
        self.auto_brightness_check.setToolTip(
            "Saat 07.00–19.00 arasında %90, diğer saatlerde %30 parlaklık kullanır."
        )
        self.auto_brightness_check.setChecked(bool(self.settings.get("auto_brightness", False)))
        brightness_line.addWidget(self.auto_brightness_check)
        self.auto_brightness_info = QLabel("i")
        self.auto_brightness_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.auto_brightness_info.setFixedSize(16, 16)
        self.auto_brightness_info.setToolTip(
            "07.00–19.00: %90 parlaklık\n"
            "19.00–07.00: %30 parlaklık\n"
            "Otomatik kapalıyken parlaklığı sürgüden ayarlayabilirsiniz."
        )
        self.auto_brightness_info.setStyleSheet(
            "color: #B9C4D2; border: 1px solid #667284; border-radius: 8px; "
            "background: #171B22; font: 700 10px 'Segoe UI';"
        )
        brightness_line.addWidget(self.auto_brightness_info)
        self.brightness_value = self._label(f"%{int(self.settings['brightness'])}", "section")
        brightness_line.addWidget(self.brightness_value)
        screen_form.addLayout(brightness_line)
        self.brightness_slider = QSlider(Qt.Orientation.Horizontal)
        self.brightness_slider.setRange(5, 100)
        self.brightness_slider.setValue(int(self.settings["brightness"]))
        self.brightness_slider.valueChanged.connect(self._sync_brightness_controls)
        self.auto_brightness_check.toggled.connect(self._sync_brightness_controls)
        screen_form.addWidget(self.brightness_slider)
        self.edit_controls.extend((self.auto_brightness_check, self.brightness_slider))
        self._sync_brightness_controls()
        rotation_line = QHBoxLayout()
        rotation_line.setSpacing(8)
        rotation_line.addWidget(self._label("Ekran yönü", "device"))
        self.rotation_combo = QComboBox()
        self.rotation_combo.addItem("0°", "normal")
        self.rotation_combo.addItem("180°", "180")
        selected_rotation = "180" if self.settings.get("rotation") == "180" else "normal"
        self.rotation_combo.setCurrentIndex(self.rotation_combo.findData(selected_rotation))
        self.rotation_combo.setFixedHeight(34)
        self.rotation_combo.currentIndexChanged.connect(self._refresh_preview)
        self.edit_controls.append(self.rotation_combo)
        rotation_line.addWidget(self.rotation_combo, 1)
        screen_form.addLayout(rotation_line)
        self.autostart_check = QCheckBox("Windows açıldığında çalıştır")
        self.autostart_check.setToolTip("Windows açıldığında uygulamayı ve mini ekranı otomatik başlatır.")
        self.autostart_check.setChecked(bool(self.settings["autostart"]))
        screen_form.addWidget(self.autostart_check)
        self.edit_controls.append(self.autostart_check)
        self.msfs_layout_combo = QComboBox()
        for label, value in MSFS_LAYOUTS.items():
            self.msfs_layout_combo.addItem(self._tr(label), value)
        current_layout = self.settings.get("msfs_layout", "cockpit")
        self.msfs_layout_combo.setCurrentIndex(self.msfs_layout_combo.findData(current_layout))
        self.msfs_layout_combo.currentIndexChanged.connect(self._msfs_layout_changed)
        self.msfs_layout_combo.setToolTip("MSFS 2024 alt bölümünde kullanılacak görünüm.")
        self.msfs_layout_combo.setFixedHeight(34)
        self.msfs_layout_combo.setMinimumWidth(190)
        self.msfs_layout_combo.setStyleSheet("padding: 4px 10px;")
        msfs_line = QHBoxLayout()
        msfs_line.addWidget(self._label("MSFS", "device"))
        msfs_line.addWidget(self.msfs_layout_combo, 1)
        screen_form.addLayout(msfs_line)
        self.edit_controls.append(self.msfs_layout_combo)
        quick_columns.addWidget(screen_box, 1)

        hardware_box = QFrame(objectName="subCard")
        hardware_form = QVBoxLayout(hardware_box)
        hardware_form.setContentsMargins(13, 11, 13, 11)
        hardware_form.setSpacing(6)
        hardware_header = QHBoxLayout()
        hardware_header.addWidget(self._label("Donanım adları", "section"))
        hardware_header.addStretch()
        self.hardware_limit_label = self._label(
            self._tr("En fazla {count} karakter", count=MAX_HARDWARE_LABEL_LENGTH), "device"
        )
        self.hardware_limit_label.setProperty("i18n_source", "")
        hardware_header.addWidget(self.hardware_limit_label)
        hardware_form.addLayout(hardware_header)
        automatic_labels = get_auto_hardware_labels()
        saved_labels = self.settings.get("hardware_labels", {})
        self.hardware_inputs: dict[str, QLineEdit] = {}
        hardware_grid = QGridLayout()
        hardware_grid.setHorizontalSpacing(8)
        hardware_grid.setVerticalSpacing(5)
        for row, (key, caption) in enumerate((
            ("cpu", "İşlemci"), ("gpu", "Ekran kartı"), ("ram", "Bellek"),
        )):
            hardware_grid.addWidget(self._label(caption, "device"), row, 0)
            field = QLineEdit()
            field.setMaxLength(MAX_HARDWARE_LABEL_LENGTH)
            field.setFixedHeight(34)
            field.setStyleSheet("padding: 5px 10px;")
            field.setText(normalise_hardware_label(saved_labels.get(key)))
            field.setPlaceholderText(automatic_labels[key])
            field.textChanged.connect(self._refresh_preview)
            self.hardware_inputs[key] = field
            self.edit_controls.append(field)
            hardware_grid.addWidget(field, row, 1)
        hardware_grid.setColumnStretch(1, 1)
        hardware_form.addLayout(hardware_grid)
        weather_line = QHBoxLayout()
        weather_line.setSpacing(8)
        weather_line.addWidget(self._label("Hava", "device"))
        saved_cities = list(normalise_city_names(self.settings.get(
            "weather_cities", DEFAULT_WEATHER_CITIES,
        )))
        saved_cities += [""] * (3 - len(saved_cities))
        self.weather_city_inputs: list[QLineEdit] = []
        for index in range(3):
            city_input = QLineEdit()
            city_input.setFixedHeight(34)
            city_input.setMaxLength(24)
            city_input.setStyleSheet("padding: 5px 7px;")
            city_input.setText(saved_cities[index])
            city_input.setProperty("city_index", index + 1)
            city_input.setPlaceholderText(
                self._tr("{index}. şehir", index=index + 1)
            )
            city_input.setToolTip("Hava durumu gösterilecek şehir.")
            city_input.textChanged.connect(self._refresh_preview)
            city_input.editingFinished.connect(
                lambda field=city_input: self._normalise_city_field(field)
            )
            city_input.setMinimumWidth(0)
            self.weather_city_inputs.append(city_input)
            self.edit_controls.append(city_input)
            weather_line.addWidget(city_input, 1)
        hardware_form.addLayout(weather_line)
        quick_columns.addWidget(hardware_box, 1)
        left_layout.addWidget(self.quick_card)

        preview_card = QFrame(objectName="card")
        preview_card.setFixedHeight(440)
        preview_layout = QVBoxLayout(preview_card)
        preview_layout.setContentsMargins(16, 14, 16, 12)
        preview_layout.setSpacing(9)
        preview_layout.addWidget(self._label("3,5 inç ekran önizlemesi", "section"))
        preview_body = QHBoxLayout()
        preview_body.setSpacing(10)
        preview_layout.addLayout(preview_body)
        self.preview_buttons: dict[str, QPushButton] = {}
        self.preview_button_group = QButtonGroup(self)
        self.preview_button_group.setExclusive(True)
        preview_modes = QVBoxLayout()
        preview_modes.setSpacing(5)
        for mode, caption in (
            ("daily", "Günlük"), ("cs2_ct", "CS2 · CT"),
            ("cs2_t", "CS2 · T"),
            ("deathmatch", "Ölüm maçı"),
            ("msfs", "MSFS"), ("weather", "Hava"),
            ("wireless", "Kablosuz"),
        ):
            button = QPushButton(caption)
            button.setObjectName("previewMode")
            button.setFixedSize(96, 34)
            button.setCheckable(True)
            button.setChecked(mode == self.preview_mode)
            button.clicked.connect(
                lambda _checked=False, selected_mode=mode: self._set_preview_mode(selected_mode)
            )
            self.preview_button_group.addButton(button)
            self.preview_buttons[mode] = button
            self.edit_controls.append(button)
            preview_modes.addWidget(button)
        preview_modes.addStretch()
        preview_body.addLayout(preview_modes)
        preview_shell = QFrame(objectName="previewShell")
        preview_shell.setFixedSize(508, 348)
        shell_layout = QVBoxLayout(preview_shell)
        shell_layout.setContentsMargins(14, 14, 14, 14)
        self.preview_label = self._label("", "preview")
        self.preview_label.setFixedSize(WIDTH, HEIGHT)
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        shell_layout.addWidget(self.preview_label)
        preview_body.addWidget(preview_shell)
        preview_body.addStretch()
        self.preview_context_label = self._label("Günlük kullanım · medya görünümü", "device")
        preview_layout.addWidget(self.preview_context_label)
        left_layout.addWidget(preview_card)
        left_layout.addStretch()

        settings_card = QFrame(objectName="card")
        settings_card.setFixedSize(410, 706)
        settings_layout = QVBoxLayout(settings_card)
        settings_layout.setContentsMargins(18, 16, 18, 16)
        settings_layout.setSpacing(10)
        settings_layout.addWidget(self._label("Renkler", "section"))
        settings_layout.addWidget(self._label("Hazır palet"))
        self.palette_combo = QComboBox()
        for name in PALETTES:
            self.palette_combo.addItem(self._tr(name), name)
        for name in sorted(self.settings.get("custom_palettes", {})):
            self.palette_combo.addItem(name, name)
        if self.settings["palette"] not in PALETTES and self.settings["palette"] not in self.settings.get("custom_palettes", {}):
            self.palette_combo.addItem(self._tr("Özel"), "Özel")
        self.palette_combo.setCurrentIndex(self.palette_combo.findData(self.settings["palette"]))
        self.palette_combo.currentIndexChanged.connect(self._palette_changed)
        settings_layout.addWidget(self.palette_combo)
        self.edit_controls.append(self.palette_combo)
        settings_layout.addWidget(self._label("Değiştirmek istediğiniz alanı seçin", "device"))

        color_editor = QHBoxLayout()
        color_editor.setSpacing(10)
        swatch_column = QVBoxLayout()
        swatch_column.setSpacing(8)
        for key, caption in (
            ("cpu", "Normal"), ("warning", "Uyarı"),
            ("danger", "Kritik"), ("bg", "Zemin"),
            ("text", "Ana yazı"), ("muted", "İkincil yazı"),
        ):
            button = QPushButton(caption)
            button.setFixedSize(126, 40)
            button.clicked.connect(lambda _checked=False, color_key=key: self._choose_color(color_key))
            self.swatches[key] = button
            self.edit_controls.append(button)
            swatch_column.addWidget(button)
        swatch_column.addStretch()
        color_editor.addLayout(swatch_column)

        self.inline_color_panel = QFrame(objectName="colorChooser")
        self.inline_color_panel.setFixedWidth(0)
        self.inline_color_panel.setVisible(False)
        chooser_layout = QVBoxLayout(self.inline_color_panel)
        chooser_layout.setContentsMargins(11, 10, 11, 10)
        chooser_layout.setSpacing(8)
        chooser_header = QHBoxLayout()
        chooser_header.setSpacing(8)
        self.inline_color_title = self._label("Renk seçin", "section")
        chooser_header.addWidget(self.inline_color_title)
        chooser_header.addStretch()
        self.selected_color_chip = QLabel()
        self.selected_color_chip.setFixedSize(34, 24)
        self.selected_color_chip.setToolTip("Seçili renk")
        chooser_header.addWidget(self.selected_color_chip)
        chooser_layout.addLayout(chooser_header)
        color_grid = QGridLayout()
        color_grid.setSpacing(7)
        self.simple_color_buttons: list[QPushButton] = []
        for index in range(12):
            color_button = QPushButton("")
            color_button.setObjectName("colorDot")
            color_button.setFixedSize(31, 31)
            color_button.clicked.connect(
                lambda _checked=False, option_index=index: self._apply_simple_color(option_index)
            )
            self.simple_color_buttons.append(color_button)
            self.edit_controls.append(color_button)
            color_grid.addWidget(color_button, index // 4, index % 4)
        chooser_layout.addLayout(color_grid)
        chooser_layout.addStretch()
        color_editor.addWidget(self.inline_color_panel)
        color_editor.addStretch()
        settings_layout.addLayout(color_editor)
        self.color_help_label = self._label("Halka, zemin ve yazı renklerinden birini seçin.", "device")
        self.color_help_label.setWordWrap(True)
        settings_layout.addWidget(self.color_help_label)
        self._paint_swatches()

        palette_actions = QHBoxLayout()
        self.save_palette_button = QPushButton("Paleti kaydet")
        self.save_palette_button.setToolTip("Geçerli renkleri yeni bir palet olarak kaydeder.")
        self.save_palette_button.clicked.connect(self._save_palette)
        self.delete_palette_button = QPushButton("Sil")
        self.delete_palette_button.setToolTip("Seçili özel renk paletini siler.")
        self.delete_palette_button.clicked.connect(self._delete_palette)
        self.edit_controls.extend((self.save_palette_button, self.delete_palette_button))
        palette_actions.addWidget(self.save_palette_button, 1)
        palette_actions.addWidget(self.delete_palette_button)
        settings_layout.addLayout(palette_actions)
        settings_layout.addStretch()
        self._update_palette_buttons()
        settings_layout.addWidget(self._rule())
        action_line = QHBoxLayout()
        self.start_button = QPushButton("Kaydet ve başlat", objectName="accent")
        self.start_button.setToolTip("Ayarları kaydeder ve mini ekrana görüntü göndermeye başlar.")
        self.start_button.clicked.connect(self.start_screen)
        self.edit_controls.append(self.start_button)
        self.stop_button = QPushButton("Durdur")
        self.stop_button.setToolTip("Veri akışını durdurur ve mini ekranı temizler.")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_screen)
        action_line.addWidget(self.start_button, 1)
        action_line.addWidget(self.stop_button)
        settings_layout.addLayout(action_line)
        secondary_actions = QHBoxLayout()
        self.save_button = QPushButton("Yalnızca kaydet")
        self.save_button.clicked.connect(self.save_settings)
        self.reset_button = QPushButton("Varsayılana dön")
        self.reset_button.setToolTip("Renkleri ve görünüm ayarlarını başlangıç değerlerine getirir.")
        self.reset_button.clicked.connect(self._reset_defaults)
        secondary_actions.addWidget(self.save_button, 1)
        secondary_actions.addWidget(self.reset_button, 1)
        settings_layout.addLayout(secondary_actions)
        self.edit_controls.extend((self.save_button, self.reset_button))
        content.addWidget(settings_card, 0, Qt.AlignmentFlag.AlignTop)
        content.addStretch(1)

        status_bar = QFrame(objectName="statusBar")
        status_bar.setMaximumWidth(1126)
        status_layout = QHBoxLayout(status_bar)
        status_layout.setContentsMargins(14, 9, 14, 9)
        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet("color: #6F7C90; background: transparent;")
        self.status_label = self._label("Ekran hazır", "status")
        status_layout.addWidget(self.status_dot)
        status_layout.addWidget(self.status_label)
        status_layout.addStretch()
        outer.addWidget(status_bar, 0, Qt.AlignmentFlag.AlignHCenter)
        # Mouse clicks should not leave the native dotted focus rectangle.
        # Keyboard navigation still receives focus through Tab.
        for button in self.findChildren(QPushButton):
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)

    def _register_static_translations(self) -> None:
        for widget_type in (QLabel, QPushButton, QCheckBox, QRadioButton):
            for widget in self.findChildren(widget_type):
                if widget.property("i18n_source") is None and widget.text():
                    widget.setProperty("i18n_source", widget.text())
                if widget.toolTip() and widget.property("i18n_tooltip") is None:
                    widget.setProperty("i18n_tooltip", widget.toolTip())

    def _retranslate_combo_items(self) -> None:
        selected_palette = self._selected_palette_name()
        self._rebuild_palette_combo(selected_palette)
        selected_layout = str(self.msfs_layout_combo.currentData() or "cockpit")
        self.msfs_layout_combo.blockSignals(True)
        self.msfs_layout_combo.clear()
        for label, value in MSFS_LAYOUTS.items():
            self.msfs_layout_combo.addItem(self._tr(label), value)
        self.msfs_layout_combo.setCurrentIndex(self.msfs_layout_combo.findData(selected_layout))
        self.msfs_layout_combo.blockSignals(False)

    def _apply_language(self, refresh_preview: bool = True) -> None:
        self.setWindowTitle(self._window_title())
        for widget_type in (QLabel, QPushButton, QCheckBox, QRadioButton):
            for widget in self.findChildren(widget_type):
                source = widget.property("i18n_source")
                if source:
                    widget.setText(self._tr(str(source)))
                tooltip = widget.property("i18n_tooltip")
                if tooltip:
                    widget.setToolTip(self._tr(str(tooltip)))
        self.hardware_limit_label.setText(self._tr(
            "En fazla {count} karakter", count=MAX_HARDWARE_LABEL_LENGTH,
        ))
        for field in self.weather_city_inputs:
            field.setPlaceholderText(self._tr(
                "{index}. şehir", index=int(field.property("city_index") or 1),
            ))
        self._retranslate_combo_items()
        if self.tray_icon is not None:
            self.tray_icon.setToolTip(self._tr(APP_DISPLAY_NAME))
            self.tray_open_action.setText(self._tr("Sabasakal Mini Ekran'ı aç"))
            self.tray_start_action.setText(self._tr("Mini ekranı başlat"))
            self.tray_stop_action.setText(self._tr("Mini ekranı durdur"))
            self.tray_exit_action.setText(self._tr("Çıkış"))
        if getattr(self, "_active_color_key", None) in self.swatches:
            self._choose_color(self._active_color_key)
        if refresh_preview:
            self._refresh_preview()

    def _language_changed(self, _index: int) -> None:
        self.language = normalize_language(self.language_combo.currentData())
        self.settings["language"] = self.language
        self._apply_language()
        self.save_settings(quiet=True)

    def _setup_tray_icon(self) -> None:
        if not self._tray_enabled or self.tray_icon is not None or not APP_ICON_PATH.exists():
            return
        if not QSystemTrayIcon.isSystemTrayAvailable():
            # Explorer and its notification area can appear a few seconds
            # after Run-key applications during Windows sign-in.
            if self._autorun:
                self._tray_setup_attempts += 1
                if self._tray_setup_attempts <= 20:
                    QTimer.singleShot(1500, self._setup_tray_icon)
            return
        icon = QIcon(str(APP_ICON_PATH))
        self.tray_icon = QSystemTrayIcon(icon, self)
        self.tray_icon.setToolTip(self._tr(APP_DISPLAY_NAME))
        menu = QMenu(self)
        self.tray_open_action = QAction(self._tr("Sabasakal Mini Ekran'ı aç"), self)
        self.tray_open_action.triggered.connect(self._show_from_tray)
        menu.addAction(self.tray_open_action)
        menu.addSeparator()
        self.tray_start_action = QAction(self._tr("Mini ekranı başlat"), self)
        self.tray_start_action.triggered.connect(self.start_screen)
        self.tray_stop_action = QAction(self._tr("Mini ekranı durdur"), self)
        worker_running = bool(self.worker and self.worker.is_alive())
        self.tray_start_action.setEnabled(not worker_running)
        self.tray_stop_action.setEnabled(worker_running)
        self.tray_stop_action.triggered.connect(self.stop_screen)
        menu.addAction(self.tray_start_action)
        menu.addAction(self.tray_stop_action)
        menu.addSeparator()
        self.tray_exit_action = QAction(self._tr("Çıkış"), self)
        self.tray_exit_action.triggered.connect(self._exit_application)
        menu.addAction(self.tray_exit_action)
        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(self._tray_activated)
        self.tray_icon.show()
        app = QApplication.instance()
        if app is not None:
            app.setQuitOnLastWindowClosed(False)
        if self._autorun:
            self.hide()

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        }:
            self._show_from_tray()

    def _show_from_tray(self) -> None:
        self.showNormal()
        self.ensurePolished()
        self.update()
        self.raise_()
        self.activateWindow()

    def _exit_application(self) -> None:
        self._exit_requested = True
        if self.tray_icon is not None:
            self.tray_icon.hide()
        if self.worker and self.worker.is_alive():
            self.stop_screen()
            return
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def prepare_system_shutdown(self) -> None:
        """Synchronously blank the LCD while Windows is ending the session."""
        if self._session_shutdown_started:
            return
        self._session_shutdown_started = True
        self._exit_requested = True
        if self.tray_icon is not None:
            self.tray_icon.hide()
        self.stop_event.set()
        worker = self.worker
        if worker and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=4.0)
        if worker and worker.is_alive():
            _runtime_log("system shutdown cleanup timed out")
        else:
            _runtime_log("system shutdown cleanup completed")

    def _rotation(self) -> str:
        return str(self.rotation_combo.currentData() or "normal")

    def _normalise_city_field(self, field: QLineEdit) -> None:
        formatted = format_city_name(field.text())
        if formatted != field.text():
            field.setText(formatted)

    def _weather_city_names(self) -> tuple[str, ...]:
        return normalise_city_names([field.text() for field in self.weather_city_inputs])

    def _preview_weather_readings(self) -> tuple[WeatherReading, ...]:
        sample_codes = (0, 61, 3)
        sample_temps = (24.0, 19.0, 16.0)
        return tuple(
            WeatherReading(city, sample_temps[index], sample_codes[index])
            for index, city in enumerate(self._weather_city_names())
        )

    def _snapshot_settings(self) -> dict:
        return {
            "palette": self._selected_palette_name(), "colors": dict(self.settings["colors"]),
            "brightness": self.brightness_slider.value(),
            "auto_brightness": self.auto_brightness_check.isChecked(),
            "rotation": self._rotation(),
            "language": self.language,
            "autostart": self.autostart_check.isChecked(), "port": self.settings.get("port", "AUTO"),
            "interval": float(self.settings.get("interval", 0.5)),
            "msfs_layout": str(self.msfs_layout_combo.currentData() or "cockpit"),
            "weather_cities": list(self._weather_city_names()),
            "hardware_labels": {
                key: normalise_hardware_label(field.text())
                for key, field in self.hardware_inputs.items()
            },
            "custom_palettes": {
                name: dict(colors) for name, colors in self.settings.get("custom_palettes", {}).items()
            },
        }

    def _selected_palette_name(self) -> str:
        return str(self.palette_combo.currentData() or self.palette_combo.currentText())

    def _palette_changed(self, _index: int) -> None:
        palette = self._selected_palette_name()
        if palette in PALETTES:
            self.settings["colors"] = dict(PALETTES[palette])
        elif palette in self.settings.get("custom_palettes", {}):
            self.settings["colors"] = dict(self.settings["custom_palettes"][palette])
        else:
            self._update_palette_buttons()
            return
        self._paint_swatches()
        self._refresh_preview()
        self._update_palette_buttons()

    def _msfs_layout_changed(self, _index: int) -> None:
        self.preview_mode = "msfs"
        if hasattr(self, "preview_buttons"):
            self.preview_buttons["msfs"].setChecked(True)
        self._refresh_preview()

    def _set_preview_mode(self, mode: str) -> None:
        if mode not in {"daily", "cs2_ct", "cs2_t", "deathmatch", "msfs", "weather", "wireless"}:
            return
        self.preview_mode = mode
        self._refresh_preview()

    def _update_palette_buttons(self) -> None:
        if hasattr(self, "delete_palette_button"):
            self.delete_palette_button.setEnabled(
                self._selected_palette_name() in self.settings.get("custom_palettes", {})
            )

    def _save_palette(self) -> None:
        custom = self.settings.setdefault("custom_palettes", {})
        index = 1
        suggested = self._tr("Paletim")
        while suggested in custom:
            index += 1
            suggested = f"{self._tr('Paletim')} {index}"
        name, accepted = QInputDialog.getText(
            self, self._tr("Renk paletini kaydet"), self._tr("Palet adı:"), text=suggested,
        )
        name = name.strip()
        if not accepted or not name:
            return
        if name in PALETTES:
            QMessageBox.warning(
                self, self._tr("Bu ad kullanılamaz"),
                self._tr("Hazır palet adları değiştirilemez."),
            )
            return
        if name in custom:
            answer = QMessageBox.question(
                self, self._tr("Paletin üzerine yazılsın mı?"),
                self._tr("{name} adlı palet zaten var.", name=name),
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        custom[name] = dict(self.settings["colors"])
        self._rebuild_palette_combo(name)
        self.save_settings(quiet=True)
        self._set_status(self._tr("{name} paleti kaydedildi", name=name), ACCENT)

    def _delete_palette(self) -> None:
        name = self._selected_palette_name()
        custom = self.settings.get("custom_palettes", {})
        if name not in custom:
            return
        answer = QMessageBox.question(
            self, self._tr("Palet silinsin mi?"),
            self._tr("{name} adlı palet silinecek.", name=name),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        del custom[name]
        self.settings["colors"] = dict(PALETTES["Kuzey"])
        self._rebuild_palette_combo("Kuzey")
        self._paint_swatches()
        self._refresh_preview()
        self.save_settings(quiet=True)
        self._set_status(self._tr("{name} paleti silindi", name=name), "#6F7C90")

    def _rebuild_palette_combo(self, selected: str) -> None:
        self.palette_combo.blockSignals(True)
        self.palette_combo.clear()
        for name in PALETTES:
            self.palette_combo.addItem(self._tr(name), name)
        for name in sorted(self.settings.get("custom_palettes", {})):
            self.palette_combo.addItem(name, name)
        if selected not in PALETTES and selected not in self.settings.get("custom_palettes", {}):
            self.palette_combo.addItem(self._tr("Özel"), "Özel")
        self.palette_combo.setCurrentIndex(self.palette_combo.findData(selected))
        self.palette_combo.blockSignals(False)
        self._update_palette_buttons()

    def _reset_defaults(self) -> None:
        self.settings["colors"] = dict(PALETTES["Kuzey"])
        self._rebuild_palette_combo("Kuzey")
        self.brightness_slider.setValue(55)
        self.auto_brightness_check.setChecked(False)
        self.rotation_combo.setCurrentIndex(self.rotation_combo.findData("normal"))
        self.autostart_check.setChecked(False)
        self.msfs_layout_combo.setCurrentIndex(self.msfs_layout_combo.findData("cockpit"))
        for field, city in zip(self.weather_city_inputs, DEFAULT_WEATHER_CITIES):
            field.setText(city)
        for field in self.hardware_inputs.values():
            field.clear()
        self._paint_swatches()
        self._refresh_preview()
        self.save_settings(quiet=True)
        self._set_status(self._tr("Varsayılan ayarlara dönüldü"), ACCENT)

    def _paint_swatches(self) -> None:
        for key, button in self.swatches.items():
            color = self.settings["colors"][key]
            active = key == getattr(self, "_active_color_key", None)
            if not self._editing_enabled:
                button.setStyleSheet(
                    "background: #111419; color: #555D68; border: 1px solid #20252D; "
                    "border-radius: 9px; font-weight: 600; padding-right: 20px;"
                )
                button.setGraphicsEffect(None)
                continue
            border_color = ACCENT if active else EDGE
            border_width = 2 if active else 1
            button.setStyleSheet(
                "background: qlineargradient(x1:0, y1:0, x2:1, y2:0, "
                f"stop:0 {CONTROL_BG}, stop:0.78 {CONTROL_BG}, "
                f"stop:0.79 {color}, stop:1 {color}); "
                f"color: {TEXT}; border: {border_width}px solid {border_color}; "
                "border-radius: 9px; font-weight: 650; padding-right: 20px;"
            )
            if active:
                shadow = QGraphicsDropShadowEffect(button)
                shadow_color = QColor(ACCENT)
                shadow_color.setAlpha(145)
                shadow.setColor(shadow_color)
                shadow.setBlurRadius(18)
                shadow.setOffset(0, 2)
                button.setGraphicsEffect(shadow)
            else:
                button.setGraphicsEffect(None)
        if hasattr(self, "_active_color_key"):
            self._update_simple_color_buttons()

    def _choose_color(self, key: str) -> None:
        if key not in self.swatches:
            return
        self._active_color_key = key
        captions = {
            "cpu": "Normal", "warning": "Uyarı", "danger": "Kritik", "bg": "Zemin",
            "text": "Ana yazı", "muted": "İkincil yazı",
        }
        self.inline_color_title.setText(
            self._tr("{name} rengi", name=self._tr(captions[key]))
        )
        self.color_help_label.setText(self._tr(COLOR_IMPACT_COPY[key]))
        self.inline_color_panel.setVisible(True)
        self.inline_color_panel.setFixedWidth(205)
        self._paint_swatches()
        self._update_simple_color_buttons()

    def _simple_color_options(self) -> tuple[str, ...]:
        if getattr(self, "_active_color_key", "") == "bg":
            return SIMPLE_BACKGROUND_COLORS
        return SIMPLE_ACCENT_COLORS

    def _update_simple_color_buttons(self) -> None:
        key = getattr(self, "_active_color_key", None)
        if key not in self.swatches:
            return
        selected = self.settings["colors"][key].casefold()
        for button, color in zip(self.simple_color_buttons, self._simple_color_options()):
            is_selected = color.casefold() == selected
            border = TEXT if is_selected else "#4A5360"
            width = 3 if is_selected else 1
            red, green, blue = QColor(color).red(), QColor(color).green(), QColor(color).blue()
            check_color = "#101319" if (red * 299 + green * 587 + blue * 114) > 150000 else "#FFFFFF"
            button.setText("✓" if is_selected else "")
            button.setToolTip(color)
            button.setStyleSheet(
                f"background: {color}; border: {width}px solid {border}; "
                f"border-radius: 15px; padding: 0; color: {check_color}; font-weight: 800;"
            )
        if hasattr(self, "selected_color_chip"):
            self.selected_color_chip.setStyleSheet(
                f"background: {self.settings['colors'][key]}; border: 2px solid {TEXT}; "
                "border-radius: 6px;"
            )
            self.selected_color_chip.setToolTip(
                self._tr("Seçili renk: {colour}", colour=self.settings['colors'][key].upper())
            )

    def _apply_simple_color(self, option_index: int) -> None:
        key = getattr(self, "_active_color_key", None)
        options = self._simple_color_options()
        if key not in self.swatches or not 0 <= option_index < len(options):
            return
        self.settings["colors"][key] = options[option_index]
        if self._selected_palette_name() not in self.settings.get("custom_palettes", {}):
            if self.palette_combo.findData("Özel") < 0:
                self.palette_combo.addItem(self._tr("Özel"), "Özel")
            self.palette_combo.blockSignals(True)
            self.palette_combo.setCurrentIndex(self.palette_combo.findData("Özel"))
            self.palette_combo.blockSignals(False)
        self._paint_swatches()
        self._refresh_preview()
        self._update_palette_buttons()

    @staticmethod
    def _preview_metrics() -> dict[str, float]:
        """Stable sample values expose normal, warning and critical theme colors."""
        return {
            "cpu_load": 48.0, "cpu_temp": 63.0, "cpu_freq": 5.05,
            "gpu_load": 76.0, "gpu_temp": 81.0, "gpu_memory": 8640.0,
            "ram_load": 100.0, "ram_used": 27.4,
        }

    def _refresh_preview(self, _checked: bool | None = None) -> None:
        try:
            labels = resolve_hardware_labels({
                key: field.text() for key, field in self.hardware_inputs.items()
            })
            metrics = self._preview_metrics()
            context_labels = {
                "daily": "Günlük kullanım · medya görünümü",
                "cs2_ct": "CS2 · CT savunma ve bomba imha görünümü",
                "cs2_t": "CS2 · T hücum ve bombayı koruma görünümü",
                "deathmatch": "CS2 · ölüm maçı öldürme ve kafa vuruşu görünümü",
                "msfs": "MSFS 2024 · canlı uçuş bilgileri",
                "weather": "Boşta kullanım · seçili şehirlerin hava durumu",
                "wireless": "Kablosuz cihazlar · pil durumları",
            }
            self.preview_context_label.setText(
                self._tr(context_labels.get(self.preview_mode, "Ekran önizlemesi"))
            )
            if self.preview_mode == "msfs":
                frame = render_screen(
                    metrics, GameSnapshot(), False, self.settings["colors"],
                    hardware_labels=labels,
                    msfs_active=True,
                    flight_telemetry=FlightTelemetry(
                        altitude_ft=12540.0, airspeed_kt=146.0,
                        vertical_speed_fpm=820.0, heading_deg=278.0,
                        connected=True, updated_at=1.0,
                    ),
                    msfs_layout=str(self.msfs_layout_combo.currentData() or "cockpit"),
                    language=self.language,
                )
                self._show_preview(pil_to_qimage(frame))
                return
            if self.preview_mode in {"cs2_ct", "cs2_t"}:
                is_ct = self.preview_mode == "cs2_ct"
                frame = render_screen(
                    metrics,
                    GameSnapshot(
                        connected=True, in_match=True, money=4250,
                        next_win_money=7500, next_loss_money=6150,
                        loss_bonus=1900, team="CT" if is_ct else "T",
                        has_defuse_kit=is_ct,
                        bomb_planted=True, bomb_seconds=8.0 if is_ct else 4.0,
                    ),
                    True,
                    self.settings["colors"],
                    hardware_labels=labels, language=self.language,
                )
            elif self.preview_mode == "deathmatch":
                frame = render_screen(
                    metrics,
                    GameSnapshot(
                        connected=True, in_match=True, team="CT",
                        game_mode="deathmatch", kills=42,
                        headshot_kills=21, headshot_percent=50,
                    ),
                    True,
                    self.settings["colors"],
                    hardware_labels=labels, language=self.language,
                )
            elif self.preview_mode == "weather":
                frame = render_screen(
                    metrics, GameSnapshot(), False, self.settings["colors"],
                    hardware_labels=labels,
                    weather_readings=self._preview_weather_readings(),
                    language=self.language,
                )
            elif self.preview_mode == "wireless":
                frame = render_screen(
                    metrics, GameSnapshot(), False, self.settings["colors"],
                    hardware_labels=labels,
                    battery_devices=(
                        BatteryDevice("preview-mouse", "PRO X SUPERLIGHT 2", 85, "mouse"),
                        BatteryDevice("preview-headset", "PRO X WIRELESS", 52, "headset"),
                        BatteryDevice(
                            "preview-keyboard", self._tr("Kablosuz Klavye"), 73, "keyboard",
                        ),
                    ),
                    weather_readings=self._preview_weather_readings(),
                    language=self.language,
                )
            else:
                frame = render_screen(
                    metrics, GameSnapshot(), False, self.settings["colors"],
                    hardware_labels=labels,
                    media_snapshot=MediaSnapshot(
                        platform="Spotify", title=self._tr("Şu an çalıyor"),
                        artist=self._tr("Müzik ve ritim görünümü"), status="playing",
                    ),
                    audio_spectrum=AudioSpectrumSnapshot(
                        bars=(0.18, 0.34, 0.56, 0.82, 0.64, 0.42, 0.73, 0.91,
                              0.77, 0.48, 0.32, 0.62, 0.84, 0.58, 0.36, 0.21),
                        level=0.62, active=True,
                    ),
                    language=self.language,
                )
            self._show_preview(pil_to_qimage(frame))
        except Exception as exc:
            self._set_status(
                self._tr("Önizleme hazırlanamadı: {error}", error=exc), "#E87878"
            )

    def _install_gsi(self, _checked: bool = False, quiet: bool = False) -> bool:
        try:
            install_gsi_config()
        except Exception as exc:
            if not quiet:
                QMessageBox.warning(
                    self, self._tr("CS2 bağlantısı kurulamadı"),
                    self._tr(
                        "{error}\n\nUygulamayı yönetici olarak açıp yeniden deneyin.", error=exc,
                    ),
                )
            return False
        if not quiet:
            message = self._tr("CS2 bağlantısı kuruldu.")
            if cs2_is_running():
                message += self._tr(" Verilerin gelmesi için CS2'yi bir kez yeniden başlatın.")
            self._set_status(message, ACCENT)
        return True

    def _show_preview(self, image: QImage) -> None:
        self._preview_image = image
        self._update_preview_pixmap()

    def _update_preview_pixmap(self) -> None:
        if self._preview_image is None or not hasattr(self, "preview_label"):
            return
        pixmap = QPixmap.fromImage(self._preview_image)
        if self._rotation() == "180":
            pixmap = pixmap.transformed(QTransform().rotate(180), Qt.TransformationMode.SmoothTransformation)
        self.preview_label.setPixmap(pixmap.scaled(
            WIDTH, HEIGHT, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "preview_label"):
            QTimer.singleShot(0, self._update_preview_pixmap)

    def save_settings(self, _checked: bool = False, quiet: bool = False) -> bool:
        self.settings = self._snapshot_settings()
        try:
            save_settings_file(self.settings)
            set_windows_autostart(self.settings["autostart"])
        except Exception as exc:
            if quiet:
                self._set_status(
                    self._tr(
                        "Ayarlar kaydedilemedi; ekran yine de başlatılıyor · {error}", error=exc,
                    ),
                    "#E3B866",
                )
            else:
                QMessageBox.critical(self, self._tr("Ayarlar kaydedilemedi"), str(exc))
            return False
        if not quiet:
            self._set_status(
                self._tr("Ayarlar kaydedildi ve çalışan ekrana uygulanacak"), ACCENT
            )
        return True

    def start_screen(self) -> None:
        _runtime_log("start_screen requested")
        if self.worker and self.worker.is_alive():
            _runtime_log("start_screen ignored; worker already alive")
            self._set_status(self._tr("Ekran zaten çalışıyor"), ACCENT)
            return
        try:
            self.save_settings(quiet=True)
            self._install_gsi(quiet=True)
            self.stop_event.clear()
            self.screen_connected.clear()
            self._screen_progress_at = time.monotonic()
            self._watchdog_restart_started = False
            self.worker = threading.Thread(target=self._screen_worker, name="screen-worker", daemon=True)
            self.worker.start()
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(True)
            if self.tray_icon is not None:
                self.tray_start_action.setEnabled(False)
                self.tray_stop_action.setEnabled(True)
            self._set_editing_enabled(False)
            self._set_status(
                self._tr("Ekran başlatılıyor · ayarları değiştirmek için Durdur"),
                "#E3B866",
            )
            _runtime_log("screen worker started")
        except Exception as exc:
            _runtime_log(f"start_screen failed: {type(exc).__name__}: {exc}")
            if self.tray_icon is not None:
                self.tray_start_action.setEnabled(True)
                self.tray_stop_action.setEnabled(False)
            self._set_status(
                self._tr("Ekran başlatılamadı · {error}", error=exc), "#E87878"
            )

    def stop_screen(self) -> None:
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            self._set_status(self._tr("Ekran durduruluyor…"), "#E3B866")
        else:
            self._screen_stopped()

    def _screen_worker(self) -> None:
        _runtime_log("screen worker entered")
        screen = None
        game_state = CS2GameState()
        gsi_server = None
        media_monitor = MediaSessionMonitor()
        audio_monitor = AudioSpectrumMonitor()
        battery_monitor = BatteryDeviceMonitor(interval=2.0, limit=3)
        msfs_monitor = MSFSTelemetryMonitor()
        weather_monitor = WeatherMonitor(self.settings.get("weather_cities"))
        applied_display_settings = None
        last_view_mode: str | None = None
        last_match_signature = None
        last_economy_signature = None
        last_bomb_tick: int | None = None
        last_media_signature = None
        last_battery_signature = None
        last_battery_layout_signature = None
        last_weather_signature = None
        sent_flight_signature = None
        last_full_gauge_refresh = 0.0
        last_fast_gauge_refresh = 0.0
        last_msfs_telemetry_refresh = 0.0
        last_context_refresh = 0.0
        last_metrics_refresh = 0.0
        last_preview_refresh = 0.0
        last_status_message = ""
        last_audio_error = ""
        last_media_error = ""
        last_battery_error = ""
        last_msfs_error = ""
        last_weather_error = ""
        audio_ready_logged = False
        media_perf_started = 0.0
        media_perf_last_frame = 0.0
        media_perf_frames = 0
        media_perf_hardware_frames = 0
        media_perf_max_gap = 0.0
        media_perf_logged = False
        last_sensor_watchdog = 0.0
        cached_metrics = None
        game_active = False
        msfs_active = False
        msfs_elapsed: float | None = None
        faceit_active = False
        panel_active = False
        snapshot = GameSnapshot()
        matchmaking_pings = []
        matchmaking_source = "VALVE"
        match_signature = None
        bomb_tick = -1
        sent_frame = None
        media_base_frame = None
        media_render_future: Future | None = None
        media_render_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="media-base-render")

        def build_media_base(
            render_snapshot, render_panel_active, render_colors, render_pings,
            render_source, render_labels, render_media, render_audio, render_batteries,
            render_language,
        ):
            metrics = get_metrics()
            frame = render_screen(
                metrics, render_snapshot, render_panel_active, render_colors,
                render_pings, render_source, render_labels,
                animation_time=time.monotonic(), media_snapshot=render_media,
                audio_spectrum=render_audio, battery_devices=render_batteries,
                language=render_language,
            )
            return metrics, frame
        try:
            try:
                gsi_server = GSIServer(game_state)
                gsi_server.start()
            except Exception:
                # The screen and FACEIT ping panel can keep working even when
                # CS2's local GSI listener is temporarily unavailable.
                gsi_server = None
            media_monitor.start()
            audio_monitor.start()
            battery_monitor.start()
            msfs_monitor.start()
            weather_monitor.start()
            media_gauge_index = 0
            media_ring_index = 0
            last_media_gauge_slice = 0.0
            last_media_ring_refresh = 0.0
            while not self.stop_event.is_set():
                # This heartbeat advances even while the display is unplugged
                # and reconnect attempts fail. If a native USB call blocks,
                # the UI watchdog can distinguish that hang from a normal
                # retry loop and replace the process automatically.
                self._screen_progress_at = time.monotonic()
                current = dict(self.settings)
                if screen is None:
                    try:
                        brightness = effective_brightness(current)
                        screen = ScreenSession(current["port"], brightness, current["rotation"])
                        _runtime_log(f"display connected; port={current['port']}; rotation={current['rotation']}")
                        applied_display_settings = (brightness, current["rotation"])
                        last_view_mode = None
                        last_match_signature = None
                        last_bomb_tick = None
                        last_media_signature = None
                        last_battery_signature = None
                        last_battery_layout_signature = None
                        last_weather_signature = None
                        sent_flight_signature = None
                        last_full_gauge_refresh = 0.0
                        last_fast_gauge_refresh = 0.0
                        sent_frame = None
                        self.signals.status.emit(translate(
                            "Mini ekran bağlandı · görüntü yenileniyor",
                            current.get("language", "tr"),
                        ))
                    except Exception as exc:
                        self.screen_connected.clear()
                        _runtime_log(f"display connect failed: {type(exc).__name__}: {exc}")
                        self.signals.status.emit(translate(
                            "Mini ekran bekleniyor · {error}",
                            current.get("language", "tr"), error=exc,
                        ))
                        self.stop_event.wait(2.0)
                        continue
                cycle_started = time.monotonic()
                try:
                    if (
                        getattr(sys, "frozen", False)
                        and cycle_started - last_sensor_watchdog >= 5.0
                    ):
                        nudge_sensor_task_if_stale()
                        last_sensor_watchdog = cycle_started
                    display_settings = (effective_brightness(current), current["rotation"])
                    if display_settings != applied_display_settings:
                        screen.configure(*display_settings)
                        applied_display_settings = display_settings
                    context_refresh_interval = (
                        BOMB_WORKER_INTERVAL if snapshot.bomb_planted
                        else LIVE_GAME_WORKER_INTERVAL if snapshot.in_match
                        else 0.45
                    )
                    if (
                        cycle_started - last_context_refresh >= context_refresh_interval
                        or last_context_refresh == 0.0
                    ):
                        game_active = cs2_is_running()
                        msfs_elapsed = msfs_session_seconds()
                        msfs_active = msfs_elapsed is not None
                        faceit_active = faceit_ac_is_running()
                        snapshot = (
                            game_state.snapshot(force_competitive_economy=faceit_active)
                            if game_active else GameSnapshot()
                        )
                        panel_active = game_active or faceit_active
                        if not snapshot.in_match and faceit_active:
                            matchmaking_pings, matchmaking_source = FACEIT_PING_MONITOR.read(), "FACEIT"
                        elif game_active and not snapshot.in_match:
                            matchmaking_pings, matchmaking_source = STEAM_PING_MONITOR.read(), "VALVE"
                        else:
                            matchmaking_pings = []
                            matchmaking_source = "FACEIT" if faceit_active else "VALVE"
                        match_signature = (
                            snapshot.connected, snapshot.in_match, snapshot.money,
                            snapshot.next_win_money, snapshot.next_loss_money,
                            snapshot.team, snapshot.has_defuse_kit,
                            snapshot.bomb_planted, snapshot.bomb_defusing,
                            snapshot.game_mode,
                            snapshot.kills, snapshot.headshot_kills,
                            snapshot.headshot_percent,
                            matchmaking_source,
                            tuple((item.code, int(round(item.latency_ms))) for item in matchmaking_pings),
                        )
                        economy_signature = (
                            snapshot.in_match, snapshot.game_mode, snapshot.team,
                            snapshot.money, snapshot.next_win_money,
                            snapshot.next_loss_money, snapshot.loss_bonus,
                        )
                        if not snapshot.in_match:
                            last_economy_signature = None
                        if snapshot.in_match and economy_signature != last_economy_signature:
                            _runtime_log(
                                "economy updated; "
                                f"mode={snapshot.game_mode or '-'}; team={snapshot.team or '-'}; "
                                f"money={snapshot.money}; win={snapshot.next_win_money}; "
                                f"loss={snapshot.next_loss_money}; bonus={snapshot.loss_bonus}"
                            )
                            last_economy_signature = economy_signature
                        bomb_tick = (
                            int(max(0.0, snapshot.bomb_seconds or 0.0) * 5)
                            if snapshot.bomb_planted else -1
                        )
                        if (
                            panel_active and not snapshot.in_match
                            and match_signature != last_match_signature
                        ):
                            ping_summary = ", ".join(
                                f"{item.name}:{int(round(item.latency_ms))}ms"
                                for item in matchmaking_pings
                            ) or "hazırlanıyor"
                            _runtime_log(
                                "matchmaking pings updated; "
                                f"source={matchmaking_source}; values={ping_summary}"
                            )
                        last_context_refresh = cycle_started

                    media_snapshot = media_monitor.snapshot()
                    audio_spectrum = audio_monitor.snapshot()
                    battery_snapshot = battery_monitor.snapshot()
                    flight_telemetry = msfs_monitor.snapshot()
                    weather_snapshot = weather_monitor.snapshot()
                    flight_signature = flight_telemetry.signature
                    media_visual_region = (
                        MEDIA_VIS_REGION_WITH_BATTERY
                        if battery_snapshot.devices else MEDIA_VIS_REGION
                    )
                    if audio_spectrum.updated_at and not audio_ready_logged and not audio_spectrum.error:
                        _runtime_log(
                            f"audio loopback ready; input_latency_ms={audio_spectrum.input_latency_ms:.1f}"
                        )
                        audio_ready_logged = True
                    if audio_spectrum.error and audio_spectrum.error != last_audio_error:
                        _runtime_log(f"audio loopback unavailable: {audio_spectrum.error}")
                    if media_snapshot.error and media_snapshot.error != last_media_error:
                        _runtime_log(f"media session unavailable: {media_snapshot.error}")
                    if battery_snapshot.error and battery_snapshot.error != last_battery_error:
                        _runtime_log(f"battery discovery unavailable: {battery_snapshot.error}")
                    if flight_telemetry.error and flight_telemetry.error != last_msfs_error:
                        _runtime_log(f"MSFS telemetry unavailable: {flight_telemetry.error}")
                    if weather_snapshot.error and weather_snapshot.error != last_weather_error:
                        _runtime_log(f"weather unavailable: {weather_snapshot.error}")
                    if media_snapshot.visible and media_snapshot.signature != last_media_signature:
                        _runtime_log(
                            "media session active; "
                            f"platform={media_snapshot.platform}; "
                            f"status={media_snapshot.status}; title={media_snapshot.title[:80]}"
                        )
                    last_audio_error = audio_spectrum.error
                    last_media_error = media_snapshot.error
                    last_battery_error = battery_snapshot.error
                    last_msfs_error = flight_telemetry.error
                    last_weather_error = weather_snapshot.error
                    media_mode = (
                        not panel_active and not msfs_active
                        and (media_snapshot.visible or audio_spectrum.active)
                    )
                    view_mode = (
                        "game" if panel_active else "msfs" if msfs_active
                        else "media" if media_mode else "idle"
                    )
                    if view_mode != last_view_mode:
                        _runtime_log(
                            "view mode changed; "
                            f"mode={view_mode}; game_active={game_active}; "
                            f"gsi_connected={snapshot.connected}; in_match={snapshot.in_match}; "
                            f"team={snapshot.team or '-'}; game_mode={snapshot.game_mode or '-'}; "
                            f"bomb={snapshot.bomb_planted}; "
                            f"ping_source={matchmaking_source}; ping_count={len(matchmaking_pings)}"
                        )
                    media_signature = media_snapshot.signature if media_snapshot.visible else ("Sistem sesi", "", "", "playing")
                    battery_signature = battery_snapshot.signature
                    weather_signature = weather_snapshot.signature
                    battery_layout_signature = tuple(
                        (device.stable_id, device.name, device.kind)
                        for device in battery_snapshot.devices
                    )
                    if (
                        last_battery_signature is not None
                        and battery_signature != last_battery_signature
                    ):
                        device_summary = ", ".join(
                            f"{device.name}:{device.percent}%"
                            for device in battery_snapshot.devices
                        ) or "none"
                        _runtime_log(f"battery devices changed; visible={device_summary}")

                    labels = resolve_hardware_labels(current.get("hardware_labels"))
                    continuing_media = view_mode == "media" and last_view_mode == "media"
                    metadata_changed = (
                        continuing_media and (
                            media_signature != last_media_signature
                            or battery_signature != last_battery_signature
                        )
                    )
                    if continuing_media and not metadata_changed:
                        if media_render_future is not None and media_render_future.done():
                            try:
                                cached_metrics, media_base_frame = media_render_future.result()
                            except Exception as exc:
                                _runtime_log(
                                    f"background media render failed: {type(exc).__name__}: {exc}"
                                )
                            media_render_future = None
                            last_metrics_refresh = cycle_started
                        if media_base_frame is None:
                            cached_metrics = get_metrics()
                            media_base_frame = render_screen(
                                cached_metrics, snapshot, panel_active, current["colors"],
                                matchmaking_pings, matchmaking_source, labels,
                                animation_time=cycle_started, media_snapshot=media_snapshot,
                                audio_spectrum=audio_spectrum,
                                battery_devices=battery_snapshot.devices,
                                language=current.get("language", "tr"),
                            )
                            last_metrics_refresh = cycle_started
                        if (
                            media_render_future is None
                            and cycle_started - last_metrics_refresh >= 0.5
                        ):
                            media_render_future = media_render_pool.submit(
                                build_media_base,
                                snapshot, panel_active, dict(current["colors"]),
                                list(matchmaking_pings), matchmaking_source, dict(labels),
                                media_snapshot, audio_spectrum, battery_snapshot.devices,
                                current.get("language", "tr"),
                            )
                        frame = media_base_frame
                    else:
                        if metadata_changed and media_render_future is not None:
                            media_render_future.cancel()
                            media_render_future = None
                        if cached_metrics is None or cycle_started - last_metrics_refresh >= 0.5:
                            cached_metrics = get_metrics()
                            last_metrics_refresh = cycle_started
                        frame = render_screen(
                            cached_metrics, snapshot, panel_active, current["colors"],
                            matchmaking_pings, matchmaking_source, labels,
                            animation_time=cycle_started,
                            media_snapshot=media_snapshot,
                            audio_spectrum=audio_spectrum,
                            battery_devices=battery_snapshot.devices,
                            msfs_active=msfs_active,
                            flight_telemetry=flight_telemetry,
                            msfs_layout=current.get("msfs_layout", "cockpit"),
                            weather_readings=weather_snapshot.readings,
                            language=current.get("language", "tr"),
                        )
                        if view_mode == "media":
                            media_base_frame = frame
                    media_patch = (
                        render_media_visual_patch(
                            audio_spectrum, current["colors"],
                            with_battery=bool(battery_snapshot.devices),
                        )
                        if view_mode == "media" else None
                    )
                    if last_view_mode is None:
                        screen.show(frame)
                        self._screen_progress_at = time.monotonic()
                        sent_frame = frame.copy()
                        self.screen_connected.set()
                        last_full_gauge_refresh = time.monotonic()
                        last_fast_gauge_refresh = last_full_gauge_refresh
                        last_msfs_telemetry_refresh = last_full_gauge_refresh - 0.25
                        sent_flight_signature = flight_signature
                        _runtime_log("first full frame sent")
                    else:
                        now = time.monotonic()
                        regions = []
                        media_gauge_cycle = False
                        if view_mode == "media":
                            if now - last_fast_gauge_refresh >= 0.5:
                                # The metric source refreshes every 0.5 s.
                                # Send all changed CPU/GPU/RAM value tiles in
                                # that same frame instead of stealing three
                                # consecutive spectrum frames.
                                last_fast_gauge_refresh = now
                                changed_value_regions = [
                                    region for region in FAST_VALUE_BOXES
                                    if _region_has_changed(frame, sent_frame, region)
                                ]
                                if changed_value_regions:
                                    regions.extend(changed_value_regions)
                                    media_gauge_cycle = True
                            elif now - last_media_ring_refresh >= 0.30:
                                ring_regions = MEDIA_RING_REGION_GROUPS[media_ring_index]
                                media_ring_index = (
                                    media_ring_index + 1
                                ) % len(MEDIA_RING_REGION_GROUPS)
                                last_media_ring_refresh = now
                                changed_ring_regions = [
                                    region for region in ring_regions
                                    if _region_has_changed(frame, sent_frame, region)
                                ]
                                if changed_ring_regions:
                                    regions.extend(changed_ring_regions)
                                    media_gauge_cycle = True
                            elif now - last_media_gauge_slice >= 0.20:
                                gauge_region = MEDIA_GAUGE_REGIONS[media_gauge_index]
                                media_gauge_index = (media_gauge_index + 1) % len(MEDIA_GAUGE_REGIONS)
                                last_media_gauge_slice = now
                                if _region_has_changed(frame, sent_frame, gauge_region):
                                    regions.append(gauge_region)
                                    media_gauge_cycle = True
                        elif now - last_fast_gauge_refresh >= 0.5:
                            # Rev-A LCDs visibly scan a full 480x160 bitmap.
                            # Logical tiles are tightened to their exact dirty
                            # pixels immediately before the serial transfer.
                            regions.extend(TOP_DIRTY_TILES)
                            last_fast_gauge_refresh = now
                            last_full_gauge_refresh = now

                        bottom_changed = view_mode != last_view_mode
                        if view_mode == "game" and match_signature != last_match_signature:
                            bottom_changed = True
                        if view_mode == "media" and media_signature != last_media_signature:
                            bottom_changed = True
                        msfs_telemetry_refresh_due = (
                            view_mode == "msfs"
                            and flight_signature != sent_flight_signature
                            and now - last_msfs_telemetry_refresh
                            >= MSFS_TELEMETRY_REFRESH_INTERVAL
                        )
                        battery_changed = battery_signature != last_battery_signature
                        weather_changed = weather_signature != last_weather_signature
                        battery_layout_changed = (
                            battery_changed
                            and battery_layout_signature != last_battery_layout_signature
                        )
                        if battery_layout_changed:
                            bottom_changed = True
                        if view_mode == "idle" and weather_changed:
                            bottom_changed = True
                        bomb_timer_changed = (
                            view_mode == "game" and snapshot.bomb_planted
                            and bomb_tick != last_bomb_tick
                        )
                        if bottom_changed:
                            regions.clear()
                            regions.append(
                                MSFS_PANEL_REGION_WITH_BATTERY
                                if view_mode == "msfs" and battery_snapshot.devices
                                and not battery_layout_changed
                                else BOTTOM_REGION
                            )
                            if view_mode == "msfs":
                                last_msfs_telemetry_refresh = now
                                sent_flight_signature = flight_signature
                            media_gauge_cycle = True
                        elif bomb_timer_changed:
                            regions.extend((BOMB_TIMER_REGION, BOMB_BAR_REGION))
                        elif battery_changed:
                            regions.clear()
                            regions.append(BATTERY_PANEL_REGION)
                            media_gauge_cycle = True
                        elif msfs_telemetry_refresh_due:
                            regions.extend(
                                MSFS_LIVE_REGIONS_WITH_BATTERY
                                if battery_snapshot.devices
                                else MSFS_LIVE_REGIONS
                            )
                            last_msfs_telemetry_refresh = now
                            sent_flight_signature = flight_signature
                        elif view_mode == "media":
                            # Never trade a sound frame for a sensor update.
                            # Small gauge regions may share this transfer, but
                            # the spectrum itself is present on every cycle.
                            regions.insert(0, media_visual_region)

                        if regions and battery_layout_changed:
                            # A device being added or removed moves both the
                            # spectrum and battery rows. Use one clean full
                            # frame so no stale row can survive a partial LCD
                            # update, especially in 180-degree orientation.
                            screen.show(frame)
                            sent_frame = frame.copy()
                        elif regions:
                            transfer_frame = sent_frame.copy() if sent_frame is not None else frame.copy()
                            for region in regions:
                                if region == FAST_GAUGE_REGION and sent_frame is not None:
                                    for box in FAST_VALUE_BOXES:
                                        transfer_frame.paste(frame.crop(box), box)
                                elif region == media_visual_region and media_patch is not None:
                                    transfer_frame.paste(media_patch, (region[0], region[1]))
                                else:
                                    transfer_frame.paste(frame.crop(region), region)
                            requested_regions = list(regions)
                            send_regions = _tight_changed_regions(
                                transfer_frame, sent_frame, requested_regions,
                            )
                            if send_regions:
                                screen.show_regions(transfer_frame, send_regions)
                            if (
                                send_regions
                                and view_mode == "media"
                                and media_visual_region in regions
                                and not media_perf_logged
                            ):
                                completed_at = time.monotonic()
                                if media_perf_started == 0.0:
                                    media_perf_started = completed_at
                                if media_perf_last_frame:
                                    media_perf_max_gap = max(
                                        media_perf_max_gap,
                                        completed_at - media_perf_last_frame,
                                    )
                                media_perf_last_frame = completed_at
                                media_perf_frames += 1
                                if any(
                                    region in FAST_VALUE_BOXES
                                    or region in MEDIA_GAUGE_REGIONS
                                    or any(
                                        region in group
                                        for group in MEDIA_RING_REGION_GROUPS
                                    )
                                    for region in requested_regions
                                ):
                                    media_perf_hardware_frames += 1
                                sample_seconds = completed_at - media_perf_started
                                if sample_seconds >= 10.0:
                                    measured_fps = (
                                        (media_perf_frames - 1) / sample_seconds
                                        if media_perf_frames > 1 else 0.0
                                    )
                                    _runtime_log(
                                        "media sync measured; "
                                        f"fps={measured_fps:.1f}; "
                                        f"max_gap_ms={media_perf_max_gap * 1000.0:.0f}; "
                                        f"audio_frames={media_perf_frames}; "
                                        f"hardware_coframes={media_perf_hardware_frames}"
                                    )
                                    media_perf_logged = True
                            if sent_frame is None:
                                sent_frame = transfer_frame.copy()
                            else:
                                for region in send_regions:
                                    sent_frame.paste(transfer_frame.crop(region), region)
                except Exception as exc:
                    self.screen_connected.clear()
                    _runtime_log(f"display refresh failed: {type(exc).__name__}: {exc}")
                    self.signals.status.emit(translate(
                        "Bağlantı yenileniyor · {error}",
                        current.get("language", "tr"), error=exc,
                    ))
                    try:
                        screen.close()
                    except Exception:
                        pass
                    screen = None
                    applied_display_settings = None
                    last_view_mode = None
                    sent_frame = None
                    self.stop_event.wait(1.5)
                    continue
                last_view_mode = view_mode
                self._screen_progress_at = time.monotonic()
                last_match_signature = match_signature
                if bomb_tick != last_bomb_tick:
                    if bomb_tick >= 0 and (last_bomb_tick is None or last_bomb_tick < 0):
                        _runtime_log(
                            f"bomb timer started; seconds={bomb_tick / 5:.1f}; team={snapshot.team}"
                        )
                    elif bomb_tick < 0 and last_bomb_tick is not None and last_bomb_tick >= 0:
                        _runtime_log("bomb timer ended")
                    elif last_bomb_tick is not None:
                        for checkpoint in (30, 20, 10, 4):
                            threshold = checkpoint * 5
                            if last_bomb_tick > threshold >= bomb_tick:
                                _runtime_log(f"bomb timer checkpoint; seconds={checkpoint}")
                                break
                last_bomb_tick = bomb_tick
                last_media_signature = media_signature
                last_battery_signature = battery_signature
                last_battery_layout_signature = battery_layout_signature
                last_weather_signature = weather_signature
                now = time.monotonic()
                if now - last_preview_refresh >= 0.25:
                    preview_frame = frame
                    if media_patch is not None:
                        preview_frame = frame.copy()
                        preview_frame.paste(
                            media_patch,
                            (media_visual_region[0], media_visual_region[1]),
                        )
                    self.signals.preview.emit(pil_to_qimage(preview_frame))
                    last_preview_refresh = now
                if snapshot.in_match:
                    if "deathmatch" in snapshot.game_mode.lower():
                        status_message = "Ölüm maçı · öldürme ve HS verisi canlı"
                    elif snapshot.next_win_money is not None:
                        status_message = "CS2 açık · C4 ve ekonomi verisi canlı"
                    else:
                        status_message = "CS2 maçı açık · canlı veriler izleniyor"
                elif faceit_active:
                    status_message = "FACEIT AC açık · FACEIT pingleri yenileniyor"
                elif game_active:
                    status_message = "CS2 açık · eşleştirme pingleri yenileniyor"
                elif msfs_active:
                    status_message = (
                        "MSFS 2024 açık · uçuş verileri canlı"
                        if flight_telemetry.updated_at
                        else "MSFS 2024 açık · kokpit bağlantısı bekleniyor"
                    )
                elif media_mode:
                    label = media_snapshot.platform or "Sistem sesi"
                    status_message = "{label} · ritim görselleştiriliyor"
                else:
                    status_message = (
                        "Ekran çalışıyor · hava durumu güncel"
                        if weather_snapshot.readings
                        else "Ekran çalışıyor"
                    )
                translated_status = translate(
                    status_message, current.get("language", "tr"),
                    label=label if media_mode else "",
                )
                if translated_status != last_status_message:
                    self.signals.status.emit(translated_status)
                    last_status_message = translated_status
                elapsed = time.monotonic() - cycle_started
                target_interval = (
                    BOMB_WORKER_INTERVAL if snapshot.bomb_planted
                    else LIVE_GAME_WORKER_INTERVAL if snapshot.in_match
                    else 0.045 if media_mode else MSFS_WORKER_INTERVAL if msfs_active
                    else float(current["interval"])
                )
                self.stop_event.wait(max(0.01, target_interval - elapsed))
        except Exception as exc:
            _runtime_log(f"screen worker failed: {type(exc).__name__}: {exc}")
            self.signals.error.emit(str(exc))
        finally:
            self.screen_connected.clear()
            if screen is not None:
                try:
                    # ScreenOff is a tiny command and takes effect much faster
                    # than transferring a complete black frame during logout.
                    screen.power_off()
                    _runtime_log("display powered off before stop")
                except Exception as exc:
                    _runtime_log(
                        f"display could not be powered off: {type(exc).__name__}: {exc}"
                    )
                try:
                    screen.show(Image.new("RGB", (WIDTH, HEIGHT), "#000000"))
                    _runtime_log("blank frame sent before display stop")
                except Exception as exc:
                    _runtime_log(
                        f"blank frame could not be sent: {type(exc).__name__}: {exc}"
                    )
                try:
                    screen.close()
                except Exception:
                    pass
            if gsi_server is not None:
                try:
                    gsi_server.close()
                except Exception:
                    pass
            media_monitor.stop()
            audio_monitor.stop()
            battery_monitor.stop()
            msfs_monitor.stop()
            weather_monitor.stop()
            media_render_pool.shutdown(wait=False, cancel_futures=True)
            self.signals.stopped.emit()

    def _worker_status(self, message: str) -> None:
        self._set_status(f"{message} · {self._tr('ayarlar kilitli')}", ACCENT)

    def _worker_error(self, message: str) -> None:
        self._set_status(
            self._tr("Bağlantı kurulamadı: {error}", error=message), "#E87878"
        )

    def _screen_stopped(self) -> None:
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        if self.tray_icon is not None:
            self.tray_start_action.setEnabled(True)
            self.tray_stop_action.setEnabled(False)
        self._set_editing_enabled(True)
        connection_prefixes = ("Bağlantı", "Connection")
        if not self.status_label.text().startswith(connection_prefixes):
            self._set_status(self._tr("Ekran durduruldu"), "#6F7C90")
        if self._exit_requested:
            app = QApplication.instance()
            if app is not None:
                app.quit()
        elif not self.stop_event.is_set() and not self._smoke_test:
            _runtime_log("screen worker stopped unexpectedly; scheduling automatic restart")
            QTimer.singleShot(1000, self.start_screen)

    def _set_editing_enabled(self, enabled: bool) -> None:
        self._editing_enabled = enabled
        if not enabled and hasattr(self, "inline_color_panel"):
            self.inline_color_panel.setVisible(False)
            self.inline_color_panel.setFixedWidth(0)
            self._active_color_key = None
        for control in self.edit_controls:
            control.setEnabled(enabled)
        self._sync_brightness_controls()
        if hasattr(self, "quick_card_effect"):
            self.quick_card_effect.setOpacity(1.0 if enabled else 0.44)
        self._paint_swatches()
        if enabled:
            self._update_palette_buttons()

    def _sync_brightness_controls(self, *_args) -> None:
        if not hasattr(self, "brightness_slider"):
            return
        automatic = bool(
            hasattr(self, "auto_brightness_check")
            and self.auto_brightness_check.isChecked()
        )
        editable = getattr(self, "_editing_enabled", True)
        self.brightness_slider.setEnabled(editable and not automatic)
        if automatic:
            self.brightness_value.setText(f"%{automatic_brightness()}")
        else:
            self.brightness_value.setText(f"%{self.brightness_slider.value()}")

    def _set_status(self, message: str, color: str) -> None:
        self.status_label.setText(message)
        self.status_dot.setStyleSheet(f"color: {color}; background: transparent;")

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.tray_icon is not None and not self._exit_requested:
            event.ignore()
            self.hide()
            if not self._tray_notice_shown:
                self.tray_icon.showMessage(
                    self._tr(APP_DISPLAY_NAME),
                    self._tr("Uygulama sistem tepsisinde çalışmaya devam ediyor."),
                    QSystemTrayIcon.MessageIcon.Information,
                    2500,
                )
                self._tray_notice_shown = True
            return
        self.stop_event.set()
        event.accept()


def present_initial_window(panel: ControlPanel, autorun: bool) -> None:
    """Present manual launches normally and keep startup launches in the tray."""
    if autorun:
        if panel.tray_icon is not None:
            panel.hide()
        else:
            # Explorer's tray may not exist yet during early sign-in. Keep a
            # recoverable minimized window until the tray retry succeeds.
            panel.showMinimized()
        return
    panel.showNormal()
    panel.raise_()
    panel.activateWindow()


def _wait_for_recovery_parent(pid: int, timeout_seconds: float = 10.0) -> None:
    """Wait for a stalled parent to release the display and instance mutex."""
    if pid <= 0 or pid == os.getpid():
        return
    try:
        process = psutil.Process(pid)
        try:
            process.wait(timeout=timeout_seconds)
        except psutil.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except psutil.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
        pass


def main() -> int:
    # Windows Run-key applications inherit System32 as their working folder.
    # Set a writable, stable folder before any screen driver is imported/used.
    prepare_runtime_working_directory()
    if "--install-sensor-task" in sys.argv:
        return install_sensor_task()
    if "--sensor-bridge" in sys.argv:
        return run_sensor_bridge()
    if "--sensor-file-bridge" in sys.argv:
        return run_sensor_bridge(SENSOR_CACHE_PATH)
    if "--steam-ping-bridge" in sys.argv:
        return run_steam_ping_bridge()
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--autorun", action="store_true")
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--language", choices=tuple(SUPPORTED_LANGUAGES))
    parser.add_argument("--recover-pid", type=int, default=0)
    args, _unknown = parser.parse_known_args()
    if args.recover_pid:
        _wait_for_recovery_parent(args.recover_pid)
    if not args.smoke_test and args.capture is None and not _acquire_single_instance(
        activate_existing=not args.autorun,
    ):
        return 0
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_DISPLAY_NAME)
    app.setOrganizationName("Sabasakal")
    panel = ControlPanel(autorun=args.autorun, smoke_test=args.smoke_test)
    if args.language:
        panel.language_combo.blockSignals(True)
        panel.language = args.language
        panel.settings["language"] = args.language
        panel.language_combo.setCurrentIndex(panel.language_combo.findData(args.language))
        panel.language_combo.blockSignals(False)
        panel._apply_language()
    if not args.smoke_test and args.capture is None:
        # A portable EXE can be moved after the option was saved. Repair the
        # Run entry only in a real application launch, never in UI tests.
        reconcile_windows_autostart(bool(panel.settings.get("autostart")))
    if panel.tray_icon is not None:
        app.setQuitOnLastWindowClosed(False)
    if hasattr(app, "commitDataRequest"):
        app.commitDataRequest.connect(lambda _manager: panel.prepare_system_shutdown())
    app.aboutToQuit.connect(panel.prepare_system_shutdown)
    if not args.smoke_test and args.capture is None:
        QTimer.singleShot(350, ensure_sensor_task)
    start_automatically = args.autorun or _RECOVERED_STALE_INSTANCE
    if start_automatically:
        present_initial_window(panel, autorun=args.autorun)
        panel.start_screen()

        def report_autorun_result(retries: int = 10) -> None:
            if panel.screen_connected.is_set():
                return
            if panel.worker and panel.worker.is_alive() and retries > 0:
                QTimer.singleShot(500, lambda: report_autorun_result(retries - 1))
                return
            if panel.tray_icon is not None:
                panel.tray_icon.showMessage(
                    panel._tr(APP_DISPLAY_NAME),
                    panel._tr(
                        "Mini ekran bağlantısı bekleniyor. Ayrıntılar için simgeye tıklayın."
                    ),
                    QSystemTrayIcon.MessageIcon.Warning,
                    4000,
                )

        QTimer.singleShot(1100, report_autorun_result)
    else:
        present_initial_window(panel, autorun=False)
    if args.capture:
        def capture_and_close() -> None:
            args.capture.parent.mkdir(parents=True, exist_ok=True)
            panel.grab().save(str(args.capture))
            panel._exit_application()
        QTimer.singleShot(700, capture_and_close)
    try:
        return app.exec()
    finally:
        _cleanup_instance_state()


if __name__ == "__main__":
    raise SystemExit(main())
