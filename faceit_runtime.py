"""FACEIT card: ELO, level, today's change and recent results.

Only FACEIT's official public Data API (open.faceit.com/data/v4) is used,
with the user's own server-side API key. Nothing here touches CS2: no game
memory, files, input or network traffic. The key is stored encrypted with
Windows DPAPI (current user), never in plain text.
"""
from __future__ import annotations

import base64
import ctypes
import datetime as dt
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

API_ROOT = "https://open.faceit.com/data/v4"
GAME = "cs2"
REFRESH_SECONDS = 10 * 60
# While the FACEIT card is pinned (CS2 + FACEIT AC) a finished match shows up
# on the card within a few minutes: two small requests every 3 minutes.
PINNED_REFRESH_SECONDS = 3 * 60
# After the game closes the match result reaches FACEIT within a few minutes.
AFTER_GAME_DELAYS = (120, 300)
# After a followed match ends (faceit_live): the result lands within a minute or two.
AFTER_MATCH_DELAYS = (60, 180)
MAX_RESPONSE_BYTES = 512 * 1024


# ---- Windows DPAPI (current user) -------------------------------------------

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> tuple[_Blob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _dpapi(data: bytes, protect: bool) -> bytes:
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    source, _keep = _blob(data)
    result = _Blob()
    entropy, _keep_entropy = _blob(b"Sabasakal-Mini-Screen/faceit")
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = function(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 0x1, ctypes.byref(result))
    if not ok:
        raise OSError(ctypes.get_last_error(), "DPAPI failed")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(result.pbData)


def protect_secret(secret: str) -> str:
    """Encrypt for the current Windows user; returns base64 text for settings.json."""
    return base64.b64encode(_dpapi(secret.encode("utf-8"), True)).decode("ascii") if secret else ""


def unprotect_secret(stored: str) -> str:
    if not stored:
        return ""
    try:
        return _dpapi(base64.b64decode(stored), False).decode("utf-8")
    except (OSError, ValueError, UnicodeDecodeError):
        return ""


# ---- Data ------------------------------------------------------------------

@dataclass(frozen=True)
class FaceitMatch:
    won: bool
    kills: int | None = None
    kd: float | None = None
    headshots: float | None = None
    map_name: str = ""
    match_id: str = ""
    score: str = ""  # "13-6", own team first


@dataclass(frozen=True)
class FaceitSnapshot:
    nickname: str = ""
    level: int | None = None
    elo: int | None = None
    elo_today: int | None = None  # change since the first reading today
    recent: tuple[FaceitMatch, ...] = ()
    kd: float | None = None  # average over ``recent``
    headshots: float | None = None
    updated_at: float = 0.0
    error: str = ""
    # ELO won/lost in the newest match; None when unknown, or when several
    # matches finished between two readings (the change can't be split).
    last_change: int | None = None

    @property
    def ready(self) -> bool:
        return self.elo is not None


def _number(value) -> float | None:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def parse_player(payload: dict) -> tuple[str, str, int | None, int | None]:
    """(player_id, nickname, level, elo) from GET /players."""
    if not isinstance(payload, dict) or not payload.get("player_id"):
        raise ValueError("FACEIT player not found")
    game = (payload.get("games") or {}).get(GAME) or {}
    level, elo = game.get("skill_level"), game.get("faceit_elo")
    return (
        str(payload["player_id"]), str(payload.get("nickname") or ""),
        int(level) if isinstance(level, (int, float)) else None,
        int(elo) if isinstance(elo, (int, float)) else None,
    )


def parse_recent_matches(payload: dict, limit: int = 5) -> tuple[FaceitMatch, ...]:
    """Recent results from GET /players/{id}/games/cs2/stats (newest first)."""
    matches = []
    for item in (payload or {}).get("items", [])[:limit]:
        stats = item.get("stats", item) if isinstance(item, dict) else {}
        result = str(stats.get("Result", "")).strip()
        if result not in {"0", "1"}:
            continue
        kills = _number(stats.get("Kills"))
        matches.append(FaceitMatch(
            won=result == "1",
            kills=int(kills) if kills is not None else None,
            kd=_number(stats.get("K/D Ratio")),
            headshots=_number(stats.get("Headshots %")),
            map_name=str(stats.get("Map", "")).replace("de_", "").capitalize(),
            match_id=str(stats.get("Match Id", "") or ""),
            score=_score(stats.get("Score"), result == "1"),
        ))
    return tuple(matches)


def _score(value, won: bool) -> str:
    """'13 / 6' -> '13-6' with the player's own team first."""
    parts = [part.strip() for part in str(value or "").split("/")]
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return ""
    high, low = sorted((int(parts[0]), int(parts[1])), reverse=True)
    return f"{high}-{low}" if won else f"{low}-{high}"


def _average(values) -> float | None:
    values = [value for value in values if value is not None]
    return sum(values) / len(values) if values else None


class FaceitClient:
    def __init__(self, api_key: str, timeout: float = 8.0) -> None:
        self._key = api_key
        self._timeout = timeout

    def _get(self, path: str, **query) -> dict:
        url = f"{API_ROOT}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self._key}",
            "Accept": "application/json",
            "User-Agent": "Sabasakal-Mini-Ekran",
        })
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            return json.loads(response.read(MAX_RESPONSE_BYTES).decode("utf-8"))

    def player(self, nickname: str) -> tuple[str, str, int | None, int | None]:
        return parse_player(self._get("/players", nickname=nickname, game=GAME))

    def recent(self, player_id: str, limit: int = 5) -> tuple[FaceitMatch, ...]:
        return parse_recent_matches(
            self._get(f"/players/{urllib.parse.quote(player_id)}/games/{GAME}/stats", limit=limit), limit,
        )


def describe_error(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return {401: "API anahtarı geçersiz", 403: "API anahtarının izni yok",
                404: "FACEIT kullanıcısı bulunamadı", 429: "FACEIT istek sınırı doldu"}.get(
            exc.code, f"FACEIT hatası ({exc.code})")
    if isinstance(exc, ValueError):
        return "FACEIT kullanıcısı bulunamadı"
    return "FACEIT'e bağlanılamadı"


# ---- Monitor ----------------------------------------------------------------

class FaceitMonitor:
    """Background refresh with a small on-disk cache and today's ELO baseline."""

    def __init__(
        self,
        settings: Callable[[], dict],
        cache_path: Path,
        game_running: Callable[[], bool],
        client_factory: Callable[[str], FaceitClient] = FaceitClient,
        clock: Callable[[], float] = time.monotonic,
        today: Callable[[], dt.date] = dt.date.today,
        log: Callable[[str], None] | None = None,
        pinned: Callable[[], bool] = lambda: False,
    ) -> None:
        self._settings = settings
        self._path = Path(cache_path)
        self._game_running = game_running
        self._pinned = pinned
        self._refreshed_at: float | None = None
        self._client_factory = client_factory
        self._clock = clock
        self._today = today
        self._log = log or (lambda _m: None)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_at = 0.0
        self._after_game: list[float] = []
        self._was_in_game: bool | None = None
        cache = self._load()
        self._baseline: dict = cache.get("baseline", {})
        # The previous reading: {"elo", "match_id" (newest match), "change"}.
        self._last: dict = cache.get("last", {}) if isinstance(cache.get("last"), dict) else {}
        self._snapshot = self._snapshot_from_cache(cache.get("snapshot", {}))

    # ---- cache ---------------------------------------------------------
    def _load(self) -> dict:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _snapshot_from_cache(data: dict) -> FaceitSnapshot:
        try:
            recent = tuple(FaceitMatch(**match) for match in data.get("recent", []))
            fields = {k: data.get(k) for k in (
                "nickname", "level", "elo", "elo_today", "last_change", "kd", "headshots")}
            return FaceitSnapshot(**{k: v for k, v in fields.items() if v is not None}, recent=recent)
        except (TypeError, ValueError, AttributeError):
            return FaceitSnapshot()

    def _save(self) -> None:
        snapshot = self._snapshot
        payload = {
            "version": 1,
            "baseline": self._baseline,
            "last": self._last,
            "snapshot": {
                "nickname": snapshot.nickname, "level": snapshot.level, "elo": snapshot.elo,
                "elo_today": snapshot.elo_today, "last_change": snapshot.last_change,
                "kd": snapshot.kd, "headshots": snapshot.headshots,
                "recent": [match.__dict__ for match in snapshot.recent],
            },
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(temporary, self._path)
        except OSError as exc:
            self._log(f"faceit cache save failed: {type(exc).__name__}: {exc}")

    # ---- public ----------------------------------------------------------
    def snapshot(self) -> FaceitSnapshot:
        with self._lock:
            return self._snapshot

    def configured(self) -> bool:
        settings = self._settings()
        return bool(settings.get("faceit_nickname")) and bool(settings.get("faceit_key_protected"))

    def refresh_now(self) -> None:
        self._next_at = 0.0
        self._wake.set()

    def refresh(self) -> FaceitSnapshot:
        """One synchronous update (used by the thread and the "Verify" button)."""
        settings = self._settings()
        nickname = str(settings.get("faceit_nickname") or "").strip()
        key = unprotect_secret(str(settings.get("faceit_key_protected") or ""))
        if not nickname or not key:
            snapshot = FaceitSnapshot(error="FACEIT ayarlanmadı")
            with self._lock:
                self._snapshot = snapshot
            return snapshot
        try:
            client = self._client_factory(key)
            player_id, name, level, elo = client.player(nickname)
            recent = client.recent(player_id)
        except Exception as exc:  # network/HTTP/parse: keep the last good card
            message = describe_error(exc)
            self._log(f"faceit refresh failed: {type(exc).__name__}: {message}")
            with self._lock:
                self._snapshot = FaceitSnapshot(**{
                    **{k: getattr(self._snapshot, k) for k in (
                        "nickname", "level", "elo", "elo_today", "last_change", "recent", "kd",
                        "headshots", "updated_at")},
                    "error": message,
                })
                return self._snapshot
        today = self._today().isoformat()
        if elo is not None and (self._baseline.get("date") != today or self._baseline.get("player") != player_id):
            self._baseline = {"date": today, "player": player_id, "elo": elo}
        start = self._baseline.get("elo")
        last_change = self._newest_match_change(elo, recent)
        snapshot = FaceitSnapshot(
            nickname=name or nickname, level=level, elo=elo,
            elo_today=(elo - start) if elo is not None and isinstance(start, int) else None,
            last_change=last_change,
            recent=recent,
            kd=_average(match.kd for match in recent),
            headshots=_average(match.headshots for match in recent),
            updated_at=time.time(),
        )
        with self._lock:
            self._snapshot = snapshot
        self._save()
        return snapshot

    def _newest_match_change(self, elo: int | None, recent: tuple[FaceitMatch, ...]) -> int | None:
        """ELO change of the newest match, from two readings around it."""
        previous = self._last
        newest = recent[0].match_id if recent else ""
        change = previous.get("change") if isinstance(previous.get("change"), int) else None
        if elo is None or not newest:
            return change
        before = previous.get("elo") if isinstance(previous.get("elo"), int) else None
        if newest == previous.get("match_id"):
            # Same newest match. A different ELO means FACEIT updated the ELO
            # before the match list: keep the pre-match reading until it lands.
            if before is None:
                self._last = {**previous, "elo": elo}
            return change
        if before is not None and elo == before:
            return change  # the match is listed but its ELO is not settled yet
        ids = [match.match_id for match in recent]
        new_matches = ids.index(previous["match_id"]) if previous.get("match_id") in ids else None
        change = elo - before if new_matches == 1 and before is not None else None
        self._last = {"elo": elo, "match_id": newest, "change": change}
        return change

    def match_finished(self) -> None:
        """A followed match just ended: look for its result shortly."""
        now = self._clock()
        self._after_game = sorted(set(self._after_game) | {now + d for d in AFTER_MATCH_DELAYS})
        self._wake.set()

    # ---- thread ----------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="faceit", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        self._thread = None

    def tick(self) -> bool:
        """Decide whether to refresh now; returns True when a refresh ran."""
        now = self._clock()
        in_game = bool(self._game_running())
        if self._was_in_game and not in_game:
            self._after_game = [now + delay for delay in AFTER_GAME_DELAYS]
        self._was_in_game = in_game
        due_after_game = any(now >= at for at in self._after_game)
        if due_after_game:
            self._after_game = [at for at in self._after_game if at > now]
        pinned = bool(self._pinned())
        if not self.configured() or (in_game and not pinned and not due_after_game):
            return False  # a plain CS2 session is never polled
        due_pinned = pinned and (self._refreshed_at is None or now - self._refreshed_at >= PINNED_REFRESH_SECONDS)
        if now >= self._next_at or due_after_game or due_pinned:
            self._next_at = now + REFRESH_SECONDS
            self._refreshed_at = now
            self.refresh()
            return True
        return False

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                self._log(f"faceit tick failed: {type(exc).__name__}: {exc}")
            self._wake.wait(15.0)
            self._wake.clear()
