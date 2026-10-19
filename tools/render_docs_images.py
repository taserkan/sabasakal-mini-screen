"""Render the real English mini-display modes used by the README."""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import render_screen  # noqa: E402
from battery_runtime import BatteryDevice  # noqa: E402
from gui import PALETTES  # noqa: E402
from media_runtime import AudioSpectrumSnapshot, MediaSnapshot  # noqa: E402
from msfs_runtime import FlightTelemetry  # noqa: E402
from weather_runtime import WeatherReading  # noqa: E402


METRICS = {
    "cpu_load": 48.0, "cpu_temp": 63.0, "cpu_freq": 5.05,
    "gpu_load": 76.0, "gpu_temp": 71.0, "gpu_memory": 8640.0,
    "ram_load": 64.0, "ram_used": 20.2,
}
COLORS = PALETTES["Kuzey"]
LABELS = {"cpu": "9800X3D", "gpu": "RTX 4070 Ti", "ram": "32 GB RAM"}
GAME_BATTERIES = (
    BatteryDevice("mouse", "PRO X SUPERLIGHT 2", 75, "mouse"),
    BatteryDevice("headset", "PRO X WIRELESS", 74, "headset"),
    BatteryDevice("keyboard", "G915 TKL", 61, "keyboard"),
)


def frame(**kwargs) -> Image.Image:
    return render_screen(METRICS, COLORS, hardware_labels=LABELS, language="en", **kwargs)


def main() -> None:
    output = ROOT / "docs" / "images"
    output.mkdir(parents=True, exist_ok=True)
    bars = (0.18, 0.34, 0.56, 0.82, 0.64, 0.42, 0.73, 0.91,
            0.77, 0.48, 0.32, 0.62, 0.84, 0.58, 0.36, 0.21)
    samples = [
        ("Daily / media", frame(
            media_snapshot=MediaSnapshot(
                platform="Spotify", title="Now playing",
                artist="Live rhythm visualisation", status="playing",
            ),
            audio_spectrum=AudioSpectrumSnapshot(bars=bars, level=0.62, active=True),
            battery_devices=GAME_BATTERIES,
        )),
        ("MSFS 2024", frame(
            msfs_active=True,
            flight_telemetry=FlightTelemetry(
                altitude_ft=12540.0, airspeed_kt=146.0,
                vertical_speed_fpm=820.0, heading_deg=278.0,
                connected=True, updated_at=1.0,
            ),
            msfs_layout="cockpit",
            battery_devices=GAME_BATTERIES,
        )),
        ("Weather + wireless", frame(
            weather_readings=(
                WeatherReading("London", 17.0, 61),
                WeatherReading("Berlin", 21.0, 3),
                WeatherReading("Madrid", 29.0, 0),
            ),
            battery_devices=GAME_BATTERIES,
        )),
        ("CS2 theme", frame(
            weather_readings=(
                WeatherReading("London", 17.0, 61),
                WeatherReading("Berlin", 21.0, 3),
                WeatherReading("Madrid", 29.0, 0),
            ),
            battery_devices=GAME_BATTERIES,
            cs2_active=True,
        )),
    ]

    try:
        caption_font = ImageFont.truetype(r"C:\Windows\Fonts\segoeuib.ttf", 20)
    except OSError:
        caption_font = ImageFont.load_default()

    cell_width, cell_height = 500, 370
    rows = (len(samples) + 1) // 2
    sheet = Image.new("RGB", (cell_width * 2, cell_height * rows), "#0D0F13")
    draw = ImageDraw.Draw(sheet)
    for index, (caption, rendered) in enumerate(samples):
        row, column = divmod(index, 2)
        left, top = column * cell_width + 10, row * cell_height + 38
        draw.text((left, top - 29), caption, font=caption_font, fill="#F3F5F7")
        sheet.paste(rendered, (left, top))
        safe_name = caption.lower().replace(" / ", "-").replace(" · ", "-")
        safe_name = safe_name.replace(" ", "-")
        rendered.save(output / f"{safe_name}.png")
    sheet.save(output / "mini-screen-modes-en.png")


if __name__ == "__main__":
    main()
