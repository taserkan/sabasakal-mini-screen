"""Electricity use and cost estimate from the CPU/GPU power sensors.

Wall power is estimated as (CPU package + GPU board + other parts) divided by
the power-supply efficiency. Monitors are not included and nothing is
counted while the application is closed or the PC sleeps. Daily watt-hours
are kept in a small JSON file next to the settings.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

SAMPLE_SECONDS = 5.0
# A gap longer than this between samples (sleep, hibernate, a stalled
# thread) is not integrated: the PC was not drawing its last power reading.
MAX_GAP_SECONDS = 20.0
SAVE_SECONDS = 60.0
KEEP_DAYS = 400
DEFAULT_OTHER_WATTS = 60.0
DEFAULT_PSU_EFFICIENCY = 90.0  # percent


@dataclass(frozen=True)
class EnergySummary:
    watts_now: float | None
    today_kwh: float
    month_kwh: float
    price_per_kwh: float | None

    @property
    def today_cost(self) -> float | None:
        return None if self.price_per_kwh is None else self.today_kwh * self.price_per_kwh

    @property
    def month_cost(self) -> float | None:
        return None if self.price_per_kwh is None else self.month_kwh * self.price_per_kwh


def _positive(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def price_per_kwh(settings: dict) -> float | None:
    """₺/kWh from the last bill (amount / kWh) or the manual unit price."""
    if settings.get("energy_price_mode") == "bill":
        amount, kwh = _positive(settings.get("energy_bill_amount")), _positive(settings.get("energy_bill_kwh"))
        return amount / kwh if amount and kwh else None
    return _positive(settings.get("energy_unit_price"))


def wall_watts(cpu: float | None, gpu: float | None, other: float, efficiency_percent: float) -> float | None:
    if cpu is None and gpu is None:
        return None
    efficiency = min(100.0, max(50.0, float(efficiency_percent))) / 100.0
    return ((cpu or 0.0) + (gpu or 0.0) + max(0.0, float(other))) / efficiency


class EnergyMeter:
    """Integrates estimated wall power into per-day watt-hours."""

    def __init__(
        self,
        read_power: Callable[[], tuple[float | None, float | None]],
        store_path: Path,
        settings: Callable[[], dict],
        clock: Callable[[], float] = time.monotonic,
        today: Callable[[], dt.date] = dt.date.today,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._read_power = read_power
        self._path = Path(store_path)
        self._settings = settings
        self._clock = clock
        self._today = today
        self._log = log or (lambda _m: None)
        self._lock = threading.Lock()
        self._days: dict[str, float] = self._load()
        self._last_at: float | None = None
        self._last_watts: float | None = None
        self._dirty = False
        self._saved_at = clock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- persistence -----------------------------------------------------
    def _load(self) -> dict[str, float]:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            days = payload.get("days", {}) if isinstance(payload, dict) else {}
            return {
                str(day): float(wh) for day, wh in days.items()
                if isinstance(day, str) and len(day) == 10 and float(wh) >= 0
            }
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def save(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            cutoff = (self._today() - dt.timedelta(days=KEEP_DAYS)).isoformat()
            self._days = {day: wh for day, wh in self._days.items() if day >= cutoff}
            payload = {"version": 1, "days": {day: round(wh, 3) for day, wh in sorted(self._days.items())}}
            self._dirty = False
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(temporary, self._path)
        except OSError as exc:
            self._log(f"energy save failed: {type(exc).__name__}: {exc}")
            with self._lock:
                self._dirty = True

    # ---- sampling --------------------------------------------------------
    def sample(self) -> None:
        settings = self._settings()
        try:
            cpu, gpu = self._read_power()
        except Exception as exc:
            self._log(f"energy power read failed: {type(exc).__name__}: {exc}")
            cpu = gpu = None
        watts = wall_watts(
            cpu, gpu,
            settings.get("energy_other_watts", DEFAULT_OTHER_WATTS),
            settings.get("energy_psu_efficiency", DEFAULT_PSU_EFFICIENCY),
        )
        now = self._clock()
        with self._lock:
            if self._last_at is not None and self._last_watts is not None:
                elapsed = now - self._last_at
                if 0 < elapsed <= MAX_GAP_SECONDS:
                    # Trapezoid between the two readings.
                    average = self._last_watts if watts is None else (self._last_watts + watts) / 2.0
                    day = self._today().isoformat()
                    self._days[day] = self._days.get(day, 0.0) + average * elapsed / 3600.0
                    self._dirty = True
            self._last_at = now
            self._last_watts = watts
        if now - self._saved_at >= SAVE_SECONDS:
            self._saved_at = now
            self.save()

    def summary(self) -> EnergySummary:
        today = self._today()
        month_prefix = today.strftime("%Y-%m-")
        with self._lock:
            today_wh = self._days.get(today.isoformat(), 0.0)
            month_wh = sum(wh for day, wh in self._days.items() if day.startswith(month_prefix))
            watts = self._last_watts
        return EnergySummary(watts, today_wh / 1000.0, month_wh / 1000.0, price_per_kwh(self._settings()))

    # ---- thread ----------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="energy-meter", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        self._thread = None
        self.save()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.sample()
            self._stop.wait(SAMPLE_SECONDS)
