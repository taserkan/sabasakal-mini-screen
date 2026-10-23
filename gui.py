# SPDX-License-Identifier: GPL-3.0-or-later
"""Friendly Qt control panel for the 3.5-inch system information screen."""

from __future__ import annotations

import argparse
from concurrent.futures import Future, ThreadPoolExecutor
import ctypes
import ctypes.wintypes
import dataclasses
import datetime as dt
import hashlib
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
from PySide6.QtCore import QByteArray, QObject, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAction, QColor, QCloseEvent, QDesktopServices, QIcon, QImage, QPainter, QPixmap, QTransform,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QFrame, QGridLayout,
    QGraphicsDropShadowEffect, QGraphicsOpacityEffect, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton,
    QMenu, QRadioButton, QScrollArea, QSizePolicy, QSlider, QSystemTrayIcon, QVBoxLayout, QWidget,
)

from app import (
    COLORS,
    HEIGHT,
    MAX_HARDWARE_LABEL_LENGTH, MEDIA_VIS_REGION, MEDIA_VIS_REGION_WITH_BATTERY,
    NVIDIA_MONITOR, SENSOR_CACHE_PATH, SENSOR_MONITOR, ScreenSession,
    cs2_is_running,
    get_auto_hardware_labels, get_metrics,
    normalise_hardware_label,
    msfs_session_seconds, render_media_visual_patch, render_screen,
    resolve_hardware_labels,
    WIDTH,
)
from battery_runtime import BatteryDevice, BatteryDeviceMonitor
from media_runtime import (
    AudioSpectrumMonitor, AudioSpectrumSnapshot, MediaSessionMonitor, MediaSnapshot,
)
from msfs_runtime import (
    FlightPhaseState, FlightPhaseTracker, FlightTelemetry, LandingRecord, MSFSTelemetryMonitor,
)
from screens_new import (
    NewScreenContext, ScreenStabilizer, available_pages, new_design_mode, render_new_screen,
)
from weather_runtime import (
    DEFAULT_WEATHER_CITIES, WeatherMonitor, WeatherReading, format_city_name,
    normalise_city_names,
)
from i18n import SUPPORTED_LANGUAGES, normalize_language, translate
from energy_runtime import (
    DEFAULT_OTHER_WATTS, DEFAULT_PSU_EFFICIENCY, EnergyMeter, EnergySummary, price_per_kwh,
)
from faceit_runtime import FaceitMatch, FaceitMonitor, FaceitSnapshot, protect_secret
from update_runtime import (
    CHECK_INTERVAL_SECONDS as UPDATE_CHECK_INTERVAL_SECONDS, UpdateInfo, fetch_latest_release,
    is_trusted_release_url,
)
from zowie_runtime import (
    DEFAULT_DESKTOP_MODE as ZOWIE_DEFAULT_DESKTOP_MODE,
    DEFAULT_GAME_MODE as ZOWIE_DEFAULT_GAME_MODE,
    ZOWIE_MODE_LABELS, ZowieModeMonitor, normalise_mode as normalise_zowie_mode,
)


RUN_VALUE_NAME = "CS2Screen"
STARTUP_TASK_NAME = "SabasakalMiniScreen"
STARTUP_SHORTCUT_NAME = "Sabasakal Mini Screen.lnk"
APP_VERSION = "1.4.0"
APP_DATA = Path(os.environ.get("APPDATA", Path.home())) / "CS2Screen"
SETTINGS_PATH = APP_DATA / "settings.json"
RUNTIME_LOG_PATH = APP_DATA / "runtime.log"
RUNTIME_FALLBACK_LOG_PATH = APP_DATA / "runtime-current.log"
ACTIVATE_REQUEST_PATH = APP_DATA / "show-window.request"
ACTIVATE_ACK_PATH = APP_DATA / "show-window.ack"
# Graceful-quit IPC: a file in the user's %APPDATA% works from a non-elevated
# caller to the elevated primary (no window messages, so UIPI does not apply).
QUIT_REQUEST_PATH = APP_DATA / "quit.request"
QUIT_ACK_PATH = APP_DATA / "quit.ack"
INSTANCE_STATE_PATH = APP_DATA / "instance.json"
STARTUP_TASK_STATE_PATH = APP_DATA / "startup-task-state.json"
SENSOR_OWNER_STALE_SECONDS = 12.0
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
SENSOR_HOST_SOURCE_PATH = (
    RESOURCE_ROOT / "sensor-helper" / "SabasakalSensorHost.exe"
    if getattr(sys, "frozen", False)
    else RESOURCE_ROOT / "sensor-helper" / "publish" / "SabasakalSensorHost.exe"
)
LEGACY_SENSOR_TASK_NAMES = ("CS2ScreenSensors", "SabasakalScreenSensors")
_SENSOR_HOST_PROCESS: subprocess.Popen | None = None
_SENSOR_HOST_STARTED_AT = 0.0
# True once this session launched its own sensor host at least once.
_SENSOR_HOST_SESSION_STARTED = False
_SENSOR_HOST_LOCK = threading.RLock()
_LEGACY_SENSOR_TASKS_CLEANED = False
APP_ICON_PATH = RESOURCE_ROOT / "sabasakal-logo.ico"
APP_LOGO_PATH = RESOURCE_ROOT / "sabasakal-logo.png"
# Control-panel themes. The module colour names below are rebound by
# apply_ui_theme(), so every f-string that uses them follows the theme.
UI_THEMES = {
    "light": {
        # Soft, low-glare variant: warm grey paper instead of white, muted rose/peach.
        "window_bg": "#F4F1EE", "card_bg": "#FAF8F6", "card_2": "#F1ECE8",
        "sidebar_bg": (
            "qlineargradient(x1:0, y1:0, x2:0.35, y2:1, "
            "stop:0 #F3E4E8, stop:0.55 #F3EAE4, stop:1 #F2ECDD)"
        ),
        "sidebar_border": "#E7DCDF",
        "edge": "#E6DDDA", "text": "#2A2A30", "muted": "#716C76", "accent": "#C76B85",
        "accent_fill": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #CF7F95, stop:1 #D9A27A)"
        ),
        "accent_fill_hover": (
            "qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #D78FA3, stop:1 #E0AF8B)"
        ),
        "accent_text": "#FFFFFF",
        "control_bg": "#EFE9E6", "control_hover": "#E9E1DD", "control_pressed": "#E2D8D4",
        "nav_hover": "rgba(255, 255, 255, 0.35)", "nav_checked": "rgba(250, 248, 246, 0.85)",
        "nav_checked_text": "#9E4E66",
        "chip_checked_bg": "#F0DEE3", "chip_checked_text": "#9E4E66",
        "selection": "#F0DEE3", "slider_track": "#E6DDDA", "indicator_off": "#FAF8F6",
        "indicator_border": "#D8CBCF", "disabled_text": "#ADA6AE", "disabled_bg": "#EEE9E6",
        "scroll_handle": "#DED3D2", "action_border": "#E6DDDA", "preview_bg": "#1A1C21",
    },
    "dark": {
        "window_bg": "#16181D", "card_bg": "#1C1F25", "card_2": "#121418",
        "sidebar_bg": "#0F1115", "sidebar_border": "#1E2229",
        "edge": "#2A2F38", "text": "#ECEFF3", "muted": "#9AA4B2", "accent": "#79A9F5",
        "accent_fill": "#79A9F5", "accent_fill_hover": "#93BAF7", "accent_text": "#0A1019",
        "control_bg": "#24282F", "control_hover": "#2C313A", "control_pressed": "#1C1F25",
        "nav_hover": "#1A1D23", "nav_checked": "#22262E", "nav_checked_text": "#ECEFF3",
        "chip_checked_bg": "#29364A", "chip_checked_text": "#ECEFF3",
        "selection": "#303846", "slider_track": "#29303A", "indicator_off": "#29303A",
        "indicator_border": "#4B5563", "disabled_text": "#555D68", "disabled_bg": "#191D24",
        "scroll_handle": "#2C313A", "action_border": "#22262E", "preview_bg": "#121418",
    },
}
UI_THEME_NAMES = {"Açık": "light", "Koyu": "dark"}
DEFAULT_UI_THEME = "light"
WINDOW_BG = CARD_BG = CARD_2 = SIDEBAR_BG = EDGE = TEXT = MUTED = ACCENT = ""
CONTROL_BG = CONTROL_HOVER = CONTROL_PRESSED = ""


def apply_ui_theme(name: str) -> dict:
    """Rebind the module colour names for ``name``; returns the palette."""
    global WINDOW_BG, CARD_BG, CARD_2, SIDEBAR_BG, EDGE, TEXT, MUTED, ACCENT
    global CONTROL_BG, CONTROL_HOVER, CONTROL_PRESSED
    palette = UI_THEMES.get(name, UI_THEMES[DEFAULT_UI_THEME])
    WINDOW_BG, CARD_BG, CARD_2 = palette["window_bg"], palette["card_bg"], palette["card_2"]
    SIDEBAR_BG, EDGE, TEXT = palette["sidebar_bg"], palette["edge"], palette["text"]
    MUTED, ACCENT = palette["muted"], palette["accent"]
    CONTROL_BG, CONTROL_HOVER = palette["control_bg"], palette["control_hover"]
    CONTROL_PRESSED = palette["control_pressed"]
    return palette


apply_ui_theme(DEFAULT_UI_THEME)
ICON_SVG_ROOT = RESOURCE_ROOT / "icons" / "svg"
SIDEBAR_WIDTH = 228
# Sidebar entries: (section key, caption, Lucide icon name).
NAV_SECTIONS = (
    ("overview", "Genel bakış", "layout-dashboard"),
    ("display", "Ekran", "monitor"),
    ("info", "Bilgiler", "cpu"),
    ("energy", "Elektrik", "zap"),
    ("colors", "Renkler", "palette"),
    ("zowie", "Zowie monitör", "gamepad-2"),
    ("faceit", "FACEIT", "trophy"),
    ("about", "Hakkında", "info"),
)


class _LastInputInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def user_idle_seconds() -> float:
    """Seconds since the last keyboard/mouse input in this session (0 if unknown)."""
    if os.name != "nt":
        return 0.0
    info = _LastInputInfo()
    info.cbSize = ctypes.sizeof(info)
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        kernel32.GetTickCount.restype = ctypes.c_uint
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        return ((kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0
    except (AttributeError, OSError):
        return 0.0


def _format_number(value: float, language: str, digits: int = 2) -> str:
    rendered = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return rendered.replace(".", ",") if language == "tr" else rendered


def _parse_number(text: str) -> float | None:
    """Accepts "3,24", "3.24", "1.234,5" and "1,234.5"; None if not a number."""
    cleaned = str(text or "").strip().replace(" ", "").replace("₺", "").replace("TL", "")
    if not cleaned:
        return 0.0
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    else:
        cleaned = cleaned.replace(",", ".")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if value == value else None


def carousel_page(settings: dict, idle_seconds: float, pages: tuple[str, ...], now: float) -> str:
    """Desktop page to show: the system screen while in use, a rotation when idle."""
    if not settings.get("carousel_enabled", True) or not pages:
        return "main"
    if idle_seconds < float(settings.get("carousel_idle_seconds", 60)):
        return "main"
    period = max(5.0, float(settings.get("carousel_seconds", 15)))
    return pages[int(now // period) % len(pages)]


def _tinted_svg_icon(name: str, color: str, size: int = 36) -> QIcon:
    """Render a Lucide (stroke="currentColor") SVG in ``color``."""
    path = ICON_SVG_ROOT / f"{name}.svg"
    try:
        source = path.read_text(encoding="utf-8").replace("currentColor", color)
    except OSError:
        return QIcon()
    renderer = QSvgRenderer(QByteArray(source.encode("utf-8")))
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    return QIcon(pixmap)
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
# Display names are product names and are not translated. Internal keys
# stay "new"/"classic" so existing settings files keep working.
DESIGNS = {
    "Claude": "new",
    "Codex": "classic",
}


def weather_cities_for_design(settings: dict) -> tuple[str, ...]:
    """Claude (new) design shows one city: the first weather_cities entry.

    The full three-city list stays stored for the Codex (classic) design.
    """
    cities = normalise_city_names(settings.get("weather_cities"))
    if settings.get("design") == "classic":
        return cities
    return cities[:1]
# Partition used to send only the changed pixels of a new-design frame.
NEW_DESIGN_TILES = tuple(
    (left, top, left + 120, top + 40)
    for top in range(0, 320, 40) for left in range(0, 480, 120)
)
# Screen-change transition. The Rev-A panel has no double buffer and paints a
# region top-to-bottom as its bytes arrive, so a mostly-changed frame sent as
# row-major tiles looks like a slow tear. When at least WIPE_AREA_FRACTION of
# the panel changes, the same pixels are sent as full-height column strips
# left-to-right instead: each strip lands in ~1/8 of the transfer time, which
# reads as a deliberate horizontal wipe. It costs no extra bandwidth (no
# crossfade frames), so it finishes as fast as one full frame. Measured on the
# real panel: 1.20 s per full frame (~256 KB/s), see tools/render_transitions.py.
WIPE_STRIP_WIDTH = 60
WIPE_STRIPS = tuple((left, 0, left + WIPE_STRIP_WIDTH, 320) for left in range(0, 480, WIPE_STRIP_WIDTH))
WIPE_AREA_FRACTION = 0.45


# Dirty-rect planning for the Claude design. v1.1.7 diffed a fixed 120x40
# grid, so one changing number that crossed a tile edge went out as two or
# more independent rectangles (and a strip value that shifted width dirtied a
# whole row of tiles). The Rev-A panel paints each rectangle top-to-bottom as
# bytes arrive, so the halves of a glyph landed at different moments: visible
# tearing. Now changed pixels are clustered into whole-widget rectangles
# (connected 8 px cells), merged when they touch, sent top-to-bottom, and a
# cycle sends at most DIRTY_CYCLE_PIXELS (~250 ms of bus time); the rest is
# deferred whole to the next cycle, never split.
DIRTY_CELL = 8
DIRTY_MERGE_GAP = 4
# Fields on one text line (e.g. "33% 61°" of the CPU cell) are one widget.
DIRTY_MERGE_GAP_X = 40
# 256 KB/s * 0.25 s / 2 bytes per RGB565 pixel.
DIRTY_CYCLE_PIXELS = 32_000


def _rect_area(rect) -> int:
    return (rect[2] - rect[0]) * (rect[3] - rect[1])


def _near(a, b, gap: int, gap_x: int | None = None) -> bool:
    gap_x = gap if gap_x is None else gap_x
    return not (
        a[2] + gap_x <= b[0] or b[2] + gap_x <= a[0]
        or a[3] + gap <= b[1] or b[3] + gap <= a[1]
    )


def merge_rects(rects, gap: int = DIRTY_MERGE_GAP,
                gap_x: int | None = None) -> list[tuple[int, int, int, int]]:
    """Union rectangles that overlap or lie within ``gap`` px of each other."""
    rects = [tuple(r) for r in rects]
    merged = True
    while merged:
        merged = False
        out: list[tuple[int, int, int, int]] = []
        for rect in rects:
            for index, other in enumerate(out):
                if _near(rect, other, gap, gap_x):
                    out[index] = (
                        min(rect[0], other[0]), min(rect[1], other[1]),
                        max(rect[2], other[2]), max(rect[3], other[3]),
                    )
                    merged = True
                    break
            else:
                out.append(rect)
        rects = out
    return rects


def widget_dirty_rects(frame: Image.Image, sent: Image.Image) -> list[tuple[int, int, int, int]]:
    """Changed areas as whole-widget rectangles (never cut through a glyph)."""
    # Max over the bands: an "L" conversion would round a 1-level change of
    # a single channel to 0 and silently leave that pixel stale.
    red, green, blue = ImageChops.difference(frame.convert("RGB"), sent.convert("RGB")).split()
    diff = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    if diff.getbbox() is None:
        return []
    width, height = frame.size
    mask = diff.point(lambda v: 255 if v else 0)
    cols, rows = -(-width // DIRTY_CELL), -(-height // DIRTY_CELL)
    # A single changed pixel (255) averages to >= 255/64 ~ 4 over an 8x8
    # cell, so BOX downsampling never loses a changed cell.
    cells = mask.resize((cols, rows), Image.Resampling.BOX, reducing_gap=None)
    data = cells.load()
    hot = {(cx, cy) for cy in range(rows) for cx in range(cols) if data[cx, cy]}
    rects = []
    seen: set = set()
    for start in sorted(hot):
        if start in seen:
            continue
        stack, component = [start], []
        seen.add(start)
        while stack:
            cx, cy = stack.pop()
            component.append((cx, cy))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    nxt = (cx + dx, cy + dy)
                    if nxt in hot and nxt not in seen:
                        seen.add(nxt)
                        stack.append(nxt)
        left = min(c[0] for c in component) * DIRTY_CELL
        top = min(c[1] for c in component) * DIRTY_CELL
        right = min(width, (max(c[0] for c in component) + 1) * DIRTY_CELL)
        bottom = min(height, (max(c[1] for c in component) + 1) * DIRTY_CELL)
        tight = mask.crop((left, top, right, bottom)).getbbox()
        rects.append((left + tight[0], top + tight[1], left + tight[2], top + tight[3]))
    aligned = []
    for left, top, right, bottom in merge_rects(rects, DIRTY_MERGE_GAP, DIRTY_MERGE_GAP_X):
        # Even x/width: identical boundaries in normal and 180-degree panels.
        left -= left % 2
        right = min(width, right + right % 2)
        aligned.append((left, top, right, bottom))
    aligned = merge_rects(aligned, 0)
    return sorted(aligned, key=lambda r: (r[1], r[0]))


def plan_new_design_update(
    frame: Image.Image, sent: Image.Image, budget: int = DIRTY_CYCLE_PIXELS,
) -> tuple[str, list[tuple[int, int, int, int]]]:
    """Return ("none"|"tiles"|"wipe", regions) for a Claude-design frame.

    "tiles" regions are whole-widget rectangles, top-to-bottom, at most
    ``budget`` pixels per cycle (the first one always goes). Unsent widgets
    stay different from the sent frame and go out on the next cycle.
    """
    regions = widget_dirty_rects(frame, sent)
    if not regions:
        return "none", []
    area = sum(_rect_area(r) for r in regions)
    if area >= WIPE_AREA_FRACTION * frame.width * frame.height:
        return "wipe", _tight_changed_regions(frame, sent, WIPE_STRIPS)
    chosen, used = [], 0
    for region in regions:
        cost = _rect_area(region)
        if chosen and used + cost > budget:
            continue
        chosen.append(region)
        used += cost
    return "tiles", chosen
NEW_DESIGN_WORKER_INTERVAL = 0.5
NEW_DESIGN_MSFS_INTERVAL = 0.25
DEFAULT_SETTINGS = {
    "palette": "Kuzey", "colors": PALETTES["Kuzey"], "brightness": 55,
    "auto_brightness": False, "rotation": "normal", "autostart": False,
    "language": "tr",
    "port": "AUTO", "interval": 0.5,
    "msfs_layout": "cockpit",
    "design": "new",
    "weather_cities": list(DEFAULT_WEATHER_CITIES),
    "custom_palettes": {}, "hardware_labels": {"cpu": "", "gpu": "", "ram": ""},
    # Zowie add-on: monitor picture mode while cs2.exe runs / after it exits.
    "zowie_auto_mode": False,
    "zowie_game_mode": ZOWIE_DEFAULT_GAME_MODE,
    "zowie_desktop_mode": ZOWIE_DEFAULT_DESKTOP_MODE,
    # Blank the LCD while Windows is locked.
    "screen_off_when_locked": True,
    # Read-only daily look at the GitHub release list (never downloads).
    "update_check": True,
    # Control-panel look: "light" (pink-yellow) or "dark".
    "ui_theme": DEFAULT_UI_THEME,
    # Desktop carousel while the PC is idle (Claude design).
    "carousel_enabled": True,
    "carousel_seconds": 15,
    "carousel_idle_seconds": 60,
    # Electricity estimate.
    "energy_price_mode": "bill",  # bill | unit
    "energy_bill_amount": 0.0,
    "energy_bill_kwh": 0.0,
    "energy_unit_price": 0.0,
    "energy_other_watts": DEFAULT_OTHER_WATTS,
    "energy_psu_efficiency": DEFAULT_PSU_EFFICIENCY,
    # FACEIT card: nickname + DPAPI-encrypted server-side API key.
    "faceit_nickname": "",
    "faceit_key_protected": "",
}
FACEIT_DEVELOPERS_URL = "https://developers.faceit.com/"
CAROUSEL_SECONDS_CHOICES = (10, 15, 30, 60)
CAROUSEL_IDLE_CHOICES = (30, 60, 120, 300)
# (min, max) of the numeric electricity settings.
ENERGY_LIMITS = {
    "energy_bill_amount": (0.0, 1_000_000.0),
    "energy_bill_kwh": (0.0, 100_000.0),
    "energy_unit_price": (0.0, 1_000.0),
    "energy_other_watts": (0.0, 400.0),
    "energy_psu_efficiency": (50.0, 100.0),
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
# In-game performance mode (cs2.exe / MSFS running).
NORMAL_BATTERY_INTERVAL = 2.0
PERFORMANCE_BATTERY_INTERVAL = 10.0
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
INSTANCE_STALE_SECONDS = 30.0
EXIT_FALLBACK_MS = 5000
QUICK_CARD_HEIGHT = 288
SETTINGS_CARD_HEIGHT = 706 + (QUICK_CARD_HEIGHT - 252)
WINDOW_DEFAULT_SIZE = (1100, 820)
WINDOW_MINIMUM_SIZE = (900, 620)
WORKER_RESTART_MAX_DELAY_MS = 60000
# Must exceed the worst-case ScreenSession reconnect (write timeout + three
# open attempts with back-off) so an unplug is recovered in-process instead of
# being mistaken for a native write that blocks forever.
DISPLAY_STALL_SECONDS = 30.0


def set_process_priority(below_normal: bool, process=None) -> bool:
    """BELOW_NORMAL while a game runs so the game always wins the CPU."""
    if os.name != "nt":
        return False
    try:
        process = process or psutil.Process()
        process.nice(
            psutil.BELOW_NORMAL_PRIORITY_CLASS if below_normal else psutil.NORMAL_PRIORITY_CLASS
        )
        return True
    except Exception:
        return False


def should_log_distinct(
    seen: dict[str, float], message: str, window: float = 300.0, now: float | None = None,
) -> bool:
    """Allow each distinct message at most once per ``window`` seconds."""
    now = time.monotonic() if now is None else now
    last = seen.get(message)
    if last is not None and now - last < window:
        return False
    seen[message] = now
    return True


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


def _clean_restart_environment() -> dict[str, str]:
    """Force a new one-file extraction when the packaged app restarts itself."""
    environment = os.environ.copy()
    if getattr(sys, "frozen", False):
        # Without this PyInstaller treats the replacement as another child of
        # the current bootloader and reuses its _MEI directory.  The old
        # bootloader then removes that directory during shutdown, leaving the
        # replacement alive but without the bundled display-driver package.
        environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return environment


def _remove_legacy_sensor_tasks_once() -> None:
    """Remove obsolete task-based sensor launchers after elevation is available."""
    global _LEGACY_SENSOR_TASKS_CLEANED
    if _LEGACY_SENSOR_TASKS_CLEANED or os.name != "nt":
        return
    _LEGACY_SENSOR_TASKS_CLEANED = True
    for task_name in LEGACY_SENSOR_TASK_NAMES:
        try:
            subprocess.run(
                ["schtasks.exe", "/End", "/TN", task_name],
                capture_output=True, timeout=8, creationflags=_hidden_flags(),
            )
            subprocess.run(
                ["schtasks.exe", "/Delete", "/TN", task_name, "/F"],
                capture_output=True, timeout=8, creationflags=_hidden_flags(),
            )
        except (OSError, subprocess.SubprocessError):
            pass
    for stale_path in (
        APP_DATA / "sensor-task.json",
        APP_DATA / "sensor-task.xml",
    ):
        try:
            stale_path.unlink(missing_ok=True)
        except OSError:
            pass


def _sensor_host_is_running() -> bool:
    process = _SENSOR_HOST_PROCESS
    return process is not None and process.poll() is None


def _serialize_sensor_host(function):
    """Make UI-timer and display-watchdog sensor actions atomic."""
    def serialized(*args, **kwargs):
        with _SENSOR_HOST_LOCK:
            return function(*args, **kwargs)
    return serialized


@_serialize_sensor_host
def ensure_sensor_host(restart: bool = False) -> bool:
    """Run exactly one hidden sensor child under the already-elevated UI."""
    global _SENSOR_HOST_PROCESS, _SENSOR_HOST_STARTED_AT, _SENSOR_HOST_SESSION_STARTED
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return False
    _remove_legacy_sensor_tasks_once()
    if _sensor_host_is_running() and not restart:
        return True
    stop_sensor_host()
    if not SENSOR_HOST_SOURCE_PATH.is_file():
        _runtime_log(f"sensor host missing: {SENSOR_HOST_SOURCE_PATH}")
        return False
    environment = _clean_restart_environment()
    # Keep .NET single-file extraction out of the volatile Windows Temp
    # directory. This prevents cleanup warnings and partially removed hosts.
    environment["DOTNET_BUNDLE_EXTRACT_BASE_DIR"] = str(
        APP_DATA / "sensor-host-runtime"
    )
    # .NET sizes the gen0 budget from the L3 cache (96 MB on X3D CPUs), so the
    # host ran ~30 min without a single collection and grew to ~60 MB. A 4 MB
    # budget (hex value) keeps it near its few-MB live set; each gen0
    # collection of such a small heap takes well under a millisecond.
    environment["DOTNET_GCgen0size"] = "400000"
    try:
        APP_DATA.mkdir(parents=True, exist_ok=True)
        _SENSOR_HOST_PROCESS = subprocess.Popen(
            [
                str(SENSOR_HOST_SOURCE_PATH),
                "--managed",
                "--cache", str(SENSOR_CACHE_PATH),
                "--owner-state", str(INSTANCE_STATE_PATH),
            ],
            cwd=str(SENSOR_HOST_SOURCE_PATH.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_hidden_flags(),
            env=environment,
        )
        _SENSOR_HOST_STARTED_AT = time.monotonic()
        _SENSOR_HOST_SESSION_STARTED = True
        _runtime_log(f"sensor host started directly; pid={_SENSOR_HOST_PROCESS.pid}")
        return True
    except OSError as exc:
        _SENSOR_HOST_PROCESS = None
        _runtime_log(f"sensor host start failed: {type(exc).__name__}: {exc}")
        return False


@_serialize_sensor_host
def stop_sensor_host() -> None:
    """Stop only the child process owned by this control-panel instance."""
    global _SENSOR_HOST_PROCESS, _SENSOR_HOST_STARTED_AT
    process = _SENSOR_HOST_PROCESS
    _SENSOR_HOST_PROCESS = None
    _SENSOR_HOST_STARTED_AT = 0.0
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=2.0)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired):
            pass


def shutdown_child_processes() -> None:
    """Terminate every helper process this instance may own (tray Exit)."""
    try:
        stop_sensor_host()
    except Exception as exc:  # pragma: no cover - defensive
        _runtime_log(f"sensor host stop failed: {type(exc).__name__}: {exc}")
    try:
        SENSOR_MONITOR.close()
    except Exception as exc:  # pragma: no cover - defensive
        _runtime_log(f"sensor bridge stop failed: {type(exc).__name__}: {exc}")


# The host writes the cache every second from a writer that no longer waits
# for LibreHardwareMonitor; 12 s tolerates disk/AV hiccups under game load.
SENSOR_CACHE_STALE_SECONDS = 12.0


def nudge_sensor_host_if_stale(
    path: Path = SENSOR_CACHE_PATH, max_age: float = SENSOR_CACHE_STALE_SECONDS,
) -> bool:
    """Restart the invisible child if its sensor cache stops advancing."""
    try:
        if time.time() - path.stat().st_mtime <= max_age:
            return False
    except OSError:
        pass
    # LibreHardwareMonitor needs a short discovery pass on first launch.
    # Do not kill a healthy new child before it can publish its first sample.
    if _sensor_host_is_running() and time.monotonic() - _SENSOR_HOST_STARTED_AT < max_age:
        return False
    if not _SENSOR_HOST_SESSION_STARTED:
        # The old cache belongs to the previous session; this is a normal
        # first start, not a watchdog restart.
        started = ensure_sensor_host(restart=False)
        _runtime_log(
            "sensor host first start (previous session cache)"
            if started else "sensor host first start failed"
        )
        return False
    restarted = ensure_sensor_host(restart=True)
    _runtime_log(
        "stale sensor cache; direct host restarted"
        if restarted else "stale sensor cache; direct host restart failed"
    )
    return restarted

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


def request_running_instance_quit(timeout_seconds: float = 15.0) -> bool:
    """Ask the primary instance to run its normal tray-Exit path."""
    try:
        APP_DATA.mkdir(parents=True, exist_ok=True)
        token = f"{os.getpid()}-{time.time_ns()}"
        QUIT_ACK_PATH.unlink(missing_ok=True)
        QUIT_REQUEST_PATH.write_text(token, encoding="ascii")
    except OSError:
        return False
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while time.monotonic() < deadline:
        try:
            if QUIT_ACK_PATH.read_text(encoding="ascii").strip() == token:
                QUIT_ACK_PATH.unlink(missing_ok=True)
                return True
        except OSError:
            pass
        time.sleep(0.10)
    # Nobody consumed it: do not let a later launch quit on a stale request.
    try:
        if QUIT_REQUEST_PATH.read_text(encoding="ascii").strip() == token:
            QUIT_REQUEST_PATH.unlink(missing_ok=True)
    except OSError:
        pass
    return False


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


def _sensor_owner_alive(
    state_path: Path = INSTANCE_STATE_PATH,
    max_age: float = SENSOR_OWNER_STALE_SECONDS,
) -> bool:
    """Keep SensorHost alive only while the exact control-panel owner is healthy."""
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        pid = int(state.get("pid", 0))
        created_at = float(state.get("created_at", 0.0))
        heartbeat = float(state.get("heartbeat", 0.0))
        if pid <= 0 or time.time() - heartbeat > max_age:
            return False
        process = psutil.Process(pid)
        return abs(process.create_time() - created_at) <= 1.0 and process.is_running()
    except (OSError, TypeError, ValueError, json.JSONDecodeError, psutil.Error):
        return False


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


def _process_has_hung_window(pid: int, timeout_ms: int = 2000) -> bool:
    """Return True only when a top-level window of ``pid`` does not answer.

    A stale heartbeat alone is not proof of a hang (sleep/resume, a busy disk
    or a debugger can delay it). WM_NULL with SMTO_ABORTIFHUNG confirms that
    the primary's message loop is really blocked before it is terminated.
    """
    if os.name != "nt" or pid <= 0:
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        windows: list[int] = []
        enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def collect(hwnd, _lparam) -> bool:
            owner = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(owner))
            if owner.value == pid:
                windows.append(hwnd)
            return True

        user32.EnumWindows(enum_proc(collect), None)
        if not windows:
            return False
        user32.SendMessageTimeoutW.argtypes = (
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_size_t),
        )
        user32.SendMessageTimeoutW.restype = ctypes.c_size_t
        for hwnd in windows[:8]:
            result = ctypes.c_size_t(0)
            answered = user32.SendMessageTimeoutW(
                ctypes.c_void_p(hwnd), 0x0000, None, None,  # WM_NULL
                0x0002, timeout_ms, ctypes.byref(result),  # SMTO_ABORTIFHUNG
            )
            if answered:
                return False
        return True
    except (OSError, AttributeError, ValueError):
        return False


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
        if not _process_has_hung_window(pid):
            _runtime_log(f"stale primary heartbeat but its window still responds; pid={pid}")
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
        if not isinstance(palette, str) or (
            palette not in PALETTES and palette not in settings["custom_palettes"]
        ):
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
        if saved.get("design") in DESIGNS.values():
            settings["design"] = saved["design"]
        settings["weather_cities"] = list(normalise_city_names(saved.get("weather_cities")))
        labels = saved.get("hardware_labels")
        if isinstance(labels, dict):
            settings["hardware_labels"] = {
                key: normalise_hardware_label(labels.get(key))
                for key in ("cpu", "gpu", "ram")
            }
        if isinstance(saved.get("zowie_auto_mode"), bool):
            settings["zowie_auto_mode"] = saved["zowie_auto_mode"]
        for flag in ("screen_off_when_locked", "update_check"):
            if isinstance(saved.get(flag), bool):
                settings[flag] = saved[flag]
        if saved.get("ui_theme") in UI_THEMES:
            settings["ui_theme"] = saved["ui_theme"]
        if isinstance(saved.get("carousel_enabled"), bool):
            settings["carousel_enabled"] = saved["carousel_enabled"]
        if saved.get("carousel_seconds") in CAROUSEL_SECONDS_CHOICES:
            settings["carousel_seconds"] = saved["carousel_seconds"]
        if saved.get("carousel_idle_seconds") in CAROUSEL_IDLE_CHOICES:
            settings["carousel_idle_seconds"] = saved["carousel_idle_seconds"]
        nickname = saved.get("faceit_nickname")
        if isinstance(nickname, str):
            settings["faceit_nickname"] = nickname.strip()[:32]
        protected = saved.get("faceit_key_protected")
        if isinstance(protected, str) and len(protected) < 4096:
            settings["faceit_key_protected"] = protected
        if saved.get("energy_price_mode") in ("bill", "unit"):
            settings["energy_price_mode"] = saved["energy_price_mode"]
        for key, (low, high) in ENERGY_LIMITS.items():
            try:
                value = float(saved.get(key, settings[key]))
            except (TypeError, ValueError):
                continue
            if value == value:  # not NaN
                settings[key] = max(low, min(high, value))
        settings["zowie_game_mode"] = normalise_zowie_mode(
            saved.get("zowie_game_mode"), ZOWIE_DEFAULT_GAME_MODE,
        )
        settings["zowie_desktop_mode"] = normalise_zowie_mode(
            saved.get("zowie_desktop_mode"), ZOWIE_DEFAULT_DESKTOP_MODE,
        )
    except (OSError, ValueError, TypeError):
        pass
    # Older test builds used a three-second pause after every completed frame.
    # Keep the complete measurement + transfer cycle close to Task Manager's cadence.
    settings["interval"] = 0.5
    return settings


def save_settings_file(settings: dict) -> None:
    APP_DATA.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_PATH.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, SETTINGS_PATH)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


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
  <Triggers><LogonTrigger><Enabled>true</Enabled><Delay>PT3S</Delay></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{xml_escape(account)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
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


def _remove_windows_startup_shortcut() -> bool:
    try:
        _windows_startup_shortcut_path().unlink(missing_ok=True)
        return True
    except OSError as exc:
        _runtime_log(f"startup shortcut removal exception: {type(exc).__name__}: {exc}")
        return False


def _expected_startup_task_state() -> dict[str, str]:
    executable, arguments = _startup_action()
    return {
        "executable": str(Path(executable).resolve()),
        "arguments": arguments,
        "mode": "elevated-interactive-v1",
    }


def _startup_task_exists() -> bool:
    try:
        result = subprocess.run(
            ["schtasks.exe", "/Query", "/TN", STARTUP_TASK_NAME],
            capture_output=True, timeout=10, creationflags=_hidden_flags(),
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _startup_task_command() -> str | None:
    """Return the registered startup task's executable, or None if absent."""
    try:
        result = subprocess.run(
            ["schtasks.exe", "/Query", "/TN", STARTUP_TASK_NAME, "/XML"],
            capture_output=True, timeout=10, creationflags=_hidden_flags(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout or b""
    text = raw if isinstance(raw, str) else None
    if text is None:
        for encoding in ("utf-16", "utf-8", "mbcs"):
            try:
                text = raw.decode(encoding)
                if "<Task" in text:
                    break
            except (UnicodeError, LookupError):
                continue
        else:
            text = raw.decode("utf-8", "replace")
    match = re.search(r"<Command>(.*?)</Command>", text or "", re.S)
    if not match:
        return ""
    from html import unescape
    return unescape(match.group(1)).strip().strip('"')


def _same_path(a: str, b: str) -> bool:
    try:
        return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))
    except OSError:
        return False


def _startup_task_is_current() -> bool:
    try:
        saved = json.loads(STARTUP_TASK_STATE_PATH.read_text(encoding="utf-8"))
        if saved != _expected_startup_task_state():
            return False
        command = _startup_task_command()
        return bool(command) and _same_path(command, _startup_action()[0])
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
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
        state = _expected_startup_task_state()
        state_temporary = STARTUP_TASK_STATE_PATH.with_suffix(".tmp")
        state_temporary.write_text(json.dumps(state), encoding="utf-8")
        os.replace(state_temporary, STARTUP_TASK_STATE_PATH)
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
            STARTUP_TASK_STATE_PATH.unlink(missing_ok=True)
            return True
        query = subprocess.run(
            ["schtasks.exe", "/Query", "/TN", STARTUP_TASK_NAME],
            capture_output=True, timeout=10, creationflags=_hidden_flags(),
        )
        absent = query.returncode != 0
        if absent:
            STARTUP_TASK_STATE_PATH.unlink(missing_ok=True)
        return absent
    except (OSError, subprocess.SubprocessError) as exc:
        _runtime_log(f"startup task removal exception: {type(exc).__name__}: {exc}")
        return False


def set_windows_autostart(enabled: bool) -> None:
    """Maintain exactly one startup path and remove legacy duplicate launchers."""
    # The elevated task is the sole startup authority. MultipleInstancesPolicy
    # and the process mutex independently prevent duplicate control panels.
    try:
        _set_windows_run_fallback(False)
    except Exception as exc:
        _runtime_log(f"legacy startup Run cleanup failed: {type(exc).__name__}: {exc}")
    if not _remove_windows_startup_shortcut():
        raise OSError("Legacy Windows Startup shortcut could not be removed")
    if enabled:
        if not _startup_task_is_current() and not _install_windows_startup_task():
            raise OSError("Elevated Windows startup task could not be installed")
    elif not _remove_windows_startup_task():
        raise OSError("Elevated Windows startup task could not be removed")


def reconcile_windows_autostart(enabled: bool) -> bool:
    """Repair the sole elevated startup task and follow the current EXE path.

    Enabled: install/refresh the task for this EXE. Disabled: a task left
    behind by an older (moved/deleted) copy is removed when its EXE no longer
    exists, or re-pointed to this EXE when it targets another existing copy.
    """
    try:
        command = _startup_task_command()
        if enabled:
            if command and not _same_path(command, _startup_action()[0]):
                _runtime_log(f"startup task repaired; old={command}")
            set_windows_autostart(True)
            return True
        if command is None:
            return True
        current = _startup_action()[0]
        if command and _same_path(command, current):
            return True
        if not command or not Path(command).exists():
            _runtime_log(f"stale startup task removed; target={command or '?'}")
            if not _remove_windows_startup_task():
                raise OSError("Stale Windows startup task could not be removed")
            return True
        _runtime_log(f"startup task repaired; old={command}; new={current}")
        if not _install_windows_startup_task():
            raise OSError("Elevated Windows startup task could not be repaired")
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
    autostart_result = Signal(bool, str)
    update_result = Signal(object, str, bool)  # UpdateInfo | None, error, manual
    faceit_result = Signal(object)  # FaceitSnapshot


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
        self.signals.autostart_result.connect(self._autostart_result)
        # Autostart is reconciled by main() at launch; afterwards Task
        # Scheduler is touched only when the user changes the checkbox.
        self._applied_autostart: bool | None = bool(self.settings.get("autostart"))
        self._autostart_thread: threading.Thread | None = None
        self._restart_failures = 0
        self._worker_started_at = 0.0
        self._exit_fallback_armed = False
        # Zowie add-on; the thread is started by main() in real launches only.
        self.zowie_monitor = ZowieModeMonitor(cs2_is_running, log=_runtime_log)
        self._configure_zowie_monitor()
        # Electricity estimate; the sampling thread is started by main() only.
        self.energy_meter = EnergyMeter(
            read_power=lambda: (SENSOR_MONITOR.read().get("cpu_power"), NVIDIA_MONITOR.read_power_watts()),
            store_path=APP_DATA / "energy.json",
            settings=lambda: self.settings,
            log=_runtime_log,
        )
        # FACEIT card; the refresh thread is started by main() only.
        self.faceit_monitor = FaceitMonitor(
            settings=lambda: self.settings,
            cache_path=APP_DATA / "faceit.json",
            game_running=cs2_is_running,
            log=_runtime_log,
        )
        # Lock screen + update check state.
        self.display_suspended = threading.Event()
        self._windows_locked = False
        self._session_notifications = False
        self.update_info: UpdateInfo | None = None
        self._update_check_running = False
        self._update_notified = ""
        self.signals.update_result.connect(self._update_result)
        self.signals.faceit_result.connect(self._faceit_result)
        self.ui_theme = self.settings.get("ui_theme", DEFAULT_UI_THEME)
        if self.ui_theme not in UI_THEMES:
            self.ui_theme = DEFAULT_UI_THEME
        apply_ui_theme(self.ui_theme)
        self.setWindowTitle(self._window_title())
        if APP_ICON_PATH.exists():
            self.setWindowIcon(QIcon(str(APP_ICON_PATH)))
        self.resize(*WINDOW_DEFAULT_SIZE)
        self.setMinimumSize(*WINDOW_MINIMUM_SIZE)
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
        self.zowie_status_timer = QTimer(self)
        self.zowie_status_timer.timeout.connect(self._refresh_zowie_status)
        self.zowie_status_timer.timeout.connect(self._refresh_energy_readout)
        self.zowie_status_timer.start(1000)
        self._refresh_zowie_status()
        self.update_status_label.setText(self._tr("Henüz denetlenmedi"))
        self._refresh_preview()
        _runtime_log(f"control panel ready; autorun={autorun}; smoke_test={smoke_test}")
        if smoke_test:
            QTimer.singleShot(900, self.close)

    def _consume_quit_request(self) -> bool:
        if not QUIT_REQUEST_PATH.exists():
            return False
        try:
            token = QUIT_REQUEST_PATH.read_text(encoding="ascii").strip()
            QUIT_REQUEST_PATH.unlink(missing_ok=True)
            QUIT_ACK_PATH.write_text(token, encoding="ascii")
        except OSError:
            return False
        _runtime_log("quit requested via --quit")
        self._exit_application()
        return True

    def _consume_activation_request(self) -> None:
        if not self._smoke_test and self._consume_quit_request():
            return
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
            subprocess.Popen(
                command,
                cwd=str(APP_DATA),
                creationflags=_hidden_flags(),
                env=_clean_restart_environment(),
            )
        except OSError as exc:
            self._watchdog_restart_started = False
            _runtime_log(f"display watchdog restart failed: {type(exc).__name__}: {exc}")
            return
        self._exit_requested = True
        self.stop_event.set()
        # A blocked vendor USB call can keep the screen worker (and therefore
        # the old PyInstaller process) alive after Qt has quit.  Give normal
        # shutdown a brief chance, then guarantee that the stale instance
        # releases the single-instance mutex and USB handle.  The replacement
        # process above waits for this PID before it starts its own worker.
        threading.Thread(
            target=self._force_exit_after_display_stall,
            name="DisplayStallHardExit",
            daemon=True,
        ).start()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    @staticmethod
    def _force_exit_after_display_stall(delay_seconds: float = 2.0) -> None:
        time.sleep(delay_seconds)
        _runtime_log("display watchdog forcing stale process exit")
        os._exit(0)

    def _style_sheet(self) -> str:
        p = UI_THEMES.get(self.ui_theme, UI_THEMES[DEFAULT_UI_THEME])
        return f"""
            QWidget {{ background: {p['window_bg']}; color: {p['text']}; font-family: 'Segoe UI Variable Text', 'Segoe UI'; font-size: 13px; }}
            QLabel, QRadioButton, QCheckBox {{ background: transparent; }}
            QFrame#sidebar {{ background: {p['sidebar_bg']}; border-right: 1px solid {p['sidebar_border']}; }}
            QFrame#sidebar QLabel, QFrame#sidebar QPushButton, QFrame#sidebar QWidget {{ background: transparent; }}
            QFrame#sidebar QComboBox {{ background: {p['nav_checked']}; }}
            QLabel#brand {{ font-size: 16px; font-weight: 700; }}
            QPushButton#navItem {{ text-align: left; padding: 9px 12px; border-radius: 10px; color: {p['muted']}; font-weight: 600; border: 0; }}
            QPushButton#navItem:hover {{ background: {p['nav_hover']}; color: {p['text']}; }}
            QPushButton#navItem:checked {{ background: {p['nav_checked']}; color: {p['nav_checked_text']}; }}
            QScrollArea#mainScroll, QWidget#page, QWidget#section {{ background: {p['window_bg']}; border: 0; }}
            QLabel#pageTitle {{ font-size: 20px; font-weight: 700; }}
            QFrame#group {{ background: {p['card_bg']}; border: 1px solid {p['edge']}; border-radius: 14px; }}
            QFrame#group QLabel, QFrame#group QCheckBox, QWidget#inline {{ background: transparent; }}
            QFrame#actionBar {{ background: {p['window_bg']}; border-top: 1px solid {p['action_border']}; }}
            QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px 2px; }}
            QScrollBar::handle:vertical {{ background: {p['scroll_handle']}; border-radius: 3px; min-height: 40px; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
            QFrame#card {{ background: {p['card_bg']}; border: 1px solid {p['edge']}; border-radius: 16px; }}
            QFrame#subCard, QFrame#colorChooser {{ background: {p['card_2']}; border: 1px solid {p['edge']}; border-radius: 11px; }}
            QFrame#previewShell {{ background: {p['preview_bg']}; border: 0; border-radius: 14px; }}
            QFrame#statusBar {{ background: {p['card_2']}; border: 0; border-radius: 11px; }}
            QLabel#title {{ font-size: 30px; font-weight: 700; }}
            QLabel#subtitle, QLabel#body, QLabel#device, QLabel#status {{ color: {p['muted']}; }}
            QLabel#section {{ font-size: 15px; font-weight: 700; }}
            QLabel#preview {{ background: #080A0E; border-radius: 5px; }}
            QComboBox, QLineEdit {{ background: {p['control_bg']}; border: 1px solid {p['edge']}; border-radius: 8px; padding: 9px 11px; }}
            QComboBox:focus, QLineEdit:focus {{ border-color: {p['accent']}; }}
            QComboBox::drop-down {{ border: 0; width: 28px; }}
            QComboBox QAbstractItemView {{ background: {p['card_bg']}; selection-background-color: {p['selection']}; selection-color: {p['text']}; outline: 0; }}
            QPushButton {{ background: {p['control_bg']}; border: 1px solid transparent; border-radius: 9px; padding: 11px 14px; font-weight: 600; outline: none; }}
            QPushButton:hover {{ background: {p['control_hover']}; }} QPushButton:pressed {{ background: {p['control_pressed']}; }}
            QPushButton:focus {{ border-color: {p['accent']}; }}
            QPushButton:disabled {{ color: {p['disabled_text']}; background: {p['disabled_bg']}; }}
            QComboBox:disabled, QLineEdit:disabled {{ color: {p['disabled_text']}; background: {p['disabled_bg']}; border-color: {p['edge']}; }}
            QRadioButton:disabled, QCheckBox:disabled {{ color: {p['disabled_text']}; }}
            QSlider::groove:horizontal:disabled {{ background: {p['disabled_bg']}; }}
            QSlider::sub-page:horizontal:disabled {{ background: {p['indicator_border']}; }}
            QSlider::handle:horizontal:disabled {{ background: {p['disabled_text']}; }}
            QPushButton#accent {{ color: {p['accent_text']}; background: {p['accent_fill']}; padding: 12px 18px; border: 0; }}
            QPushButton#accent:hover {{ background: {p['accent_fill_hover']}; }}
            QPushButton#accent:disabled {{ color: {p['disabled_text']}; background: {p['disabled_bg']}; border: 1px solid {p['edge']}; }}
            QPushButton#previewMode {{ padding: 6px 14px; color: {p['muted']}; background: transparent; border: 1px solid {p['edge']}; border-radius: 16px; font-size: 12px; }}
            QPushButton#previewMode:hover {{ color: {p['text']}; background: {p['control_bg']}; }}
            QPushButton#previewMode:checked {{ color: {p['chip_checked_text']}; background: {p['chip_checked_bg']}; border-color: {p['accent']}; }}
            QPushButton#previewMode:disabled {{ color: {p['disabled_text']}; background: transparent; border-color: {p['edge']}; }}
            QSlider::groove:horizontal {{ height: 6px; background: {p['slider_track']}; border-radius: 3px; }}
            QSlider::sub-page:horizontal {{ background: {p['accent_fill']}; border-radius: 2px; }}
            QSlider::handle:horizontal {{ width: 14px; height: 14px; margin: -6px 0; background: {p['card_bg']}; border: 2px solid {p['accent']}; border-radius: 9px; }}
            QRadioButton, QCheckBox {{ spacing: 8px; }}
            QRadioButton::indicator, QCheckBox::indicator {{ width: 17px; height: 17px; }}
            QRadioButton::indicator:unchecked, QCheckBox::indicator:unchecked {{ background: {p['indicator_off']}; border: 1px solid {p['indicator_border']}; border-radius: 8px; }}
            QRadioButton::indicator:checked, QCheckBox::indicator:checked {{ background: {p['accent_fill']}; border: 3px solid {p['indicator_off']}; border-radius: 8px; }}
            QFrame#rule {{ background: {p['edge']}; max-height: 1px; border: 0; }}
            QToolTip {{ background: {p['card_bg']}; color: {p['text']}; border: 1px solid {p['edge']}; padding: 6px; }}
        """

    def _label(self, text: str, name: str = "body") -> QLabel:
        label = QLabel(self._tr(text))
        label.setProperty("i18n_source", text)
        label.setObjectName(name)
        return label

    def _tr(self, text: str, **values: object) -> str:
        return translate(text, self.language, **values)

    def _window_title(self) -> str:
        # Must stay version-free: _activate_existing_window() finds the
        # running panel by this exact title.
        return f"{self._tr(APP_DISPLAY_NAME)} · {self._tr('Ayarlar')}"

    def _rule(self) -> QFrame:
        rule = QFrame()
        rule.setObjectName("rule")
        rule.setFixedHeight(1)
        return rule

    def _build_ui(self) -> None:
        """Sidebar navigation + one scrolling settings column + fixed action bar.

        Every control lives on one page (the sidebar only scrolls to a
        section), so nothing is ever hidden behind a tab.
        """
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- Sidebar -----------------------------------------------------
        sidebar = QFrame(objectName="sidebar")
        sidebar.setFixedWidth(SIDEBAR_WIDTH)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(14, 18, 14, 16)
        side.setSpacing(4)
        brand = QHBoxLayout()
        brand.setSpacing(10)
        if APP_LOGO_PATH.exists():
            brand_mark = QLabel()
            brand_mark.setFixedSize(34, 34)
            brand_mark.setPixmap(QPixmap(str(APP_LOGO_PATH)).scaled(
                34, 34, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))
            brand.addWidget(brand_mark)
        brand_copy = QVBoxLayout()
        brand_copy.setSpacing(0)
        brand_copy.addWidget(self._label("Sabasakal", "brand"))
        self.version_label = self._label(f"v{APP_VERSION}", "device")
        self.version_label.setProperty("i18n_source", "")  # never translated
        self.version_label.setToolTip(f"{APP_DISPLAY_NAME} {APP_VERSION}")
        self.version_label.setProperty("i18n_tooltip", "")
        brand_copy.addWidget(self.version_label)
        brand.addLayout(brand_copy)
        brand.addStretch()
        side.addLayout(brand)
        side.addSpacing(18)

        self.nav_buttons: dict[str, QPushButton] = {}
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        for key, caption, icon in NAV_SECTIONS:
            button = QPushButton(caption, objectName="navItem")
            button.setCheckable(True)
            button.setIcon(_tinted_svg_icon(icon, MUTED))
            button.setIconSize(QSize(18, 18))
            button.clicked.connect(lambda _checked=False, section=key: self._scroll_to_section(section))
            self.nav_group.addButton(button)
            self.nav_buttons[key] = button
            side.addWidget(button)
        self.nav_buttons[NAV_SECTIONS[0][0]].setChecked(True)
        side.addStretch()
        side.addWidget(self._label("Tema", "device"))
        self.theme_combo = QComboBox()
        for label, value in UI_THEME_NAMES.items():
            self.theme_combo.addItem(self._tr(label), value)
        self.theme_combo.setCurrentIndex(max(0, self.theme_combo.findData(self.ui_theme)))
        self.theme_combo.setFixedHeight(36)
        self.theme_combo.currentIndexChanged.connect(self._theme_changed)
        side.addWidget(self.theme_combo)
        side.addSpacing(6)
        side.addWidget(self._label("Dil", "device"))
        self.language_combo = QComboBox()
        for code, label in SUPPORTED_LANGUAGES.items():
            self.language_combo.addItem(label, code)
        self.language_combo.setCurrentIndex(self.language_combo.findData(self.language))
        self.language_combo.setFixedHeight(36)
        self.language_combo.currentIndexChanged.connect(self._language_changed)
        side.addWidget(self.language_combo)
        self.edit_controls.append(self.language_combo)
        root.addWidget(sidebar)

        # ---- Main column: scrolling sections + action bar ----------------
        main = QFrame(objectName="main")
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        self.scroll_area = QScrollArea(objectName="mainScroll")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        page = QWidget(objectName="page")
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(36, 26, 36, 30)
        page_layout.setSpacing(10)
        self.section_anchors: dict[str, QWidget] = {}

        def section(key: str, title: str, subtitle: str = "") -> QVBoxLayout:
            anchor = QWidget(objectName="section")
            layout = QVBoxLayout(anchor)
            layout.setContentsMargins(0, 18, 0, 6)
            layout.setSpacing(8)
            layout.addWidget(self._label(title, "pageTitle"))
            if subtitle:
                caption = self._label(subtitle, "subtitle")
                caption.setWordWrap(True)
                layout.addWidget(caption)
            page_layout.addWidget(anchor)
            self.section_anchors[key] = anchor
            return layout

        def group() -> tuple[QFrame, QGridLayout]:
            frame = QFrame(objectName="group")
            grid = QGridLayout(frame)
            grid.setContentsMargins(18, 12, 18, 12)
            grid.setHorizontalSpacing(18)
            grid.setVerticalSpacing(12)
            grid.setColumnStretch(1, 1)
            return frame, grid

        # Overview: live preview + preview modes
        overview = section(
            "overview", "Genel bakış",
            "Sistem, oyun, medya ve kablosuz cihaz bilgilerini tek yerden yönetin",
        )
        preview_card = QFrame(objectName="group")
        preview_layout = QVBoxLayout(preview_card)
        preview_layout.setContentsMargins(18, 14, 18, 14)
        preview_layout.setSpacing(10)
        self.preview_buttons: dict[str, QPushButton] = {}
        self.preview_button_group = QButtonGroup(self)
        self.preview_button_group.setExclusive(True)
        preview_modes = QHBoxLayout()
        preview_modes.setSpacing(6)
        for mode, caption in (
            ("daily", "Günlük"), ("clock", "Saat"), ("weather", "Hava"),
            ("energy", "Elektrik"), ("faceit", "FACEIT"), ("cs2", "CS2 teması"),
            ("msfs", "MSFS"), ("wireless", "Kablosuz"),
        ):
            button = QPushButton(caption)
            button.setObjectName("previewMode")
            button.setFixedHeight(32)
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
        preview_layout.addLayout(preview_modes)
        preview_shell = QFrame(objectName="previewShell")
        preview_shell.setFixedSize(WIDTH + 28, HEIGHT + 28)
        shell_layout = QVBoxLayout(preview_shell)
        shell_layout.setContentsMargins(14, 14, 14, 14)
        self.preview_label = self._label("", "preview")
        self.preview_label.setFixedSize(WIDTH, HEIGHT)
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        shell_layout.addWidget(self.preview_label)
        preview_layout.addWidget(preview_shell, 0, Qt.AlignmentFlag.AlignLeft)
        self.preview_context_label = self._label("Günlük kullanım · medya görünümü", "device")
        preview_layout.addWidget(self.preview_context_label)
        overview.addWidget(preview_card)

        # Display settings (one group: rows never overlap, combos share a column)
        display = section("display", "Ekran", "Mini ekranın parlaklığı, yönü ve tasarımı")
        self.quick_card, screen_grid = group()
        self.quick_card_effect = QGraphicsOpacityEffect(self.quick_card)
        self.quick_card_effect.setOpacity(1.0)
        self.quick_card.setGraphicsEffect(self.quick_card_effect)
        row = 0
        screen_grid.addWidget(self._label("Parlaklık"), row, 0)
        brightness_line = QHBoxLayout()
        brightness_line.setSpacing(10)
        self.brightness_slider = QSlider(Qt.Orientation.Horizontal)
        self.brightness_slider.setRange(5, 100)
        self.brightness_slider.setValue(int(self.settings["brightness"]))
        brightness_line.addWidget(self.brightness_slider, 1)
        self.brightness_value = self._label(f"%{int(self.settings['brightness'])}", "section")
        self.brightness_value.setMinimumWidth(44)
        brightness_line.addWidget(self.brightness_value)
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
            f"color: {MUTED}; border: 1px solid {EDGE}; border-radius: 8px; "
            "background: transparent; font: 700 10px 'Segoe UI';"
        )
        brightness_line.addWidget(self.auto_brightness_info)
        screen_grid.addLayout(brightness_line, row, 1)
        self.brightness_slider.valueChanged.connect(self._sync_brightness_controls)
        self.auto_brightness_check.toggled.connect(self._sync_brightness_controls)
        self.edit_controls.extend((self.auto_brightness_check, self.brightness_slider))
        self._sync_brightness_controls()

        self.rotation_combo = QComboBox()
        self.rotation_combo.addItem("0°", "normal")
        self.rotation_combo.addItem("180°", "180")
        selected_rotation = "180" if self.settings.get("rotation") == "180" else "normal"
        self.rotation_combo.setCurrentIndex(self.rotation_combo.findData(selected_rotation))
        self.rotation_combo.currentIndexChanged.connect(self._rotation_changed)
        self.design_combo = QComboBox()
        for label, value in DESIGNS.items():
            self.design_combo.addItem(self._tr(label), value)
        self.design_combo.setCurrentIndex(
            max(0, self.design_combo.findData(self.settings.get("design", "new")))
        )
        self.design_combo.currentIndexChanged.connect(self._design_changed)
        self.design_combo.setToolTip(
            "Claude: oyuna ve uçuş evresine göre değişen sade ekranlar. Codex: önceki görünüm."
        )
        self.edit_controls.append(self.design_combo)
        self.msfs_layout_combo = QComboBox()
        for label, value in MSFS_LAYOUTS.items():
            self.msfs_layout_combo.addItem(self._tr(label), value)
        current_layout = self.settings.get("msfs_layout", "cockpit")
        self.msfs_layout_combo.setCurrentIndex(self.msfs_layout_combo.findData(current_layout))
        self.msfs_layout_combo.currentIndexChanged.connect(self._msfs_layout_changed)
        self.msfs_layout_combo.setToolTip("MSFS 2024 alt bölümünde kullanılacak görünüm.")
        self.edit_controls.append(self.msfs_layout_combo)
        for caption, combo in (
            ("Ekran yönü", self.rotation_combo),
            ("Tasarım", self.design_combo),
            ("MSFS", self.msfs_layout_combo),
        ):
            row += 1
            combo.setFixedHeight(34)
            combo.setMinimumWidth(220)
            caption_label = self._label(caption)
            if combo is self.msfs_layout_combo:
                self.msfs_layout_caption = caption_label
            screen_grid.addWidget(caption_label, row, 0)
            screen_grid.addWidget(combo, row, 1, Qt.AlignmentFlag.AlignLeft)

        row += 1
        screen_grid.addWidget(self._label("Başlangıç"), row, 0)
        self.autostart_check = QCheckBox("Windows açıldığında çalıştır")
        self.autostart_check.setToolTip("Windows açıldığında uygulamayı ve mini ekranı otomatik başlatır.")
        self.autostart_check.setChecked(bool(self.settings["autostart"]))
        screen_grid.addWidget(self.autostart_check, row, 1, Qt.AlignmentFlag.AlignLeft)
        self.edit_controls.append(self.autostart_check)
        row += 1
        screen_grid.addWidget(self._label("Kilit ekranı"), row, 0)
        self.lock_off_check = QCheckBox("Windows kilitlenince mini ekranı kapat")
        self.lock_off_check.setToolTip(
            "Bilgisayar kilitlenince mini ekranın ışığı kapanır; kilit açılınca geri gelir."
        )
        self.lock_off_check.setChecked(bool(self.settings.get("screen_off_when_locked", True)))
        self.lock_off_check.toggled.connect(self._lock_off_toggled)
        screen_grid.addWidget(self.lock_off_check, row, 1, Qt.AlignmentFlag.AlignLeft)
        # Idle carousel (live settings, usable while the screen runs).
        row += 1
        screen_grid.addWidget(self._label("Boştayken"), row, 0)
        self.carousel_check = QCheckBox("Saat, hava, sistem ve elektrik ekranlarını döndür")
        self.carousel_check.setToolTip(
            "Klavye ve fareye bir süre dokunulmayınca mini ekran bu sayfalar arasında döner. "
            "Oyun ve uçuş ekranlarını etkilemez."
        )
        self.carousel_check.setChecked(bool(self.settings.get("carousel_enabled", True)))
        self.carousel_check.toggled.connect(
            lambda checked: self._set_live_setting("carousel_enabled", bool(checked))
        )
        screen_grid.addWidget(self.carousel_check, row, 1, Qt.AlignmentFlag.AlignLeft)
        row += 1
        screen_grid.addWidget(self._label("Döndürme"), row, 0)
        carousel_line = QHBoxLayout()
        carousel_line.setSpacing(10)
        self.carousel_idle_combo = QComboBox()
        for seconds in CAROUSEL_IDLE_CHOICES:
            label = (
                self._tr("{n} sn dokunulmayınca", n=seconds) if seconds < 60
                else self._tr("{n} dk dokunulmayınca", n=seconds // 60)
            )
            self.carousel_idle_combo.addItem(label, seconds)
        self.carousel_idle_combo.setCurrentIndex(max(0, self.carousel_idle_combo.findData(
            self.settings.get("carousel_idle_seconds", 60))))
        self.carousel_idle_combo.currentIndexChanged.connect(
            lambda _i: self._set_live_setting(
                "carousel_idle_seconds", int(self.carousel_idle_combo.currentData()))
        )
        self.carousel_seconds_combo = QComboBox()
        for seconds in CAROUSEL_SECONDS_CHOICES:
            self.carousel_seconds_combo.addItem(self._tr("{n} sn'de bir", n=seconds), seconds)
        self.carousel_seconds_combo.setCurrentIndex(max(0, self.carousel_seconds_combo.findData(
            self.settings.get("carousel_seconds", 15))))
        self.carousel_seconds_combo.currentIndexChanged.connect(
            lambda _i: self._set_live_setting(
                "carousel_seconds", int(self.carousel_seconds_combo.currentData()))
        )
        for combo in (self.carousel_idle_combo, self.carousel_seconds_combo):
            combo.setFixedHeight(34)
            combo.setMinimumWidth(180)
            carousel_line.addWidget(combo)
        carousel_line.addStretch()
        screen_grid.addLayout(carousel_line, row, 1)
        display.addWidget(self.quick_card)

        # Information shown on the screen: hardware names + weather
        info = section("info", "Bilgiler", "Mini ekranda görünen adlar ve hava durumu şehirleri")
        info_card, info_grid = group()
        info_header = QHBoxLayout()
        info_header.addWidget(self._label("Donanım adları", "section"))
        info_header.addStretch()
        self.hardware_limit_label = self._label(
            self._tr("En fazla {count} karakter", count=MAX_HARDWARE_LABEL_LENGTH), "device"
        )
        self.hardware_limit_label.setProperty("i18n_source", "")
        info_header.addWidget(self.hardware_limit_label)
        info_grid.addLayout(info_header, 0, 0, 1, 2)
        automatic_labels = get_auto_hardware_labels()
        saved_labels = self.settings.get("hardware_labels", {})
        self.hardware_inputs: dict[str, QLineEdit] = {}
        for index, (key, caption) in enumerate((
            ("cpu", "İşlemci"), ("gpu", "Ekran kartı"), ("ram", "Bellek"),
        ), start=1):
            info_grid.addWidget(self._label(caption), index, 0)
            field = QLineEdit()
            field.setMaxLength(MAX_HARDWARE_LABEL_LENGTH)
            field.setFixedHeight(34)
            field.setText(normalise_hardware_label(saved_labels.get(key)))
            field.setPlaceholderText(automatic_labels[key])
            field.textChanged.connect(self._refresh_preview)
            self.hardware_inputs[key] = field
            self.edit_controls.append(field)
            info_grid.addWidget(field, index, 1)
        info_grid.addWidget(self._label("Hava durumu"), 4, 0)
        weather_line = QHBoxLayout()
        weather_line.setSpacing(8)
        saved_cities = list(normalise_city_names(self.settings.get(
            "weather_cities", DEFAULT_WEATHER_CITIES,
        )))
        saved_cities += [""] * (3 - len(saved_cities))
        self.weather_city_inputs: list[QLineEdit] = []
        for index in range(3):
            city_input = QLineEdit()
            city_input.setFixedHeight(34)
            city_input.setMaxLength(24)
            city_input.setText(saved_cities[index])
            city_input.setProperty("city_index", index + 1)
            city_input.setPlaceholderText(self._tr("{index}. şehir", index=index + 1))
            city_input.setToolTip("Hava durumu gösterilecek şehir.")
            city_input.textChanged.connect(self._refresh_preview)
            city_input.editingFinished.connect(
                lambda field=city_input: self._normalise_city_field(field)
            )
            city_input.setMinimumWidth(0)
            self.weather_city_inputs.append(city_input)
            self.edit_controls.append(city_input)
            weather_line.addWidget(city_input, 1)
        info_grid.addLayout(weather_line, 4, 1)
        info.addWidget(info_card)
        self._sync_design_dependent_controls()

        # Electricity estimate (live settings)
        energy = section(
            "energy", "Elektrik",
            "İşlemci ve ekran kartı güç sensörlerinden tahmini tüketim ve maliyet",
        )
        energy_card, energy_grid = group()
        energy_grid.addWidget(self._label("Fiyat"), 0, 0)
        self.energy_mode_combo = QComboBox()
        self.energy_mode_combo.addItem(self._tr("Son faturadan hesapla"), "bill")
        self.energy_mode_combo.addItem(self._tr("Birim fiyatı kendim gireceğim"), "unit")
        self.energy_mode_combo.setCurrentIndex(max(0, self.energy_mode_combo.findData(
            self.settings.get("energy_price_mode", "bill"))))
        self.energy_mode_combo.setFixedHeight(34)
        self.energy_mode_combo.setMinimumWidth(260)
        self.energy_mode_combo.currentIndexChanged.connect(self._energy_mode_changed)
        energy_grid.addWidget(self.energy_mode_combo, 0, 1, Qt.AlignmentFlag.AlignLeft)
        self.energy_inputs: dict[str, QLineEdit] = {}

        def energy_field(key: str, placeholder: str, suffix: str) -> QHBoxLayout:
            line = QHBoxLayout()
            line.setSpacing(6)
            field = QLineEdit()
            field.setFixedHeight(34)
            field.setFixedWidth(130)
            field.setPlaceholderText(self._tr(placeholder))
            field.setProperty("i18n_placeholder", placeholder)
            value = float(self.settings.get(key, DEFAULT_SETTINGS[key]) or 0.0)
            field.setText(_format_number(value, self.language) if value else "")
            field.editingFinished.connect(lambda key=key: self._energy_field_changed(key))
            self.energy_inputs[key] = field
            line.addWidget(field)
            line.addWidget(self._label(suffix, "device"))
            return line

        self.energy_bill_caption = self._label("Son fatura")
        energy_grid.addWidget(self.energy_bill_caption, 1, 0)
        bill_line = QHBoxLayout()
        bill_line.setSpacing(14)
        bill_line.addLayout(energy_field("energy_bill_amount", "Toplam tutar", "TL"))
        bill_line.addLayout(energy_field("energy_bill_kwh", "Tüketim", "kWh"))
        bill_line.addStretch()
        bill_line.setContentsMargins(0, 0, 0, 0)
        self.energy_bill_line = QWidget(objectName="inline")
        self.energy_bill_line.setLayout(bill_line)
        energy_grid.addWidget(self.energy_bill_line, 1, 1)
        self.energy_unit_caption = self._label("Birim fiyat")
        energy_grid.addWidget(self.energy_unit_caption, 2, 0)
        unit_line = energy_field("energy_unit_price", "Örn. 3,24", "TL/kWh")
        unit_line.addStretch()
        unit_line.setContentsMargins(0, 0, 0, 0)
        self.energy_unit_line = QWidget(objectName="inline")
        self.energy_unit_line.setLayout(unit_line)
        energy_grid.addWidget(self.energy_unit_line, 2, 1)
        energy_grid.addWidget(self._label("Hesaplanan"), 3, 0)
        self.energy_price_label = self._label("", "section")
        energy_grid.addWidget(self.energy_price_label, 3, 1)
        energy_grid.addWidget(self._label("Tahmin ayarı"), 4, 0)
        tuning_line = QHBoxLayout()
        tuning_line.setSpacing(14)
        tuning_line.addLayout(energy_field("energy_other_watts", "60", "W diğer parçalar"))
        tuning_line.addLayout(energy_field("energy_psu_efficiency", "90", "% güç kaynağı verimi"))
        tuning_line.addStretch()
        energy_grid.addLayout(tuning_line, 4, 1)
        energy_grid.addWidget(self._label("Tüketim"), 5, 0)
        self.energy_readout_label = self._label("", "section")
        energy_grid.addWidget(self.energy_readout_label, 5, 1)
        energy_note = self._label(
            "Tahmindir: işlemci ve ekran kartı sensörlerine anakart, bellek, disk ve fanlar için "
            "bir pay eklenir ve güç kaynağı verimine bölünür. Monitörler ile uygulamanın kapalı "
            "olduğu süre sayılmaz. En doğru fiyat için son faturanızdaki toplam tutarı ve kWh'i girin.",
            "device",
        )
        energy_note.setWordWrap(True)
        energy_grid.addWidget(energy_note, 6, 0, 1, 2)
        energy.addWidget(energy_card)
        self._sync_energy_controls()

        # Colours
        colours = section("colors", "Renkler", "Halka, zemin ve yazı renklerinden birini seçin.")
        colour_card = QFrame(objectName="group")
        colour_layout = QVBoxLayout(colour_card)
        colour_layout.setContentsMargins(18, 14, 18, 14)
        colour_layout.setSpacing(10)
        palette_line = QHBoxLayout()
        palette_line.setSpacing(10)
        palette_line.addWidget(self._label("Hazır palet"))
        self.palette_combo = QComboBox()
        for name in PALETTES:
            self.palette_combo.addItem(self._tr(name), name)
        for name in sorted(self.settings.get("custom_palettes", {})):
            self.palette_combo.addItem(name, name)
        if self.settings["palette"] not in PALETTES and self.settings["palette"] not in self.settings.get("custom_palettes", {}):
            self.palette_combo.addItem(self._tr("Özel"), "Özel")
        self.palette_combo.setCurrentIndex(self.palette_combo.findData(self.settings["palette"]))
        self.palette_combo.currentIndexChanged.connect(self._palette_changed)
        self.palette_combo.setFixedHeight(34)
        self.palette_combo.setMinimumWidth(220)
        palette_line.addWidget(self.palette_combo)
        palette_line.addStretch()
        self.save_palette_button = QPushButton("Paleti kaydet")
        self.save_palette_button.setToolTip("Geçerli renkleri yeni bir palet olarak kaydeder.")
        self.save_palette_button.clicked.connect(self._save_palette)
        self.delete_palette_button = QPushButton("Sil")
        self.delete_palette_button.setToolTip("Seçili özel renk paletini siler.")
        self.delete_palette_button.clicked.connect(self._delete_palette)
        self.edit_controls.extend((self.palette_combo, self.save_palette_button, self.delete_palette_button))
        palette_line.addWidget(self.save_palette_button)
        palette_line.addWidget(self.delete_palette_button)
        colour_layout.addLayout(palette_line)
        colour_layout.addWidget(self._label("Değiştirmek istediğiniz alanı seçin", "device"))
        color_editor = QHBoxLayout()
        color_editor.setSpacing(12)
        swatch_grid = QGridLayout()
        swatch_grid.setSpacing(8)
        for index, (key, caption) in enumerate((
            ("cpu", "Normal"), ("warning", "Uyarı"),
            ("danger", "Kritik"), ("bg", "Zemin"),
            ("text", "Ana yazı"), ("muted", "İkincil yazı"),
        )):
            button = QPushButton(caption)
            button.setFixedSize(126, 40)
            button.clicked.connect(lambda _checked=False, color_key=key: self._choose_color(color_key))
            self.swatches[key] = button
            self.edit_controls.append(button)
            swatch_grid.addWidget(button, index // 3, index % 3)
        color_editor.addLayout(swatch_grid)
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
            color_grid.addWidget(color_button, index // 6, index % 6)
        chooser_layout.addLayout(color_grid)
        color_editor.addWidget(self.inline_color_panel)
        color_editor.addStretch()
        colour_layout.addLayout(color_editor)
        self.color_help_label = self._label("Halka, zemin ve yazı renklerinden birini seçin.", "device")
        self.color_help_label.setWordWrap(True)
        self.color_help_label.setVisible(False)  # the section subtitle says it
        colour_layout.addWidget(self.color_help_label)
        reset_line = QHBoxLayout()
        reset_line.addStretch()
        self.reset_button = QPushButton("Varsayılana dön")
        self.reset_button.setToolTip("Renkleri ve görünüm ayarlarını başlangıç değerlerine getirir.")
        self.reset_button.clicked.connect(self._reset_defaults)
        self.edit_controls.append(self.reset_button)
        reset_line.addWidget(self.reset_button)
        colour_layout.addLayout(reset_line)
        self._paint_swatches()
        self._update_palette_buttons()
        colours.addWidget(colour_card)

        # Zowie add-on (live: usable while the screen runs)
        zowie = section(
            "zowie", "Zowie monitör",
            "CS2 açılınca monitörü oyun moduna, kapanınca masaüstü moduna alır.",
        )
        zowie_card, zowie_grid = group()
        self.zowie_check = QCheckBox("CS2'de otomatik mod")
        self.zowie_check.setToolTip(
            "CS2 açılınca monitörü oyun moduna, kapanınca masaüstü moduna alır."
        )
        self.zowie_check.setChecked(bool(self.settings.get("zowie_auto_mode")))
        self.zowie_check.toggled.connect(self._zowie_toggled)
        zowie_grid.addWidget(self._label("Otomatik mod"), 0, 0)
        zowie_grid.addWidget(self.zowie_check, 0, 1, Qt.AlignmentFlag.AlignLeft)
        zowie_grid.addWidget(self._label("Monitör modu"), 1, 0)
        self.zowie_status_label = self._label("", "section")
        zowie_grid.addWidget(self.zowie_status_label, 1, 1)
        zowie.addWidget(zowie_card)

        # FACEIT card (live: usable while the screen runs)
        faceit = section(
            "faceit", "FACEIT kartı",
            "ELO, seviye ve son maçların; yalnızca FACEIT'in resmi web API'sinden okunur, oyuna dokunulmaz.",
        )
        faceit_card, faceit_grid = group()
        faceit_grid.addWidget(self._label("Kullanıcı adı"), 0, 0)
        self.faceit_nickname_input = QLineEdit()
        self.faceit_nickname_input.setFixedHeight(34)
        self.faceit_nickname_input.setMaxLength(32)
        self.faceit_nickname_input.setMinimumWidth(260)
        self.faceit_nickname_input.setPlaceholderText(self._tr("FACEIT kullanıcı adın"))
        self.faceit_nickname_input.setText(str(self.settings.get("faceit_nickname", "")))
        faceit_grid.addWidget(self.faceit_nickname_input, 0, 1, Qt.AlignmentFlag.AlignLeft)
        faceit_grid.addWidget(self._label("API anahtarı"), 1, 0)
        key_line = QHBoxLayout()
        key_line.setSpacing(10)
        self.faceit_key_input = QLineEdit()
        self.faceit_key_input.setFixedHeight(34)
        self.faceit_key_input.setMinimumWidth(260)
        self.faceit_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.faceit_key_input.setMaxLength(128)
        key_line.addWidget(self.faceit_key_input)
        self.faceit_help_button = QPushButton("Anahtar nasıl alınır?")
        self.faceit_help_button.clicked.connect(
            lambda _checked=False: QDesktopServices.openUrl(QUrl(FACEIT_DEVELOPERS_URL))
        )
        key_line.addWidget(self.faceit_help_button)
        key_line.addStretch()
        faceit_grid.addLayout(key_line, 1, 1)
        faceit_grid.addWidget(self._label("Durum"), 2, 0)
        status_line = QHBoxLayout()
        status_line.setSpacing(10)
        self.faceit_status_label = self._label("", "section")
        status_line.addWidget(self.faceit_status_label, 1)
        self.faceit_save_button = QPushButton("Kaydet ve doğrula", objectName="accent")
        self.faceit_save_button.clicked.connect(self._faceit_save_and_verify)
        status_line.addWidget(self.faceit_save_button)
        faceit_grid.addLayout(status_line, 2, 1)
        faceit_note = self._label(
            "Anahtar Windows'un şifrelemesiyle (DPAPI) yalnızca bu Windows hesabına özel saklanır.",
            "device",
        )
        faceit_note.setWordWrap(True)
        faceit_grid.addWidget(faceit_note, 3, 0, 1, 2)
        faceit.addWidget(faceit_card)

        # About + updates
        about = section("about", "Hakkında")
        about_card, about_grid = group()
        about_grid.addWidget(self._label("Sürüm"), 0, 0)
        about_version = self._label(f"{APP_VERSION}", "section")
        about_version.setProperty("i18n_source", "")
        about_grid.addWidget(about_version, 0, 1)
        about_grid.addWidget(self._label("Güncellemeler"), 1, 0)
        update_line = QHBoxLayout()
        update_line.setSpacing(10)
        self.update_status_label = self._label("", "device")
        update_line.addWidget(self.update_status_label, 1)
        self.update_open_button = QPushButton("İndirme sayfasını aç")
        self.update_open_button.setVisible(False)
        self.update_open_button.clicked.connect(self._open_update_page)
        update_line.addWidget(self.update_open_button)
        self.update_check_button = QPushButton("Şimdi denetle")
        self.update_check_button.clicked.connect(lambda _checked=False: self._check_updates(manual=True))
        update_line.addWidget(self.update_check_button)
        about_grid.addLayout(update_line, 1, 1)
        self.update_auto_check = QCheckBox("Yeni sürümleri otomatik denetle")
        self.update_auto_check.setToolTip(
            "Günde bir kez GitHub'daki sürüm listesine bakar. Hiçbir şeyi kendiliğinden indirmez."
        )
        self.update_auto_check.setChecked(bool(self.settings.get("update_check", True)))
        self.update_auto_check.toggled.connect(self._update_check_toggled)
        about_grid.addWidget(self.update_auto_check, 2, 1, Qt.AlignmentFlag.AlignLeft)
        about.addWidget(about_card)
        page_layout.addStretch()

        self.scroll_area.setWidget(page)
        self.scroll_area.verticalScrollBar().valueChanged.connect(self._sync_nav_with_scroll)
        main_layout.addWidget(self.scroll_area, 1)

        # ---- Action bar ----------------------------------------------------
        action_bar = QFrame(objectName="actionBar")
        action_layout = QHBoxLayout(action_bar)
        action_layout.setContentsMargins(24, 12, 24, 12)
        action_layout.setSpacing(10)
        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet("color: #6F7C90; background: transparent;")
        self.status_label = self._label("Ekran hazır", "status")
        action_layout.addWidget(self.status_dot)
        action_layout.addWidget(self.status_label, 1)
        self.save_button = QPushButton("Yalnızca kaydet")
        self.save_button.clicked.connect(self.save_settings)
        self.stop_button = QPushButton("Durdur")
        self.stop_button.setToolTip("Veri akışını durdurur ve mini ekranı temizler.")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_screen)
        self.start_button = QPushButton("Kaydet ve başlat", objectName="accent")
        self.start_button.setToolTip("Ayarları kaydeder ve mini ekrana görüntü göndermeye başlar.")
        self.start_button.clicked.connect(self.start_screen)
        self.edit_controls.extend((self.save_button, self.start_button))
        action_layout.addWidget(self.save_button)
        action_layout.addWidget(self.stop_button)
        action_layout.addWidget(self.start_button)
        main_layout.addWidget(action_bar)
        root.addWidget(main, 1)
        # Mouse clicks should not leave the native dotted focus rectangle.
        # Keyboard navigation still receives focus through Tab.
        for button in self.findChildren(QPushButton):
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)

    def _theme_changed(self, _index: int) -> None:
        """Live theme switch (not an edit control: works while the screen runs)."""
        theme = str(self.theme_combo.currentData() or DEFAULT_UI_THEME)
        if theme == self.ui_theme:
            return
        self.ui_theme = theme
        apply_ui_theme(theme)
        self.setStyleSheet(self._style_sheet())
        for key, _caption, icon in NAV_SECTIONS:
            self.nav_buttons[key].setIcon(_tinted_svg_icon(icon, MUTED))
        self.auto_brightness_info.setStyleSheet(
            f"color: {MUTED}; border: 1px solid {EDGE}; border-radius: 8px; "
            "background: transparent; font: 700 10px 'Segoe UI';"
        )
        self._paint_swatches()
        self._refresh_zowie_status(force=True)
        self.settings = {**self.settings, "ui_theme": theme}
        try:
            save_settings_file(self.settings)
        except Exception as exc:
            _runtime_log(f"theme save failed: {type(exc).__name__}: {exc}")

    def _scroll_to_section(self, key: str) -> None:
        anchor = self.section_anchors.get(key)
        if anchor is None:
            return
        self._nav_scrolling = True
        self.scroll_area.verticalScrollBar().setValue(anchor.y())
        self._nav_scrolling = False
        self.nav_buttons[key].setChecked(True)

    def _sync_nav_with_scroll(self, value: int) -> None:
        if getattr(self, "_nav_scrolling", False):
            return
        bar = self.scroll_area.verticalScrollBar()
        current = NAV_SECTIONS[0][0]
        if value >= bar.maximum() - 4:
            current = NAV_SECTIONS[-1][0]
        else:
            for key, _caption, _icon in NAV_SECTIONS:
                if self.section_anchors[key].y() <= value + 40:
                    current = key
        if not self.nav_buttons[current].isChecked():
            self.nav_buttons[current].setChecked(True)

    # Labels whose text is rewritten at runtime. Restoring their construction
    # text on a language change would show stale values (e.g. "Ekran hazır"
    # while the screen is running, or an old brightness percentage).
    DYNAMIC_LABEL_ATTRIBUTES = (
        "status_label", "brightness_value", "zowie_status_label", "update_status_label",
        "energy_price_label", "energy_readout_label", "faceit_status_label",
    )

    def _register_static_translations(self) -> None:
        dynamic = {id(getattr(self, name)) for name in self.DYNAMIC_LABEL_ATTRIBUTES if hasattr(self, name)}
        for name in self.DYNAMIC_LABEL_ATTRIBUTES:
            if hasattr(self, name):
                getattr(self, name).setProperty("i18n_source", "")
        for widget_type in (QLabel, QPushButton, QCheckBox, QRadioButton):
            for widget in self.findChildren(widget_type):
                if id(widget) in dynamic:
                    continue
                if widget.property("i18n_source") is None and widget.text():
                    widget.setProperty("i18n_source", widget.text())
        for widget_type in (QLabel, QPushButton, QCheckBox, QRadioButton, QComboBox, QLineEdit, QSlider):
            for widget in self.findChildren(widget_type):
                if widget.toolTip() and widget.property("i18n_tooltip") is None:
                    widget.setProperty("i18n_tooltip", widget.toolTip())

    def _retranslate_combo_items(self) -> None:
        for combo, labels in (
            (self.carousel_idle_combo, {
                seconds: (self._tr("{n} sn dokunulmayınca", n=seconds) if seconds < 60
                          else self._tr("{n} dk dokunulmayınca", n=seconds // 60))
                for seconds in CAROUSEL_IDLE_CHOICES
            }),
            (self.carousel_seconds_combo, {
                seconds: self._tr("{n} sn'de bir", n=seconds) for seconds in CAROUSEL_SECONDS_CHOICES
            }),
            (self.energy_mode_combo, {
                "bill": self._tr("Son faturadan hesapla"),
                "unit": self._tr("Birim fiyatı kendim gireceğim"),
            }),
            (self.theme_combo, {value: self._tr(label) for label, value in UI_THEME_NAMES.items()}),
        ):
            for index in range(combo.count()):
                combo.setItemText(index, labels.get(combo.itemData(index), combo.itemText(index)))
        selected_palette = self._selected_palette_name()
        self._rebuild_palette_combo(selected_palette)
        selected_layout = str(self.msfs_layout_combo.currentData() or "cockpit")
        self.msfs_layout_combo.blockSignals(True)
        self.msfs_layout_combo.clear()
        for label, value in MSFS_LAYOUTS.items():
            self.msfs_layout_combo.addItem(self._tr(label), value)
        self.msfs_layout_combo.setCurrentIndex(self.msfs_layout_combo.findData(selected_layout))
        self.msfs_layout_combo.blockSignals(False)
        selected_design = str(self.design_combo.currentData() or "new")
        self.design_combo.blockSignals(True)
        self.design_combo.clear()
        for label, value in DESIGNS.items():
            self.design_combo.addItem(self._tr(label), value)
        self.design_combo.setCurrentIndex(max(0, self.design_combo.findData(selected_design)))
        self.design_combo.blockSignals(False)
        self._sync_design_dependent_controls()

    def _apply_language(self, refresh_preview: bool = True) -> None:
        self.setWindowTitle(self._window_title())
        for widget_type in (QLabel, QPushButton, QCheckBox, QRadioButton, QComboBox, QLineEdit, QSlider):
            for widget in self.findChildren(widget_type):
                if widget_type in (QLabel, QPushButton, QCheckBox, QRadioButton):
                    source = widget.property("i18n_source")
                    if source:
                        widget.setText(self._tr(str(source)))
                tooltip = widget.property("i18n_tooltip")
                if tooltip:
                    widget.setToolTip(self._tr(str(tooltip)))
        self._sync_brightness_controls()
        if hasattr(self, "zowie_monitor"):
            self._refresh_zowie_status()
        if hasattr(self, "faceit_monitor"):
            self._sync_faceit_controls()
        if hasattr(self, "energy_mode_combo"):
            for field in self.energy_inputs.values():
                field.setPlaceholderText(self._tr(str(field.property("i18n_placeholder") or "")))
            self._sync_energy_controls()
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
            self.tray_zowie_action.setText(self._zowie_action_text())
            self.tray_zowie_action.setToolTip(self._tr(
                "CS2 açılınca monitörü oyun moduna, kapanınca masaüstü moduna alır."
            ))
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
        self.tray_zowie_action = QAction(self._zowie_action_text(), self)
        self.tray_zowie_action.setCheckable(True)
        self.tray_zowie_action.setChecked(bool(self.settings.get("zowie_auto_mode")))
        self.tray_zowie_action.setToolTip(self._tr(
            "CS2 açılınca monitörü oyun moduna, kapanınca masaüstü moduna alır."
        ))
        self.tray_zowie_action.toggled.connect(self._zowie_toggled)
        menu.addAction(self.tray_zowie_action)
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

    def _zowie_action_text(self) -> str:
        return self._tr(
            "Zowie eklentisi · CS2'de {game}, sonra {desktop}",
            game=ZOWIE_MODE_LABELS[normalise_zowie_mode(
                self.settings.get("zowie_game_mode"), ZOWIE_DEFAULT_GAME_MODE,
            )],
            desktop=ZOWIE_MODE_LABELS[normalise_zowie_mode(
                self.settings.get("zowie_desktop_mode"), ZOWIE_DEFAULT_DESKTOP_MODE,
            )],
        )

    def _configure_zowie_monitor(self) -> None:
        self.zowie_monitor.configure(
            bool(self.settings.get("zowie_auto_mode")),
            str(self.settings.get("zowie_game_mode", ZOWIE_DEFAULT_GAME_MODE)),
            str(self.settings.get("zowie_desktop_mode", ZOWIE_DEFAULT_DESKTOP_MODE)),
        )

    def start_zowie_monitor(self) -> None:
        self.zowie_monitor.start()

    # ---- Lock screen: blank the LCD while Windows is locked ---------------
    WM_WTSSESSION_CHANGE = 0x02B1
    WTS_SESSION_LOCK, WTS_SESSION_UNLOCK = 0x7, 0x8

    def register_session_notifications(self) -> None:
        """Ask Windows for lock/unlock messages (real launches only)."""
        if os.name != "nt" or self._session_notifications:
            return
        try:
            hwnd = int(self.winId())
            if ctypes.WinDLL("wtsapi32").WTSRegisterSessionNotification(ctypes.c_void_p(hwnd), 0):
                self._session_notifications = True
        except Exception as exc:
            _runtime_log(f"session notifications unavailable: {type(exc).__name__}: {exc}")

    def unregister_session_notifications(self) -> None:
        if not self._session_notifications:
            return
        self._session_notifications = False
        try:
            ctypes.WinDLL("wtsapi32").WTSUnRegisterSessionNotification(ctypes.c_void_p(int(self.winId())))
        except Exception:
            pass

    def nativeEvent(self, event_type, message):  # noqa: N802 - Qt override
        if event_type == b"windows_generic_MSG" or event_type == "windows_generic_MSG":
            try:
                msg = ctypes.wintypes.MSG.from_address(int(message))
                if msg.message == self.WM_WTSSESSION_CHANGE:
                    self._session_changed(int(msg.wParam))
            except Exception:
                pass
        return super().nativeEvent(event_type, message)

    def _session_changed(self, code: int) -> None:
        if code == self.WTS_SESSION_LOCK:
            self._windows_locked = True
        elif code == self.WTS_SESSION_UNLOCK:
            self._windows_locked = False
        else:
            return
        self._apply_display_suspension()

    def _apply_display_suspension(self) -> None:
        suspend = self._windows_locked and bool(self.settings.get("screen_off_when_locked", True))
        if suspend and not self.display_suspended.is_set():
            self.display_suspended.set()
            _runtime_log("windows locked; mini display off")
        elif not suspend and self.display_suspended.is_set():
            self.display_suspended.clear()
            _runtime_log("mini display resumed")

    # ---- Live settings (saved at once; usable while the screen runs) -------
    def _set_live_setting(self, key: str, value) -> None:
        if self.settings.get(key) == value:
            return
        self.settings = {**self.settings, key: value}
        try:
            save_settings_file(self.settings)
        except Exception as exc:
            _runtime_log(f"setting save failed; key={key}: {type(exc).__name__}: {exc}")

    def _sync_energy_controls(self) -> None:
        bill = self.energy_mode_combo.currentData() == "bill"
        for widget in (self.energy_bill_caption, self.energy_bill_line):
            widget.setVisible(bill)
        for widget in (self.energy_unit_caption, self.energy_unit_line):
            widget.setVisible(not bill)
        price = price_per_kwh(self.settings)
        self.energy_price_label.setText(
            self._tr("{price} TL/kWh", price=_format_number(price, self.language, 3)) if price
            else self._tr("Fiyat girilmedi")
        )
        self._refresh_energy_readout()

    def _energy_mode_changed(self, _index: int) -> None:
        self._set_live_setting("energy_price_mode", str(self.energy_mode_combo.currentData()))
        self._sync_energy_controls()

    def _energy_field_changed(self, key: str) -> None:
        field = self.energy_inputs[key]
        value = _parse_number(field.text())
        low, high = ENERGY_LIMITS[key]
        if value is None:
            value = float(self.settings.get(key, DEFAULT_SETTINGS[key]) or 0.0)
        value = max(low, min(high, value)) if value else (0.0 if low == 0 else DEFAULT_SETTINGS[key])
        field.setText(_format_number(value, self.language) if value else "")
        self._set_live_setting(key, value)
        self._sync_energy_controls()

    def _refresh_energy_readout(self) -> None:
        if not hasattr(self, "energy_readout_label"):
            return
        if not self.energy_meter.running:
            text = self._tr("Ölçüm uygulama açıldığında başlar")
        else:
            summary = self.energy_meter.summary()
            watts = "—" if summary.watts_now is None else f"{int(round(summary.watts_now))} W"
            parts = [
                self._tr("Şu an {watts}", watts=watts),
                self._tr("bugün {kwh} kWh", kwh=_format_number(summary.today_kwh, self.language)),
                self._tr("bu ay {kwh} kWh", kwh=_format_number(summary.month_kwh, self.language)),
            ]
            if summary.month_cost is not None:
                parts.append(self._tr("≈ {cost} TL", cost=_format_number(summary.month_cost, self.language)))
            text = " · ".join(parts)
        if self.energy_readout_label.text() != text:
            self.energy_readout_label.setText(text)

    # ---- FACEIT ---------------------------------------------------------------
    def _sync_faceit_controls(self) -> None:
        if not hasattr(self, "faceit_key_input"):
            return
        has_key = bool(self.settings.get("faceit_key_protected"))
        self.faceit_key_input.setPlaceholderText(self._tr(
            "Kayıtlı (değiştirmek için yenisini yapıştır)" if has_key else "Server side API anahtarı"
        ))
        self.faceit_nickname_input.setPlaceholderText(self._tr("FACEIT kullanıcı adın"))
        self._show_faceit_status(self.faceit_monitor.snapshot())

    def _show_faceit_status(self, snapshot: FaceitSnapshot) -> None:
        if snapshot.ready:
            text = self._tr(
                "Bağlandı: {name} · Seviye {level} · {elo} ELO",
                name=snapshot.nickname, level=snapshot.level or "—", elo=snapshot.elo,
            )
            color = ACCENT
            if snapshot.error:
                text += f" · {self._tr(snapshot.error)}"
        elif snapshot.error:
            text, color = self._tr(snapshot.error), "#E3B866"
        else:
            text, color = self._tr("Ayarlanmadı"), MUTED
        self.faceit_status_label.setText(text)
        self.faceit_status_label.setStyleSheet(f"color: {color};")

    def _faceit_save_and_verify(self, _checked: bool = False) -> None:
        nickname = self.faceit_nickname_input.text().strip()[:32]
        new_key = self.faceit_key_input.text().strip()
        updates = {"faceit_nickname": nickname}
        if new_key:
            try:
                updates["faceit_key_protected"] = protect_secret(new_key)
            except OSError as exc:
                _runtime_log(f"faceit key protect failed: {type(exc).__name__}")
                self.faceit_status_label.setText(self._tr("FACEIT'e bağlanılamadı"))
                return
        # The plain key never stays in the widget or in memory longer than needed.
        self.faceit_key_input.clear()
        self.settings = {**self.settings, **updates}
        try:
            save_settings_file(self.settings)
        except Exception as exc:
            _runtime_log(f"faceit setting save failed: {type(exc).__name__}: {exc}")
        self._sync_faceit_controls()
        self.faceit_save_button.setEnabled(False)
        self.faceit_status_label.setText(self._tr("Doğrulanıyor…"))
        monitor = self.faceit_monitor
        threading.Thread(
            target=lambda: self.signals.faceit_result.emit(monitor.refresh()),
            name="faceit-verify", daemon=True,
        ).start()

    def _faceit_result(self, snapshot: FaceitSnapshot) -> None:
        self.faceit_save_button.setEnabled(True)
        self._show_faceit_status(snapshot)
        if snapshot.ready:
            _runtime_log(f"faceit connected; level={snapshot.level}")

    def _lock_off_toggled(self, checked: bool) -> None:
        # Live setting (not an edit control): saved at once, works while running.
        self.settings = {**self.settings, "screen_off_when_locked": bool(checked)}
        self._apply_display_suspension()
        try:
            save_settings_file(self.settings)
        except Exception as exc:
            _runtime_log(f"lock setting save failed: {type(exc).__name__}: {exc}")

    # ---- Update check (read-only; never downloads) -------------------------
    def start_update_checks(self) -> None:
        """Real launches only: first look shortly after start, then daily."""
        self.update_timer = QTimer(self)
        self.update_timer.timeout.connect(lambda: self._check_updates(manual=False))
        self.update_timer.start(UPDATE_CHECK_INTERVAL_SECONDS * 1000)
        QTimer.singleShot(45_000, lambda: self._check_updates(manual=False))

    def _update_check_toggled(self, checked: bool) -> None:
        self.settings = {**self.settings, "update_check": bool(checked)}
        try:
            save_settings_file(self.settings)
        except Exception as exc:
            _runtime_log(f"update setting save failed: {type(exc).__name__}: {exc}")

    def _check_updates(self, manual: bool) -> None:
        if self._update_check_running:
            return
        if not manual and not self.settings.get("update_check", True):
            return
        self._update_check_running = True
        self.update_check_button.setEnabled(False)
        self.update_status_label.setText(self._tr("Denetleniyor…"))

        def work() -> None:
            try:
                info = fetch_latest_release(APP_VERSION)
                self.signals.update_result.emit(info, "", manual)
            except Exception as exc:
                self.signals.update_result.emit(None, f"{type(exc).__name__}: {exc}", manual)

        threading.Thread(target=work, name="update-check", daemon=True).start()

    def _update_result(self, info: UpdateInfo | None, error: str, manual: bool) -> None:
        self._update_check_running = False
        self.update_check_button.setEnabled(True)
        if info is None:
            _runtime_log(f"update check failed: {error}")
            self.update_status_label.setText(self._tr("Denetlenemedi · internet bağlantısını kontrol edin"))
            return
        self.update_info = info
        if not info.newer:
            self.update_status_label.setText(self._tr("Güncel · en son sürüm {version}", version=info.latest))
            self.update_open_button.setVisible(False)
            return
        self.update_status_label.setText(self._tr("Yeni sürüm var: {version}", version=info.latest))
        self.update_open_button.setVisible(True)
        _runtime_log(f"update available; latest={info.latest}")
        if not manual and self.tray_icon is not None and self._update_notified != info.latest:
            self._update_notified = info.latest
            self.tray_icon.showMessage(
                self._tr(APP_DISPLAY_NAME),
                self._tr("Yeni sürüm var: {version}", version=info.latest),
                QSystemTrayIcon.MessageIcon.Information, 6000,
            )

    def _open_update_page(self) -> None:
        info = self.update_info
        if info is not None and is_trusted_release_url(info.url):
            QDesktopServices.openUrl(QUrl(info.url))

    def _refresh_zowie_status(self, force: bool = False) -> None:
        """Show the monitor's current mode; DDC reads only while the window is visible."""
        monitor = self.zowie_monitor
        visible = self.isVisible() and not self.isMinimized()
        if visible and not monitor.status_wanted:
            monitor._status_read_at = -1e9  # read at once when the window opens
        monitor.status_wanted = visible
        if not hasattr(self, "zowie_status_label"):
            return
        if monitor.monitor_found is False:
            text, color = self._tr("Monitör bulunamadı"), "#E3B866"
        elif monitor.current_mode:
            label = ZOWIE_MODE_LABELS.get(monitor.current_mode, monitor.current_mode)
            text, color = self._tr("Şu an: {mode}", mode=label), ACCENT
        else:
            text, color = self._tr("Mod okunuyor…"), "#8C97A8"
        if force or self.zowie_status_label.text() != text:
            self.zowie_status_label.setText(text)
            self.zowie_status_label.setStyleSheet(f"color: {color};")

    def _zowie_toggled(self, checked: bool) -> None:
        # Saved straight away (like rotation): the window checkbox and the
        # tray entry work even while the form is locked by a running screen.
        checked = bool(checked)
        if checked == bool(self.settings.get("zowie_auto_mode")):
            return
        for control in (getattr(self, "zowie_check", None), getattr(self, "tray_zowie_action", None)):
            if control is not None and control.isChecked() != checked:
                control.blockSignals(True)
                control.setChecked(checked)
                control.blockSignals(False)
        self.settings = {**self.settings, "zowie_auto_mode": checked}
        self._configure_zowie_monitor()
        self._refresh_zowie_status()
        try:
            save_settings_file(self.settings)
        except Exception as exc:
            _runtime_log(f"zowie setting save failed: {type(exc).__name__}: {exc}")
        _runtime_log(f"zowie add-on {'on' if checked else 'off'}")

    def _show_from_tray(self) -> None:
        self.showNormal()
        self.ensurePolished()
        self.update()
        self.raise_()
        self.activateWindow()

    def _exit_application(self) -> None:
        self._exit_requested = True
        self.zowie_monitor.stop(timeout=1.0)
        self.energy_meter.stop(timeout=1.0)
        self.faceit_monitor.stop(timeout=1.0)
        self.unregister_session_notifications()
        if self.tray_icon is not None:
            self.tray_icon.hide()
        if self.worker and self.worker.is_alive():
            self.stop_screen()
            self._arm_exit_fallback()
            return
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _arm_exit_fallback(self) -> None:
        """Guarantee exit even if a native USB call keeps the worker blocked."""
        if self._exit_fallback_armed or self._smoke_test:
            return
        self._exit_fallback_armed = True

        def force_exit() -> None:
            worker = self.worker
            if worker is not None and worker.is_alive():
                _runtime_log("exit fallback: worker did not stop within 5 s; forcing exit")
                _cleanup_instance_state()
                shutdown_child_processes()
                os._exit(0)

        QTimer.singleShot(EXIT_FALLBACK_MS, force_exit)

    def prepare_system_shutdown(self) -> None:
        """Synchronously blank the LCD while Windows is ending the session."""
        if self._session_shutdown_started:
            return
        self._session_shutdown_started = True
        self._exit_requested = True
        if self.tray_icon is not None:
            self.tray_icon.hide()
        self.zowie_monitor.stop(timeout=1.0)
        self.energy_meter.stop(timeout=1.0)
        self.faceit_monitor.stop(timeout=1.0)
        self.unregister_session_notifications()
        self.stop_event.set()
        worker = self.worker
        if worker and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=4.0)
        if worker and worker.is_alive():
            _runtime_log("system shutdown cleanup timed out")
        else:
            _runtime_log("system shutdown cleanup completed")

    def _is_classic_design(self) -> bool:
        return str(self.design_combo.currentData() or "new") == "classic"

    def _sync_design_dependent_controls(self) -> None:
        classic = self._is_classic_design()
        # The idle carousel pages exist only in the Claude design.
        for mode in ("clock", "energy", "faceit"):
            button = getattr(self, "preview_buttons", {}).get(mode)
            if button is not None:
                button.setVisible(not classic)
                if classic and self.preview_mode == mode:
                    self.preview_mode = "daily"
                    self.preview_buttons["daily"].setChecked(True)
        if hasattr(self, "msfs_layout_caption"):
            self.msfs_layout_caption.setVisible(classic)
            self.msfs_layout_combo.setVisible(classic)
        for index, field in enumerate(getattr(self, "weather_city_inputs", ())):
            field.setVisible(classic or index == 0)

    def _design_changed(self, _index: int) -> None:
        self._sync_design_dependent_controls()
        self._refresh_preview()

    def _rotation_changed(self, _index: int) -> None:
        # Rotation stays editable while the screen runs; the worker re-reads
        # self.settings every cycle and reconfigures the panel.
        if self.worker and self.worker.is_alive():
            self.settings = {**self.settings, "rotation": self._rotation()}
            try:
                save_settings_file(self.settings)
            except Exception as exc:
                _runtime_log(f"rotation save failed: {type(exc).__name__}: {exc}")
            _runtime_log(f"rotation changed while running; rotation={self._rotation()}")
        self._refresh_preview()

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
            for index, city in enumerate(weather_cities_for_design({
                "design": str(self.design_combo.currentData() or "new"),
                "weather_cities": self._weather_city_names(),
            }))
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
            "design": str(self.design_combo.currentData() or "new"),
            "weather_cities": list(self._weather_city_names()),
            "hardware_labels": {
                key: normalise_hardware_label(field.text())
                for key, field in self.hardware_inputs.items()
            },
            "custom_palettes": {
                name: dict(colors) for name, colors in self.settings.get("custom_palettes", {}).items()
            },
            # Zowie add-on is toggled from the tray, not from the form.
            "zowie_auto_mode": bool(self.settings.get("zowie_auto_mode", False)),
            "zowie_game_mode": self.settings.get("zowie_game_mode", ZOWIE_DEFAULT_GAME_MODE),
            "zowie_desktop_mode": self.settings.get("zowie_desktop_mode", ZOWIE_DEFAULT_DESKTOP_MODE),
            "screen_off_when_locked": self.lock_off_check.isChecked(),
            "update_check": self.update_auto_check.isChecked(),
            "ui_theme": self.ui_theme,
            # Live settings (saved as they change) are carried over unchanged.
            **{key: self.settings.get(key, DEFAULT_SETTINGS[key]) for key in (
                "carousel_enabled", "carousel_seconds", "carousel_idle_seconds",
                "energy_price_mode", *ENERGY_LIMITS,
                "faceit_nickname", "faceit_key_protected",
            )},
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
        if mode not in {"daily", "clock", "energy", "faceit", "cs2", "msfs", "weather", "wireless"}:
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
        self.design_combo.setCurrentIndex(self.design_combo.findData("new"))
        self._sync_design_dependent_controls()
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
                "cs2": "CS2 açıkken · turuncu tema",
                "msfs": "MSFS 2024 · canlı uçuş bilgileri",
                "weather": "Boşta kullanım · seçili şehirlerin hava durumu",
                "wireless": "Kablosuz cihazlar · pil durumları",
            }
            if str(self.design_combo.currentData() or "new") == "new":
                new_labels = {
                    "daily": "Yeni tasarım · masaüstü",
                    "clock": "Boştayken dönen ekran · saat",
                    "weather": "Boştayken dönen ekran · hava durumu",
                    "energy": "Boştayken dönen ekran · elektrik",
                    "faceit": "Boştayken dönen ekran · FACEIT",
                    "wireless": "Yeni tasarım · masaüstü",
                    "cs2": "Yeni tasarım · CS2 açıkken turuncu tema",
                    "msfs": "Yeni tasarım · MSFS seyir",
                }
                self.preview_context_label.setText(
                    self._tr(new_labels.get(self.preview_mode, "Ekran önizlemesi"))
                )
                self._show_preview(pil_to_qimage(self._new_design_preview(metrics, labels)))
                return
            self.preview_context_label.setText(
                self._tr(context_labels.get(self.preview_mode, "Ekran önizlemesi"))
            )
            if self.preview_mode == "msfs":
                frame = render_screen(
                    metrics, self.settings["colors"],
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
            if self.preview_mode == "cs2":
                frame = render_screen(
                    metrics, self.settings["colors"],
                    hardware_labels=labels,
                    weather_readings=self._preview_weather_readings(),
                    language=self.language,
                    cs2_active=True,
                )
            elif self.preview_mode == "weather":
                frame = render_screen(
                    metrics, self.settings["colors"],
                    hardware_labels=labels,
                    weather_readings=self._preview_weather_readings(),
                    language=self.language,
                )
            elif self.preview_mode == "wireless":
                frame = render_screen(
                    metrics, self.settings["colors"],
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
                    metrics, self.settings["colors"],
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

    def _new_design_preview(self, metrics: dict, labels: dict):
        batteries = (
            BatteryDevice("preview-mouse", "PRO X SUPERLIGHT 2", 82, "mouse"),
            BatteryDevice("preview-keyboard", self._tr("Kablosuz Klavye"), 64, "keyboard"),
            BatteryDevice("preview-headset", "PRO X WIRELESS", 18, "headset"),
        )
        preview_metrics = {**metrics, "cpu_power": 71.0, "gpu_power": 212.0}
        common = dict(
            metrics=preview_metrics, batteries=batteries, language=self.language,
            hardware_labels={"cpu": labels.get("cpu", ""), "gpu": labels.get("gpu", "")},
        )
        mode = self.preview_mode
        if mode == "cs2":
            context = NewScreenContext(
                **common, mode="cs2", weather=self._preview_weather_readings(),
            )
        elif mode == "msfs":
            context = NewScreenContext(
                **common, mode="msfs",
                flight=FlightTelemetry(
                    connected=True, updated_at=1.0, on_ground=False, altitude_ft=36000.0,
                    ground_speed_kt=438.0, vertical_speed_fpm=0.0, headwind_kt=42.0,
                    next_wp_id="TOMBI", next_wp_distance_nm=84.0, destination_ete_s=5100.0,
                    fuel_lbs=18000.0,
                ),
                flight_state=FlightPhaseState(
                    phase="cruise", route_total_nm=1450.0, route_remaining_nm=612.0,
                    fuel_at_destination_lbs=6830.0, fuel_flow_pph=5400.0,
                ),
            )
        elif mode in {"clock", "weather", "energy", "faceit"}:
            faceit = self.faceit_monitor.snapshot()
            if not faceit.ready:  # sample card until the user's own data arrives
                faceit = FaceitSnapshot(
                    "Sabasakal", 7, 1654, 25,
                    tuple(FaceitMatch(won) for won in (True, True, False, True, False)), 1.15, 46.6,
                )
            context = NewScreenContext(
                **common, weather=self._preview_weather_readings(), page=mode,
                energy=EnergySummary(
                    412.0, 1.84, 41.3, price_per_kwh(self.settings) or 3.24,
                ),
                faceit=faceit,
            )
        else:
            context = NewScreenContext(**common, weather=self._preview_weather_readings())
        return render_new_screen(context)

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

    def _apply_autostart_async(self, enabled: bool) -> None:
        """Run schtasks.exe on a worker thread; it can take seconds."""
        if self._applied_autostart == enabled:
            return
        self._applied_autostart = enabled
        signals = self.signals

        def apply() -> None:
            try:
                set_windows_autostart(enabled)
                signals.autostart_result.emit(True, "")
            except Exception as exc:
                _runtime_log(f"autostart update failed: {type(exc).__name__}: {exc}")
                signals.autostart_result.emit(False, str(exc))

        self._autostart_thread = threading.Thread(target=apply, name="autostart", daemon=True)
        self._autostart_thread.start()

    def _autostart_result(self, ok: bool, error: str) -> None:
        if ok:
            return
        # Allow a retry with the same value on the next save.
        self._applied_autostart = None
        self._set_status(
            self._tr("Otomatik başlatma ayarlanamadı · {error}", error=error), "#E3B866",
        )

    def save_settings(self, _checked: bool = False, quiet: bool = False) -> bool:
        self.settings = self._snapshot_settings()
        try:
            save_settings_file(self.settings)
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
        self._apply_autostart_async(bool(self.settings["autostart"]))
        if not quiet:
            self._set_status(
                self._tr("Ayarlar kaydedildi ve çalışan ekrana uygulanacak"), ACCENT
            )
        return True

    def start_screen(self, _checked: bool = False, *, persist: bool = True) -> None:
        # QPushButton.clicked / QAction.triggered pass a "checked" bool as the
        # first positional argument. In v1.1.1 that bool landed in ``persist``
        # (False), so the Start button never saved the UI choices and the
        # worker ran with the settings loaded at launch.
        _runtime_log("start_screen requested")
        if self.worker and self.worker.is_alive():
            _runtime_log("start_screen ignored; worker already alive")
            self._set_status(self._tr("Ekran zaten çalışıyor"), ACCENT)
            return
        try:
            if persist:
                self.save_settings(quiet=True)
            self.stop_event.clear()
            self._worker_started_at = time.monotonic()
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

    def _restart_worker(self) -> None:
        """Restart only the worker; settings were saved when it first started."""
        if self._exit_requested or (self.worker and self.worker.is_alive()):
            return
        self.start_screen(persist=False)

    def stop_screen(self) -> None:
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            self._set_status(self._tr("Ekran durduruluyor…"), "#E3B866")
        else:
            self._screen_stopped()

    def _screen_worker(self) -> None:
        _runtime_log("screen worker entered")
        screen = None
        media_monitor = MediaSessionMonitor()
        audio_monitor = AudioSpectrumMonitor()
        battery_monitor = BatteryDeviceMonitor(interval=NORMAL_BATTERY_INTERVAL, limit=3)
        msfs_monitor = MSFSTelemetryMonitor()
        weather_monitor = WeatherMonitor(weather_cities_for_design(self.settings))
        applied_display_settings = None
        last_view_mode: str | None = None
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
        battery_error_log_times: dict[str, float] = {}
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
        applied_cs2_theme: bool | None = None
        sent_frame = None
        media_base_frame = None
        media_render_future: Future | None = None
        media_render_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="media-base-render")
        phase_tracker = FlightPhaseTracker()
        screen_stabilizer = ScreenStabilizer()
        transfer_logs = {"count": 0, "at": -1e9}

        def log_transfer(kind: str, seconds: float, regions: int) -> None:
            now_t = time.monotonic()
            if transfer_logs["count"] < 4 or now_t - transfer_logs["at"] >= 600.0:
                transfer_logs["count"] += 1
                transfer_logs["at"] = now_t
                _runtime_log(
                    f"frame transfer measured; kind={kind}; ms={seconds * 1000:.0f}; regions={regions}"
                )
        new_design_active = False
        applied_design: str | None = None

        def build_media_base(
            render_colors, render_labels, render_media, render_audio, render_batteries,
            render_language,
        ):
            metrics = get_metrics()
            frame = render_screen(
                metrics, render_colors, render_labels,
                animation_time=time.monotonic(), media_snapshot=render_media,
                audio_spectrum=render_audio, battery_devices=render_batteries,
                language=render_language,
            )
            return metrics, frame
        perf_state: dict = {"state": None}

        def apply_performance_mode(enabled: bool, audio_wanted: bool) -> None:
            """In-game performance mode: fewer wakeups while a game runs."""
            battery_monitor.interval = (
                PERFORMANCE_BATTERY_INTERVAL if enabled else NORMAL_BATTERY_INTERVAL
            )
            if audio_wanted:
                audio_monitor.start()
            else:
                audio_monitor.pause()
            set_process_priority(below_normal=enabled)
            _runtime_log(
                f"performance mode {'on' if enabled else 'off'}; "
                f"audio_capture={'on' if audio_wanted else 'off'}; "
                f"battery_interval={battery_monitor.interval:.0f}s"
            )
        try:
            media_monitor.start()
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
                if self.display_suspended.is_set():
                    # Windows is locked: blank the panel and release the USB
                    # session. On unlock the normal connect path below wakes
                    # the panel (ScreenOn) and sends a fresh full frame.
                    if screen is not None:
                        try:
                            screen.power_off()
                        except Exception as exc:
                            _runtime_log(f"lock blank failed: {type(exc).__name__}: {exc}")
                        try:
                            screen.close()
                        except Exception:
                            pass
                        screen = None
                        self.screen_connected.clear()
                        self.signals.status.emit(translate(
                            "Windows kilitli · mini ekran kapalı", current.get("language", "tr"),
                        ))
                    self.stop_event.wait(1.0)
                    continue
                if screen is None:
                    try:
                        brightness = effective_brightness(current)
                        screen = ScreenSession(current["port"], brightness, current["rotation"])
                        _runtime_log(f"display connected; port={current['port']}; rotation={current['rotation']}")
                        applied_display_settings = (brightness, current["rotation"])
                        last_view_mode = None
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
                        nudge_sensor_host_if_stale()
                        last_sensor_watchdog = cycle_started
                    display_settings = (effective_brightness(current), current["rotation"])
                    if display_settings != applied_display_settings:
                        rotation_changed = screen.configure(*display_settings)
                        if (
                            applied_display_settings is not None
                            and applied_display_settings[1] != display_settings[1]
                        ) or rotation_changed is True:
                            # Every pixel moves when the orientation flips:
                            # drop the diff state and resend a full frame.
                            last_view_mode = None
                            sent_frame = None
                        applied_display_settings = display_settings
                    if (
                        cycle_started - last_context_refresh >= 0.45
                        or last_context_refresh == 0.0
                    ):
                        game_active = cs2_is_running()
                        msfs_elapsed = msfs_session_seconds()
                        msfs_active = msfs_elapsed is not None
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
                    if (
                        battery_snapshot.error and battery_snapshot.error != last_battery_error
                        and should_log_distinct(battery_error_log_times, battery_snapshot.error)
                    ):
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
                        not game_active and not msfs_active
                        and (media_snapshot.visible or audio_spectrum.active)
                    )
                    view_mode = (
                        "msfs" if msfs_active and not game_active
                        else "media" if media_mode else "idle"
                    )
                    if view_mode != last_view_mode:
                        _runtime_log(
                            "view mode changed; "
                            f"mode={view_mode}; cs2_active={game_active}"
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
                    design = "classic" if current.get("design") == "classic" else "new"
                    if design != applied_design:
                        # A different renderer owns every pixel: forget the
                        # frame/diff caches and start with a full frame.
                        if applied_design is not None:
                            _runtime_log(f"design switched; design={design}")
                        else:
                            _runtime_log(f"design active; design={design}")
                        applied_design = design
                        last_view_mode = None
                        sent_frame = None
                        media_base_frame = None
                        if media_render_future is not None:
                            media_render_future.cancel()
                            media_render_future = None
                    new_design_active = design == "new"
                    if game_active != applied_cs2_theme:
                        if applied_cs2_theme is not None:
                            _runtime_log(f"cs2 theme {'on' if game_active else 'off'}")
                        applied_cs2_theme = game_active
                        if not new_design_active:
                            # The classic palette changes every pixel (margins
                            # included): resend one clean full frame.
                            last_view_mode = None
                            sent_frame = None
                            media_base_frame = None
                    performance_mode = game_active or msfs_active
                    # The spectrum is only drawn by the classic media screen.
                    audio_wanted = not performance_mode and not new_design_active
                    if (performance_mode, audio_wanted) != perf_state["state"]:
                        perf_state["state"] = (performance_mode, audio_wanted)
                        apply_performance_mode(performance_mode, audio_wanted)
                    elif audio_wanted and not audio_monitor.running:
                        audio_monitor.start()
                    if new_design_active:
                        if cached_metrics is None or cycle_started - last_metrics_refresh >= 0.5:
                            cached_metrics = get_metrics()
                            last_metrics_refresh = cycle_started
                        language = current.get("language", "tr")
                        design_mode = new_design_mode(game_active, msfs_active)
                        if msfs_active:
                            flight_state = phase_tracker.update(flight_telemetry)
                        else:
                            # MSFS closed: the next session starts clean.
                            phase_tracker.reset()
                            flight_state = None
                        energy_summary = (
                            self.energy_meter.summary() if self.energy_meter.running else None
                        )
                        faceit_snapshot = (
                            self.faceit_monitor.snapshot() if self.faceit_monitor.configured() else None
                        )
                        if design_mode == "msfs":
                            requested_detail = getattr(flight_state, "phase", "")
                        elif design_mode == "desktop":
                            requested_detail = carousel_page(
                                current, user_idle_seconds(),
                                available_pages(NewScreenContext(
                                    metrics={}, weather=weather_snapshot.readings,
                                    energy=energy_summary, faceit=faceit_snapshot,
                                )),
                                time.time(),
                            )
                        else:
                            requested_detail = ""
                        design_mode, shown_detail = screen_stabilizer.update(
                            (design_mode, requested_detail), cycle_started,
                        )
                        if design_mode == "msfs" and flight_state is not None and shown_detail:
                            flight_state = dataclasses.replace(flight_state, phase=shown_detail)
                        frame = render_new_screen(NewScreenContext(
                            metrics=cached_metrics,
                            batteries=battery_snapshot.devices,
                            weather=weather_snapshot.readings,
                            language=language,
                            mode=design_mode,
                            flight=flight_telemetry,
                            flight_state=flight_state,
                            hardware_labels=labels,
                            page=(shown_detail or "main") if design_mode == "desktop" else "main",
                            energy=energy_summary,
                            faceit=faceit_snapshot,
                        ))
                        media_patch = None
                        if last_view_mode is None or sent_frame is None:
                            transfer_started = time.monotonic()
                            screen.show(frame)
                            sent_frame = frame.copy()
                            self.screen_connected.set()
                            _runtime_log("first full frame sent (new design)")
                            log_transfer("full", time.monotonic() - transfer_started, 1)
                        else:
                            # Send only the pixels that changed; a screen change
                            # goes out as a left-to-right column wipe.
                            update_kind, send_regions = plan_new_design_update(frame, sent_frame)
                            if send_regions:
                                transfer_started = time.monotonic()
                                screen.show_regions(frame, send_regions)
                                if update_kind == "wipe":
                                    log_transfer(
                                        "wipe", time.monotonic() - transfer_started,
                                        len(send_regions),
                                    )
                                for region in send_regions:
                                    sent_frame.paste(frame.crop(region), region)
                    else:
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
                                    cached_metrics, current["colors"], labels,
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
                                    dict(current["colors"]), dict(labels),
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
                                cached_metrics, current["colors"], labels,
                                animation_time=cycle_started,
                                media_snapshot=media_snapshot,
                                audio_spectrum=audio_spectrum,
                                battery_devices=battery_snapshot.devices,
                                msfs_active=msfs_active,
                                flight_telemetry=flight_telemetry,
                                msfs_layout=current.get("msfs_layout", "cockpit"),
                                weather_readings=weather_snapshot.readings,
                                language=current.get("language", "tr"),
                                cs2_active=game_active,
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
                    if not isinstance(exc, OSError):
                        # A sensor/render/runtime bug is not a USB failure:
                        # keep the serial session, log it and resend a full
                        # frame next cycle instead of reconnecting the panel.
                        _runtime_log(f"frame cycle failed: {type(exc).__name__}: {exc}")
                        sent_frame = None
                        last_view_mode = None
                        self._screen_progress_at = time.monotonic()
                        self.stop_event.wait(1.0)
                        continue
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
                if game_active:
                    status_message = "CS2 açık · ekran CS2 renklerinde"
                elif msfs_active:
                    status_message = (
                        "MSFS 2024 açık · uçuş verileri canlı"
                        if flight_telemetry.updated_at
                        else "MSFS 2024 açık · kokpit bağlantısı bekleniyor"
                    )
                elif media_mode:
                    label = media_snapshot.platform or translate(
                        "Sistem sesi", current.get("language", "tr"),
                    )
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
                    (NEW_DESIGN_MSFS_INTERVAL if msfs_active else NEW_DESIGN_WORKER_INTERVAL)
                    if new_design_active
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
                    getattr(screen, "show_once", screen.show)(
                        Image.new("RGB", (WIDTH, HEIGHT), "#000000")
                    )
                    _runtime_log("blank frame sent before display stop")
                except Exception as exc:
                    _runtime_log(
                        f"blank frame could not be sent: {type(exc).__name__}: {exc}"
                    )
                try:
                    screen.close()
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
        connection_prefixes = (
            "Bağlantı", "Connection", "Mini ekran bekleniyor",
            "Refreshing connection", "Waiting for mini display",
        )
        if not self.status_label.text().startswith(connection_prefixes):
            self._set_status(self._tr("Ekran durduruldu"), "#6F7C90")
        if self._exit_requested:
            app = QApplication.instance()
            if app is not None:
                app.quit()
        elif not self.stop_event.is_set() and not self._smoke_test:
            if time.monotonic() - self._worker_started_at > 120.0:
                self._restart_failures = 0
            delay_ms = min(WORKER_RESTART_MAX_DELAY_MS, 1000 * (2 ** self._restart_failures))
            self._restart_failures += 1
            _runtime_log(
                "screen worker stopped unexpectedly; "
                f"scheduling automatic restart in {delay_ms} ms"
            )
            QTimer.singleShot(delay_ms, self._restart_worker)

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
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--autorun", action="store_true")
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--language", choices=tuple(SUPPORTED_LANGUAGES))
    parser.add_argument("--recover-pid", type=int, default=0)
    parser.add_argument("--quit", action="store_true")
    args, _unknown = parser.parse_known_args()
    if args.quit:
        return 0 if request_running_instance_quit() else 1
    if args.recover_pid:
        _wait_for_recovery_parent(args.recover_pid)
    if not args.smoke_test and args.capture is None and not _acquire_single_instance(
        activate_existing=not args.autorun,
    ):
        return 0
    if not args.smoke_test and args.capture is None:
        QUIT_REQUEST_PATH.unlink(missing_ok=True)
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
        threading.Thread(
            target=reconcile_windows_autostart,
            args=(bool(panel.settings.get("autostart")),),
            name="autostart-reconcile", daemon=True,
        ).start()
        panel.start_zowie_monitor()
        panel.energy_meter.start()
        panel.faceit_monitor.start()
        panel.register_session_notifications()
        panel.start_update_checks()
    if panel.tray_icon is not None:
        app.setQuitOnLastWindowClosed(False)
    if hasattr(app, "commitDataRequest"):
        app.commitDataRequest.connect(lambda _manager: panel.prepare_system_shutdown())
    app.aboutToQuit.connect(panel.prepare_system_shutdown)
    if not args.smoke_test and args.capture is None:
        QTimer.singleShot(350, lambda: threading.Thread(
            target=ensure_sensor_host, name="sensor-host-start", daemon=True,
        ).start())
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
        shutdown_child_processes()


if __name__ == "__main__":
    raise SystemExit(main())
