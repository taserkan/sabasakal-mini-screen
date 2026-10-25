"""Live FACEIT match card: both teams' ELO, the map and each team's record on it.

FACEIT's public Data API has no "current match of a player" call, but every
match room is a page at faceit.com/<lang>/cs2/room/<match id>. While CS2 and
FACEIT AC both run, the address bar of a Chromium browser window whose
active tab is a FACEIT page is read through Windows UI Automation (read-only,
the same interface screen readers use). Only a room match id is kept; no
other address is stored or logged. Everything else comes from the official
Data API with the user's own key (see faceit_runtime).
"""
from __future__ import annotations

import ctypes
import re
import threading
import time
import urllib.parse
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable, Iterable

from faceit_runtime import FaceitClient, GAME, describe_error, unprotect_secret

ROOM_URL = re.compile(
    r"faceit\.com/(?:[a-z]{2}(?:-[a-z]{2,4})?/)?cs2/room/"
    r"(1-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)
# Chrome, Edge, Brave, Opera, Vivaldi: all Chromium top-level windows.
BROWSER_WINDOW_CLASS = "Chrome_WidgetWin_1"
SCAN_SECONDS = 5.0  # looking for a room while none is known
VOTING_POLL_SECONDS = 15.0  # until the map is picked
MATCH_POLL_SECONDS = 60.0  # then only to notice the end of the match
ENDED = {"FINISHED", "CANCELLED", "ABORTED"}
ELO_K = 50  # FACEIT's spread: +25/-25 between equal teams


def match_id_from_url(url: str) -> str | None:
    found = ROOM_URL.search(str(url or ""))
    return found.group(1).lower() if found else None


def map_label(value: str) -> str:
    """'de_mirage' / 'Mirage' -> 'Mirage'."""
    name = str(value or "").strip()
    if name.lower().startswith("de_"):
        name = name[3:]
    return name[:1].upper() + name[1:].lower() if name else ""


def elo_change_estimate(my_team_elo: float, other_team_elo: float) -> tuple[int, int]:
    """(points for a win, points lost for a defeat), Elo-style; FACEIT does not
    publish its exact formula, so the screen shows these as approximate."""
    expected = 1.0 / (1.0 + 10 ** ((other_team_elo - my_team_elo) / 400.0))
    win = min(ELO_K - 1, max(1, round(ELO_K * (1.0 - expected))))
    return win, ELO_K - win


# ---- Data --------------------------------------------------------------------

@dataclass(frozen=True)
class LivePlayer:
    player_id: str
    nickname: str
    level: int | None = None
    elo: int | None = None


@dataclass(frozen=True)
class LiveTeam:
    name: str
    players: tuple[LivePlayer, ...]
    mine: bool = False
    map_matches: int = 0
    map_wins: int = 0

    @property
    def avg_elo(self) -> int | None:
        values = [p.elo for p in self.players if p.elo is not None]
        return round(sum(values) / len(values)) if values else None

    @property
    def map_win_rate(self) -> float | None:
        return self.map_wins / self.map_matches * 100.0 if self.map_matches else None


@dataclass(frozen=True)
class LiveMatch:
    match_id: str
    status: str = ""
    map_name: str = ""
    teams: tuple[LiveTeam, ...] = ()
    my_elo: int | None = None
    error: str = ""

    @property
    def ready(self) -> bool:
        return len(self.teams) == 2 and all(team.avg_elo is not None for team in self.teams)

    def elo_estimate(self) -> tuple[int, int] | None:
        if not self.ready:
            return None
        mine = next((team for team in self.teams if team.mine), None)
        other = next((team for team in self.teams if not team.mine), None)
        if mine is None or other is None:
            return None
        return elo_change_estimate(mine.avg_elo, other.avg_elo)


def parse_match(payload: dict) -> tuple[str, str, list[tuple[str, list[LivePlayer]]]]:
    """(status, map, [(team name, roster), ...]) from GET /matches/{id}."""
    if not isinstance(payload, dict):
        raise ValueError("FACEIT match not found")
    teams = []
    for key in ("faction1", "faction2"):
        team = (payload.get("teams") or {}).get(key) or {}
        roster = [
            LivePlayer(str(p.get("player_id") or ""), str(p.get("nickname") or ""),
                       p.get("game_skill_level") if isinstance(p.get("game_skill_level"), int) else None)
            for p in team.get("roster") or [] if isinstance(p, dict) and p.get("player_id")
        ]
        teams.append((str(team.get("name") or key), roster))
    picks = (((payload.get("voting") or {}).get("map") or {}).get("pick") or [])
    return str(payload.get("status") or "").upper(), map_label(picks[0]) if picks else "", teams


def parse_map_record(payload: dict, map_name: str) -> tuple[int, int]:
    """(matches, wins) on ``map_name`` from GET /players/{id}/stats/cs2."""
    wanted = map_label(map_name).lower()
    for segment in (payload or {}).get("segments") or []:
        if not isinstance(segment, dict) or str(segment.get("type", "")).lower() != "map":
            continue
        if map_label(segment.get("label", "")).lower() != wanted:
            continue
        stats = segment.get("stats") or {}
        try:
            return int(float(stats.get("Matches", 0))), int(float(stats.get("Wins", 0)))
        except (TypeError, ValueError):
            return 0, 0
    return 0, 0


class LiveClient(FaceitClient):
    def match(self, match_id: str) -> dict:
        return self._get(f"/matches/{urllib.parse.quote(match_id)}")

    def player_by_id(self, player_id: str) -> dict:
        return self._get(f"/players/{urllib.parse.quote(player_id)}")

    def player_stats(self, player_id: str) -> dict:
        return self._get(f"/players/{urllib.parse.quote(player_id)}/stats/{GAME}")


# ---- Browser address bar (UI Automation through raw COM) ---------------------

class _Guid(ctypes.Structure):
    _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16),
                ("d", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text: str) -> "_Guid":
        guid = cls()
        ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p("{" + text + "}"), ctypes.byref(guid))
        return guid


class _Variant(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                ("r3", ctypes.c_ushort), ("value", ctypes.c_void_p), ("extra", ctypes.c_void_p)]


CLSID_CUIAUTOMATION = "ff48dba4-60ef-4201-aa87-54103eef594e"
IID_IUIAUTOMATION = "30cbe57d-d9d0-452a-ab13-7ac5ac4825ee"
UIA_CONTROL_TYPE_PROPERTY = 30003
UIA_VALUE_VALUE_PROPERTY = 30045
UIA_EDIT_CONTROL_TYPE = 50004
TREE_SCOPE_DESCENDANTS = 4
VT_I4, VT_BSTR = 3, 8


def _method(obj: ctypes.c_void_p, index: int, *argtypes):
    table = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(table[index])


def _release(obj: ctypes.c_void_p) -> None:
    if obj and obj.value:
        table = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(table[2])(obj)


class AddressBarReader:
    """Reads the address bar text of a Chromium window (one COM object, reused)."""

    def __init__(self) -> None:
        self._automation = ctypes.c_void_p()
        self._condition = ctypes.c_void_p()

    def _ensure(self) -> None:
        if self._automation.value:
            return
        ole32 = ctypes.oledll.ole32
        try:
            ole32.CoInitializeEx(None, 0x0)  # COINIT_MULTITHREADED, once per thread
        except OSError:
            pass  # already initialised
        ole32.CoCreateInstance(
            ctypes.byref(_Guid.parse(CLSID_CUIAUTOMATION)), None, 0x1 | 0x4,  # INPROC | LOCAL
            ctypes.byref(_Guid.parse(IID_IUIAUTOMATION)), ctypes.byref(self._automation),
        )
        variant = _Variant(vt=VT_I4, value=UIA_EDIT_CONTROL_TYPE)
        # IUIAutomation::CreatePropertyCondition (vtable 23)
        _method(self._automation, 23, ctypes.c_int, _Variant, ctypes.POINTER(ctypes.c_void_p))(
            self._automation, UIA_CONTROL_TYPE_PROPERTY, variant, ctypes.byref(self._condition))

    def read(self, hwnd: int) -> str:
        self._ensure()
        window, edit = ctypes.c_void_p(), ctypes.c_void_p()
        value = _Variant()
        try:
            # IUIAutomation::ElementFromHandle (6)
            _method(self._automation, 6, wintypes.HWND, ctypes.POINTER(ctypes.c_void_p))(
                self._automation, hwnd, ctypes.byref(window))
            # IUIAutomationElement::FindFirst (5): the omnibox is the first Edit
            _method(window, 5, ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
                window, TREE_SCOPE_DESCENDANTS, self._condition, ctypes.byref(edit))
            if not edit.value:
                return ""
            # IUIAutomationElement::GetCurrentPropertyValue (10)
            _method(edit, 10, ctypes.c_int, ctypes.POINTER(_Variant))(
                edit, UIA_VALUE_VALUE_PROPERTY, ctypes.byref(value))
            return ctypes.wstring_at(value.value) if value.vt == VT_BSTR and value.value else ""
        finally:
            ctypes.oledll.oleaut32.VariantClear(ctypes.byref(value))
            _release(edit)
            _release(window)

    def close(self) -> None:
        _release(self._condition)
        _release(self._automation)
        self._condition, self._automation = ctypes.c_void_p(), ctypes.c_void_p()


def faceit_browser_windows() -> list[int]:
    """Visible Chromium windows whose active tab title mentions FACEIT."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        name = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, name, 64)
        if name.value != BROWSER_WINDOW_CLASS:
            return True
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, 512)
        if "faceit" in title.value.casefold():
            found.append(int(hwnd))
        return True

    user32.EnumWindows(visit, 0)
    return found


def browser_room_match_id(
    windows: Callable[[], Iterable[int]] = faceit_browser_windows,
    read_url: Callable[[int], str] | None = None,
) -> str | None:
    for hwnd in windows():
        try:
            match_id = match_id_from_url(read_url(hwnd) if read_url else "")
        except OSError:
            continue
        if match_id:
            return match_id
    return None


# ---- Monitor -----------------------------------------------------------------

class FaceitLiveMonitor:
    """Follows the room of the match being played while ``active()`` is true."""

    def __init__(
        self,
        settings: Callable[[], dict],
        active: Callable[[], bool],
        client_factory: Callable[[str], LiveClient] = LiveClient,
        find_match: Callable[[], str | None] | None = None,
        on_finished: Callable[[], None] = lambda: None,
        clock: Callable[[], float] = time.monotonic,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._settings = settings
        self._active = active
        self._client_factory = client_factory
        self._find_match = find_match
        self._on_finished = on_finished
        self._clock = clock
        self._log = log or (lambda _m: None)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._snapshot: LiveMatch | None = None
        self._match_id: str | None = None
        self._ended: set[str] = set()
        self._my_id: tuple[str, str] | None = None  # (nickname, player_id)
        self._players: dict[str, tuple[LivePlayer, dict]] = {}
        self._next_at = 0.0
        self._reader: AddressBarReader | None = None
        self._reader_failed = False

    def snapshot(self) -> LiveMatch | None:
        with self._lock:
            return self._snapshot

    def _publish(self, snapshot: LiveMatch | None) -> None:
        with self._lock:
            self._snapshot = snapshot

    def _reset(self) -> None:
        self._match_id, self._players, self._next_at = None, {}, 0.0
        self._publish(None)

    def _client(self) -> tuple[LiveClient, str] | None:
        settings = self._settings()
        nickname = str(settings.get("faceit_nickname") or "").strip()
        key = unprotect_secret(str(settings.get("faceit_key_protected") or ""))
        if not nickname or not key:
            return None
        return self._client_factory(key), nickname

    def _my_player_id(self, client: LiveClient, nickname: str) -> str:
        if self._my_id is None or self._my_id[0] != nickname:
            player_id, _name, _level, _elo = client.player(nickname)
            self._my_id = (nickname, player_id)
        return self._my_id[1]

    def _default_find_match(self) -> str | None:
        try:
            if self._reader is None:
                self._reader = AddressBarReader()
            return browser_room_match_id(read_url=self._reader.read)
        except OSError as exc:  # UI Automation unavailable: the plain card stays
            if not self._reader_failed:
                self._reader_failed = True
                self._log(f"faceit live: address bar unreadable: {exc}")
            return None

    def tick(self) -> None:
        if not self._active():
            if self._match_id is not None or self._snapshot is not None:
                self._reset()
            return
        now = self._clock()
        if now < self._next_at:
            return
        if self._match_id is None:
            self._next_at = now + SCAN_SECONDS
            match_id = (self._find_match or self._default_find_match)()
            if not match_id or match_id in self._ended:
                return
            self._match_id = match_id
            self._log("faceit live: match room found")
        self._update(now)

    def _update(self, now: float) -> None:
        access = self._client()
        if access is None:
            return
        client, nickname = access
        match_id = self._match_id
        try:
            my_id = self._my_player_id(client, nickname)
            status, map_name, teams = parse_match(client.match(match_id))
            if not any(p.player_id == my_id for _name, roster in teams for p in roster):
                # Someone else's room (e.g. watching a friend): forget it.
                self._ended.add(match_id)
                self._reset()
                return
            if status in ENDED:
                self._log(f"faceit live: match {status.lower()}")
                self._ended.add(match_id)
                self._reset()
                self._on_finished()
                return
            for _name, roster in teams:
                for player in roster:
                    if player.player_id not in self._players:
                        profile = client.player_by_id(player.player_id)
                        elo = ((profile.get("games") or {}).get(GAME) or {}).get("faceit_elo")
                        self._players[player.player_id] = (
                            LivePlayer(player.player_id, player.nickname, player.level,
                                       int(elo) if isinstance(elo, (int, float)) else None),
                            client.player_stats(player.player_id),
                        )
            live_teams = []
            for name, roster in teams:
                players = tuple(self._players[p.player_id][0] for p in roster)
                matches = wins = 0
                if map_name:
                    for p in roster:
                        played, won = parse_map_record(self._players[p.player_id][1], map_name)
                        matches, wins = matches + played, wins + won
                live_teams.append(LiveTeam(name, players, any(p.player_id == my_id for p in roster),
                                           matches, wins))
            me = self._players[my_id][0]
            self._publish(LiveMatch(match_id, status, map_name, tuple(live_teams), me.elo))
            self._next_at = now + (MATCH_POLL_SECONDS if map_name else VOTING_POLL_SECONDS)
        except Exception as exc:  # keep the last good card, retry soon
            self._log(f"faceit live update failed: {type(exc).__name__}: {describe_error(exc)}")
            self._next_at = now + VOTING_POLL_SECONDS
            current = self.snapshot()
            if current is not None:
                self._publish(LiveMatch(current.match_id, current.status, current.map_name,
                                        current.teams, current.my_elo, describe_error(exc)))

    # ---- thread --------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="faceit-live", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        self._thread = None

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception as exc:
                    self._log(f"faceit live tick failed: {type(exc).__name__}: {exc}")
                self._stop.wait(1.0)
        finally:
            if self._reader is not None:
                self._reader.close()
