from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass


OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_REFRESH_SECONDS = 15 * 60
WEATHER_RETRY_SECONDS = 60
DEFAULT_WEATHER_CITIES = ("Batman", "Muğla", "İstanbul")
KNOWN_CITY_COORDINATES = {
    "batman": (37.8812, 41.1351),
    "muğla": (37.2153, 28.3636),
    "mugla": (37.2153, 28.3636),
    "istanbul": (41.0082, 28.9784),
    "i̇stanbul": (41.0082, 28.9784),
}


def format_city_name(value: object) -> str:
    """Return a tidy city label using Turkish dotted/dotless-I casing."""
    text = " ".join(str(value).strip().split())[:24]

    def capitalise(part: str) -> str:
        if not part:
            return part
        lowered = part.translate(str.maketrans({"I": "ı", "İ": "i"})).lower()
        first = {"i": "İ", "ı": "I"}.get(lowered[0], lowered[0].upper())
        return first + lowered[1:]

    words: list[str] = []
    for word in text.split(" "):
        pieces = re.split(r"([-’'])", word)
        words.append("".join(
            piece if piece in {"-", "’", "'"} else capitalise(piece)
            for piece in pieces
        ))
    return " ".join(words)


def normalise_city_names(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        candidates = value.split(",")
    elif isinstance(value, (list, tuple)):
        candidates = value
    else:
        candidates = ()
    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        name = format_city_name(candidate)
        key = name.casefold()
        if len(name) < 2 or key in seen:
            continue
        result.append(name)
        seen.add(key)
        if len(result) == 3:
            break
    return tuple(result) or DEFAULT_WEATHER_CITIES


@dataclass(frozen=True)
class WeatherReading:
    city: str
    temperature_c: float
    weather_code: int
    is_day: bool = True

    @property
    def signature(self) -> tuple[str, int, int, bool]:
        return (
            self.city,
            int(round(self.temperature_c)),
            self.weather_code,
            self.is_day,
        )


@dataclass(frozen=True)
class WeatherSnapshot:
    readings: tuple[WeatherReading, ...] = ()
    updated_at: float = 0.0
    error: str = ""

    @property
    def signature(self) -> tuple[tuple[str, int, int, bool], ...]:
        return tuple(reading.signature for reading in self.readings)


def fetch_city_weather(
    city: str,
    latitude: float,
    longitude: float,
    timeout: float = 5.0,
) -> WeatherReading:
    query = urllib.parse.urlencode({
        "latitude": f"{latitude:.4f}",
        "longitude": f"{longitude:.4f}",
        "current": "temperature_2m,weather_code,is_day",
        "temperature_unit": "celsius",
        "timezone": "auto",
    })
    request = urllib.request.Request(
        f"{OPEN_METEO_URL}?{query}",
        headers={"User-Agent": "Sabasakal-Mini-Ekran/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    current = payload.get("current") or {}
    return WeatherReading(
        city=city,
        temperature_c=float(current["temperature_2m"]),
        weather_code=int(current["weather_code"]),
        is_day=bool(int(current.get("is_day", 1))),
    )


def resolve_city(city: str, timeout: float = 5.0) -> tuple[float, float]:
    known = KNOWN_CITY_COORDINATES.get(city.casefold())
    if known is not None:
        return known
    query = urllib.parse.urlencode({
        "name": city,
        "count": 1,
        "language": "tr",
        "format": "json",
    })
    request = urllib.request.Request(
        f"{OPEN_METEO_GEOCODING_URL}?{query}",
        headers={"User-Agent": "Sabasakal-Mini-Ekran/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    results = payload.get("results") or []
    if not results:
        raise ValueError(f"Şehir bulunamadı: {city}")
    return float(results[0]["latitude"]), float(results[0]["longitude"])


def fetch_weather(
    cities: object = DEFAULT_WEATHER_CITIES,
    timeout: float = 5.0,
) -> tuple[WeatherReading, ...]:
    readings: list[WeatherReading] = []
    errors: list[str] = []
    for city in normalise_city_names(cities):
        try:
            latitude, longitude = resolve_city(city, timeout)
            readings.append(fetch_city_weather(city, latitude, longitude, timeout))
        except Exception as exc:
            errors.append(f"{city}: {type(exc).__name__}")
    if not readings:
        raise RuntimeError(", ".join(errors) or "Hava durumu alınamadı")
    return tuple(readings)


class WeatherMonitor:
    """Fetches weather away from the display loop and keeps the last good values."""

    def __init__(
        self,
        cities: object = DEFAULT_WEATHER_CITIES,
        refresh_seconds: float = WEATHER_REFRESH_SECONDS,
        retry_seconds: float = WEATHER_RETRY_SECONDS,
    ) -> None:
        self.refresh_seconds = max(60.0, refresh_seconds)
        self.retry_seconds = max(15.0, retry_seconds)
        self._snapshot = WeatherSnapshot()
        self._cities = normalise_city_names(cities)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._wake.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="weather-monitor",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def snapshot(self) -> WeatherSnapshot:
        with self._lock:
            return self._snapshot

    def set_cities(self, cities: object) -> None:
        normalised = normalise_city_names(cities)
        with self._lock:
            if normalised == self._cities:
                return
            self._cities = normalised
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            succeeded = False
            try:
                with self._lock:
                    cities = self._cities
                readings = fetch_weather(cities)
                with self._lock:
                    self._snapshot = WeatherSnapshot(
                        readings=readings,
                        updated_at=time.time(),
                    )
                succeeded = True
            except Exception as exc:
                with self._lock:
                    self._snapshot = WeatherSnapshot(
                        readings=self._snapshot.readings,
                        updated_at=self._snapshot.updated_at,
                        error=f"{type(exc).__name__}: {exc}",
                    )
            self._wake.wait(self.refresh_seconds if succeeded else self.retry_seconds)
            self._wake.clear()
