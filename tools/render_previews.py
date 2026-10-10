# SPDX-License-Identifier: GPL-3.0-or-later
"""Render every screen of the new ("Yeni") design to PNG with sample data.

No hardware, game or network access is needed:

    python tools/render_previews.py [output_dir] [--language en] [--rotation 180]

``--rotation 180`` additionally writes the exact pixels sent to a panel that
is mounted upside down (``*-rot180.png``).

PNGs are written to docs/images/new/ by default.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import GameSnapshot, orient_frame  # noqa: E402
from battery_runtime import BatteryDevice  # noqa: E402
from msfs_runtime import FlightPhaseState, FlightTelemetry, LandingRecord  # noqa: E402
from screens_new import AlertTracker, NewScreenContext, render_new_screen  # noqa: E402
from weather_runtime import WeatherReading  # noqa: E402

NOW = dt.datetime(2026, 10, 10, 21, 47)
BATTERIES = (
    BatteryDevice("m", "PRO X SUPERLIGHT 2", 82, "mouse", dpi=800),
    BatteryDevice("k", "G915", 64, "keyboard"),
    BatteryDevice("h", "PRO X WIRELESS", 18, "headset"),
)
DESKTOP = {
    "cpu_load": 38.0, "cpu_temp": 62.0, "cpu_freq": 4.8, "cpu_power": 71.0,
    "gpu_load": 22.0, "gpu_temp": 48.0, "gpu_memory": 3174.0, "gpu_power": 64.0,
    "ram_load": 52.0, "ram_used_gb": 16.6, "ram_total_gb": 32.0,
}
GAMING = {
    "cpu_load": 71.0, "cpu_temp": 66.0, "cpu_freq": 5.1, "cpu_power": 98.0,
    "gpu_load": 96.0, "gpu_temp": 78.0, "gpu_memory": 9830.0, "gpu_power": 265.0,
    "ram_load": 74.0, "ram_used_gb": 23.7, "ram_total_gb": 32.0,
}
ROUND = dict(
    connected=True, in_match=True, team="CT", money=4150, map_name="de_mirage",
    round_number=14, score_ct=6, score_t=7, loss_streak=2, game_mode="competitive",
    match_kills=14, match_deaths=9, match_assists=3, match_mvps=2, match_score=33,
)
FLIGHT = dict(
    connected=True, updated_at=1.0, aircraft_title="Airbus A320neo",
    fuel_lbs=20723.0, total_weight_lbs=150354.0, flaps_index=1, parking_brake=False,
    strobe_on=False, com1_active_mhz=121.800, com1_standby_mhz=119.900,
    nav1_active_mhz=110.550, nav1_standby_mhz=109.300, transponder_code="4521",
    transponder_state=4, next_wp_id="TOMBI",
)


def contexts() -> dict[str, NewScreenContext]:
    weather = (WeatherReading("İstanbul", 18.0, 2),)
    return {
        "main": NewScreenContext(DESKTOP, BATTERIES, weather, NOW),
        "cs2-buy": NewScreenContext(
            GAMING, BATTERIES, now=NOW, fps=287, mode="cs2", cs_screen="buy",
            game=GameSnapshot(round_phase="freezetime", alive=True, **ROUND),
        ),
        "cs2-death": NewScreenContext(
            GAMING, BATTERIES, now=NOW, fps=287, mode="cs2", cs_screen="death",
            game=GameSnapshot(round_phase="live", alive=False, round_kills=2,
                              round_headshots=1, round_damage=187, **ROUND),
        ),
        "cs2-live-alert": NewScreenContext(
            {**GAMING, "gpu_load": 99.0, "gpu_temp": 86.0}, BATTERIES, now=NOW, fps=241,
            mode="cs2", cs_screen="live",
            game=GameSnapshot(round_phase="live", alive=True, **ROUND),
            alerts=AlertTracker().update({**GAMING, "gpu_temp": 86.0}),
        ),
        "cs2-live-dark": NewScreenContext(
            GAMING, BATTERIES, now=NOW, fps=287, mode="cs2", cs_screen="live",
            game=GameSnapshot(round_phase="live", alive=True, **ROUND),
        ),
        "cs2-faceit": NewScreenContext(GAMING, BATTERIES, now=NOW, fps=287, mode="faceit"),
        "msfs-ground": NewScreenContext(
            {**GAMING, "gpu_load": 64.0, "gpu_temp": 58.0}, BATTERIES, now=NOW, mode="msfs",
            flight=FlightTelemetry(on_ground=True, ground_speed_kt=18.0, altitude_agl_ft=0.0, **FLIGHT),
            flight_state=FlightPhaseState(phase="ground"),
        ),
        "msfs-cruise": NewScreenContext(
            {**GAMING, "gpu_load": 64.0, "gpu_temp": 58.0}, BATTERIES, now=NOW, mode="msfs",
            flight=FlightTelemetry(
                on_ground=False, altitude_ft=36000.0, ground_speed_kt=438.0,
                vertical_speed_fpm=0.0, next_wp_distance_nm=84.0, destination_ete_s=5100.0,
                headwind_kt=42.0, **{**FLIGHT, "fuel_lbs": 18000.0},
            ),
            flight_state=FlightPhaseState(
                phase="cruise", route_total_nm=1450.0, route_remaining_nm=612.0,
                fuel_at_destination_lbs=6830.0, fuel_flow_pph=5400.0,
            ),
        ),
        "msfs-approach": NewScreenContext(
            {**GAMING, "gpu_load": 64.0, "gpu_temp": 58.0}, BATTERIES, now=NOW, mode="msfs",
            flight=FlightTelemetry(
                on_ground=False, airspeed_kt=141.0, vapp_kt=138.0, vertical_speed_fpm=-780.0,
                altitude_agl_ft=1940.0, gear_down=True, spoilers_armed=True,
                next_wp_distance_nm=6.2, **{**FLIGHT, "flaps_index": 4},
            ),
            flight_state=FlightPhaseState(phase="approach"),
        ),
        "msfs-landing": NewScreenContext(
            {**GAMING, "gpu_load": 64.0, "gpu_temp": 58.0}, BATTERIES, now=NOW, mode="msfs",
            flight=FlightTelemetry(on_ground=True, ground_speed_kt=96.0, **FLIGHT),
            flight_state=FlightPhaseState(
                phase="landing", landing=LandingRecord(-128.0, 1.18, False, 0.0),
                landing_average_fpm=-164.0, landing_count=5,
            ),
        ),
        "msfs-radio": NewScreenContext(
            {**GAMING, "gpu_load": 64.0, "gpu_temp": 58.0}, BATTERIES, now=NOW, mode="msfs",
            flight=FlightTelemetry(on_ground=False, next_wp_distance_nm=84.0, **FLIGHT),
            flight_state=FlightPhaseState(phase="radio"),
        ),
    }


DEVICE_SETS = {
    0: (),
    1: BATTERIES[:1],
    2: (BATTERIES[0], BATTERIES[2]),  # mouse + headset, no keyboard
    3: BATTERIES,
}
BATTERY_SCREENS = ("main", "cs2-buy", "cs2-live-dark", "cs2-faceit", "msfs-cruise")


def dpi_warning_context() -> NewScreenContext:
    """CS2 running but G HUB still on the desktop profile (1200 DPI)."""
    context = contexts()["cs2-live-dark"]
    context.batteries = (BatteryDevice("m", "PRO X SUPERLIGHT 2", 82, "mouse",
                                       dpi=1200, dpi_warning=True), BATTERIES[2])
    return context


def battery_contexts() -> dict[str, NewScreenContext]:
    """Every screen that shows batteries, with 0/1/2/3 connected devices."""
    result = {}
    base = contexts()
    for name in BATTERY_SCREENS:
        for count, devices in DEVICE_SETS.items():
            context = base[name] if count == 0 else contexts()[name]
            context.batteries = devices
            result[f"{name}-bat{count}"] = context
    result["cs2-dpi-warning"] = dpi_warning_context()
    return result


def classic_battery_frames(language: str = "tr"):
    """Classic (Klasik) idle screen with 0/1/2/3 connected devices."""
    from app import COLORS, render_screen  # noqa: E402
    for count, devices in DEVICE_SETS.items():
        yield f"classic-bat{count}", render_screen(
            dict(DESKTOP, ram_used=16.6), GameSnapshot(), False, dict(COLORS),
            battery_devices=devices, language=language,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", type=Path, default=ROOT / "docs" / "images" / "new")
    parser.add_argument("--language", default="tr", choices=("tr", "en"))
    parser.add_argument("--rotation", default="normal", choices=("normal", "180"))
    parser.add_argument(
        "--batteries", action="store_true",
        help="also write every battery screen with 0/1/2/3 connected devices",
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    suffix = "" if args.language == "tr" else f"-{args.language}"
    for name, context in contexts().items():
        context.language = args.language
        if context.alerts:
            context.alerts = AlertTracker().update(context.metrics, language=args.language)
        path = args.output / f"{name}{suffix}.png"
        frame = render_new_screen(context)
        frame.save(path)
        print(path)
        if args.rotation == "180":
            rotated = args.output / f"{name}{suffix}-rot180.png"
            orient_frame(frame, "180").save(rotated)
            print(rotated)
    if args.batteries:
        for name, context in battery_contexts().items():
            context.language = args.language
            path = args.output / f"{name}{suffix}.png"
            render_new_screen(context).save(path)
            print(path)
        for name, frame in classic_battery_frames(args.language):
            path = args.output / f"{name}{suffix}.png"
            frame.save(path)
            print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
