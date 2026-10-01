# SPDX-License-Identifier: GPL-3.0-or-later
"""Turing 3.5" CS2 network + system monitor prototype.

Uses the GPL-3.0-or-later display driver from:
https://github.com/mathoudebine/turing-smart-screen-python
"""

from __future__ import annotations

import argparse
import atexit
import collections
import concurrent.futures
import contextvars
import ctypes
import ctypes.wintypes as wintypes
import functools
import http.server
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import psutil
from PIL import Image, ImageColor, ImageDraw, ImageFont
from ping3 import ping

from battery_runtime import BatteryDevice
from i18n import normalize_language, translate
from media_runtime import (
    AudioSpectrumSnapshot, MediaSnapshot, media_service_name, sanitise_media_text,
)
from msfs_runtime import FlightTelemetry
from weather_runtime import WeatherReading


WIDTH = 480
HEIGHT = 320
# Keep the live spectrum compact: on the Rev-A serial display this region
# transfers in about 40 ms instead of the old 123 ms 309x51 rectangle.
MEDIA_VIS_REGION = (154, 266, 327, 295)
MEDIA_VIS_REGION_WITH_BATTERY = (96, 266, 269, 295)
ROOT = Path(__file__).resolve().parent
APP_DATA = Path(os.environ.get("APPDATA", Path.home())) / "CS2Screen"
SENSOR_CACHE_PATH = APP_DATA / "elevated-sensors.json"
DEATHMATCH_CACHE_PATH = APP_DATA / "deathmatch-state.json"
if hasattr(sys, "_MEIPASS"):
    DRIVER_ROOT = Path(sys._MEIPASS)
elif (ROOT / "third_party" / "turing-smart-screen-python").is_dir():
    DRIVER_ROOT = ROOT / "third_party" / "turing-smart-screen-python"
else:
    DRIVER_ROOT = ROOT.parent / "turing-driver"
ICON_ROOT = ROOT / "icons" / "png"
SENSOR_ROOT = ROOT / "vendor"
GSI_PORT = 38791
GSI_TOKEN = "cs2-screen-local-v1"
GSI_THROTTLE_SECONDS = 0.2
GSI_MAX_PAYLOAD_BYTES = 1024 * 1024
DEATHMATCH_RESULT_HOLD_SECONDS = 7.0
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

# Category-specific visual pressure limits. Load limits describe saturation,
# not hardware damage. Temperature limits keep headroom below the documented
# 95°C Ryzen 7 9800X3D Tjmax and 90°C RTX 4070 Ti maximum temperature.
LOAD_COLOR_LIMITS = {
    "cpu": (75.0, 92.0, 100.0),
    "gpu": (70.0, 80.0, 95.0),
    "ram": (70.0, 85.0, 100.0),
}
TEMPERATURE_COLOR_LIMITS = {
    "cpu": (55.0, 70.0, 85.0, 95.0),
    "gpu": (50.0, 65.0, 78.0, 90.0),
}


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
    "bomb": load_font(42, True),
    "bomb_label": load_font(14, heavy=True),
    "bomb_badge": load_font(11, heavy=True),
    "bomb_marker": load_font(10, True),
    # The complete value tile is repainted for every live update, so the
    # original, wider Segoe face remains tear-free without sacrificing size.
    "sensor": load_font(21, heavy=True),
    "sensor_compact": load_font(18, heavy=True),
    "ram_sensor": load_font(17, heavy=True),
    "detail": load_font(12, heavy=True),
}


@dataclass(frozen=True)
class GameSnapshot:
    connected: bool = False
    in_match: bool = False
    ping_ms: float | None = None
    money: int | None = None
    next_win_money: int | None = None
    next_loss_money: int | None = None
    loss_bonus: int | None = None
    team: str = ""
    has_defuse_kit: bool = False
    bomb_planted: bool = False
    bomb_seconds: float | None = None
    bomb_defusing: bool = False
    game_mode: str = ""
    kills: int | None = None
    headshot_kills: int | None = None
    headshot_percent: int | None = None


def _round_winner(value: str) -> str | None:
    lowered = value.lower()
    if lowered.startswith("ct_") or "ct_win" in lowered:
        return "CT"
    if lowered.startswith("t_") or "terrorist_win" in lowered:
        return "T"
    return None


def _completed_round_winners(map_data: dict) -> list[str]:
    round_wins = map_data.get("round_wins") or {}
    if not isinstance(round_wins, dict):
        return []
    ordered: list[tuple[int, str]] = []
    for key, value in round_wins.items():
        try:
            index = int(key)
        except (TypeError, ValueError):
            continue
        winner = _round_winner(str(value))
        if winner:
            ordered.append((index, winner))
    return [winner for _index, winner in sorted(ordered)]


def _economy_mode(map_data: dict) -> str:
    mode = str(map_data.get("mode") or "").strip().lower()
    if any(token in mode for token in ("scrimcomp2v2", "competitive2v2", "wingman")):
        return "wingman"
    if any(token in mode for token in ("competitive", "premier", "scrimcomp5v5")):
        return "competitive"
    return "unsupported"


def _is_deathmatch_mode(mode: str) -> bool:
    lowered = str(mode or "").strip().lower()
    return any(token in lowered for token in ("deathmatch", "teamdeathmatch", "dm_freeforall"))


def _current_half_winners(winners: list[str], half_length: int) -> list[str]:
    """Return only rounds played on the player's current CT/T side.

    GSI records winners as CT or T, not as the player's permanent team.  Old
    halves therefore must not be counted after the side switch. Regulation
    halves use the selected mode's length; competitive overtime uses
    three-round halves.
    """
    completed = len(winners)
    regulation_rounds = half_length * 2
    if completed < regulation_rounds:
        start = (completed // half_length) * half_length
    else:
        overtime_rounds = completed - regulation_rounds
        start = regulation_rounds + (overtime_rounds // 3) * 3
    return winners[start:]


def calculate_economy(
    money: int | None, team: str, map_data: dict, bomb_planted: bool,
    force_competitive: bool = False,
) -> tuple[int | None, int | None, int | None]:
    """Return win total, loss total and the upcoming loss award.

    Values mirror CS2's local competitive and competitive2v2 configuration.
    The loss estimate assumes the player receives the normal team award; a
    T-side timeout save is the one in-game exception and cannot be predicted.
    """
    if money is None or team not in {"CT", "T"}:
        return None, None, None
    mode = _economy_mode(map_data)
    raw_mode = str(map_data.get("mode") or "").strip().lower()
    # FACEIT community servers may identify themselves as `custom` although
    # they use the normal 5v5 competitive economy. The caller only enables
    # this override while FACEIT AC is active; explicit casual/deathmatch
    # modes are never overridden.
    if mode == "unsupported" and force_competitive and raw_mode in {"", "custom"}:
        mode = "competitive"
    if mode == "unsupported":
        return None, None, None
    if mode == "wingman":
        loss_base, loss_step, starting_losses = 2000, 300, 0
        standard_win, max_money, half_length = 2750, 8000, 8
    else:
        loss_base, loss_step, starting_losses = 1400, 500, 1
        standard_win, max_money, half_length = 3250, 16000, 12

    # Since the March 2019 economy change, winning lowers the loss counter by
    # one instead of resetting it.  The counter is capped at four steps.
    loss_counter = starting_losses
    winners = _current_half_winners(_completed_round_winners(map_data), half_length)
    for winner in winners:
        if winner == team:
            loss_counter = max(0, loss_counter - 1)
        else:
            loss_counter = min(4, loss_counter + 1)
    loss_award = loss_base + (loss_step * loss_counter)
    if team == "T" and bomb_planted:
        loss_award += 600
    return (
        min(max_money, money + standard_win),
        min(max_money, money + loss_award),
        loss_award,
    )


class CS2GameState:
    """Thread-safe receiver state for CS2's official Game State Integration."""

    def __init__(self, deathmatch_cache_path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._data: dict = {}
        self._last_update = 0.0
        self._bomb_started: float | None = None
        self._last_local_player: dict = {}
        # CS2 resets state.round_kills/round_killhs after every death in
        # Deathmatch.  Keep a match-local accumulator so the panel represents
        # the scoreboard instead of only the current life.
        self._dm_active = False
        self._dm_identity = ""
        self._dm_phase = ""
        self._dm_countdown_seconds: float | None = None
        self._dm_kills = 0
        self._dm_headshots = 0
        self._dm_life_kills = 0
        self._dm_life_headshots = 0
        self._dm_match_kills: int | None = None
        self._dm_match_deaths: int | None = None
        self._dm_match_score: int | None = None
        self._dm_last_health: int | None = None
        self._dm_death_seen = False
        self._dm_last_reset_reason = ""
        self._dm_gameover_started: float | None = None
        self._dm_cache_path = (
            deathmatch_cache_path
            if deathmatch_cache_path is not None
            else DEATHMATCH_CACHE_PATH if getattr(sys, "frozen", False) else None
        )
        self._restore_deathmatch_stats()

    def _restore_deathmatch_stats(self) -> None:
        if self._dm_cache_path is None:
            return
        try:
            cached = json.loads(self._dm_cache_path.read_text(encoding="utf-8"))
            saved_at = float(cached.get("saved_at", 0.0))
            if time.time() - saved_at > 45.0:
                return
            identity = str(cached.get("identity") or "")
            if not identity:
                return
            self._dm_active = True
            self._dm_identity = identity
            self._dm_phase = str(cached.get("phase") or "")
            cached_seconds = cached.get("countdown_seconds")
            self._dm_countdown_seconds = (
                max(0.0, float(cached_seconds)) if cached_seconds is not None else None
            )
            self._dm_kills = max(0, int(cached.get("kills", 0)))
            self._dm_headshots = max(0, int(cached.get("headshots", 0)))
            self._dm_life_kills = max(0, int(cached.get("life_kills", 0)))
            self._dm_life_headshots = max(0, int(cached.get("life_headshots", 0)))
            self._dm_match_kills = (
                max(0, int(cached["match_kills"]))
                if cached.get("match_kills") is not None else None
            )
            self._dm_match_deaths = (
                max(0, int(cached["match_deaths"]))
                if cached.get("match_deaths") is not None else None
            )
            self._dm_match_score = (
                int(cached["match_score"])
                if cached.get("match_score") is not None else None
            )
            self._dm_last_health = (
                max(0, int(cached["last_health"]))
                if cached.get("last_health") is not None else None
            )
            self._dm_death_seen = bool(cached.get("death_seen", False))
            self._dm_last_reset_reason = str(cached.get("last_reset_reason") or "")
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return

    def _save_deathmatch_stats_locked(self) -> None:
        if self._dm_cache_path is None:
            return
        try:
            payload = {
                "saved_at": time.time(),
                "identity": self._dm_identity,
                "phase": self._dm_phase,
                "countdown_seconds": self._dm_countdown_seconds,
                "kills": self._dm_kills,
                "headshots": self._dm_headshots,
                "life_kills": self._dm_life_kills,
                "life_headshots": self._dm_life_headshots,
                # These official GSI cumulative fields also make the cache a
                # privacy-safe diagnostic record: no name or Steam ID is
                # written, only the counters needed to verify match borders.
                "match_kills": self._dm_match_kills,
                "match_deaths": self._dm_match_deaths,
                "match_score": self._dm_match_score,
                "last_health": self._dm_last_health,
                "death_seen": self._dm_death_seen,
                "last_reset_reason": self._dm_last_reset_reason,
            }
            self._dm_cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._dm_cache_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(temporary, self._dm_cache_path)
        except OSError:
            pass

    def _clear_deathmatch_stats_locked(self) -> None:
        if self._dm_cache_path is None:
            return
        try:
            self._dm_cache_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _update_deathmatch_stats_locked(self, payload: dict, player: dict) -> None:
        map_data = payload.get("map") or self._data.get("map") or {}
        mode = str(map_data.get("mode") or "")
        if not mode:
            # A heartbeat may arrive before the first complete map packet.
            # Do not discard a fresh restart cache until the mode is known.
            return
        if not _is_deathmatch_mode(mode):
            self._dm_active = False
            self._dm_identity = ""
            self._dm_phase = ""
            self._dm_countdown_seconds = None
            self._dm_kills = 0
            self._dm_headshots = 0
            self._dm_life_kills = 0
            self._dm_life_headshots = 0
            self._dm_match_kills = None
            self._dm_match_deaths = None
            self._dm_match_score = None
            self._dm_last_health = None
            self._dm_death_seen = False
            self._dm_last_reset_reason = ""
            self._dm_gameover_started = None
            self._clear_deathmatch_stats_locked()
            return

        phase_data = payload.get("phase_countdowns") or self._data.get("phase_countdowns") or {}
        countdown_phase = str(phase_data.get("phase") or "").lower()
        map_phase = str(map_data.get("phase") or "").lower()
        phase = countdown_phase or map_phase or self._dm_phase
        if phase == "gameover":
            if self._dm_gameover_started is None:
                self._dm_gameover_started = time.monotonic()
        else:
            self._dm_gameover_started = None
        try:
            countdown_seconds = max(0.0, float(phase_data["phase_ends_in"]))
        except (KeyError, TypeError, ValueError):
            countdown_seconds = None
        match_stats = player.get("match_stats") or {} if player else {}

        def cumulative_stat(name: str) -> int | None:
            try:
                return max(0, int(match_stats[name]))
            except (KeyError, TypeError, ValueError):
                return None

        match_kills = cumulative_stat("kills")
        match_deaths = cumulative_stat("deaths")
        try:
            match_score = int(match_stats["score"])
        except (KeyError, TypeError, ValueError):
            match_score = None
        state = player.get("state") or {} if player else {}
        try:
            health = max(0, int(state["health"]))
        except (KeyError, TypeError, ValueError):
            health = None
        try:
            incoming_life_kills = max(0, int(state["round_kills"]))
        except (KeyError, TypeError, ValueError):
            incoming_life_kills = None
        if health is not None and health <= 0:
            self._dm_death_seen = True
        identity = f"{mode.lower()}|{str(map_data.get('name') or '').lower()}"
        entered_warmup = phase == "warmup" and self._dm_phase != "warmup"
        entered_live = phase == "live" and self._dm_phase in {
            "warmup", "intermission", "gameover",
        }
        # Some Deathmatch servers keep map.phase="live" during warm-up. In
        # that case the only reliable boundary is the countdown jumping from
        # a few warm-up seconds to the full match clock (normally 600 s).
        match_clock_restarted = (
            countdown_seconds is not None
            and self._dm_countdown_seconds is not None
            and countdown_seconds >= 300.0
            and countdown_seconds > self._dm_countdown_seconds + 120.0
        )
        # On the official Deathmatch servers tested here, both map.phase and
        # phase_countdowns can remain "live" (or the latter can be omitted)
        # across warm-up. player_match_stats is cumulative across respawns,
        # but resets when the real match begins; a decrease is therefore a
        # direct GSI match boundary rather than a timer guess.
        cumulative_stats_reset = any((
            match_kills is not None
            and self._dm_match_kills is not None
            and match_kills < self._dm_match_kills,
            match_deaths is not None
            and self._dm_match_deaths is not None
            and match_deaths < self._dm_match_deaths,
        ))
        # When Valve omits both countdowns and player_match_stats, the local
        # life counter is the remaining official signal. A normal respawn is
        # preceded by health=0; the warm-up reset drops round_kills while the
        # player stays alive. Require known positive health on both packets so
        # missing death packets never erase a real match total.
        alive_counter_reset = (
            incoming_life_kills is not None
            and incoming_life_kills < self._dm_life_kills
            and health is not None and health > 0
            and self._dm_last_health is not None and self._dm_last_health > 0
            and not self._dm_death_seen
        )
        reset_reasons = []
        if not self._dm_active:
            reset_reasons.append("first-packet")
        if self._dm_identity and identity != self._dm_identity:
            reset_reasons.append("map-change")
        if entered_warmup:
            reset_reasons.append("warmup-phase")
        if entered_live:
            reset_reasons.append("live-phase")
        if match_clock_restarted:
            reset_reasons.append("match-clock")
        if cumulative_stats_reset:
            reset_reasons.append("match-stats")
        if alive_counter_reset:
            reset_reasons.append("alive-counter-reset")
        reset_match = bool(reset_reasons)
        if reset_match:
            self._dm_kills = 0
            self._dm_headshots = 0
            self._dm_life_kills = 0
            self._dm_life_headshots = 0
            self._dm_last_reset_reason = "+".join(reset_reasons)

        self._dm_active = True
        self._dm_identity = identity
        self._dm_phase = phase
        self._dm_countdown_seconds = countdown_seconds
        if match_kills is not None:
            self._dm_match_kills = match_kills
        if match_deaths is not None:
            self._dm_match_deaths = match_deaths
        if match_score is not None:
            self._dm_match_score = match_score

        if not player:
            return
        try:
            life_kills = max(0, int(state["round_kills"]))
        except (KeyError, TypeError, ValueError):
            life_kills = None
        try:
            life_headshots = max(0, int(state["round_killhs"]))
        except (KeyError, TypeError, ValueError):
            life_headshots = None

        if life_kills is not None:
            if life_kills >= self._dm_life_kills:
                self._dm_kills += life_kills - self._dm_life_kills
            else:
                # A lower value is a respawn. The old life's kills are
                # already in the total; count only events in the new life.
                self._dm_kills += life_kills
            self._dm_life_kills = life_kills
        if life_headshots is not None:
            if life_headshots >= self._dm_life_headshots:
                self._dm_headshots += life_headshots - self._dm_life_headshots
            else:
                self._dm_headshots += life_headshots
            self._dm_life_headshots = life_headshots

        if health is not None:
            if health > 0 and self._dm_last_health == 0:
                self._dm_death_seen = False
            self._dm_last_health = health

        # match_stats.kills is cumulative on builds that expose it. Use it as
        # a floor to recover kills if a short-lived GSI packet was missed.
        scoreboard_kills = match_kills
        if scoreboard_kills is not None:
            self._dm_kills = max(self._dm_kills, scoreboard_kills)
        self._save_deathmatch_stats_locked()

    def update(self, payload: dict) -> None:
        now = time.monotonic()
        with self._lock:
            provider_id = str((payload.get("provider") or {}).get("steamid") or "")
            player = payload.get("player") or {}
            player_id = str(player.get("steamid") or "")
            # On death CS2 can briefly omit `player`, then publish the player
            # being spectated. Neither packet represents a lobby transition.
            # Keep the last known local player until CS2 sends that local
            # player's state again so the screen does not flash matchmaking
            # pings between death and spectator/economy updates.
            if player and (not provider_id or (player_id and provider_id == player_id)):
                self._last_local_player = player
                self._update_deathmatch_stats_locked(payload, player)
            elif not player:
                # Map phase changes (warm-up -> live, gameover -> warm-up)
                # can arrive without a player section and must still reset
                # the Deathmatch totals at the correct boundary.
                self._update_deathmatch_stats_locked(payload, {})
            round_data = payload.get("round") or {}
            bomb_data = payload.get("bomb") or {}
            bomb_state = str(bomb_data.get("state") or round_data.get("bomb") or "").lower()
            planted = bomb_state in {"planted", "defusing"}
            if planted and self._bomb_started is None:
                phase_countdowns = payload.get("phase_countdowns") or {}
                countdown = bomb_data.get("countdown")
                if (
                    countdown is None
                    and str(phase_countdowns.get("phase") or "").lower() == "bomb"
                ):
                    countdown = phase_countdowns.get("phase_ends_in")
                try:
                    if bomb_state != "planted":
                        raise ValueError
                    remaining = max(0.0, min(40.0, float(countdown)))
                    self._bomb_started = now - (40.0 - remaining)
                except (TypeError, ValueError):
                    # GSI batches updates according to its throttle value. If
                    # CS2 omits phase_ends_in, compensate that known delivery
                    # delay instead of starting a fresh 40.0-second clock.
                    self._bomb_started = now - GSI_THROTTLE_SECONDS
            elif not planted:
                self._bomb_started = None
            # GSI packets are normally complete, but death/observer boundary
            # packets can omit a top-level section for one delivery. Preserve
            # the last value for omitted sections while accepting every
            # section that CS2 did send.
            merged = dict(self._data)
            merged.update(payload)
            self._data = merged
            self._last_update = now

    def snapshot(self, force_competitive_economy: bool = False) -> GameSnapshot:
        now = time.monotonic()
        with self._lock:
            payload = dict(self._data)
            player = dict(self._last_local_player)
            last_update = self._last_update
            bomb_started = self._bomb_started
            dm_kills = self._dm_kills
            dm_headshots = self._dm_headshots
            dm_gameover_started = self._dm_gameover_started
        connected = bool(last_update and now - last_update < 8.0)
        map_data = payload.get("map") or {}
        state = player.get("state") or {}
        team = str(player.get("team") or "").upper()
        try:
            money = int(state["money"])
        except (KeyError, TypeError, ValueError):
            money = None
        match_stats = player.get("match_stats") or {}
        if _is_deathmatch_mode(str(map_data.get("mode") or "")):
            kills = dm_kills
            headshot_kills = dm_headshots
        else:
            try:
                kills = int(state.get("round_kills", match_stats.get("kills")))
            except (TypeError, ValueError):
                kills = None
            try:
                headshot_kills = int(state["round_killhs"])
            except (KeyError, TypeError, ValueError):
                headshot_kills = None
        headshot_percent = (
            # CS2's scoreboard truncates the fractional part instead of
            # rounding it (12 headshots / 17 kills is shown as 70%, not 71%).
            int((headshot_kills * 100.0) / kills)
            if kills and headshot_kills is not None else 0 if kills == 0 else None
        )
        has_kit = bool(state.get("defusekit"))
        round_data = payload.get("round") or {}
        bomb_data = payload.get("bomb") or {}
        bomb_state = str(bomb_data.get("state") or round_data.get("bomb") or "").lower()
        bomb_planted = bomb_state in {"planted", "defusing"} and bomb_started is not None
        bomb_seconds = max(0.0, 40.0 - (now - bomb_started)) if bomb_planted else None
        next_win, next_loss, loss_bonus = calculate_economy(
            money, team, map_data, bomb_planted,
            force_competitive=force_competitive_economy,
        )
        # GSI can keep the last map payload alive for a few seconds after the
        # player returns to the menu.  Requiring the local player's activity
        # to be "playing" prevents that stale payload from hiding the
        # pre-match FACEIT/Valve ping panel.
        activity = str(player.get("activity") or "").lower()
        in_match = (
            connected
            and activity == "playing"
            and str(map_data.get("phase") or "").lower() not in {"", "gameover"}
        )
        # CS2 publishes gameover shortly before its result screen has finished.
        # Keep the final Deathmatch kills/HS visible long enough to read them
        # instead of flashing straight back to the pre-match ping panel.
        if (
            connected
            and _is_deathmatch_mode(str(map_data.get("mode") or ""))
            and dm_gameover_started is not None
            and now - dm_gameover_started < DEATHMATCH_RESULT_HOLD_SECONDS
        ):
            in_match = True
        return GameSnapshot(
            connected=connected,
            in_match=in_match,
            money=money,
            next_win_money=next_win,
            next_loss_money=next_loss,
            loss_bonus=loss_bonus,
            team=team,
            has_defuse_kit=has_kit,
            bomb_planted=bomb_planted,
            bomb_seconds=bomb_seconds,
            bomb_defusing=bomb_state == "defusing",
            game_mode=str(map_data.get("mode") or ""),
            kills=kills,
            headshot_kills=headshot_kills,
            headshot_percent=headshot_percent,
        )


class _GSIHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - stdlib handler name
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > GSI_MAX_PAYLOAD_BYTES:
                self.send_response(413)
                self.end_headers()
                return
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            token = str((payload.get("auth") or {}).get("token") or "")
            if token == GSI_TOKEN:
                self.server.game_state.update(payload)  # type: ignore[attr-defined]
            self.send_response(200)
            self.end_headers()
        except Exception:
            self.send_response(400)
            self.end_headers()

    def log_message(self, _format: str, *_args: object) -> None:
        return


class GSIServer:
    def __init__(self, game_state: CS2GameState) -> None:
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", GSI_PORT), _GSIHandler)
        self.server.game_state = game_state  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, name="cs2-gsi", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)


@dataclass(frozen=True)
class RelayPing:
    code: str
    name: str
    ip: str
    latency_ms: float


MATCHMAKING_POP_NAMES = {
    "ams": "Amsterdam", "atl": "Atlanta", "bom": "Mumbai", "dxb": "Dubai",
    "dfw": "Dallas", "eze": "Buenos Aires", "fra": "Frankfurt", "fsn": "Falkenstein",
    "gru": "São Paulo", "hel": "Helsinki", "hkg": "Hong Kong", "iad": "Virginia",
    "jnb": "Johannesburg", "lax": "Los Angeles", "lhr": "Birleşik Krallık",
    "lim": "Lima", "maa": "Chennai", "mad": "Madrid", "man": "Manchester",
    "ord": "Chicago", "par": "Paris", "scl": "Santiago", "sea": "Seattle",
    "sgp": "Singapur", "sto": "Stockholm", "syd": "Sidney", "tyo": "Tokyo",
    "vie": "Viyana", "waw": "Varşova",
}
CS2_CONSOLE_PING_READ_INTERVAL_SECONDS = 1.0
CS2_EXACT_PING_MODE = "console_log"


class CS2ConsolePingMonitor:
    """Read the SDR table written by CS2 itself to its local console log."""

    _PING_LINE = re.compile(r"(?i)\b([a-z][a-z0-9]{2,3}):\s*(\d+)\s*ms\b")
    _PING_LOCATION = re.compile(r"(?im)Ping location:\s*([^\r\n]+)")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: list[RelayPing] = []
        self._last_read = 0.0
        self._last_size = -1

    @classmethod
    def parse_ping_text(cls, text: str) -> dict[str, int]:
        table = {
            code.lower(): int(latency)
            for code, latency in cls._PING_LINE.findall(text)
            if code.lower() in MATCHMAKING_POP_NAMES and code.lower() != "fsn"
        }
        locations = cls._PING_LOCATION.findall(text)
        if not locations:
            return table
        latest: dict[str, int] = {}
        for item in locations[-1].split(","):
            code, separator, routes = item.strip().partition("=")
            code = code.lower()
            if not separator or code not in MATCHMAKING_POP_NAMES or code == "fsn":
                continue
            estimates = [int(value) for value in re.findall(r"/?(\d+)\+\d+", routes)]
            if estimates:
                latest[code] = min(estimates)
        return latest or table

    @staticmethod
    def _console_log_path() -> Path | None:
        cfg_dir = find_cs2_cfg_dir()
        return cfg_dir.parent / "console.log" if cfg_dir is not None else None

    def read(self) -> list[RelayPing]:
        now = time.monotonic()
        with self._lock:
            if now - self._last_read < CS2_CONSOLE_PING_READ_INTERVAL_SECONDS:
                return list(self._values)
            self._last_read = now
        path = self._console_log_path()
        if path is None:
            return []
        try:
            size = path.stat().st_size
            with self._lock:
                if size == self._last_size:
                    return list(self._values)
                self._last_size = size
            with path.open("rb") as stream:
                stream.seek(max(0, size - 1024 * 1024))
                text = stream.read().decode("utf-8", errors="ignore")
            parsed = self.parse_ping_text(text)
            rows = [
                RelayPing(code, MATCHMAKING_POP_NAMES[code], "", float(latency))
                for code, latency in parsed.items()
            ]
            rows.sort(key=lambda item: item.latency_ms)
            if len(rows) >= 3:
                with self._lock:
                    self._values = rows[:3]
            with self._lock:
                return list(self._values)
        except OSError:
            return []

    def close(self) -> None:
        return None


class MatchmakingPingMonitor:
    """Read only the matchmaking table that CS2 writes to its own log."""

    def __init__(self) -> None:
        self.exact = CS2ConsolePingMonitor()

    def read(self) -> list[RelayPing]:
        return self.exact.read()

    def close(self) -> None:
        self.exact.close()


STEAM_PING_MONITOR = MatchmakingPingMonitor()
atexit.register(STEAM_PING_MONITOR.close)


# Addresses are selected from HiperZ's current EU diagnostic package, the
# latency test FACEIT support directs players to for FACEIT server routing.
FACEIT_LOCATION_TARGETS = {
    "Almanya": ("85.114.146.1", "fsn.icmp.hetzner.com", "145.239.244.75"),
    "Hollanda": ("mirror.nforce.com", "iperf.worldstream.nl", "mirror.i3d.net"),
    "İsveç": ("185.76.9.135", "185.62.206.1", "speed.se.m247.ro"),
    "Finlandiya": ("hel.icmp.hetzner.com",),
    "Birleşik Krallık": ("198.244.202.178", "185.38.150.1", "185.59.221.51"),
    "Fransa": ("54.37.87.229", "185.93.2.193"),
}


def faceit_ac_is_running() -> bool:
    for process in psutil.process_iter(["name"]):
        try:
            if (process.info.get("name") or "").lower() == "faceitclient.exe":
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


class FaceitPingMonitor:
    """Measure the official HiperZ diagnostic nodes while FACEIT AC is active."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started = False
        self._stop = threading.Event()
        self._values: list[RelayPing] = []

    def read(self) -> list[RelayPing]:
        with self._lock:
            if not self._started:
                self._started = True
                threading.Thread(target=self._run, name="faceit-ping-reader", daemon=True).start()
            return list(self._values)

    def close(self) -> None:
        self._stop.set()

    @staticmethod
    def _probe(item: tuple[str, str]) -> tuple[str, float] | None:
        location, host = item
        try:
            latency = ping(host, timeout=0.8, unit="ms")
        except Exception:
            latency = None
        if latency is None or latency <= 0:
            return None
        return location, float(latency)

    def _run(self) -> None:
        targets = [
            (location, host)
            for location, hosts in FACEIT_LOCATION_TARGETS.items()
            for host in hosts
        ]
        while not self._stop.is_set():
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(targets)) as executor:
                samples = list(executor.map(self._probe, targets))
            grouped: dict[str, list[float]] = {}
            for sample in samples:
                if sample is not None:
                    grouped.setdefault(sample[0], []).append(sample[1])
            values = [
                RelayPing(location.lower(), location, "", min(latencies))
                for location, latencies in grouped.items()
                if latencies
            ]
            values.sort(key=lambda item: item.latency_ms)
            with self._lock:
                self._values = values[:3]
            self._stop.wait(4.0)


FACEIT_PING_MONITOR = FaceitPingMonitor()
atexit.register(FACEIT_PING_MONITOR.close)


def cs2_is_running() -> bool:
    for process in psutil.process_iter(["name"]):
        try:
            if (process.info.get("name") or "").lower() == "cs2.exe":
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def msfs_session_seconds() -> float | None:
    """Return the current MSFS 2024 process age, or None when it is closed."""
    for process in psutil.process_iter(["name", "create_time"]):
        try:
            if (process.info.get("name") or "").lower() != "flightsimulator2024.exe":
                continue
            started_at = float(process.info.get("create_time") or time.time())
            return max(0.0, time.time() - started_at)
        except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError, ValueError):
            continue
    return None


def find_cs2_cfg_dir() -> Path | None:
    candidates: list[Path] = []
    if os.name == "nt":
        try:
            import winreg
            for hive, key_name in (
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
                (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
            ):
                try:
                    with winreg.OpenKey(hive, key_name) as key:
                        value, _kind = winreg.QueryValueEx(key, "InstallPath")
                        candidates.append(Path(value))
                except OSError:
                    continue
        except ImportError:
            pass
    libraries: list[Path] = []
    for steam_root in candidates:
        libraries.append(steam_root)
        library_file = steam_root / "steamapps" / "libraryfolders.vdf"
        try:
            content = library_file.read_text(encoding="utf-8", errors="ignore")
            for line in content.splitlines():
                stripped = line.strip()
                if not stripped.startswith('"path"'):
                    continue
                value = stripped.split('"')[3].replace("\\\\", "\\")
                libraries.append(Path(value))
        except OSError:
            continue
    for library in libraries:
        cfg_dir = library / "steamapps" / "common" / "Counter-Strike Global Offensive" / "game" / "csgo" / "cfg"
        if cfg_dir.is_dir():
            return cfg_dir
    return None


def _vdf_block_bounds(content: str, key: str) -> tuple[int, int] | None:
    """Locate a quoted VDF object's braces without being confused by nesting."""
    for match in re.finditer(rf'"{re.escape(key)}"\s*\{{', content):
        opening = content.find("{", match.start())
        depth = 0
        quoted = False
        escaped = False
        for index in range(opening, len(content)):
            char = content[index]
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
                continue
            if char == '"':
                quoted = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return opening, index
    return None


def add_cs2_console_log_launch_option(content: str) -> tuple[str, bool]:
    """Enable CS2 console logging while preserving the user's other options."""
    search_from = 0
    while search_from < len(content):
        relative = _vdf_block_bounds(content[search_from:], "730")
        if relative is None:
            return content, False
        opening = search_from + relative[0]
        closing = search_from + relative[1]
        block = content[opening:closing + 1]
        option_match = re.search(r'("LaunchOptions"\s*")([^"]*)(")', block)
        if option_match is None:
            search_from = closing + 1
            continue
        options = option_match.group(2).strip()
        # Remove the unsupported VConsole switch if an earlier build added it.
        updated_options = re.sub(
            r"(?:^|\s)-vconsole(?=\s|$)", " ", options, flags=re.IGNORECASE,
        )
        updated_options = " ".join(updated_options.split())
        if not re.search(r"(?:^|\s)-condebug(?:\s|$)", updated_options, re.IGNORECASE):
            updated_options = f"{updated_options} -condebug".strip()
        if updated_options == options:
            return content, False
        updated_block = (
            block[:option_match.start(2)] + updated_options + block[option_match.end(2):]
        )
        return content[:opening] + updated_block + content[closing + 1:], True
    return content, False


def install_cs2_console_log_launch_option() -> Path | None:
    """Enable CS2's own local console log for exact ping reads next launch."""
    if os.name != "nt":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam\ActiveProcess") as key:
            active_user, _kind = winreg.QueryValueEx(key, "ActiveUser")
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            steam_path, _kind = winreg.QueryValueEx(key, "SteamPath")
    except (ImportError, OSError):
        return None
    if not active_user:
        return None
    target = Path(steam_path) / "userdata" / str(int(active_user)) / "config" / "localconfig.vdf"
    try:
        content = target.read_text(encoding="utf-8", errors="ignore")
        updated, changed = add_cs2_console_log_launch_option(content)
        if changed:
            temporary = target.with_suffix(".vdf.sabasakal.tmp")
            temporary.write_text(updated, encoding="utf-8")
            os.replace(temporary, target)
        return target
    except OSError:
        return None


def gsi_config_text() -> str:
    return f'''"CS2 Screen v1.0"
{{
    "uri" "http://127.0.0.1:{GSI_PORT}"
    "timeout" "0.1"
    "buffer" "0.0"
    "throttle" "{GSI_THROTTLE_SECONDS:.1f}"
    "heartbeat" "5.0"
    "auth"
    {{
        "token" "{GSI_TOKEN}"
    }}
    "data"
    {{
        "provider" "1"
        "map" "1"
        "map_round_wins" "1"
        "round" "1"
        "player_id" "1"
        "player_state" "1"
        "player_match_stats" "1"
        "player_weapons" "1"
        "bomb" "1"
        "phase_countdowns" "1"
    }}
}}
'''


def install_gsi_config() -> Path:
    cfg_dir = find_cs2_cfg_dir()
    if cfg_dir is None:
        raise FileNotFoundError("CS2 kurulum klasörü bulunamadı.")
    target = cfg_dir / "gamestate_integration_cs2_screen.cfg"
    content = gsi_config_text()
    if not target.exists() or target.read_text(encoding="utf-8", errors="ignore") != content:
        target.write_text(content, encoding="utf-8")
    install_cs2_console_log_launch_option()
    return target


class EmbeddedSensorMonitor:
    """Read CPU sensors through an isolated helper bundled with the application."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started = False
        self._process: subprocess.Popen[str] | None = None
        self._values: dict[str, float | None] = {"cpu_temp": None, "cpu_freq": None}

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
        path: Path = SENSOR_CACHE_PATH, max_age: float = 3.0,
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
            values = {
                "cpu_temp": cpu_temp,
                "cpu_freq": (cpu_freq_mhz / 1000.0) if cpu_freq_mhz else None,
                "timestamp": time.time(),
            }
            line = json.dumps(values) + "\n"
            if cache_path is not None:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = cache_path.with_suffix(".tmp")
                temporary.write_text(json.dumps(values), encoding="utf-8")
                os.replace(temporary, cache_path)
            elif sys.stdout is not None:
                sys.stdout.write(line)
                sys.stdout.flush()
            elif os.name == "nt":
                payload = line.encode("utf-8")
                written = ctypes.c_ulong(0)
                handle = ctypes.windll.kernel32.GetStdHandle(-11)
                ctypes.windll.kernel32.WriteFile(handle, payload, len(payload), ctypes.byref(written), None)
            time.sleep(0.5)
    except Exception:
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

    def __init__(self, sample_interval: float = 0.25) -> None:
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
        """Return Task Manager's busiest physical engine utilization.

        The GPU counter set exposes one row per process.  Task Manager first
        totals rows belonging to the same physical engine, then displays the
        busiest engine as the overall load.
        """
        engines: dict[str, float] = {}
        for name, value in items:
            match = re.search(
                r"(?P<adapter>luid_0x[0-9a-f]+_0x[0-9a-f]+_phys_\d+)_eng_\d+",
                name,
                flags=re.IGNORECASE,
            )
            if match is None or not math.isfinite(value) or value < 0:
                continue
            adapter = match.group("adapter").lower()
            if target_luid and not adapter.startswith(f"{target_luid.lower()}_phys_"):
                continue
            key = match.group(0).lower()
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


NVIDIA_MONITOR = NvidiaNvmlMonitor()


def get_gpu_metrics() -> tuple[float, float, float]:
    return NVIDIA_MONITOR.read()


def get_metrics() -> dict[str, float | None]:
    fallback_cpu_load = psutil.cpu_percent(interval=None)
    memory = psutil.virtual_memory()
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
    return {
        "cpu_load": cpu_load,
        "cpu_freq": cpu_frequency,
        "cpu_temp": sensor_values["cpu_temp"],
        "gpu_load": gpu_load,
        "gpu_temp": gpu_temp if gpu_temp > 0 else None,
        "gpu_memory": task_gpu_memory if task_gpu_memory is not None else fallback_gpu_memory,
        # Task Manager defines used RAM as Total - Available.  This explicitly
        # matches that formula on Windows instead of relying on platform-
        # specific psutil percentage semantics.
        "ram_load": ((memory.total - memory.available) / memory.total * 100.0) if memory.total else 0.0,
        "ram_used": memory.used / (1024 ** 3),
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
    scale = 4
    layer = Image.new("RGBA", (image.width * scale, image.height * scale), (0, 0, 0, 0))
    layer_draw = ImageDraw.Draw(layer)
    layer_draw.polygon(
        _annular_sector_points(bbox, start, end, width, scale), fill=fill,
    )
    layer = layer.resize(image.size, Image.Resampling.LANCZOS)
    image.paste(layer, (0, 0), layer)


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
    scale = 4
    layer = Image.new("RGBA", (image.width * scale, image.height * scale), (0, 0, 0, 0))
    layer_draw = ImageDraw.Draw(layer)
    layer_draw.polygon(
        _annular_sector_points(bbox, start, end, width, scale),
        fill=track_fill,
    )
    if progress_end > start:
        layer_draw.polygon(
            _annular_sector_points(bbox, start, min(progress_end, end), width, scale),
            fill=progress_fill,
        )
    # Downsample only once. Two independently antialiased layers created the
    # bright triangular wedge at the common open-ring start on the LCD.
    layer = layer.resize(image.size, Image.Resampling.LANCZOS)
    image.paste(layer, (0, 0), layer)


def point_on_circle(cx: int, cy: int, radius: int, angle_deg: float) -> tuple[float, float]:
    angle = math.radians(angle_deg)
    return cx + radius * math.cos(angle), cy + radius * math.sin(angle)


def mix_color(first: str, second: str, amount: float) -> str:
    amount = max(0.0, min(1.0, amount))
    a = ImageColor.getrgb(first)
    b = ImageColor.getrgb(second)
    rgb = tuple(round(a[index] + (b[index] - a[index]) * amount) for index in range(3))
    return "#%02x%02x%02x" % rgb


def _scale_color(
    value: float, limits: tuple[float, float, float], colors: dict[str, str],
) -> str:
    normal_end, warning_end, critical_end = limits
    if value <= normal_end:
        return colors["cpu"]
    if value <= warning_end:
        return mix_color(
            colors["cpu"], colors["warning"],
            (value - normal_end) / (warning_end - normal_end),
        )
    if value <= critical_end:
        return mix_color(
            colors["warning"], colors["danger"],
            (value - warning_end) / (critical_end - warning_end),
        )
    return colors["danger"]


def load_color(value: float, component: str, colors: dict[str, str]) -> str:
    """Theme-aware saturation scale with limits appropriate to each resource."""
    return _scale_color(value, LOAD_COLOR_LIMITS[component], colors)


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
    """Blink the central load value only after the critical transition starts."""
    critical_start = LOAD_COLOR_LIMITS[component][1]
    if value <= critical_start:
        return None
    return _critical_blink_color(load_color(value, component, colors), colors, animation_time)


def temperature_color(
    value: float | None, component: str, colors: dict[str, str],
) -> str:
    """Thermal scale with a warning plateau before the critical transition."""
    if value is None:
        return colors["dim"]
    normal_end, warning_end, critical_start, critical_end = TEMPERATURE_COLOR_LIMITS[component]
    if value <= normal_end:
        return colors["cpu"]
    if value <= warning_end:
        return mix_color(
            colors["cpu"], colors["warning"],
            (value - normal_end) / (warning_end - normal_end),
        )
    if value <= critical_start:
        return colors["warning"]
    if value <= critical_end:
        return mix_color(
            colors["warning"], colors["danger"],
            (value - critical_start) / (critical_end - critical_start),
        )
    return colors["danger"]


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

    normal_end, _warning_end, critical_start, _critical_end = TEMPERATURE_COLOR_LIMITS[component]
    if value <= normal_end:
        return base
    if value <= critical_start:
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
    """Keep the number calm until the critical transition begins."""
    if value is None:
        return colors["dim"]
    critical_start = TEMPERATURE_COLOR_LIMITS[component][2]
    if value <= critical_start:
        return colors["text"]
    return temperature_unit_color(value, component, colors, animation_time)


@functools.lru_cache(maxsize=1)
def _load_cs2_hero() -> Image.Image | None:
    """Load the bundled CS2 showcase background once per process."""
    path = ROOT / "cs2-hero.jpg"
    if not path.is_file():
        return None
    try:
        return Image.open(path).convert("RGB").resize((450, 144), Image.Resampling.LANCZOS)
    except (OSError, ValueError):
        return None


def add_cs2_backdrop(image: Image.Image, colors: dict[str, str]) -> None:
    """Draw the CS2 hero image with a theme-aware tactical overlay."""
    width, height = 450, 144
    hero = _load_cs2_hero()
    panel_rgb = ImageColor.getrgb(colors["panel"])
    if hero is None:
        base = Image.new("RGB", (width, height), panel_rgb)
    else:
        # Retain the recognisable artwork while keeping live values readable
        # in every light/dark colour preset.
        tint = Image.new("RGB", (width, height), panel_rgb)
        base = Image.blend(hero, tint, 0.52)
        base = Image.blend(base, Image.new("RGB", (width, height), (0, 0, 0)), 0.12)
    overlay = base.convert("RGBA")

    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, width - 1, height - 1), radius=7, fill=255)
    image.paste(overlay.convert("RGB"), (15, 161), mask)


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
    value_width = draw.textlength(value_text, font=FONTS["value"])
    percent_width = draw.textlength("%", font=FONTS["gauge_unit"])
    value_gap = 4
    value_left = cx - (value_width + value_gap + percent_width) / 2
    draw.text(
        (value_left + value_width / 2, cy - 6), value_text,
        font=FONTS["value"], fill=value_color or colors["text"], anchor="mm",
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
    if sensor_text.endswith("°C"):
        number_text, unit_text = sensor_text[:-2], "°C"
        number_width = draw.textlength(number_text, font=FONTS["sensor"])
        unit_width = draw.textlength(unit_text, font=FONTS["sensor"])
        start_x = cx - (number_width + unit_width) / 2
        draw.text(
            (start_x, cy + 28), number_text, font=FONTS["sensor"],
            fill=sensor_number_color or colors["text"], anchor="lm",
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


def ping_color(latency_ms: float, colors: dict[str, str] | None = None) -> str:
    colors = colors or COLORS
    if latency_ms < 55:
        return colors["cpu"]
    if latency_ms < 85:
        return colors["warning"]
    return colors["danger"]


CITY_NAMES_TR = {
    "vienna": "Viyana",
    "frankfurt": "Frankfurt",
    "paris": "Paris",
    "amsterdam": "Amsterdam",
    "stockholm": "Stockholm",
    "helsinki": "Helsinki",
    "madrid": "Madrid",
    "istanbul": "İstanbul",
    "dubai": "Dubai",
    "singapore": "Singapur",
    "tokyo": "Tokyo",
    "warsaw": "Varşova",
    "london": "Londra",
    "bucharest": "Bükreş",
    "budapest": "Budapeşte",
    "prague": "Prag",
    "sofia": "Sofya",
    "moscow": "Moskova",
    "munich": "Münih",
    "copenhagen": "Kopenhag",
    "brussels": "Brüksel",
    "athens": "Atina",
    "belgrade": "Belgrad",
    "milan": "Milano",
    "rome": "Roma",
    "cologne": "Köln",
}


def relay_display_name(relay: RelayPing) -> str:
    lower_name = relay.name.lower()
    for english, turkish in CITY_NAMES_TR.items():
        if english in lower_name:
            return turkish if _RENDER_LANGUAGE.get() == "tr" else english.title()
    for provider in ("datapacket ", "valve ", "partner "):
        if lower_name.startswith(provider):
            return relay.name[len(provider):]
    return relay.name


def _money(value: int | None) -> str:
    if value is None:
        return "—"
    rendered = f"${value:,}"
    return rendered.replace(",", ".") if _RENDER_LANGUAGE.get() == "tr" else rendered


def _bomb_color(
    seconds: float,
    team: str,
    has_kit: bool,
    colors: dict[str, str],
    defusing: bool = False,
) -> str:
    """Interpret the defuse window from the local player's side."""
    if defusing:
        return colors["danger"]
    if team == "CT":
        if not has_kit:
            if seconds <= 10.0:
                return colors["danger"]
            if seconds <= 12.0:
                return mix_color(colors["danger"], colors["cpu"], (seconds - 10.0) / 2.0)
            return colors["cpu"]
        if seconds <= 4.0:
            return colors["danger"]
        if seconds <= 5.0:
            return mix_color(colors["danger"], colors["warning"], seconds - 4.0)
        if seconds <= 10.0:
            return colors["warning"]
        return colors["cpu"]
    if team == "T":
        if seconds <= 4.0:
            return colors["cpu"]
        if seconds <= 5.0:
            return mix_color(colors["cpu"], colors["warning"], seconds - 4.0)
        if seconds <= 10.0:
            return colors["warning"]
        return _team_accent(team, colors)
    if seconds <= 4.0:
        return colors["danger"]
    if seconds <= 10.0:
        return colors["warning"]
    return colors["cpu"]


def draw_matchmaking_panel(
    draw: ImageDraw.ImageDraw,
    pings: list[RelayPing],
    colors: dict[str, str],
    source: str,
    right: int = 449,
) -> None:
    if source == "FACEIT":
        title, detail = "FACEIT", _t("HiperZ · arama öncesi")
    else:
        title, detail = _t("Eşleştirme öncesi"), _t("Valve rotası")
    draw.text((28, 181), title, font=FONTS["small_bold"], fill=colors["muted"])
    draw.text((right, 181), detail, font=FONTS["tiny"], fill=colors["dim"], anchor="ra")
    if not pings:
        center = (28 + right) // 2
        draw.text((center, 235), _t("Steam ölçümü hazırlanıyor…"), font=FONTS["label"], fill=colors["text"], anchor="mm")
        waiting = _t("FACEIT AC açıkken otomatik yenilenir") if source == "FACEIT" else _t("CS2 açıkken otomatik yenilenir")
        draw.text((center, 268), waiting, font=FONTS["small"], fill=colors["muted"], anchor="mm")
        return
    column_width = (right - 28) / 3
    centers = tuple(round(28 + column_width * (index + 0.5)) for index in range(3))
    for index, (center, item) in enumerate(zip(centers, pings[:3])):
        if index:
            divider = round(28 + column_width * index)
            draw.line((divider, 210, divider, 287), fill=colors["edge"], width=1)
        color = ping_color(item.latency_ms, colors)
        name = _fit_text(draw, relay_display_name(item), FONTS["small_bold"], column_width - 8)
        draw.text((center, 219), name, font=FONTS["small_bold"], fill=colors["muted"], anchor="mm")
        draw.text((center - 7, 263), f"{int(round(item.latency_ms))}", font=FONTS["value"], fill=colors["text"], anchor="mm")
        draw.text((center + 31, 266), "ms", font=FONTS["tiny"], fill=color, anchor="lm")


def draw_economy_panel(
    draw: ImageDraw.ImageDraw, snapshot: GameSnapshot,
    colors: dict[str, str], right: int = 449,
) -> None:
    team_color = _team_accent(snapshot.team, colors)
    if snapshot.team == "CT":
        title, team_label = _t("Savunma ekonomisi"), "CT"
    elif snapshot.team == "T":
        title, team_label = _t("Hücum ekonomisi"), "T"
    else:
        title, team_label = _t("Maç ekonomisi"), ""
    draw.rounded_rectangle((18, 169, 22, 296), radius=2, fill=team_color)
    draw.text((28, 181), title, font=FONTS["bomb_label"], fill=team_color)
    if team_label:
        badge_left = right - 35
        draw.rounded_rectangle(
            (badge_left, 171, right, 198), radius=12,
            fill=colors["panel_2"], outline=team_color, width=1,
        )
        draw.text(((badge_left + right) // 2, 184), team_label, font=FONTS["small_bold"], fill=team_color, anchor="mm")
    column_width = (right - 28) / 3
    columns = (
        (28, _t("Şimdi"), snapshot.money, colors["text"]),
        (round(28 + column_width), _t("Kazanırsan"), snapshot.next_win_money, team_color),
        (round(28 + column_width * 2), _t("Kaybedersen"), snapshot.next_loss_money, colors["warning"]),
    )
    for x, label, value, color in columns:
        draw.text((x, 210), label, font=FONTS["tiny_bold"], fill=colors["muted"])
        draw.text((x, 248), _money(value), font=FONTS["money"], fill=color, anchor="ls")
    if snapshot.loss_bonus is not None:
        draw.text((28, 280), _t("Kayıp bonusu +${value}", value=snapshot.loss_bonus), font=FONTS["tiny"], fill=colors["dim"])
    else:
        draw.text((28, 280), _t("Maç verisi bekleniyor"), font=FONTS["tiny"], fill=colors["dim"])


def draw_deathmatch_panel(
    draw: ImageDraw.ImageDraw, snapshot: GameSnapshot,
    colors: dict[str, str], right: int = 449,
) -> None:
    draw.rounded_rectangle((18, 169, 22, 296), radius=2, fill=colors["danger"])
    draw.text((28, 181), _t("Ölüm Maçı"), font=FONTS["bomb_label"], fill=colors["text"])
    center = round((28 + right) / 2)
    draw.line((center, 209, center, 284), fill=colors["edge"], width=1)
    left_center = round((28 + center) / 2)
    right_center = round((center + right) / 2)
    draw.text(
        (left_center, 220), _t("ÖLDÜRME"), font=FONTS["tiny_bold"],
        fill=colors["muted"], anchor="mm",
    )
    draw.text(
        (left_center, 259), str(snapshot.kills or 0), font=FONTS["bomb"],
        fill=colors["text"], anchor="mm",
    )
    draw.text(
        (right_center, 220), _t("KAFA VURUŞU"), font=FONTS["tiny_bold"],
        fill=colors["muted"], anchor="mm",
    )
    hs_text = "—" if snapshot.headshot_percent is None else f"%{snapshot.headshot_percent}"
    draw.text(
        (right_center, 259), hs_text, font=FONTS["money"],
        fill=colors["warning"], anchor="mm",
    )
    if snapshot.headshot_kills is not None:
        draw.text(
            (right_center, 284), _t("{value} kafa vuruşu", value=snapshot.headshot_kills),
            font=FONTS["small_bold"], fill=colors["muted"], anchor="mm",
        )


def draw_bomb_panel(
    image: Image.Image, draw: ImageDraw.ImageDraw, snapshot: GameSnapshot,
    colors: dict[str, str], right: int = 449,
) -> None:
    seconds = max(0.0, min(40.0, float(snapshot.bomb_seconds or 0.0)))
    color = _bomb_color(
        seconds, snapshot.team, snapshot.has_defuse_kit, colors,
        defusing=snapshot.bomb_defusing,
    )
    team_color = _team_accent(snapshot.team, colors)
    if snapshot.team == "CT":
        title = _t("CT · C4'ü imha et")
    elif snapshot.team == "T":
        title = _t("T · Bombayı koru")
    else:
        title = _t("C4 kuruldu")
    draw.rounded_rectangle((18, 169, 22, 296), radius=2, fill=team_color)
    draw.text((28, 179), title, font=FONTS["bomb_label"], fill=team_color)
    draw.text((28, 249), f"{seconds:04.1f}", font=FONTS["bomb"], fill=colors["text"], anchor="ls")
    draw.text((147, 246), _t("sn"), font=FONTS["small_bold"], fill=color, anchor="ls")
    if snapshot.team == "CT":
        required = 5.0 if snapshot.has_defuse_kit else 10.0
        if snapshot.bomb_defusing:
            kit_text, kit_color = _t("Çözülüyor"), colors["warning"]
        elif seconds <= required:
            kit_text, kit_color = _t("Çözme yetişmez"), colors["danger"]
        else:
            kit_text = _defuse_duration_label(snapshot.has_defuse_kit)
            kit_color = colors["cpu"] if snapshot.has_defuse_kit else colors["warning"]
    elif snapshot.team == "T":
        if snapshot.bomb_defusing:
            kit_text, kit_color = _t("CT çözüyor"), colors["danger"]
        elif seconds <= 5.0:
            kit_text, kit_color = _t("Kitli de yetişmez"), colors["cpu"]
        elif seconds <= 10.0:
            kit_text, kit_color = _t("Kitsiz yetişmez"), colors["warning"]
        else:
            kit_text, kit_color = _t("Bombayı koru"), team_color
    else:
        kit_text = _t("Patlamaya kalan")
        kit_color = colors["warning"]
    badge_left = max(181, right - 137)
    if snapshot.team == "CT":
        _draw_defuse_kit_icon(
            image, draw, badge_left + 4, 181, kit_color, colors,
            available=snapshot.has_defuse_kit,
        )
        draw.text((badge_left + 30, 192), kit_text, font=FONTS["bomb_badge"], fill=colors["text"], anchor="lm")
    elif snapshot.team == "T":
        _draw_bomb_icon(image, draw, badge_left + 5, 182, kit_color, colors)
        draw.text((badge_left + 30, 192), kit_text, font=FONTS["bomb_badge"], fill=colors["text"], anchor="lm")
    else:
        draw.text((right, 192), kit_text, font=FONTS["bomb_badge"], fill=colors["text"], anchor="rm")

    track = (28, 269, right, 286)
    draw.rounded_rectangle(track, radius=8, fill=colors["track"])
    fill_right = 28 + round((right - 28) * (seconds / 40.0))
    if fill_right > 28:
        draw.rounded_rectangle((28, 269, fill_right, 286), radius=8, fill=color)
    track_width = right - 28
    for threshold in (5, 10):
        marker_x = 28 + round(track_width * (threshold / 40.0))
        draw.line((marker_x, 267, marker_x, 288), fill=colors["text"], width=1)
        draw.text(
            (marker_x, 297), str(threshold), font=FONTS["bomb_marker"],
            fill=colors["muted"], anchor="mm",
        )
    draw.text((right - 2, 249), f"{_money(snapshot.money)}", font=FONTS["money"], fill=colors["text"], anchor="rs")
    draw.text((right - 2, 226), _t("Para"), font=FONTS["small_bold"], fill=colors["muted"], anchor="rs")


def _defuse_duration_label(has_kit: bool) -> str:
    return _t("5 sn çözme") if has_kit else _t("10 sn çözme")


def _team_accent(team: str, colors: dict[str, str]) -> str:
    if team == "CT":
        return mix_color("#5E9EFF", colors["text"], 0.08)
    if team == "T":
        return mix_color("#F0A15F", colors["text"], 0.06)
    return colors["muted"]


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


def _draw_bomb_icon(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    color: str,
    colors: dict[str, str],
) -> None:
    if not _paste_tinted_icon(image, "dynamite", (x, y, x + 21, y + 21), color):
        draw.rounded_rectangle((x + 2, y + 4, x + 18, y + 17), radius=2, outline=color, width=2)


def _draw_defuse_kit_icon(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    color: str,
    colors: dict[str, str],
    available: bool,
) -> None:
    """Compact bolt-cutter icon; intentionally ringless to save HUD space."""
    cutter = mix_color("#5E9EFF", colors["text"], 0.08) if available else colors["danger"]
    if not _paste_tinted_icon(image, "bolt-cutter", (x, y, x + 21, y + 21), cutter):
        draw.line((x + 4, y + 17, x + 17, y + 4), fill=cutter, width=2)
        draw.line((x + 4, y + 4, x + 17, y + 17), fill=cutter, width=2)
    if not available:
        draw.line((x + 2, y + 19, x + 19, y + 2), fill=colors["danger"], width=2)


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
                (center_x, 254), reading.city,
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
    game_overlay: bool = False,
) -> None:
    devices = devices[:3]
    if not devices:
        return
    if game_overlay:
        # Blend the device rail into the CS2 artwork instead of placing a hard,
        # opaque rectangle over the character and weapon.  The gradual shade
        # keeps labels readable while preserving the scene behind them.
        shade = Image.new("RGBA", (142, 130), (0, 0, 0, 0))
        shade_draw = ImageDraw.Draw(shade, "RGBA")
        panel_rgb = ImageColor.getrgb(colors["panel"])
        for x in range(shade.width):
            progress = x / max(1, shade.width - 1)
            alpha = round(25 + (185 * progress * progress))
            shade_draw.line((x, 0, x, shade.height), fill=(*panel_rgb, alpha))
        if image.mode == "RGBA":
            image.alpha_composite(shade, (323, 169))
        else:
            image.paste(shade, (323, 169), shade)
        draw.line((337, 207, 337, 290), fill=colors["edge"], width=1)
    else:
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
        label = labels.get(device.kind) or _fit_text(draw, device.name, FONTS["tiny"], 46)
        draw.text(
            (370, center_y), label,
            font=FONTS["small_bold"], fill=colors["text"], anchor="lm",
        )
        draw.text(
            (451, center_y), f"{device.percent}%",
            font=FONTS["small_bold"], fill=color, anchor="rm",
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
            value_width = draw.textlength(value, font=FONTS["value"])
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


def render_screen(
    metrics: dict[str, float | None],
    game_state: GameSnapshot | None,
    game_active: bool,
    colors: dict[str, str] | None = None,
    matchmaking_pings: list[RelayPing] | None = None,
    matchmaking_source: str = "VALVE",
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
) -> Image.Image:
    _RENDER_LANGUAGE.set(normalize_language(language))
    colors = {**COLORS, **(colors or {})}
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
        f"{int(float(metrics['gpu_memory'] or 0))} MB VRAM",
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
        f"{int(round(float(metrics['ram_used'] or 0)))} GB",
        _t("kullanımda"),
        colors,
        value_color=ram_load_alert,
        percent_color=ram_load_alert,
    )

    draw.rounded_rectangle((14, 160, 466, 306), radius=8, fill=colors["panel"], outline=colors["edge"], width=1)
    media_snapshot = media_snapshot or MediaSnapshot()
    audio_spectrum = audio_spectrum or AudioSpectrumSnapshot()
    if not game_active and msfs_active:
        draw_msfs_panel(
            image, draw, colors, flight_telemetry or FlightTelemetry(), battery_devices,
            msfs_layout,
        )
        return image
    if not game_active and (media_snapshot.visible or audio_spectrum.active):
        draw_media_panel(image, draw, media_snapshot, audio_spectrum, colors, battery_devices)
        return image
    if not game_active:
        draw_weather_panel(image, draw, weather_readings, colors, battery_devices)
        return image

    add_cs2_backdrop(image, colors)
    draw.rounded_rectangle((14, 160, 466, 306), radius=8, outline=colors["edge"], width=1)

    snapshot = game_state or GameSnapshot()
    content_right = 320 if battery_devices else 449
    if snapshot.bomb_planted and snapshot.bomb_seconds is not None:
        draw_bomb_panel(image, draw, snapshot, colors, content_right)
    elif snapshot.in_match and _is_deathmatch_mode(snapshot.game_mode):
        draw_deathmatch_panel(draw, snapshot, colors, content_right)
    elif snapshot.in_match and snapshot.next_win_money is not None:
        draw_economy_panel(draw, snapshot, colors, content_right)
    elif not snapshot.in_match:
        draw_matchmaking_panel(
            draw, matchmaking_pings or [], colors, matchmaking_source, content_right,
        )
    if battery_devices:
        draw_battery_devices(image, draw, battery_devices, colors, game_overlay=True)

    return image


def _orientation_value(orientation_module: object, rotation: str):
    if rotation == "180":
        return orientation_module.REVERSE_LANDSCAPE
    return orientation_module.LANDSCAPE


def send_to_screen(image: Image.Image, port: str, brightness: int, rotation: str = "normal") -> None:
    sys.path.insert(0, str(DRIVER_ROOT))
    from library.lcd.lcd_comm import Orientation
    from library.lcd.lcd_comm_rev_a import LcdCommRevA

    display = LcdCommRevA(com_port=port)
    try:
        display.InitializeComm()
        display.ScreenOn()
        display.SetOrientation(_orientation_value(Orientation, rotation))
        display.SetBrightness(brightness)
        display.DisplayPILImage(image.convert("RGB"))
    finally:
        display.closeSerial()


class ScreenSession:
    def __init__(self, port: str, brightness: int, rotation: str = "normal") -> None:
        sys.path.insert(0, str(DRIVER_ROOT))
        from library.lcd.lcd_comm import Orientation
        from library.lcd.lcd_comm_rev_a import LcdCommRevA

        self.orientation_module = Orientation
        self.display = LcdCommRevA(com_port=port)
        self.display.InitializeComm()
        # Stop/shutdown powers the panel down immediately. Always wake it
        # before configuring and drawing the first frame of a new session.
        self.display.ScreenOn()
        serial_connection = getattr(self.display, "lcd_serial", None)
        if serial_connection is not None:
            # A disconnected or contended USB display must not leave the
            # worker blocked forever inside a serial write.
            serial_connection.write_timeout = 6.0
        self.display.SetOrientation(_orientation_value(Orientation, rotation))
        self.display.SetBrightness(brightness)

    def configure(self, brightness: int, rotation: str) -> None:
        self.display.SetOrientation(_orientation_value(self.orientation_module, rotation))
        self.display.SetBrightness(brightness)

    def show(self, image: Image.Image) -> None:
        self.display.DisplayPILImage(image.convert("RGB"))
        serial_connection = getattr(self.display, "lcd_serial", None)
        if serial_connection is not None:
            serial_connection.flush()

    def show_regions(self, image: Image.Image, regions: list[tuple[int, int, int, int]]) -> None:
        for left, top, right, bottom in regions:
            crop = image.crop((left, top, right, bottom)).convert("RGB")
            self.display.DisplayPILImage(crop, x=left, y=top)
        serial_connection = getattr(self.display, "lcd_serial", None)
        if serial_connection is not None:
            serial_connection.flush()

    def power_off(self) -> None:
        """Blank the physical panel immediately without a full-frame transfer."""
        self.display.ScreenOff()
        serial_connection = getattr(self.display, "lcd_serial", None)
        if serial_connection is not None:
            serial_connection.flush()

    def close(self) -> None:
        self.display.closeSerial()


def main() -> int:
    parser = argparse.ArgumentParser(description="CS2 game-state + system ring display prototype")
    parser.add_argument("--preview", type=Path, help="Save one rendered frame as PNG")
    parser.add_argument("--screen-once", action="store_true", help="Send one rendered frame to the 3.5-inch display")
    parser.add_argument("--run-screen", action="store_true", help="Continuously update the display")
    parser.add_argument("--force-cs2", action="store_true", help="Show/test the CS2 mode even if the game is closed")
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
    game_state = CS2GameState()
    game_active = args.force_cs2 or cs2_is_running()
    faceit_active = faceit_ac_is_running()
    metrics = get_metrics()
    frame = render_screen(metrics, game_state.snapshot(), game_active or faceit_active)

    if args.preview:
        args.preview.parent.mkdir(parents=True, exist_ok=True)
        frame.save(args.preview)
        print(f"Önizleme: {args.preview}")

    if args.screen_once:
        send_to_screen(frame, args.port, args.brightness)
        print("Ekrana tek kare gönderildi.")

    if args.run_screen:
        screen = ScreenSession(args.port, args.brightness)
        gsi_server = GSIServer(game_state)
        gsi_server.start()
        frame_count = 0
        try:
            while True:
                started = time.monotonic()
                game_active = args.force_cs2 or cs2_is_running()
                metrics = get_metrics()
                faceit_active = faceit_ac_is_running()
                if faceit_active:
                    pings, source = FACEIT_PING_MONITOR.read(), "FACEIT"
                elif game_active:
                    pings, source = STEAM_PING_MONITOR.read(), "VALVE"
                else:
                    pings, source = [], "VALVE"
                frame = render_screen(
                    metrics, game_state.snapshot(), game_active or faceit_active,
                    matchmaking_pings=pings, matchmaking_source=source,
                    animation_time=started,
                )
                screen.show(frame)
                frame_count += 1
                if args.cycles and frame_count >= args.cycles:
                    break
                elapsed = time.monotonic() - started
                time.sleep(max(0.2, args.interval - elapsed))
        finally:
            gsi_server.close()
            screen.close()

    if not (args.preview or args.screen_once or args.run_screen):
        default_preview = ROOT / "preview.png"
        frame.save(default_preview)
        print(f"Önizleme: {default_preview}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
