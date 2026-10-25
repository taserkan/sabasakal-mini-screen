"""Game profiles: per-game monitor mode, NVIDIA vibrance, audio output, screen.

When a profiled game's process appears its settings are applied once; when it
exits (confirmed over a few polls) everything is put back. The values that
were replaced are written to a small state file first, so a crash or power
loss mid-game is repaired on the next start. Detection is a process-name
check only; nothing reads or touches the game itself.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable

POLL_SECONDS = 1.0
EXIT_CONFIRMATIONS = 3
MONITOR_MODES = ("GAMER1", "GAMER2", "GAMER3")
SCREENS = ("theme", "strip")
MAX_PROFILES = 20


@dataclass(frozen=True)
class GameProfile:
    exe: str
    name: str = ""
    monitor_mode: str = ""  # "" = leave the monitor alone
    vibrance: int | None = None  # None = leave vibrance alone
    audio_device: str = ""  # "" = leave the default output alone
    audio_name: str = ""
    screen: str = "theme"  # theme (orange desktop) | strip (in-game strip)

    @property
    def title(self) -> str:
        return self.name or self.exe


def normalise_profile(data) -> GameProfile | None:
    if not isinstance(data, dict):
        return None
    exe = str(data.get("exe") or "").strip().lower()
    if not exe.endswith(".exe") or "/" in exe or "\\" in exe or len(exe) > 64:
        return None
    vibrance = data.get("vibrance")
    if isinstance(vibrance, bool) or not isinstance(vibrance, (int, float)):
        vibrance = None
    else:
        vibrance = max(0, min(100, int(vibrance)))
    mode = str(data.get("monitor_mode") or "").upper()
    screen = data.get("screen") if data.get("screen") in SCREENS else "theme"
    return GameProfile(
        exe=exe,
        name=str(data.get("name") or "").strip()[:40],
        monitor_mode=mode if mode in MONITOR_MODES else "",
        vibrance=vibrance,
        audio_device=str(data.get("audio_device") or "")[:200],
        audio_name=str(data.get("audio_name") or "")[:80],
        screen=screen,
    )


def normalise_profiles(items) -> list[GameProfile]:
    profiles, seen = [], set()
    for item in items if isinstance(items, list) else []:
        profile = normalise_profile(item)
        if profile is not None and profile.exe not in seen:
            seen.add(profile.exe)
            profiles.append(profile)
    return profiles[:MAX_PROFILES]


def profiles_to_settings(profiles: Iterable[GameProfile]) -> list[dict]:
    return [asdict(profile) for profile in profiles]


@dataclass
class ProfileActions:
    """The side effects, injected so tests never touch real hardware."""
    switch_monitor: Callable[[str], object]
    get_vibrance: Callable[[], int]
    set_vibrance: Callable[[int], object]
    get_audio: Callable[[], str]
    set_audio: Callable[[str], object]
    audio_present: Callable[[str], bool]


class GameProfileMonitor:
    def __init__(
        self,
        profiles: Callable[[], list[GameProfile]],
        settings: Callable[[], dict],
        running_names: Callable[[], Iterable[str]],
        actions: ProfileActions,
        state_path: Path,
        log: Callable[[str], None] | None = None,
        toast: Callable[[str], None] | None = None,
        poll_seconds: float = POLL_SECONDS,
        exit_confirmations: int = EXIT_CONFIRMATIONS,
    ) -> None:
        self._profiles = profiles
        self._settings = settings
        self._running_names = running_names
        self._actions = actions
        self._state_path = Path(state_path)
        self._log = log or (lambda _m: None)
        self._toast = toast or (lambda _t: None)
        self.poll_seconds = poll_seconds
        self.exit_confirmations = max(1, exit_confirmations)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._first = True
        self._active: GameProfile | None = None  # profile whose game is running
        self._applied: GameProfile | None = None  # profile whose settings are applied
        self._missing = 0

    # ---- state file (crash recovery) -----------------------------------
    def _write_state(self, state: dict | None) -> None:
        try:
            if state is None:
                self._state_path.unlink(missing_ok=True)
                return
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._state_path.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(json.dumps(state), encoding="utf-8")
            os.replace(temporary, self._state_path)
        except OSError as exc:
            self._log(f"profiles: state save failed: {type(exc).__name__}: {exc}")

    def _read_state(self) -> dict | None:
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
            return state if isinstance(state, dict) else None
        except (OSError, ValueError):
            return None

    # ---- public ----------------------------------------------------------
    @property
    def active(self) -> GameProfile | None:
        """The running game's profile (when profiles are enabled)."""
        with self._lock:
            return self._active if self._enabled() else None

    def _enabled(self) -> bool:
        return bool(self._settings().get("game_profiles_enabled", True))

    def _find(self, names: set[str]) -> GameProfile | None:
        for profile in self._profiles():
            if profile.exe in names:
                return profile
        return None

    def poll_once(self) -> None:
        try:
            names = {str(name).lower() for name in self._running_names()}
        except Exception as exc:  # a failed scan must never look like an exit
            self._log(f"profiles: process check failed: {type(exc).__name__}: {exc}")
            return
        found = self._find(names)
        if self._first:
            self._first = False
            with self._lock:
                self._active = found
            leftover = self._read_state()
            if found is None or (leftover and leftover.get("exe") != found.exe):
                self._restore_leftovers()
            if found is not None and self._enabled():
                # The app (re)started while the game runs, e.g. after an update:
                # apply now. A state file left for this game still holds the
                # values from before it started, so those are kept.
                self._apply(found, keep_state=bool(leftover) and leftover.get("exe") == found.exe)
            return
        with self._lock:
            current = self._active
        if found is not None and (current is None or found.exe != current.exe):
            self._missing = 0
            if self._applied is not None:
                self._revert(switch_monitor=False)
            with self._lock:
                self._active = found
            if self._enabled():
                self._apply(found)
            return
        if found is not None:
            self._missing = 0
            return
        if current is None:
            return
        self._missing += 1
        if self._missing < self.exit_confirmations:
            return
        self._missing = 0
        with self._lock:
            self._active = None
        if self._applied is not None:
            self._revert(switch_monitor=True)

    # ---- apply / revert ----------------------------------------------------
    def _safe(self, label: str, call: Callable[[], object]):
        try:
            return call()
        except Exception as exc:
            self._log(f"profiles: {label} failed: {type(exc).__name__}: {exc}")
            return None

    def _apply(self, profile: GameProfile, keep_state: bool = False) -> None:
        state: dict = (self._read_state() or {}) if keep_state else {}
        state["exe"] = profile.exe
        if profile.vibrance is not None and "vibrance" not in state:
            previous = self._safe("read vibrance", self._actions.get_vibrance)
            if previous is not None:
                state["vibrance"] = int(previous)
        if profile.audio_device and "audio_device" not in state:
            previous = self._safe("read audio", self._actions.get_audio)
            if previous:
                state["audio_device"] = previous
        self._write_state(state)  # before changing anything
        if profile.vibrance is not None and "vibrance" in state:
            self._safe("set vibrance", lambda: self._actions.set_vibrance(profile.vibrance))
        if profile.audio_device and self._actions.audio_present(profile.audio_device):
            self._safe("set audio", lambda: self._actions.set_audio(profile.audio_device))
        if profile.monitor_mode:
            if self._safe("monitor mode", lambda: self._actions.switch_monitor(profile.monitor_mode)) is not None:
                self._toast(profile.monitor_mode)
        self._applied = profile
        self._log(f"profiles: {profile.exe} started -> applied")

    def _restore(self, state: dict) -> None:
        if "vibrance" in state:
            self._safe("restore vibrance", lambda: self._actions.set_vibrance(int(state["vibrance"])))
        device = state.get("audio_device")
        if device and self._actions.audio_present(device):
            self._safe("restore audio", lambda: self._actions.set_audio(device))

    def _revert(self, switch_monitor: bool) -> None:
        profile, self._applied = self._applied, None
        state = self._read_state() or {}
        self._restore(state)
        desktop = str(self._settings().get("zowie_desktop_mode") or "").upper()
        if switch_monitor and profile is not None and profile.monitor_mode and desktop in MONITOR_MODES:
            if self._safe("monitor mode", lambda: self._actions.switch_monitor(desktop)) is not None:
                self._toast(desktop)
        self._write_state(None)
        self._log(f"profiles: {getattr(profile, 'exe', '?')} exited -> restored")

    def _restore_leftovers(self) -> None:
        """A previous run ended mid-game (crash, power loss): put things back."""
        state = self._read_state()
        if state:
            self._log("profiles: restoring settings left by an interrupted session")
            self._restore(state)
            self._write_state(None)

    def profile_edited(self, profile: GameProfile) -> bool:
        """Apply an edited profile at once if its game is running now.

        The state file keeps the values from *before* the game started, so
        the exit still restores those (not the previous profile values).
        """
        with self._lock:
            active = self._active
        if active is None or active.exe != profile.exe or self._applied is None or not self._enabled():
            return False
        state = self._read_state() or {"exe": profile.exe}
        if profile.vibrance is not None and "vibrance" not in state:
            previous = self._safe("read vibrance", self._actions.get_vibrance)
            if previous is not None:
                state["vibrance"] = int(previous)
        if profile.audio_device and "audio_device" not in state:
            previous = self._safe("read audio", self._actions.get_audio)
            if previous:
                state["audio_device"] = previous
        self._write_state(state)
        if profile.vibrance is not None:
            self._safe("set vibrance", lambda: self._actions.set_vibrance(profile.vibrance))
        elif "vibrance" in state:  # vibrance switched off for this game: back to the old value
            self._safe("restore vibrance", lambda: self._actions.set_vibrance(int(state["vibrance"])))
        if profile.audio_device and self._actions.audio_present(profile.audio_device):
            self._safe("set audio", lambda: self._actions.set_audio(profile.audio_device))
        with self._lock:
            self._active = profile
        self._applied = profile
        self._log(f"profiles: {profile.exe} edited while running -> re-applied")
        return True

    def restore_now(self) -> None:
        """Called on app exit: never leave game settings applied."""
        if self._applied is not None:
            self._revert(switch_monitor=False)

    # ---- thread ----------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="game-profiles", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self.poll_seconds)
