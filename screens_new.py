# SPDX-License-Identifier: GPL-3.0-or-later
"""The "Yeni" (new) screen design for the 480x320 Turing panel.

Pure PIL rendering of the HTML mockups in the design hand-off:
desktop (Main), in-game strip (Strip), CS2 buy / death / live, CS2 FACEIT
(system data only) and the MSFS phase screens (ground, cruise, approach,
landing, radio). Nothing here reads game memory or sends input; the CS2
values come exclusively from Valve's official Game State Integration and
MSFS values from read-only SimConnect variables.
"""

from __future__ import annotations

import collections
import datetime as dt
import functools
import math
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageColor, ImageDraw, ImageFont

from i18n import normalize_language, translate

WIDTH, HEIGHT = 480, 320
ROOT = Path(__file__).resolve().parent
FONT_ROOT = Path(getattr(sys, "_MEIPASS", ROOT)) / "fonts"

GREEN = "#4ADE80"
ORANGE = "#FF9F43"
RED = "#F87171"
WHITE = "#FFFFFF"
TEXT = "#E8EDF2"
TRACK = "#243040"
CT_BLUE = "#3FB5FF"
T_ORANGE = "#FF9F43"
CHIP_OK = ("#12301C", "#86EFAC")
CHIP_WARN = ("#3A2410", "#FFB870")
CHIP_BAD = ("#3A1414", "#FCA5A5")
STRIP_HEIGHT = 56

# Mode palettes: (root background, strip background, accent).
MODE_DESKTOP = ("#0B0F14", "#141B24", "#3FB5FF")
MODE_CS2 = ("#1C1305", "#2B1D07", "#F5A524")
MODE_FACEIT = ("#1E0C0C", "#2A1212", "#FF5A4E")
MODE_MSFS = {
    "ground": ("#0C1A10", "#12281A", "#4ADE80"),
    "cruise": ("#0A1530", "#10224A", "#3FB5FF"),
    "radio": ("#0A1530", "#10224A", "#3FB5FF"),
    "approach": ("#1A1030", "#27184A", "#A78BFA"),
    "landing": ("#0A1F1F", "#103030", "#2DD4BF"),
}

# Threshold colours used everywhere a CPU/GPU value appears.
THRESHOLDS = {
    ("cpu", "temp"): (60.0, 75.0),
    ("cpu", "load"): (60.0, 75.0),
    ("gpu", "temp"): (55.0, 65.0),
    ("gpu", "load"): (60.0, 80.0),
    ("ram", "load"): (70.0, 85.0),
}


def threshold_color(component: str, kind: str, value: float | None) -> str:
    """green below the first limit, orange up to the second, red at/above it."""
    if value is None:
        return WHITE
    warn, critical = THRESHOLDS[(component, kind)]
    if value >= critical:
        return RED
    if value >= warn:
        return ORANGE
    return GREEN


def battery_color(percent: int | None) -> str:
    if percent is None:
        return TRACK
    if percent <= 10:
        return RED
    if percent <= 30:
        return ORANGE
    return GREEN


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=64)
def font(size: int, condensed: bool = False) -> ImageFont.FreeTypeFont:
    names = (
        ("BarlowCondensed-Bold.ttf",) if condensed else ("Barlow-Bold.ttf",)
    )
    windir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    candidates = [FONT_ROOT / name for name in names] + [
        windir / "segoeuib.ttf", windir / "arialbd.ttf",
    ]
    for candidate in candidates:
        if candidate.is_file():
            try:
                return ImageFont.truetype(str(candidate), size)
            except OSError:
                continue
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Data passed into the renderer
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BatteryInfo:
    kind: str
    percent: int | None


@dataclass(frozen=True)
class Alert:
    title: str
    detail: str
    severity: str = "warn"  # "warn" | "critical"


@dataclass
class NewScreenContext:
    metrics: dict
    batteries: tuple = ()
    weather: tuple = ()
    now: dt.datetime | None = None
    language: str = "tr"
    fps: float | None = None
    mode: str = "desktop"  # desktop | cs2 | faceit | msfs
    game: object | None = None  # app.GameSnapshot
    cs_screen: str = "live"  # buy | death | live
    alerts: tuple = ()
    ping_ms: float | None = None
    flight: object | None = None  # msfs_runtime.FlightTelemetry
    flight_state: object | None = None  # msfs_runtime.FlightPhaseState
    hardware_labels: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Drawing primitives
# ---------------------------------------------------------------------------

def _rgb(color: str) -> tuple[int, int, int]:
    return ImageColor.getrgb(color)[:3]


def blend(base: str, overlay: str, alpha: float) -> str:
    a, b = _rgb(base), _rgb(overlay)
    return "#%02x%02x%02x" % tuple(round(a[i] + (b[i] - a[i]) * alpha) for i in range(3))


def panel_color(background: str, alpha: float = 0.07) -> str:
    """rgba(255,255,255,alpha) composited over the mode background."""
    return blend(background, "#FFFFFF", alpha)


def _text_width(draw: ImageDraw.ImageDraw, text: str, typeface, spacing: float = 0.0) -> float:
    return draw.textlength(text, font=typeface) + spacing * max(0, len(text) - 1)


def text(draw: ImageDraw.ImageDraw, xy, value: str, size: int, color: str = WHITE,
         condensed: bool = False, anchor: str = "la", spacing: float = 0.0) -> float:
    """Draw text; returns its width. ``spacing`` emulates CSS letter-spacing."""
    typeface = font(size, condensed)
    if not spacing:
        draw.text(xy, value, font=typeface, fill=color, anchor=anchor)
        return draw.textlength(value, font=typeface)
    width = _text_width(draw, value, typeface, spacing)
    x, y = xy
    if anchor[0] == "r":
        x -= width
    elif anchor[0] == "m":
        x -= width / 2
    vertical = "l" + anchor[1]
    for char in value:
        draw.text((x, y), char, font=typeface, fill=color, anchor=vertical)
        x += draw.textlength(char, font=typeface) + spacing
    return width


def fit(draw: ImageDraw.ImageDraw, value: str, size: int, max_width: float, condensed=False) -> str:
    typeface = font(size, condensed)
    if draw.textlength(value, font=typeface) <= max_width:
        return value
    while value and draw.textlength(value + "…", font=typeface) > max_width:
        value = value[:-1]
    return value.rstrip() + "…"


def rounded(draw: ImageDraw.ImageDraw, box, radius: int, fill: str, outline: str | None = None,
            width: int = 1) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def ring(image: Image.Image, center: tuple[int, int], diameter: int, stroke: float,
         fraction: float, color: str, track: str, scale: int = 4) -> None:
    """SVG-like ring: full track + clockwise arc from 12 o'clock, round caps.

    Supersampling is limited to the ring's own box (not the whole frame).
    """
    size = diameter * scale
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    stroke_px = stroke * scale
    radius = (size - stroke_px) / 2.0  # circle radius at the stroke centre
    box = (stroke_px / 2, stroke_px / 2, size - stroke_px / 2, size - stroke_px / 2)
    outer = (box[0] - stroke_px / 2, box[1] - stroke_px / 2, box[2] + stroke_px / 2, box[3] + stroke_px / 2)
    draw.ellipse(outer, fill=track)
    inner_inset = stroke_px
    draw.ellipse((outer[0] + inner_inset, outer[1] + inner_inset,
                  outer[2] - inner_inset, outer[3] - inner_inset), fill=(0, 0, 0, 0))
    fraction = max(0.0, min(1.0, fraction))
    if fraction > 0.0:
        progress = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        pdraw = ImageDraw.Draw(progress)
        start, end = -90.0, -90.0 + 360.0 * fraction
        pdraw.pieslice(outer, start, end, fill=color)
        pdraw.ellipse((outer[0] + inner_inset, outer[1] + inner_inset,
                       outer[2] - inner_inset, outer[3] - inner_inset), fill=(0, 0, 0, 0))
        c = size / 2.0
        for angle in (start, end):
            rad = math.radians(angle)
            x, y = c + radius * math.cos(rad), c + radius * math.sin(rad)
            pdraw.ellipse((x - stroke_px / 2, y - stroke_px / 2, x + stroke_px / 2, y + stroke_px / 2), fill=color)
        layer.alpha_composite(progress)
    layer = layer.resize((diameter, diameter), Image.Resampling.LANCZOS)
    image.paste(layer, (center[0] - diameter // 2, center[1] - diameter // 2), layer)


def chip(draw: ImageDraw.ImageDraw, x: int, y: int, label: str, palette=CHIP_OK, size: int = 14) -> int:
    """Pill chip; returns its right edge."""
    width = int(draw.textlength(label, font=font(size))) + 18
    rounded(draw, (x, y, x + width, y + size + 12), 6, palette[0])
    text(draw, (x + 9, y + (size + 12) / 2), label, size, palette[1], anchor="lm")
    return x + width


def progress_bar(draw: ImageDraw.ImageDraw, box, fraction: float, color: str, track: str = TRACK) -> None:
    left, top, right, bottom = box
    radius = (bottom - top) // 2
    rounded(draw, box, radius, track)
    fraction = max(0.0, min(1.0, fraction))
    if fraction > 0:
        fill_right = left + max(bottom - top, round((right - left) * fraction))
        rounded(draw, (left, top, min(right, fill_right), bottom), radius, color)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _t(ctx: NewScreenContext, value: str, **kw) -> str:
    return translate(value, ctx.language, **kw)


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{int(round(float(value)))}%"


def _deg(value: float | None) -> str:
    return "—" if value is None else f"{int(round(float(value)))}°"


def _num(value: float | None, digits: int = 0, language: str = "tr") -> str:
    if value is None:
        return "—"
    rendered = f"{value:,.{digits}f}"
    if normalize_language(language) == "tr":
        rendered = rendered.replace(",", "_").replace(".", ",").replace("_", ".")
    return rendered


def money(value: int | None, language: str = "tr") -> str:
    if value is None:
        return "—"
    return "$" + _num(float(value), 0, language)


BATTERY_ORDER = {"mouse": 0, "keyboard": 1, "headset": 2}


def present_batteries(devices: Iterable, limit: int = 3) -> list[BatteryInfo]:
    """Only devices battery_runtime reports as connected and with a reading.

    Absent devices are never drawn (no "—" placeholders); the caller reflows
    the remaining items over the available space.
    """
    items: list[BatteryInfo] = []
    seen: set[str] = set()
    for device in devices or ():
        percent = getattr(device, "percent", None)
        if percent is None:
            continue
        kind = getattr(device, "kind", "other")
        if kind not in BATTERY_ORDER:
            kind = "other"
        if kind != "other" and kind in seen:
            continue
        seen.add(kind)
        items.append(BatteryInfo(kind, max(0, min(100, int(percent)))))
    items.sort(key=lambda item: BATTERY_ORDER.get(item.kind, 3))
    return items[: max(0, limit)]


def ram_reading(metrics: dict) -> tuple[float | None, float | None, float | None]:
    """(percent, used GB, total GB) of system RAM, or Nones when unknown."""
    load = metrics.get("ram_load")
    used = metrics.get("ram_used_gb", metrics.get("ram_used"))
    total = metrics.get("ram_total_gb")
    return (
        None if load is None else float(load),
        None if used is None else float(used),
        None if total is None else float(total),
    )


BATTERY_LABELS = {
    "mouse": ("Fare", "Fare"),
    "keyboard": ("Klavye", "Klv"),
    "headset": ("Kulaklık", "Kulak"),
    "other": ("Cihaz", "Chz"),
}

WEATHER_TEXT = {
    "clear": "açık", "cloud": "parçalı bulutlu", "fog": "sisli",
    "snow": "karlı", "storm": "fırtınalı", "rain": "yağmurlu",
}


def _weather_kind(code: int) -> str:
    if code == 0:
        return "clear"
    if code in {45, 48}:
        return "fog"
    if code in {1, 2, 3}:
        return "cloud"
    if code in {71, 73, 75, 77, 85, 86}:
        return "snow"
    if code in {95, 96, 99}:
        return "storm"
    return "rain"


# ---------------------------------------------------------------------------
# Shared blocks
# ---------------------------------------------------------------------------

def draw_strip(image: Image.Image, ctx: NewScreenContext, strip_bg: str, accent: str,
               allow_ram: bool = True) -> list[tuple[str, tuple[float, float, float, float]]]:
    """56 px in-game status strip (Strip.dc.html).

    Returns the drawn items as ``(name, (left, top, right, bottom))``. Battery
    items exist only for connected devices. RAM is added only when it fits
    completely between the FPS cell and the battery items.
    """
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, WIDTH, STRIP_HEIGHT - 4), fill=strip_bg)
    draw.rectangle((0, STRIP_HEIGHT - 3, WIDTH, STRIP_HEIGHT - 1), fill=accent)
    m = ctx.metrics
    layout: list[tuple[str, tuple[float, float, float, float]]] = []
    x = 12
    label_y, value_y = 20, 45
    value_font = font(22, True)

    def cell_width(label: str, parts) -> float:
        width = 0.0
        for index, (value, _color) in enumerate(parts):
            if index:
                width += draw.textlength(" ", font=value_font)
            width += draw.textlength(value, font=value_font)
        return max(width, _text_width(draw, label, font(12), 1.0))

    def draw_cell(left: float, label: str, parts) -> float:
        text(draw, (left, label_y), label, 12, WHITE, anchor="ls", spacing=1.0)
        cursor = left
        for index, (value, color) in enumerate(parts):
            if index:
                cursor += draw.textlength(" ", font=value_font)
            cursor += text(draw, (cursor, value_y), value, 22, color, condensed=True, anchor="ls")
        return left + cell_width(label, parts)

    cells = (
        ("CPU", (
            (_pct(m.get("cpu_load")), threshold_color("cpu", "load", m.get("cpu_load"))),
            (_deg(m.get("cpu_temp")), threshold_color("cpu", "temp", m.get("cpu_temp"))),
        )),
        ("GPU", (
            (_pct(m.get("gpu_load")), threshold_color("gpu", "load", m.get("gpu_load"))),
            (_deg(m.get("gpu_temp")), threshold_color("gpu", "temp", m.get("gpu_temp"))),
        )),
        ("FPS", ((("—" if ctx.fps is None else str(int(round(ctx.fps)))), TEXT),)),
    )
    for label, parts in cells:
        end = draw_cell(x, label, parts)
        layout.append((label.lower(), (x, 6, end, value_y + 4)))
        x = end + 14

    # Battery items, right-aligned, measured before anything is drawn.
    small = font(14)
    center_y = (STRIP_HEIGHT - 3) / 2
    right = WIDTH - 12
    planned = []
    for info in reversed(present_batteries(ctx.batteries)):
        label = _t(ctx, BATTERY_LABELS[info.kind][1]) + " "
        value = _pct(info.percent)
        width = draw.textlength(label, font=small) + draw.textlength(value, font=small)
        if right - width < x:
            break
        planned.append((info, label, value, right - width, right))
        right -= width + 12
    battery_left = planned[-1][3] if planned else WIDTH - 12

    if allow_ram:
        load, _used, _total = ram_reading(m)
        if load is not None:
            parts = ((_pct(load), threshold_color("ram", "load", load)),)
            width = cell_width("RAM", parts)
            if x + width <= battery_left - 12:
                end = draw_cell(x, "RAM", parts)
                layout.append(("ram", (x, 6, end, value_y + 4)))
                x = end + 14

    for info, label, value, left, item_right in planned:
        color = battery_color(info.percent) if info.percent <= 30 else TEXT
        value_width = text(draw, (item_right, center_y), value, 14, color, anchor="rm")
        text(draw, (item_right - value_width, center_y), label, 14, WHITE, anchor="rm")
        layout.append((f"battery:{info.kind}", (left, center_y - 9, item_right, center_y + 9)))
    return layout


def _header(draw, ctx, y: int, left: str, right: str, size: int = 14) -> None:
    text(draw, (14, y), left, size, WHITE)
    text(draw, (WIDTH - 14, y), right, size, WHITE, anchor="ra")


def _stat_panel(draw, box, background: str, label: str, value: str, sub: str = "",
                value_size: int = 46, sub_color: str = WHITE, value_color: str = TEXT,
                label_size: int = 14, sub_size: int = 14) -> None:
    left, top, right, bottom = box
    rounded(draw, box, 10, panel_color(background))
    text(draw, (left + 10, top + 10), fit(draw, label, label_size, right - left - 20), label_size, WHITE)
    middle = (top + bottom) / 2 + (2 if sub else 6)
    text(draw, (left + 10, middle), fit(draw, value, value_size, right - left - 20, True),
         value_size, value_color, condensed=True, anchor="lm")
    if sub:
        text(draw, (left + 10, bottom - 10), fit(draw, sub, sub_size, right - left - 20),
             sub_size, sub_color, anchor="ls")


def _columns(left: int, right: int, count: int, gap: int) -> list[tuple[int, int]]:
    width = (right - left - gap * (count - 1)) / count
    return [(round(left + i * (width + gap)), round(left + i * (width + gap) + width)) for i in range(count)]


# ---------------------------------------------------------------------------
# Desktop (Main.dc.html) and FACEIT (CsFaceit.dc.html)
# ---------------------------------------------------------------------------

def _system_panel(image, draw, box, background, component: str, ctx, diameter: int,
                  track: str, detail_lines: list[str], temp_size: int, load_caption: bool) -> None:
    left, top, right, bottom = box
    rounded(draw, box, 10, panel_color(background))
    m = ctx.metrics
    load = m.get(f"{component}_load")
    temp = m.get(f"{component}_temp")
    load_color = threshold_color(component, "load", load)
    cy = (top + bottom) // 2
    cx = left + 10 + diameter // 2
    ring(image, (cx, cy), diameter, diameter * 0.10, (float(load or 0.0)) / 100.0, load_color, track)
    scale = diameter / 100.0
    value_y = cy + (round(3 * scale) if load_caption else round(7 * scale))
    text(draw, (cx, value_y), _pct(load), round(24 * scale), load_color if load_caption else TEXT,
         condensed=True, anchor="ms")
    if load_caption:
        text(draw, (cx, cy + round(22 * scale)), _t(ctx, "yük"), round(11 * scale), WHITE, anchor="ms")
    column_x = cx + diameter // 2 + (10 if load_caption else 12)
    name = (ctx.hardware_labels or {}).get(component) or component.upper()
    lines_height = 18 * len(detail_lines)
    block = 18 + temp_size + lines_height
    y = cy - block / 2
    text(draw, (column_x, y), fit(draw, name, 14, right - column_x - 8), 14, WHITE, spacing=1.0)
    y += 18
    text(draw, (column_x, y + temp_size * 0.85), _deg(temp), temp_size,
         threshold_color(component, "temp", temp), condensed=True, anchor="ls")
    y += temp_size + 2
    for line in detail_lines:
        text(draw, (column_x, y), fit(draw, line, 14, right - column_x - 8), 14, WHITE)
        y += 18


def _cpu_details(ctx) -> list[str]:
    m = ctx.metrics
    lines = []
    if m.get("cpu_freq"):
        lines.append(f"{float(m['cpu_freq']):.1f} GHz")
    if m.get("cpu_power"):
        lines.append(f"{int(round(float(m['cpu_power'])))} W")
    return lines or ["—"]


def _gpu_details(ctx, include_fps: bool = False) -> list[str]:
    m = ctx.metrics
    lines = []
    if include_fps and ctx.fps is not None:
        lines.append(f"{int(round(ctx.fps))} FPS")
    if m.get("gpu_memory"):
        lines.append(f"VRAM {float(m['gpu_memory']) / 1024.0:.1f} GB")
    if m.get("gpu_power") and not include_fps:
        lines.append(f"{int(round(float(m['gpu_power'])))} W")
    return lines or ["—"]


def main_layout(ctx: NewScreenContext, draw: ImageDraw.ImageDraw | None = None) -> dict:
    """Boxes of every block of the desktop screen (used by the renderer and tests)."""
    if draw is None:
        draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    now = ctx.now or dt.datetime.now()
    layout: dict = {}
    clock = now.strftime("%H:%M")
    clock_width = draw.textlength(clock, font=font(30, True))
    layout["clock"] = (14, 14, 14 + clock_width, 46)
    cursor = 14 + clock_width + 14
    load, used, total = ram_reading(ctx.metrics)
    if load is not None:
        parts = [("RAM ", WHITE), (_pct(load), threshold_color("ram", "load", load))]
        if used is not None and total:
            parts.append((f" · {used:.1f}/{total:.0f} GB", WHITE))
        elif used is not None:
            parts.append((f" · {used:.1f} GB", WHITE))
        width = sum(draw.textlength(value, font=font(14)) for value, _ in parts)
        layout["ram"] = (cursor, 28, cursor + width, 44)
        layout["ram_parts"] = parts
        cursor += width + 14
    if ctx.weather:
        reading = ctx.weather[0]
        kind = _weather_kind(int(getattr(reading, "weather_code", 0)))
        weather = (
            f"{reading.city} {int(round(reading.temperature_c))}° · "
            f"{_t(ctx, WEATHER_TEXT[kind])}"
        )
        available = WIDTH - 14 - cursor
        if available >= 60:
            weather = fit(draw, weather, 14, min(300, available))
            width = draw.textlength(weather, font=font(14))
            layout["weather"] = (WIDTH - 14 - width, 28, WIDTH - 14, 44)
            layout["weather_text"] = weather
    batteries = present_batteries(ctx.batteries)
    battery_top = HEIGHT - 14 - 42
    panels_bottom = battery_top - 10 if batteries else HEIGHT - 14
    (l1, r1), (l2, r2) = _columns(14, WIDTH - 14, 2, 10)
    layout["cpu"] = (l1, 60, r1, panels_bottom)
    layout["gpu"] = (l2, 60, r2, panels_bottom)
    layout["batteries"] = [
        (info, (left, battery_top, right, HEIGHT - 14))
        for (left, right), info in zip(_columns(14, WIDTH - 14, len(batteries), 10), batteries)
    ] if batteries else []
    return layout


def render_main(ctx: NewScreenContext) -> Image.Image:
    background = MODE_DESKTOP[0]
    image = Image.new("RGB", (WIDTH, HEIGHT), background)
    draw = ImageDraw.Draw(image)
    now = ctx.now or dt.datetime.now()
    layout = main_layout(ctx, draw)
    text(draw, (14, 44), now.strftime("%H:%M"), 30, TEXT, condensed=True, anchor="ls")
    if "ram" in layout:
        x = layout["ram"][0]
        for value, color in layout["ram_parts"]:
            x += text(draw, (x, 42), value, 14, color, anchor="ls")
    if "weather" in layout:
        text(draw, (WIDTH - 14, 42), layout["weather_text"], 14, WHITE, anchor="rs")
    _system_panel(image, draw, layout["cpu"], background, "cpu", ctx,
                  96, TRACK, _cpu_details(ctx), 30, False)
    _system_panel(image, draw, layout["gpu"], background, "gpu", ctx,
                  96, TRACK, _gpu_details(ctx), 30, False)
    for info, box in layout["batteries"]:
        left, battery_top, right, _bottom = box
        percent = info.percent
        rounded(draw, box, 8, panel_color(background))
        text(draw, (left + 10, battery_top + 8), _t(ctx, BATTERY_LABELS[info.kind][0]), 14, WHITE)
        text(draw, (right - 10, battery_top + 8), _pct(percent), 14,
             battery_color(percent) if percent <= 30 else TEXT, anchor="ra")
        progress_bar(draw, (left + 10, battery_top + 28, right - 10, battery_top + 34),
                     percent / 100.0, battery_color(percent))
    return image


def faceit_battery_layout(ctx: NewScreenContext, draw: ImageDraw.ImageDraw | None = None) -> list:
    """Placement of the FACEIT battery items; empty when no device is present.

    Each item is ``(info, x, center_y, label, label_width, value, value_width)``.
    """
    if draw is None:
        draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    batteries = present_batteries(ctx.batteries)
    if not batteries:
        return []
    battery_top = HEIGHT - 14 - 39
    center_y = (battery_top + HEIGHT - 14) / 2
    items: list = []
    slots = _columns(26, WIDTH - 26, len(batteries), 10)
    last = len(batteries) - 1
    for index, ((left, right), info) in enumerate(zip(slots, batteries)):
        label = _t(ctx, BATTERY_LABELS[info.kind][0]) + " "
        value = _pct(info.percent)
        label_width = draw.textlength(label, font=font(15))
        value_width = draw.textlength(value, font=font(15))
        total = label_width + value_width
        if len(batteries) == 1 or 0 < index < last:
            x = (left + right - total) / 2
        elif index == 0:
            x = left
        else:
            x = right - total
        items.append((info, x, center_y, label, label_width, value, value_width))
    return items


def render_faceit(ctx: NewScreenContext) -> Image.Image:
    background, _strip, accent = MODE_FACEIT
    image = Image.new("RGB", (WIDTH, HEIGHT), background)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, WIDTH, 3), fill=accent)
    now = ctx.now or dt.datetime.now()
    _header(draw, ctx, 18, _t(ctx, "FACEIT · sadece sistem verisi"), now.strftime("%H:%M"))
    items = faceit_battery_layout(ctx, draw)
    battery_top = HEIGHT - 14 - 39
    panels_top = 46
    panels_bottom = battery_top - 10 if items else HEIGHT - 14
    (l1, r1), (l2, r2) = _columns(14, WIDTH - 14, 2, 10)
    track = panel_color(panel_color(background), 0.12)
    _system_panel(image, draw, (l1, panels_top, r1, panels_bottom), background, "cpu", ctx,
                  112, track, _cpu_details(ctx)[:1], 40, True)
    _system_panel(image, draw, (l2, panels_top, r2, panels_bottom), background, "gpu", ctx,
                  112, track, _gpu_details(ctx, include_fps=True)[:1], 40, True)
    if items:
        rounded(draw, (14, battery_top, WIDTH - 14, HEIGHT - 14), 8, panel_color(background))
    for info, x, center_y, label, label_width, value, _value_width in items:
        value_color = battery_color(info.percent) if info.percent <= 30 else TEXT
        text(draw, (x, center_y), label, 15, WHITE, anchor="lm")
        text(draw, (x + label_width, center_y), value, 15, value_color, anchor="lm")
    return image


# ---------------------------------------------------------------------------
# CS2 on Valve servers (official GSI only)
# ---------------------------------------------------------------------------

def loss_award(loss_streak: int | None, team: str = "", bomb_planted: bool = False) -> int | None:
    """Money awarded if the team loses the *next* round (CS2 competitive).

    map.team_X.consecutive_round_losses is the team's loss counter. Losing
    the next round pays 1400 + 500 x counter, capped at 3400. A T-side bomb
    plant adds 600.
    """
    if loss_streak is None:
        return None
    award = 1400 + 500 * max(0, min(4, int(loss_streak)))
    if team == "T" and bomb_planted:
        award += 600
    return award


FULL_BUY_COST = {"CT": 4100, "T": 3700}


def buy_suggestion(money_now: int | None, team: str, award: int | None,
                   round_number: int | None, half_length: int = 12) -> str:
    """FULL BUY / ECO / FORCE from money, team and the guaranteed loss award."""
    if money_now is None:
        return ""
    cost = FULL_BUY_COST.get(team, 3900)
    if money_now >= cost:
        return "full"
    last_of_half = round_number is not None and round_number in (half_length, half_length * 2)
    if last_of_half:
        return "force"  # money resets after the half: nothing to save for
    if award is not None and money_now + award >= cost:
        return "eco"  # saving now guarantees a full buy next round
    return "force"


SUGGESTION_STYLE = {
    "full": ("FULL BUY", GREEN, "#08240F"),
    "eco": ("ECO", RED, "#2A0B0B"),
    "force": ("FORCE", ORANGE, "#2A1604"),
}


def _score_text(game) -> tuple[str, str]:
    ct = "—" if game.score_ct is None else str(game.score_ct)
    t = "—" if game.score_t is None else str(game.score_t)
    return ct, t


def render_cs_buy(ctx: NewScreenContext) -> Image.Image:
    background, strip_bg, accent = MODE_CS2
    image = Image.new("RGB", (WIDTH, HEIGHT), background)
    draw_strip(image, ctx, strip_bg, accent)
    draw = ImageDraw.Draw(image)
    game = ctx.game
    ct, t = _score_text(game)
    round_label = (
        _t(ctx, "SATIN ALMA · Round {n}", n=game.round_number)
        if game.round_number else _t(ctx, "SATIN ALMA")
    )
    _header(draw, ctx, 68, round_label, f"CT {ct} – {t} T")
    text(draw, (14, 96), _t(ctx, "Paran"), 14, WHITE)
    text(draw, (14, 166), money(game.money, ctx.language), 58, TEXT, condensed=True, anchor="ls")
    award = loss_award(game.loss_streak, game.team, game.bomb_planted)
    if award is None:
        award = game.loss_bonus
    suggestion = buy_suggestion(game.money, game.team, award, game.round_number)
    if suggestion:
        label, fill, ink = SUGGESTION_STYLE[suggestion]
        width = int(draw.textlength(label, font=font(34, True))) + 36
        box = (WIDTH - 14 - width, 104, WIDTH - 14, 158)
        rounded(draw, box, 10, fill)
        text(draw, ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2 + 1), label, 34, ink,
             condensed=True, anchor="mm")
    next_min = None
    if game.money is not None and award is not None:
        next_min = min(16000, game.money + award)
    cells = (
        (_t(ctx, "Kayıp serisi"), "—" if game.loss_streak is None else str(game.loss_streak)),
        (_t(ctx, "Kaybedersen"), "—" if award is None else "+" + money(award, ctx.language)),
        (_t(ctx, "Sonraki round min."), money(next_min, ctx.language)),
    )
    top = 182
    for (left, right), (label, value) in zip(_columns(14, WIDTH - 14, 3, 8), cells):
        rounded(draw, (left, top, right, top + 58), 8, panel_color(background))
        text(draw, (left + 8, top + 8), fit(draw, label, 13, right - left - 16), 13, WHITE)
        text(draw, (left + 8, top + 50), value, 20, TEXT, anchor="ls")
    if game.match_kills is not None:
        summary = _t(ctx, "Maç K/D/A {k} / {d} / {a}", k=game.match_kills,
                     d=game.match_deaths if game.match_deaths is not None else "—",
                     a=game.match_assists if game.match_assists is not None else "—")
        text(draw, (14, 266), summary, 14, WHITE)
    return image


def render_cs_death(ctx: NewScreenContext) -> Image.Image:
    background, strip_bg, accent = MODE_CS2
    image = Image.new("RGB", (WIDTH, HEIGHT), background)
    draw_strip(image, ctx, strip_bg, accent)
    draw = ImageDraw.Draw(image)
    game = ctx.game
    text(draw, (14, 70), _t(ctx, "BU ROUND"), 14, WHITE)
    ct, t = _score_text(game)
    right = WIDTH - 14
    right -= text(draw, (right, 88), f"{t} T", 26, T_ORANGE, condensed=True, anchor="rs")
    right -= text(draw, (right, 88), " – ", 26, WHITE, condensed=True, anchor="rs")
    text(draw, (right, 88), f"CT {ct}", 26, CT_BLUE, condensed=True, anchor="rs")
    top, bottom = 100, 184
    (l1, r1), (l2, r2) = _columns(14, WIDTH - 14, 2, 10)
    for left, right_edge in ((l1, r1), (l2, r2)):
        rounded(draw, (left, top, right_edge, bottom), 10, panel_color(background))
    text(draw, (l1 + 10, top + 10), _t(ctx, "Kill"), 14, WHITE)
    kills = "—" if game.round_kills is None else str(game.round_kills)
    width = text(draw, (l1 + 10, bottom - 14), kills, 44, TEXT, condensed=True, anchor="ls")
    if game.round_headshots is not None:
        text(draw, (l1 + 18 + width, bottom - 16), f"{game.round_headshots} HS", 18, WHITE, anchor="ls")
    text(draw, (l2 + 10, top + 10), _t(ctx, "Hasar"), 14, WHITE)
    damage = "—" if game.round_damage is None else str(game.round_damage)
    text(draw, (l2 + 10, bottom - 14), damage, 44, TEXT, condensed=True, anchor="ls")

    def pair(a, b):
        return f"{'—' if a is None else a} / {'—' if b is None else b}"

    cells = (
        (_t(ctx, "MAÇ K/D"), pair(game.match_kills, game.match_deaths)),
        (_t(ctx, "Asist"), "—" if game.match_assists is None else str(game.match_assists)),
        ("MVP", "—" if game.match_mvps is None else str(game.match_mvps)),
        (_t(ctx, "Skor"), "—" if game.match_score is None else str(game.match_score)),
    )
    for (left, _right), (label, value) in zip(_columns(14, WIDTH - 14, 4, 8), cells):
        text(draw, (left, 196), label, 14, WHITE)
        text(draw, (left, 236), value, 20, TEXT, anchor="ls")
    return image


def render_cs_live(ctx: NewScreenContext) -> Image.Image:
    """Live round: strip + alerts only; otherwise the content stays dark."""
    background, strip_bg, accent = MODE_CS2
    image = Image.new("RGB", (WIDTH, HEIGHT), background)
    draw_strip(image, ctx, strip_bg, accent)
    draw = ImageDraw.Draw(image)
    if ctx.alerts:
        alert = ctx.alerts[0]
        border = RED if alert.severity == "critical" else ORANGE
        fill = "#3A1414" if alert.severity == "critical" else "#3A2410"
        title_color = "#FCA5A5" if alert.severity == "critical" else "#FFB870"
        box = (14, 128, WIDTH - 14, 216)
        rounded(draw, box, 12, fill, outline=border, width=2)
        text(draw, (30, 178), alert.title, 40, title_color, condensed=True, anchor="ls")
        text(draw, (30, 202), alert.detail, 14, "#F3D9BF", anchor="ls")
        if len(ctx.alerts) > 1:
            text(draw, (14, 238), ctx.alerts[1].title + " · " + ctx.alerts[1].detail, 14, WHITE)
    return image


# ---------------------------------------------------------------------------
# MSFS phase screens
# ---------------------------------------------------------------------------

def _tons(pounds: float | None) -> float | None:
    return None if pounds is None else pounds * 0.45359237 / 1000.0


def _altitude_label(ctx, altitude_ft: float | None) -> str:
    if altitude_ft is None:
        return ""
    if altitude_ft >= 18000:
        return f"FL{int(round(altitude_ft / 100.0)):03d}"
    return f"{_num(altitude_ft, 0, ctx.language)} ft"


def _duration(ctx, seconds: float | None) -> str:
    if not seconds:
        return "—"
    minutes = int(round(seconds / 60.0))
    hours, minutes = divmod(minutes, 60)
    if hours:
        return _t(ctx, "{h} sa {m} dk", h=hours, m=minutes)
    return _t(ctx, "{m} dk", m=minutes)


def _msfs_base(ctx: NewScreenContext, phase: str) -> tuple[Image.Image, ImageDraw.ImageDraw, str]:
    background, strip_bg, accent = MODE_MSFS[phase]
    image = Image.new("RGB", (WIDTH, HEIGHT), background)
    # The MSFS screens stay uncluttered: no RAM item in their strip.
    draw_strip(image, ctx, strip_bg, accent, allow_ram=False)
    return image, ImageDraw.Draw(image), background


def _three_panels(draw, background, top, bottom, cells, value_size=46):
    for (left, right), cell in zip(_columns(14, WIDTH - 14, 3, 10), cells):
        label, value, sub = cell[:3]
        sub_color = cell[3] if len(cell) > 3 else WHITE
        _stat_panel(draw, (left, top, right, bottom), background, label, value, sub,
                    value_size=value_size, sub_color=sub_color)


def _no_data(draw, ctx, top: int) -> None:
    text(draw, (WIDTH / 2, top), _t(ctx, "Uçuş verisi bekleniyor"), 16, WHITE, anchor="mm")
    text(draw, (WIDTH / 2, top + 26), _t(ctx, "Kokpit açıldığında otomatik bağlanır"), 14, WHITE, anchor="mm")


def render_msfs_ground(ctx: NewScreenContext) -> Image.Image:
    image, draw, background = _msfs_base(ctx, "ground")
    f = ctx.flight
    if f is None or not getattr(f, "updated_at", 0):
        _no_data(draw, ctx, 180)
        return image
    moving = (f.ground_speed_kt or 0.0) >= 3.0
    title = _t(ctx, "YERDE · TAKSİ") if moving else _t(ctx, "YERDE · PARK")
    _header(draw, ctx, 68, title, fit(draw, f.aircraft_title or "", 14, 250))
    _three_panels(draw, background, 92, 238, (
        (_t(ctx, "Yer hızı"), _num(f.ground_speed_kt, 0), _t(ctx, "kt · sınır 25"),
         ORANGE if (f.ground_speed_kt or 0) > 25 else WHITE),
        (_t(ctx, "Yakıt"), _num(_tons(f.fuel_lbs), 1, ctx.language), _t(ctx, "ton")),
        (_t(ctx, "Ağırlık"), _num(_tons(f.total_weight_lbs), 1, ctx.language), _t(ctx, "ton")),
    ), value_size=48)
    x = 14
    if f.parking_brake is not None:
        x = chip(draw, x, 250, _t(ctx, "Park freni AÇIK") if f.parking_brake else _t(ctx, "Park freni KAPALI"),
                 CHIP_OK) + 8
    if f.flaps_index is not None:
        x = chip(draw, x, 250, _t(ctx, "Flap {n}", n=f.flaps_index), CHIP_OK) + 8
    if f.strobe_on is not None:
        chip(draw, x, 250, _t(ctx, "Strobe AÇIK") if f.strobe_on else _t(ctx, "Strobe KAPALI"),
             CHIP_OK if f.strobe_on else CHIP_WARN)
    return image


def render_msfs_cruise(ctx: NewScreenContext) -> Image.Image:
    image, draw, background = _msfs_base(ctx, "cruise")
    f, state = ctx.flight, ctx.flight_state
    if f is None or not getattr(f, "updated_at", 0):
        _no_data(draw, ctx, 180)
        return image
    climbing = (f.vertical_speed_fpm or 0.0) > 500
    phase = _t(ctx, "TIRMANIŞ") if climbing else _t(ctx, "SEYİR")
    title = f"{phase} · {_altitude_label(ctx, f.altitude_ft)}"
    next_wp = ""
    if f.next_wp_id and f.next_wp_distance_nm is not None:
        next_wp = _t(ctx, "Sonraki: {wp} {nm} nm", wp=f.next_wp_id, nm=_num(f.next_wp_distance_nm, 0, ctx.language))
    _header(draw, ctx, 68, title, next_wp)
    remaining = getattr(state, "route_remaining_nm", None)
    total = getattr(state, "route_total_nm", None)
    if remaining is not None:
        middle = _t(ctx, "Kalan {nm} nm", nm=_num(remaining, 0, ctx.language))
        fraction = 1.0 - remaining / total if total else 0.0
    else:
        middle, fraction = _t(ctx, "Uçuş planı yok"), 0.0
    text(draw, (14, 94), _t(ctx, "Rota"), 14, WHITE)
    text(draw, (WIDTH / 2, 94), middle, 14, WHITE, anchor="ma")
    text(draw, (WIDTH - 14, 94), _t(ctx, "Varış"), 14, WHITE, anchor="ra")
    progress_bar(draw, (14, 116, WIDTH - 14, 124), fraction, MODE_MSFS["cruise"][2])
    now = ctx.now or dt.datetime.now()
    ete = f.destination_ete_s
    eta = (now + dt.timedelta(seconds=ete)).strftime("%H:%M") if ete else "—"
    fuel_dest = getattr(state, "fuel_at_destination_lbs", None)
    flow = getattr(state, "fuel_flow_pph", None)
    if fuel_dest is None:
        fuel_value, fuel_sub, fuel_color = "—", _t(ctx, "hesaplanıyor"), WHITE
    else:
        reserve = (flow or 0.0) * 0.75  # 45 min final reserve at current flow
        above = fuel_dest >= reserve
        fuel_value = f"{_num(_tons(max(0.0, fuel_dest)), 1, ctx.language)} t"
        fuel_sub = _t(ctx, "Rezerv üstü") if above else _t(ctx, "Rezerv altı")
        fuel_color = GREEN if above else RED
    head = f.headwind_kt
    if head is None:
        wind_value, wind_sub = "—", f"GS {_num(f.ground_speed_kt, 0)}"
    else:
        wind_value = f"{'-' if head > 0 else '+'}{int(round(abs(head)))} kt"
        wind_sub = (_t(ctx, "karşıdan") if head > 0 else _t(ctx, "arkadan")) + f" · GS {_num(f.ground_speed_kt, 0)}"
    _three_panels(draw, background, 136, 306, (
        (_t(ctx, "Varış (ETA)"), eta, _duration(ctx, ete)),
        (_t(ctx, "Varışta yakıt"), fuel_value, fuel_sub, fuel_color),
        (_t(ctx, "Rüzgar"), wind_value, wind_sub),
    ), value_size=40)
    return image


def render_msfs_approach(ctx: NewScreenContext) -> Image.Image:
    image, draw, background = _msfs_base(ctx, "approach")
    f = ctx.flight
    if f is None or not getattr(f, "updated_at", 0):
        _no_data(draw, ctx, 180)
        return image
    right = ""
    if f.next_wp_distance_nm is not None:
        right = _t(ctx, "Sonraki nokta {nm} nm", nm=_num(f.next_wp_distance_nm, 1, ctx.language))
    _header(draw, ctx, 68, _t(ctx, "YAKLAŞMA"), right)
    ias = f.airspeed_kt
    if f.vapp_kt and ias is not None:
        diff = ias - f.vapp_kt
        speed_label = _t(ctx, "Hız / Vapp")
        speed_sub = f"Vapp≈{int(round(f.vapp_kt))} · {'+' if diff >= 0 else '−'}{int(round(abs(diff)))}"
        speed_color = GREEN if abs(diff) <= 5 else ORANGE if abs(diff) <= 10 else RED
    else:
        speed_label, speed_sub, speed_color = _t(ctx, "Hız"), "kt IAS", WHITE
    _three_panels(draw, background, 92, 238, (
        (speed_label, _num(ias, 0), speed_sub, speed_color),
        (_t(ctx, "Alçalma"), _num(f.vertical_speed_fpm, 0, ctx.language), "fpm"),
        (_t(ctx, "Yükseklik"), _num(f.altitude_agl_ft, 0, ctx.language), "ft AGL"),
    ))
    x = 14
    if f.gear_down is not None:
        x = chip(draw, x, 250, _t(ctx, "Teker AÇIK") if f.gear_down else _t(ctx, "Teker KAPALI"),
                 CHIP_OK if f.gear_down else CHIP_WARN) + 8
    if f.flaps_index is not None:
        x = chip(draw, x, 250, _t(ctx, "Flap {n}", n=f.flaps_index), CHIP_OK) + 8
    if f.spoilers_armed is not None:
        chip(draw, x, 250, "Spoiler ARM" if f.spoilers_armed else _t(ctx, "Spoiler KAPALI"),
             CHIP_OK if f.spoilers_armed else CHIP_WARN)
    return image


LANDING_STYLE = {
    "soft": ("YUMUŞAK", GREEN, "#08240F"),
    "firm": ("NORMAL", ORANGE, "#2A1604"),
    "hard": ("SERT", RED, "#2A0B0B"),
}


def render_msfs_landing(ctx: NewScreenContext) -> Image.Image:
    image, draw, background = _msfs_base(ctx, "landing")
    state = ctx.flight_state
    landing = getattr(state, "landing", None)
    average = getattr(state, "landing_average_fpm", None)
    right = (
        _t(ctx, "Son 5 iniş ort. {v} fpm", v=int(round(average))) if average is not None else ""
    )
    _header(draw, ctx, 68, _t(ctx, "İNİŞ"), right)
    if landing is None:
        _no_data(draw, ctx, 180)
        return image
    text(draw, (14, 96), _t(ctx, "Temas anı dikey hız"), 14, WHITE)
    width = text(draw, (14, 180), str(int(round(landing.vertical_speed_fpm))), 72, TEXT,
                 condensed=True, anchor="ls")
    text(draw, (24 + width, 176), "fpm", 26, WHITE, anchor="ls")
    label, fill, ink = LANDING_STYLE[landing.rating]
    label = _t(ctx, label)
    pill_width = int(draw.textlength(label, font=font(34, True))) + 32
    box = (WIDTH - 14 - pill_width, 116, WIDTH - 14, 170)
    rounded(draw, box, 10, fill)
    text(draw, ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2 + 1), label, 34, ink, condensed=True, anchor="mm")
    f = ctx.flight
    cells = (
        (_t(ctx, "G kuvveti"), "—" if landing.g_force is None else f"{landing.g_force:.2f} G"),
        (_t(ctx, "Zıplama"), _t(ctx, "Var") if landing.bounced else _t(ctx, "Yok")),
        (_t(ctx, "Yer hızı"), "—" if f is None or f.ground_speed_kt is None else f"{int(round(f.ground_speed_kt))} kt"),
        (_t(ctx, "Kayıtlı iniş"), str(getattr(state, "landing_count", "") or "—")),
    )
    for (left, _right), (label_text, value) in zip(_columns(14, WIDTH - 14, 4, 8), cells):
        text(draw, (left, 200), label_text, 14, WHITE)
        text(draw, (left, 240), value, 20, TEXT, anchor="ls")
    return image


def _mhz(value: float | None) -> str:
    return "—" if not value else f"{value:.3f}"


def render_msfs_radio(ctx: NewScreenContext) -> Image.Image:
    image, draw, background = _msfs_base(ctx, "radio")
    f = ctx.flight
    if f is None or not getattr(f, "updated_at", 0):
        _no_data(draw, ctx, 180)
        return image
    columns = _columns(14, WIDTH - 14, 2, 10)
    rows = ((68, 182), (192, 306))
    state_names = {0: "OFF", 1: "STBY", 2: "TEST", 3: "ON", 4: "ALT"}
    cells = (
        (_t(ctx, "COM1 aktif / yedek"), _mhz(f.com1_active_mhz), _mhz(f.com1_standby_mhz), GREEN, WHITE),
        (_t(ctx, "NAV1 aktif / yedek"), _mhz(f.nav1_active_mhz), _mhz(f.nav1_standby_mhz), TEXT, WHITE),
        (_t(ctx, "Transponder"), f.transponder_code or "—",
         state_names.get(f.transponder_state, "—") if f.transponder_state is not None else "—",
         TEXT, GREEN if f.transponder_state in (3, 4) else ORANGE),
        (_t(ctx, "Sonraki nokta"), f.next_wp_id or "—",
         "—" if f.next_wp_distance_nm is None else f"{_num(f.next_wp_distance_nm, 1, ctx.language)} nm",
         TEXT, WHITE),
    )
    for index, (label, value, sub, value_color, sub_color) in enumerate(cells):
        left, right = columns[index % 2]
        top, bottom = rows[index // 2]
        rounded(draw, (left, top, right, bottom), 10, panel_color(background))
        text(draw, (left + 10, top + 10), label, 14, WHITE)
        text(draw, (left + 10, (top + bottom) / 2 + 14), fit(draw, value, 38, right - left - 20, True),
             38, value_color, condensed=True, anchor="ls")
        text(draw, (left + 10, bottom - 10), sub, 16 if index < 2 else 14, sub_color, anchor="ls")
    return image


MSFS_RENDERERS = {
    "ground": render_msfs_ground,
    "cruise": render_msfs_cruise,
    "approach": render_msfs_approach,
    "landing": render_msfs_landing,
    "radio": render_msfs_radio,
}


# ---------------------------------------------------------------------------
# State helpers used by the worker
# ---------------------------------------------------------------------------

def cs_screen_for(game) -> str:
    """Pick buy / death / live from official GSI round state."""
    if game is None:
        return "live"
    phase = getattr(game, "round_phase", "")
    if phase == "freezetime":
        return "buy"
    if phase == "over" or getattr(game, "alive", None) is False:
        return "death"
    return "live"


class AlertTracker:
    """30 s rolling averages; raise an alert at the red threshold."""

    WINDOW_SECONDS = 30.0

    def __init__(self) -> None:
        self._samples: dict[str, collections.deque] = {
            key: collections.deque() for key in ("cpu_temp", "gpu_temp", "ping")
        }

    def _average(self, key: str, value: float | None, now: float) -> float | None:
        samples = self._samples[key]
        if value is not None:
            samples.append((now, float(value)))
        while samples and now - samples[0][0] > self.WINDOW_SECONDS:
            samples.popleft()
        if not samples:
            return None
        return sum(item[1] for item in samples) / len(samples)

    def update(self, metrics: dict, ping_ms: float | None = None, language: str = "tr",
               now: float | None = None) -> tuple[Alert, ...]:
        now = time.monotonic() if now is None else now
        alerts = []
        for component in ("gpu", "cpu"):
            current = metrics.get(f"{component}_temp")
            average = self._average(f"{component}_temp", current, now)
            limit = THRESHOLDS[(component, "temp")][1]
            if average is not None and average >= limit and current is not None:
                alerts.append(Alert(
                    f"{component.upper()} {int(round(current))}°C",
                    translate("Sınır {limit}°C · son 30 sn ortalaması {avg}°C", language,
                              limit=int(limit), avg=int(round(average))),
                    "critical" if current >= limit + 10 else "warn",
                ))
        baseline = self._average("ping", ping_ms, now)
        if ping_ms is not None and baseline is not None and ping_ms > baseline * 1.5 + 15:
            alerts.append(Alert(
                f"Ping {int(round(ping_ms))} ms",
                translate("Ani ping artışı · ortalama {avg} ms", language, avg=int(round(baseline))),
            ))
        return tuple(alerts)


def render_new_screen(ctx: NewScreenContext) -> Image.Image:
    ctx.language = normalize_language(ctx.language)
    if ctx.mode == "faceit":
        return render_faceit(ctx)
    if ctx.mode == "cs2" and ctx.game is not None:
        return {
            "buy": render_cs_buy, "death": render_cs_death,
        }.get(ctx.cs_screen, render_cs_live)(ctx)
    if ctx.mode == "msfs":
        phase = getattr(ctx.flight_state, "phase", "ground") or "ground"
        return MSFS_RENDERERS.get(phase, render_msfs_ground)(ctx)
    return render_main(ctx)


__all__ = [
    "AlertTracker", "BatteryInfo", "NewScreenContext", "buy_suggestion", "cs_screen_for",
    "loss_award", "render_new_screen", "threshold_color",
]
