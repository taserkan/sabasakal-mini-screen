"""Keep G HUB on a game's own profile while that game is in front.

G HUB switches profiles when the foreground window changes. When a game
starts it often takes the focus before G HUB has matched the new process, so
the desktop profile (and its DPI) stays active until the next alt-tab.

While the foreground process is one of the user's game profiles (cs2.exe ...)
this asks G HUB's local websocket which profile is active and, if it is not
the profile G HUB itself assigned to that game, activates that profile. That
is the same switch G HUB makes on its own; no G HUB file or setting is
edited, and nothing happens on the desktop or in other apps.
"""
from __future__ import annotations

import contextlib
import ctypes
import json
import ntpath
import secrets
import threading
import time
from ctypes import wintypes
from typing import Callable, Iterable, Iterator

import psutil

import battery_runtime as br

CHECK_SECONDS = 3.0
PROFILE_ID_TYPE = "type.googleapis.com/logi.protocol.profiles.ProfileId"
CATALOG_SECONDS = 600.0  # applications/profiles are re-read at most this often


def foreground_exe() -> str:
    """Lower-case process name of the foreground window ("" if unknown)."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        return psutil.Process(pid.value).name().lower()
    except (psutil.Error, ValueError):
        return ""


Call = Callable[[str, str, dict | None], dict | None]


@contextlib.contextmanager
def lghub_session(timeout: float = 1.5) -> Iterator[Call]:
    """One websocket connection; yields call(verb, path, payload) -> message."""
    with br._connect_lghub_websocket(timeout=timeout) as stream:
        stream.settimeout(timeout)

        def call(verb: str, path: str, payload: dict | None = None) -> dict | None:
            message_id = str(secrets.randbelow(2_000_000_000) + 1)
            request = {"msgId": message_id, "verb": verb, "path": path}
            if payload is not None:
                request["payload"] = payload
            br._send_websocket_text(stream, json.dumps(request, separators=(",", ":")))
            for _ in range(40):
                message = json.loads(br._receive_websocket_message(stream))
                if isinstance(message, dict) and str(message.get("msgId", "")) == message_id:
                    return message
            return None

        yield call


def _ok(message: dict | None) -> dict | None:
    if not isinstance(message, dict):
        return None
    code = str((message.get("result") or {}).get("code", "")).upper()
    payload = message.get("payload")
    return payload if code == "SUCCESS" and isinstance(payload, dict) else None


def game_profile_ids(applications: dict | None, profiles: dict | None) -> dict[str, str]:
    """{exe name: id of the profile G HUB assigned to it} from /applications + /profiles."""
    app_exes = {}
    for app in (applications or {}).get("applications") or []:
        path = str(app.get("applicationPath") or "") if isinstance(app, dict) else ""
        if path.lower().endswith(".exe") and app.get("applicationId"):
            app_exes[str(app["applicationId"])] = ntpath.basename(path).lower()
    result: dict[str, str] = {}
    for profile in (profiles or {}).get("profiles") or []:
        if not isinstance(profile, dict) or profile.get("disabled"):
            continue
        exe = app_exes.get(str(profile.get("applicationId") or ""))
        if exe and profile.get("activeForApplication") and profile.get("id"):
            result[exe] = str(profile["id"])
    return result


class GHubProfileGuard:
    def __init__(
        self,
        game_exes: Callable[[], Iterable[str]],
        enabled: Callable[[], bool],
        foreground: Callable[[], str] = foreground_exe,
        session: Callable[[], contextlib.AbstractContextManager[Call]] = lghub_session,
        clock: Callable[[], float] = time.monotonic,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._game_exes = game_exes
        self._enabled = enabled
        self._foreground = foreground
        self._session = session
        self._clock = clock
        self._log = log or (lambda _m: None)
        self._catalog: dict[str, str] = {}
        self._catalog_at: float | None = None
        self._failed = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> bool:
        """Returns True when it had to switch G HUB to the game's profile."""
        if not self._enabled():
            return False
        exe = self._foreground()
        if not exe or exe not in {name.lower() for name in self._game_exes()}:
            return False
        try:
            with self._session() as call:
                now = self._clock()
                if self._catalog_at is None or now - self._catalog_at >= CATALOG_SECONDS:
                    self._catalog = game_profile_ids(_ok(call("GET", "/applications", None)),
                                                     _ok(call("GET", "/profiles", None)))
                    self._catalog_at = now
                target = self._catalog.get(exe)
                if not target:
                    return False  # G HUB has no profile of its own for this game
                active = (_ok(call("GET", "/profile/active", None)) or {}).get("id")
                if active == target:
                    return False
                result = call("SET", "/profile/active", {"@type": PROFILE_ID_TYPE, "id": target})
            self._failed = False
            switched = _ok(result) is not None or str(
                ((result or {}).get("result") or {}).get("code", "")).upper() == "SUCCESS"
            self._log(f"ghub: {exe} profile was not active -> "
                      + ("activated" if switched else f"switch refused: {(result or {}).get('result')}"))
            return switched
        except (OSError, ValueError) as exc:  # G HUB closed or restarting
            if not self._failed:
                self._failed = True
                self._log(f"ghub: profile check failed: {type(exc).__name__}: {exc}")
            return False

    # ---- thread --------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ghub-profiles", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                self._log(f"ghub: tick failed: {type(exc).__name__}: {exc}")
            self._stop.wait(CHECK_SECONDS)
