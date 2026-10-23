# SPDX-License-Identifier: GPL-3.0-or-later
"""The "Yeni" (new) screen design for the 480x320 Turing panel.

Pure PIL rendering of the HTML mockups in the design hand-off:
desktop (Main, also drawn in CS2 orange while CS2 runs), in-game strip
(Strip) and the MSFS phase screens (ground, cruise, approach, landing,
radio). Nothing here reads game memory or sends input; MSFS values come
exclusively from read-only SimConnect variables.
"""

from __future__ import annotations

import dataclasses
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

import thresholds as _thresholds
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
CHIP_OK = ("#12301C", "#86EFAC")
CHIP_WARN = ("#3A2410", "#FFB870")
CHIP_BAD = ("#3A1414", "#FCA5A5")
STRIP_HEIGHT = 56
STRIP_PAD = 6  # min gap between strip text and a divider
STRIP_RULE = 1  # divider width
STRIP_VALUE_SIZES = (22, 20, 18)

# Mode palettes: (root background, strip background, accent).
MODE_DESKTOP = ("#0B0F14", "#141B24", "#3FB5FF")
MODE_CS2 = ("#1C1305", "#2B1D07", "#F5A524")
MODE_MSFS = {
    "ground": ("#0C1A10", "#12281A", "#4ADE80"),
    "cruise": ("#0A1530", "#10224A", "#3FB5FF"),
    "radio": ("#0A1530", "#10224A", "#3FB5FF"),
    "approach": ("#1A1030", "#27184A", "#A78BFA"),
    "landing": ("#0A1F1F", "#103030", "#2DD4BF"),
}

# Threshold colours used everywhere a CPU/GPU value appears (shared limits).
THRESHOLDS = _thresholds.THRESHOLDS


def threshold_color(component: str, kind: str, value: float | None) -> str:
    """green below the first limit, orange up to the second, red at/above it."""
    lvl = _thresholds.level(component, kind, value)
    if lvl is None:
        return WHITE
    return (GREEN, ORANGE, RED)[lvl]


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
    dpi: int | None = None


@dataclass
class NewScreenContext:
    metrics: dict
    batteries: tuple = ()
    weather: tuple = ()
    now: dt.datetime | None = None
    language: str = "tr"
    fps: float | None = None
    mode: str = "desktop"  # desktop | cs2 (desktop in CS2 colours) | msfs
    flight: object | None = None  # msfs_runtime.FlightTelemetry
    flight_state: object | None = None  # msfs_runtime.FlightPhaseState
    hardware_labels: dict = field(default_factory=dict)
    # Desktop carousel page: main | clock | weather | energy (v1.3).
    page: str = "main"
    energy: object | None = None  # energy_runtime.EnergySummary
    faceit: object | None = None  # faceit_runtime.FaceitSnapshot (v1.4)
    # v1.5: short banner (e.g. "GAMER 2") drawn over any screen.
    toast: str = ""
    game_name: str = ""


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


PLACEHOLDER = "—"


@functools.lru_cache(maxsize=128)
def _digit_cell(typeface) -> float:
    """Width of the widest digit: every digit is drawn in a cell this wide."""
    return max(typeface.getlength(d) for d in "0123456789")


def _tabular(value: str) -> bool:
    return any(ch.isdigit() for ch in value)


def _tlen(value: str, typeface, spacing: float = 0.0) -> float:
    """Text width with tabular (fixed-cell) digits.

    The UI fonts have proportional figures ("1" is narrower than "0"), which
    made every neighbour shift when a value changed. Measuring and drawing
    digits in fixed cells keeps a field's width a function of its *shape*
    only, never of the digits shown.
    """
    if not _tabular(value):
        return typeface.getlength(value) + spacing * max(0, len(value) - 1)
    cell = _digit_cell(typeface)
    width = sum(cell if ch.isdigit() else typeface.getlength(ch) for ch in value)
    return width + spacing * max(0, len(value) - 1)


def slot_width(template: str, size: int, condensed: bool = False) -> float:
    """Fixed slot for a numeric field, measured from its widest form ("100%")."""
    return _tlen(template, font(size, condensed))


def _text_width(draw: ImageDraw.ImageDraw, text: str, typeface, spacing: float = 0.0) -> float:
    return _tlen(text, typeface, spacing)


def text(draw: ImageDraw.ImageDraw, xy, value: str, size: int, color: str = WHITE,
         condensed: bool = False, anchor: str = "la", spacing: float = 0.0) -> float:
    """Draw text; returns its width. ``spacing`` emulates CSS letter-spacing.

    Digits are tabular. A bare placeholder ("—" / empty) is never drawn: an
    unavailable metric is hidden, not shown as a dash.
    """
    if not value or value.strip() == PLACEHOLDER:
        return 0.0
    typeface = font(size, condensed)
    if not spacing and not _tabular(value):
        draw.text(xy, value, font=typeface, fill=color, anchor=anchor)
        return _tlen(value, typeface)
    width = _tlen(value, typeface, spacing)
    x, y = xy
    if anchor[0] == "r":
        x -= width
    elif anchor[0] == "m":
        x -= width / 2
    vertical = "l" + anchor[1]
    cell = _digit_cell(typeface)
    for char in value:
        if char.isdigit():
            glyph = typeface.getlength(char)
            draw.text((x + (cell - glyph) / 2, y), char, font=typeface, fill=color, anchor=vertical)
            x += cell + spacing
        else:
            draw.text((x, y), char, font=typeface, fill=color, anchor=vertical)
            x += typeface.getlength(char) + spacing
    return width


def fit(draw: ImageDraw.ImageDraw, value: str, size: int, max_width: float, condensed=False) -> str:
    typeface = font(size, condensed)
    if _tlen(value, typeface) <= max_width:
        return value
    while value and _tlen(value + "…", typeface) > max_width:
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
    width = int(_tlen(label, font(size))) + 18
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
        dpi = getattr(device, "dpi", None) if kind == "mouse" else None
        items.append(BatteryInfo(kind, max(0, min(100, int(percent))), dpi))
    items.sort(key=lambda item: BATTERY_ORDER.get(item.kind, 3))
    return items[: max(0, limit)]


def battery_value_parts(info: BatteryInfo) -> list[tuple[str, str]]:
    """``82%`` (+ `` · 1200`` DPI for a G HUB mouse)."""
    parts = [(_pct(info.percent), battery_color(info.percent) if info.percent <= 30 else TEXT)]
    if info.dpi is not None:
        parts.append((f" · {info.dpi}", TEXT))
    return parts


def _part_slot(value: str) -> str:
    """Slot template of a part: battery percentages reserve two tabular
    digits ("00%"); only a full 100% battery takes the wider slot."""
    if value.endswith("%"):
        return "100%" if value == "100%" else "00%"
    return value


def _parts_width(draw, parts, size: int) -> float:
    return sum(_tlen(_part_slot(value), font(size)) for value, _ in parts)


def _draw_parts(draw, x: float, y: float, parts, size: int, anchor_v: str) -> float:
    for value, color in parts:
        x += _tlen(_part_slot(value), font(size))
        text(draw, (x, y), value, size, color, anchor="r" + anchor_v)
    return x


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
    label_y, value_y = 20, 45
    pad, rule = STRIP_PAD, STRIP_RULE
    divider = blend(strip_bg, WHITE, 0.25)

    base = [
        ("CPU", (
            (_pct(m.get("cpu_load")), threshold_color("cpu", "load", m.get("cpu_load")), "100%"),
            (_deg(m.get("cpu_temp")), threshold_color("cpu", "temp", m.get("cpu_temp")), "100°"),
        )),
        ("GPU", (
            (_pct(m.get("gpu_load")), threshold_color("gpu", "load", m.get("gpu_load")), "100%"),
            (_deg(m.get("gpu_temp")), threshold_color("gpu", "temp", m.get("gpu_temp")), "100°"),
        )),
    ]
    if ctx.fps is not None:
        # FPS only exists when a source measures it; never reserve its slot.
        base.append(("FPS", ((str(int(round(ctx.fps))), TEXT, "999"),)))
    ram_cell = None
    if allow_ram:
        load, _used, _total = ram_reading(m)
        if load is not None:
            ram_cell = ("RAM", ((_pct(load), threshold_color("ram", "load", load), "100%"),))

    # Every value sits in a fixed slot measured from its widest form, so a
    # reading going 9 -> 10 or 99 -> 100 never moves anything else. Each
    # group is a column between thin dividers; its label is centred over
    # the value block, and the value font steps down before anything overlaps.
    def block_width(parts, size) -> float:
        face = font(size, True)
        return sum(_tlen(t, face) for _v, _c, t in parts) + _tlen(" ", face) * (len(parts) - 1)

    def columns(cells, size):
        placed, x = [], 4 - rule
        for label, parts in cells:
            x += rule
            width = math.ceil(max(block_width(parts, size), _tlen(label, font(12), 1.0)))
            left = x + pad
            placed.append((label, parts, left, left + width))
            x = left + width + pad
        return placed, x  # x = left edge of the trailing divider

    small = font(14)
    center_y = (STRIP_HEIGHT - 3) / 2

    def plan(infos, start):
        right = WIDTH - 10
        placed = []
        for info in reversed(infos):
            label = _t(ctx, BATTERY_LABELS[info.kind][1]) + " "
            value = battery_value_parts(info)
            width = _tlen(label, small) + _parts_width(draw, value, 14)
            if right - width < start:
                break
            placed.append((info, label, value, right - width, right))
            right -= width + 6
        return placed

    infos = present_batteries(ctx.batteries)
    choice = None
    attempts = [(cells, size) for cells in ((base + [ram_cell],) if ram_cell else ()) + (base,)
                for size in STRIP_VALUE_SIZES]
    for cells, size in attempts:
        cols, end = columns(cells, size)
        start = end + rule + pad
        if end > WIDTH - 4:
            continue
        planned = plan(infos, start)
        if len(planned) < len(infos):
            # Never lose a device for the DPI suffix: drop the DPI first.
            planned = plan([dataclasses.replace(info, dpi=None) for info in infos], start)
        if len(planned) == len(infos):
            choice = (cols, end, size, planned)
            break
    if choice is None:
        cols, end = columns(base, STRIP_VALUE_SIZES[-1])
        size = STRIP_VALUE_SIZES[-1]
        planned = plan([dataclasses.replace(info, dpi=None) for info in infos], end + rule + pad)
        choice = (cols, end, size, planned)
    cols, end, size, planned = choice
    value_font = font(size, True)
    space = _tlen(" ", value_font)
    top, bottom = 8, STRIP_HEIGHT - 4 - 8
    rules = []
    for index, (label, parts, left, right) in enumerate(cols):
        middle = (left + right) / 2
        text(draw, (middle, label_y), label, 12, WHITE, anchor="ms", spacing=1.0)
        cursor = middle - block_width(parts, size) / 2
        for i, (value, color, template) in enumerate(parts):
            if i:
                cursor += space
            cursor += _tlen(template, value_font)
            text(draw, (cursor, value_y), value, size, color, condensed=True, anchor="rs")
        layout.append((label.lower(), (left, 6, right, value_y + 4)))
        rx = right + pad
        if index < len(cols) - 1 or planned:
            draw.rectangle((round(rx), top, round(rx) + rule - 1, bottom), fill=divider)
            rules.append((f"divider:{index}", (round(rx), top, round(rx) + rule, bottom)))
    layout.extend(rules)

    for info, label, value, left, item_right in planned:
        label_end = left + text(draw, (left, center_y), label, 14, WHITE, anchor="lm")
        _draw_parts(draw, label_end, center_y, value, 14, "m")
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
# Desktop (Main.dc.html)
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
    return lines


def _gpu_details(ctx) -> list[str]:
    m = ctx.metrics
    lines = []
    if m.get("gpu_memory"):
        lines.append(f"VRAM {float(m['gpu_memory']) / 1024.0:.1f} GB")
    if m.get("gpu_power"):
        lines.append(f"{int(round(float(m['gpu_power'])))} W")
    return lines


def main_layout(ctx: NewScreenContext, draw: ImageDraw.ImageDraw | None = None) -> dict:
    """Boxes of every block of the desktop screen (used by the renderer and tests)."""
    if draw is None:
        draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    now = ctx.now or dt.datetime.now()
    layout: dict = {}
    clock = now.strftime("%H:%M")
    clock_width = _tlen(clock, font(30, True))
    layout["clock"] = (14, 14, 14 + clock_width, 46)
    cursor = 14 + clock_width + 14
    load, used, total = ram_reading(ctx.metrics)
    if load is not None:
        parts = [("RAM ", WHITE, "RAM "), (_pct(load), threshold_color("ram", "load", load), "100%")]
        if used is not None and total:
            # Used GB never has more integer digits than the total.
            used_slot = "0" * max(len(f"{total:.0f}"), len(f"{used:.0f}")) + ".0"
            parts.append((f" · {used:.1f}/{total:.0f} GB", WHITE, f" · {used_slot}/{total:.0f} GB"))
        elif used is not None:
            parts.append((f" · {used:.1f} GB", WHITE, f" · {max(99.9, used):.1f} GB"))
        width = sum(_tlen(template, font(14)) for _value, _color, template in parts)
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
            width = _tlen(weather, font(14))
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


def main_theme(mode: str) -> tuple[str, str, str]:
    """(background, ring track, clock colour) of the desktop screen.

    While CS2 runs the same desktop screen is drawn in CS2's orange: during a
    match nobody reads the panel, so only the colours follow the game.
    """
    if mode == "cs2":
        background, _strip, accent = MODE_CS2
        return background, blend(background, accent, 0.22), accent
    return MODE_DESKTOP[0], TRACK, TEXT


def render_main(ctx: NewScreenContext) -> Image.Image:
    background, track, clock_color = main_theme(ctx.mode)
    image = Image.new("RGB", (WIDTH, HEIGHT), background)
    draw = ImageDraw.Draw(image)
    if ctx.mode == "cs2":
        draw.rectangle((0, 0, WIDTH, 3), fill=MODE_CS2[2])
    now = ctx.now or dt.datetime.now()
    layout = main_layout(ctx, draw)
    text(draw, (14, 44), now.strftime("%H:%M"), 30, clock_color, condensed=True, anchor="ls")
    if "ram" in layout:
        x = layout["ram"][0]
        for value, color, template in layout["ram_parts"]:
            x += _tlen(template, font(14))
            text(draw, (x, 42), value, 14, color, anchor="rs")
    if "weather" in layout:
        text(draw, (WIDTH - 14, 42), layout["weather_text"], 14, WHITE, anchor="rs")
    _system_panel(image, draw, layout["cpu"], background, "cpu", ctx,
                  96, track, _cpu_details(ctx), 30, False)
    _system_panel(image, draw, layout["gpu"], background, "gpu", ctx,
                  96, track, _gpu_details(ctx), 30, False)
    for info, box in layout["batteries"]:
        left, battery_top, right, _bottom = box
        percent = info.percent
        rounded(draw, box, 8, panel_color(background))
        text(draw, (left + 10, battery_top + 8), _t(ctx, BATTERY_LABELS[info.kind][0]), 14, WHITE)
        parts = battery_value_parts(info)
        _draw_parts(draw, right - 10 - _parts_width(draw, parts, 14), battery_top + 8, parts, 14, "a")
        progress_bar(draw, (left + 10, battery_top + 28, right - 10, battery_top + 34),
                     percent / 100.0, battery_color(percent))
    return image


def new_design_mode(game_active: bool, msfs_active: bool = False) -> str:
    """Screen family for the Claude design.

    CS2 running shows the desktop screen in the CS2 colours; MSFS has its own
    flight screens.
    """
    if game_active:
        return "cs2"
    if msfs_active:
        return "msfs"
    return "desktop"


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
        (_t(ctx, "Yer hızı"), _num(f.ground_speed_kt, 0, ctx.language), _t(ctx, "kt · sınır 25"),
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
    altitude = _altitude_label(ctx, f.altitude_ft)
    title = f"{phase} · {altitude}" if altitude else phase
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
        wind_value, wind_sub = "—", f"GS {_num(f.ground_speed_kt, 0, ctx.language)}"
    else:
        wind_value = f"{'-' if head > 0 else '+'}{int(round(abs(head)))} kt"
        wind_sub = (_t(ctx, "karşıdan") if head > 0 else _t(ctx, "arkadan")) + f" · GS {_num(f.ground_speed_kt, 0, ctx.language)}"
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
        (speed_label, _num(ias, 0, ctx.language), speed_sub, speed_color),
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
    pill_width = int(_tlen(label, font(34, True))) + 32
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


class ScreenStabilizer:
    """Hysteresis for screen switches so the LCD never flaps between layouts.

    A cross-family switch (desktop / CS2 / MSFS) repaints most of the panel
    over serial, so a requested screen must be

    * requested continuously for ``confirm`` seconds (debounces flicker such
      as an MSFS phase bouncing at a threshold), and
    * the currently shown screen must have been up for ``min_dwell`` seconds.
    """

    def __init__(self, min_dwell: float = 2.5, confirm: float = 0.6) -> None:
        self.min_dwell = min_dwell
        self.confirm = confirm
        self.shown: tuple | None = None
        self._shown_since = 0.0
        self._pending: tuple | None = None
        self._pending_since = 0.0

    def update(self, requested: tuple, now: float | None = None) -> tuple:
        now = time.monotonic() if now is None else now
        if self.shown is None or requested == self.shown:
            if requested != self.shown:
                self._shown_since = now
            self.shown = requested
            self._pending = None
            return self.shown
        if requested != self._pending:
            self._pending, self._pending_since = requested, now
        if (now - self._pending_since >= self.confirm
                and now - self._shown_since >= self.min_dwell):
            self.shown, self._shown_since, self._pending = requested, now, None
        return self.shown


# ---------------------------------------------------------------------------
# Desktop carousel pages (shown while the PC is idle, v1.3)
# ---------------------------------------------------------------------------

ICON_ROOT = Path(getattr(sys, "_MEIPASS", ROOT)) / "icons" / "png"
WEATHER_ICONS = {
    "clear": "sun", "cloud": "cloud", "fog": "cloud-fog",
    "snow": "snowflake", "storm": "cloud-lightning", "rain": "cloud-rain",
}
MONTHS = ("Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz",
          "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık")
WEEKDAYS = ("Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar")
CAROUSEL_PAGES = ("clock", "weather", "main", "faceit", "energy")


@functools.lru_cache(maxsize=16)
def _icon(name: str, size: int, color: str) -> Image.Image | None:
    """A Lucide PNG recoloured to ``color`` (alpha kept), or None if missing."""
    try:
        with Image.open(ICON_ROOT / f"{name}.png") as source:
            alpha = source.convert("RGBA").getchannel("A").resize((size, size), Image.Resampling.LANCZOS)
    except OSError:
        return None
    tinted = Image.new("RGBA", (size, size), _rgb(color) + (0,))
    tinted.putalpha(alpha)
    return tinted


def available_pages(ctx: NewScreenContext) -> tuple[str, ...]:
    """Carousel pages that have something to show."""
    pages = []
    for page in CAROUSEL_PAGES:
        if page == "weather" and not ctx.weather:
            continue
        if page == "energy" and ctx.energy is None:
            continue
        if page == "faceit" and getattr(ctx.faceit, "elo", None) is None:
            continue
        pages.append(page)
    return tuple(pages)


def _date_line(ctx: NewScreenContext, now: dt.datetime) -> str:
    month = _t(ctx, MONTHS[now.month - 1])
    weekday = _t(ctx, WEEKDAYS[now.weekday()])
    if ctx.language == "en":
        return f"{weekday}, {month} {now.day}"
    return f"{now.day} {month} {weekday}"


def _weather_icon_name(reading, now: dt.datetime) -> str:
    kind = _weather_kind(int(getattr(reading, "weather_code", 0)))
    if kind == "clear" and not 7 <= now.hour < 19:
        return "moon"
    return WEATHER_ICONS[kind]


def render_clock_page(ctx: NewScreenContext) -> Image.Image:
    background = MODE_DESKTOP[0]
    image = Image.new("RGB", (WIDTH, HEIGHT), background)
    draw = ImageDraw.Draw(image)
    now = ctx.now or dt.datetime.now()
    text(draw, (WIDTH / 2, 178), now.strftime("%H:%M"), 132, TEXT, condensed=True, anchor="ms")
    text(draw, (WIDTH / 2, 224), _date_line(ctx, now), 22, MODE_DESKTOP[2], anchor="ms")
    if ctx.weather:
        reading = ctx.weather[0]
        kind = _weather_kind(int(getattr(reading, "weather_code", 0)))
        line = f"{reading.city} {int(round(reading.temperature_c))}° · {_t(ctx, WEATHER_TEXT[kind])}"
        text(draw, (WIDTH / 2, 278), fit(draw, line, 16, WIDTH - 60), 16, WHITE, anchor="ms")
    return image


def render_weather_page(ctx: NewScreenContext) -> Image.Image:
    background = MODE_DESKTOP[0]
    image = Image.new("RGB", (WIDTH, HEIGHT), background)
    draw = ImageDraw.Draw(image)
    now = ctx.now or dt.datetime.now()
    text(draw, (14, 30), now.strftime("%H:%M"), 22, TEXT, condensed=True, anchor="ls")
    readings = tuple(ctx.weather)[:3]
    if not readings:
        return image
    first = readings[0]
    kind = _weather_kind(int(getattr(first, "weather_code", 0)))
    icon = _icon(_weather_icon_name(first, now), 112, MODE_DESKTOP[2])
    top = 52 if len(readings) > 1 else 78
    if icon is not None:
        image.paste(icon, (34, top + 6), icon)
    text(draw, (170, top + 84), f"{int(round(first.temperature_c))}°", 92, TEXT, condensed=True, anchor="ls")
    text(draw, (172, top + 112), fit(draw, first.city, 22, WIDTH - 190), 22, WHITE, anchor="ls")
    text(draw, (172, top + 136), _t(ctx, WEATHER_TEXT[kind]), 16, MODE_DESKTOP[2], anchor="ls")
    others = readings[1:]
    if others:
        for (left, right), reading in zip(_columns(14, WIDTH - 14, len(others), 10), others):
            box = (left, HEIGHT - 72, right, HEIGHT - 14)
            rounded(draw, box, 10, panel_color(background))
            small = _icon(_weather_icon_name(reading, now), 30, WHITE)
            if small is not None:
                image.paste(small, (left + 12, HEIGHT - 58), small)
            text(draw, (left + 52, HEIGHT - 38), f"{int(round(reading.temperature_c))}°", 26,
                 TEXT, condensed=True, anchor="ls")
            text(draw, (left + 52, HEIGHT - 22), fit(draw, reading.city, 13, right - left - 62),
                 13, WHITE, anchor="ls")
    return image


def _kwh(ctx, value: float) -> str:
    return f"{_num(value, 2 if value < 10 else 1, ctx.language)} kWh"


def _money(ctx, value: float | None) -> str:
    if value is None:
        return _t(ctx, "fiyat girilmedi")
    return f"≈ {_num(value, 2, ctx.language)} TL"


def render_energy_page(ctx: NewScreenContext) -> Image.Image:
    background = MODE_DESKTOP[0]
    image = Image.new("RGB", (WIDTH, HEIGHT), background)
    draw = ImageDraw.Draw(image)
    now = ctx.now or dt.datetime.now()
    text(draw, (14, 38), _t(ctx, "Elektrik"), 24, TEXT, anchor="ls")
    text(draw, (WIDTH - 14, 38), now.strftime("%H:%M"), 22, TEXT, condensed=True, anchor="rs")
    energy = ctx.energy
    if energy is None:
        return image
    watts = getattr(energy, "watts_now", None)
    _stat_panel(draw, (14, 56, WIDTH - 14, 150), background, _t(ctx, "Şu an (tahmini)"),
                "—" if watts is None else f"{int(round(watts))} W",
                value_size=54, value_color=MODE_DESKTOP[2])
    cells = (
        (_t(ctx, "Bugün"), _kwh(ctx, energy.today_kwh), _money(ctx, energy.today_cost)),
        (_t(ctx, "Bu ay"), _kwh(ctx, energy.month_kwh), _money(ctx, energy.month_cost)),
    )
    for (left, right), (label, value, sub) in zip(_columns(14, WIDTH - 14, 2, 10), cells):
        _stat_panel(draw, (left, 160, right, 284), background, label, value, sub, value_size=38)
    text(draw, (WIDTH / 2, 306), _t(ctx, "CPU + GPU + diğer parçalar · monitör hariç"), 12,
         WHITE, anchor="ms")
    return image


FACEIT_ORANGE = "#FF5500"
# FACEIT's own skill-level colours.
FACEIT_LEVEL_COLORS = {1: "#EEEEEE", 2: "#1CE400", 3: "#1CE400", 4: "#FFC800", 5: "#FFC800",
                       6: "#FFC800", 7: "#FFC800", 8: "#FF6309", 9: "#FF6309", 10: "#FE1F00"}


def render_faceit_page(ctx: NewScreenContext) -> Image.Image:
    background = MODE_DESKTOP[0]
    image = Image.new("RGB", (WIDTH, HEIGHT), background)
    draw = ImageDraw.Draw(image)
    now = ctx.now or dt.datetime.now()
    data = ctx.faceit
    text(draw, (14, 38), "FACEIT", 24, FACEIT_ORANGE, anchor="ls")
    nickname = getattr(data, "nickname", "") or ""
    text(draw, (WIDTH - 14, 38), fit(draw, nickname or now.strftime("%H:%M"), 18, 240), 18, TEXT, anchor="rs")
    if data is None or getattr(data, "elo", None) is None:
        text(draw, (WIDTH / 2, 170), _t(ctx, getattr(data, "error", "") or "FACEIT verisi bekleniyor"),
             18, WHITE, anchor="mm")
        return image
    # Level badge + ELO
    level = getattr(data, "level", None)
    level_color = FACEIT_LEVEL_COLORS.get(level or 0, WHITE)
    ring(image, (66, 116), 88, 8, (level or 0) / 10.0, level_color, panel_color(background, 0.12))
    text(draw, (66, 132), str(level or "—"), 40, level_color, condensed=True, anchor="ms")
    text(draw, (130, 132), str(data.elo), 76, TEXT, condensed=True, anchor="ls")
    elo_width = _tlen(str(data.elo), font(76, True))
    text(draw, (136 + elo_width, 104), "ELO", 16, WHITE, anchor="ls")
    change = getattr(data, "elo_today", None)
    if change is not None:
        sign = "+" if change > 0 else ""
        color = GREEN if change > 0 else RED if change < 0 else WHITE
        text(draw, (136 + elo_width, 132), _t(ctx, "bugün {change}", change=f"{sign}{change}"), 18,
             color, anchor="ls")
    # Last five results, newest first
    recent = tuple(getattr(data, "recent", ()))[:5]
    text(draw, (14, 186), _t(ctx, "Son maçlar"), 14, WHITE, anchor="ls")
    for index, match in enumerate(recent):
        left = 14 + index * 52
        box = (left, 196, left + 44, 238)
        rounded(draw, box, 8, "#14361F" if match.won else "#3A1414")
        text(draw, ((box[0] + box[2]) / 2, 225), _t(ctx, "G") if match.won else _t(ctx, "M"), 22,
             GREEN if match.won else RED, condensed=True, anchor="ms")
    # K/D and HS% over the recent matches
    cells = (
        ("K/D", "—" if data.kd is None else _num(data.kd, 2, ctx.language)),
        ("HS%", "—" if data.headshots is None else f"{int(round(data.headshots))}%"),
    )
    for (left, right), (label, value) in zip(_columns(290, WIDTH - 14, 2, 10), cells):
        _stat_panel(draw, (left, 168, right, 238), background, label, value, value_size=30)
    footer = _t(ctx, "Son {n} maç ortalaması", n=len(recent)) if recent else ""
    if getattr(data, "error", ""):
        footer = _t(ctx, data.error)
    text(draw, (14, 300), footer, 13, WHITE, anchor="ls")
    return image


PAGE_RENDERERS = {
    "clock": render_clock_page,
    "weather": render_weather_page,
    "energy": render_energy_page,
    "faceit": render_faceit_page,
}


def render_game_strip(ctx: NewScreenContext) -> Image.Image:
    """In-game screen: status strip on top, large CPU/GPU panels below."""
    background, strip_bg, accent = MODE_CS2
    image = Image.new("RGB", (WIDTH, HEIGHT), background)
    draw_strip(image, ctx, strip_bg, accent)
    draw = ImageDraw.Draw(image)
    track = blend(background, accent, 0.22)
    bottom = HEIGHT - 34
    (l1, r1), (l2, r2) = _columns(14, WIDTH - 14, 2, 10)
    _system_panel(image, draw, (l1, STRIP_HEIGHT + 10, r1, bottom), background, "cpu", ctx,
                  96, track, _cpu_details(ctx), 30, False)
    _system_panel(image, draw, (l2, STRIP_HEIGHT + 10, r2, bottom), background, "gpu", ctx,
                  96, track, _gpu_details(ctx), 30, False)
    now = ctx.now or dt.datetime.now()
    text(draw, (14, HEIGHT - 10), fit(draw, ctx.game_name, 14, 300), 14, accent, anchor="ls")
    text(draw, (WIDTH - 14, HEIGHT - 10), now.strftime("%H:%M"), 16, TEXT, condensed=True, anchor="rs")
    return image


TOAST_HEIGHT = 46


def draw_toast(image: Image.Image, label: str) -> None:
    """Bottom banner, e.g. the monitor's new picture mode."""
    draw = ImageDraw.Draw(image)
    top = HEIGHT - TOAST_HEIGHT
    draw.rectangle((0, top, WIDTH, HEIGHT), fill="#0B0F14")
    draw.rectangle((0, top, WIDTH, top + 2), fill=MODE_DESKTOP[2])
    text(draw, (WIDTH / 2, top + 32), fit(draw, label, 24, WIDTH - 40), 24, TEXT, anchor="ms")


def render_new_screen(ctx: NewScreenContext) -> Image.Image:
    ctx.language = normalize_language(ctx.language)
    if ctx.mode == "msfs":
        phase = getattr(ctx.flight_state, "phase", "ground") or "ground"
        image = MSFS_RENDERERS.get(phase, render_msfs_ground)(ctx)
    elif ctx.mode == "game":
        image = render_game_strip(ctx)
    elif ctx.mode == "desktop" and ctx.page in PAGE_RENDERERS:
        image = PAGE_RENDERERS[ctx.page](ctx)
    else:
        image = render_main(ctx)
    if ctx.toast:
        draw_toast(image, _t(ctx, "Monitör: {mode}", mode=ctx.toast))
    return image


__all__ = [
    "BatteryInfo", "NewScreenContext", "ScreenStabilizer", "main_theme",
    "new_design_mode", "render_new_screen", "threshold_color",
]
