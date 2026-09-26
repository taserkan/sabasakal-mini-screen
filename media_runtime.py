"""Low-latency Windows media metadata and WASAPI loopback snapshots."""

from __future__ import annotations

import asyncio
import threading
import time
import unicodedata
from dataclasses import dataclass

import numpy as np
import pyaudiowpatch as pyaudio
from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)


MEDIA_BAND_COUNT = 16
MEDIA_METADATA_STABILITY_SECONDS = 0.65


def sanitise_media_text(value: object) -> str:
    """Remove glyphs the mini-screen font cannot render without harming text."""
    cleaned: list[str] = []
    for character in unicodedata.normalize("NFC", str(value or "")):
        codepoint = ord(character)
        category = unicodedata.category(character)
        unsupported_symbol = (
            0x1F000 <= codepoint <= 0x1FAFF
            or 0x2600 <= codepoint <= 0x27BF
            or 0x1F3FB <= codepoint <= 0x1F3FF
            or character in {"\u200d", "\ufe0e", "\ufe0f", "\u20e3"}
            or category in {"Cs", "Co"}
        )
        if not unsupported_symbol and category != "Cf":
            cleaned.append(character)
    return " ".join("".join(cleaned).split())


@dataclass(frozen=True)
class MediaSnapshot:
    platform: str = ""
    title: str = ""
    artist: str = ""
    status: str = "closed"
    source_id: str = ""
    updated_at: float = 0.0
    error: str = ""

    @property
    def visible(self) -> bool:
        return bool(self.title) and self.status in {"playing", "paused"}

    @property
    def playing(self) -> bool:
        return self.status == "playing"

    @property
    def signature(self) -> tuple[str, str, str, str]:
        return self.platform, self.title, self.artist, self.status


@dataclass(frozen=True)
class AudioSpectrumSnapshot:
    bars: tuple[float, ...] = (0.0,) * MEDIA_BAND_COUNT
    level: float = 0.0
    active: bool = False
    input_latency_ms: float = 0.0
    updated_at: float = 0.0
    error: str = ""


def platform_name(source_id: str) -> str:
    value = source_id.casefold()
    mappings = (
        ("spotify", "Spotify"),
        ("youtube", "YouTube"),
        ("chrome", "Chrome"),
        ("msedge", "Microsoft Edge"),
        ("firefox", "Firefox"),
        ("brave", "Brave"),
        ("vlc", "VLC"),
        ("tidal", "TIDAL"),
        ("applemusic", "Apple Music"),
        ("zune", "Medya Oynatıcı"),
    )
    for marker, label in mappings:
        if marker in value:
            return label
    stem = source_id.rsplit("!", 1)[-1].rsplit("\\", 1)[-1]
    if stem.casefold().endswith(".exe"):
        stem = stem[:-4]
    return stem.strip() or "Medya"


def media_service_name(media: MediaSnapshot) -> str:
    """Prefer the listening service over a generic browser process name."""
    combined = f"{media.title} {media.artist} {media.source_id}".casefold()
    services = (
        (("youtube music", "music.youtube"), "YouTube Music"),
        (("watch live on kick", " kick ", "kick.com"), "Kick"),
        (("watch live on twitch", " twitch ", "twitch.tv"), "Twitch"),
        (("youtube", "youtu.be"), "YouTube"),
        (("spotify",), "Spotify"),
    )
    for markers, label in services:
        if any(marker in combined for marker in markers):
            return label
    if media.platform in {"Chrome", "Microsoft Edge", "Firefox", "Brave"}:
        return "Medya"
    return media.platform or "Medya"


class AudioSpectrumMonitor:
    """Capture the default output without queuing stale audio blocks."""

    def __init__(self, band_count: int = MEDIA_BAND_COUNT) -> None:
        self.band_count = band_count
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._latest_pcm = b""
        self._sequence = 0
        self._last_signal_at = 0.0
        self._snapshot = AudioSpectrumSnapshot(bars=(0.0,) * band_count)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="AudioSpectrum", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)

    def snapshot(self) -> AudioSpectrumSnapshot:
        with self._lock:
            return self._snapshot

    def _publish(
        self,
        bars: np.ndarray,
        level: float,
        latency_ms: float,
        error: str = "",
    ) -> None:
        now = time.monotonic()
        if level > 0.018 and not error:
            self._last_signal_at = now
        with self._lock:
            self._snapshot = AudioSpectrumSnapshot(
                bars=tuple(float(max(0.0, min(1.0, item))) for item in bars),
                level=float(max(0.0, min(1.0, level))),
                active=(now - self._last_signal_at) < 1.0 and not error,
                input_latency_ms=latency_ms,
                updated_at=now,
                error=error,
            )

    def _analyze(self, pcm: bytes, channels: int, rate: int) -> tuple[np.ndarray, float]:
        raw = np.frombuffer(pcm, dtype=np.int16)
        if raw.size < channels:
            return np.zeros(self.band_count, dtype=np.float32), 0.0
        usable = raw[: raw.size - (raw.size % channels)]
        mono = usable.reshape(-1, channels).astype(np.float32).mean(axis=1) / 32768.0
        rms = float(np.sqrt(np.mean(mono * mono))) if mono.size else 0.0
        if mono.size < 32 or rms < 0.0001:
            return np.zeros(self.band_count, dtype=np.float32), 0.0
        spectrum = np.abs(np.fft.rfft(mono * np.hanning(mono.size)))
        frequencies = np.fft.rfftfreq(mono.size, 1.0 / rate)
        upper = max(120.0, min(16000.0, rate / 2.0))
        edges = np.geomspace(55.0, upper, self.band_count + 1)
        bands = np.zeros(self.band_count, dtype=np.float32)
        for index in range(self.band_count):
            mask = (frequencies >= edges[index]) & (frequencies < edges[index + 1])
            if np.any(mask):
                bands[index] = float(np.mean(spectrum[mask]))
            else:
                # A 512-frame live block has ~94 Hz FFT spacing at 48 kHz.
                # Interpolating empty low-frequency bands keeps bars 1 and 3
                # responsive without adding latency by buffering more audio.
                center = float(np.sqrt(edges[index] * edges[index + 1]))
                bands[index] = float(np.interp(center, frequencies, spectrum))
        peak = float(np.max(bands)) if bands.size else 0.0
        if peak > 0.0:
            bands = np.power(bands / peak, 0.62)
        amplitude = min(1.0, max(0.0, (rms - 0.0015) * 18.0))
        return bands * amplitude, amplitude

    def _run(self) -> None:
        smoothed = np.zeros(self.band_count, dtype=np.float32)
        while not self._stop.is_set():
            stream = None
            try:
                with pyaudio.PyAudio() as audio:
                    device = audio.get_default_wasapi_loopback()
                    rate = int(device["defaultSampleRate"])
                    channels = max(1, int(device["maxInputChannels"]))

                    def callback(in_data, _frame_count, _time_info, _status_flags):
                        with self._lock:
                            self._latest_pcm = in_data
                            self._sequence += 1
                        return None, pyaudio.paContinue

                    stream = audio.open(
                        format=pyaudio.paInt16,
                        channels=channels,
                        rate=rate,
                        input=True,
                        input_device_index=int(device["index"]),
                        frames_per_buffer=512,
                        stream_callback=callback,
                    )
                    latency_ms = float(stream.get_input_latency()) * 1000.0
                    seen_sequence = -1
                    while stream.is_active() and not self._stop.wait(0.012):
                        with self._lock:
                            sequence = self._sequence
                            pcm = self._latest_pcm
                        if sequence == seen_sequence or not pcm:
                            target = np.zeros(self.band_count, dtype=np.float32)
                            level = 0.0
                        else:
                            seen_sequence = sequence
                            target, level = self._analyze(pcm, channels, rate)
                        attack = target > smoothed
                        smoothed = np.where(
                            attack,
                            (smoothed * 0.22) + (target * 0.78),
                            (smoothed * 0.82) + (target * 0.18),
                        )
                        self._publish(smoothed, level, latency_ms)
            except Exception as exc:
                smoothed *= 0.65
                self._publish(smoothed, 0.0, 0.0, f"{type(exc).__name__}: {exc}")
                self._stop.wait(0.75)
            finally:
                if stream is not None:
                    try:
                        stream.stop_stream()
                        stream.close()
                    except Exception:
                        pass


class MediaSessionMonitor:
    """Observe GSMTC metadata without controlling playback."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._snapshot = MediaSnapshot()
        self._candidate_signature: tuple[str, str, str, str] | None = None
        self._candidate_since = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="MediaSession", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)

    def snapshot(self) -> MediaSnapshot:
        with self._lock:
            return self._snapshot

    def _publish(self, snapshot: MediaSnapshot) -> None:
        with self._lock:
            current = self._snapshot
            metadata_changed = (
                snapshot.visible and current.visible
                and snapshot.source_id == current.source_id
                and snapshot.status == current.status
                and (snapshot.title, snapshot.artist) != (current.title, current.artist)
            )
            if metadata_changed:
                candidate = (
                    snapshot.source_id, snapshot.status,
                    snapshot.title, snapshot.artist,
                )
                now = time.monotonic()
                if candidate != self._candidate_signature:
                    self._candidate_signature = candidate
                    self._candidate_since = now
                    return
                if now - self._candidate_since < MEDIA_METADATA_STABILITY_SECONDS:
                    return
            self._candidate_signature = None
            self._candidate_since = 0.0
            self._snapshot = snapshot

    @staticmethod
    def _status_name(value: object) -> str:
        return {
            0: "closed", 1: "opened", 2: "changing",
            3: "stopped", 4: "playing", 5: "paused",
        }.get(int(value), "unknown")

    async def _observe(self) -> None:
        manager = await SessionManager.request_async()
        loop = asyncio.get_running_loop()
        refresh = asyncio.Event()

        def request_refresh(*_args) -> None:
            loop.call_soon_threadsafe(refresh.set)

        manager_tokens = (
            (manager.remove_sessions_changed, manager.add_sessions_changed(request_refresh)),
            (manager.remove_current_session_changed, manager.add_current_session_changed(request_refresh)),
        )
        active_session = None
        session_tokens: list[tuple[object, object]] = []
        try:
            while not self._stop.is_set():
                sessions = list(manager.get_sessions())
                current = manager.get_current_session()
                playing = [
                    session for session in sessions
                    if session.get_playback_info().playback_status == PlaybackStatus.PLAYING
                ]
                if playing and current in playing:
                    session = current
                else:
                    session = playing[0] if playing else current

                if session is not active_session:
                    for remover, token in session_tokens:
                        try:
                            remover(token)
                        except Exception:
                            pass
                    session_tokens.clear()
                    active_session = session
                    if session is not None:
                        session_tokens.extend((
                            (session.remove_media_properties_changed,
                             session.add_media_properties_changed(request_refresh)),
                            (session.remove_playback_info_changed,
                             session.add_playback_info_changed(request_refresh)),
                        ))

                if session is None:
                    self._publish(MediaSnapshot(updated_at=time.monotonic()))
                else:
                    properties = await session.try_get_media_properties_async()
                    source_id = str(session.source_app_user_model_id or "")
                    status = self._status_name(session.get_playback_info().playback_status)
                    self._publish(MediaSnapshot(
                        platform=platform_name(source_id),
                        title=sanitise_media_text(properties.title) if properties else "",
                        artist=sanitise_media_text(properties.artist) if properties else "",
                        status=status,
                        source_id=source_id,
                        updated_at=time.monotonic(),
                    ))

                refresh.clear()
                try:
                    await asyncio.wait_for(refresh.wait(), timeout=0.75)
                except asyncio.TimeoutError:
                    pass
        finally:
            for remover, token in session_tokens:
                try:
                    remover(token)
                except Exception:
                    pass
            for remover, token in manager_tokens:
                try:
                    remover(token)
                except Exception:
                    pass

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                asyncio.run(self._observe())
            except Exception as exc:
                self._publish(MediaSnapshot(
                    updated_at=time.monotonic(), error=f"{type(exc).__name__}: {exc}",
                ))
                self._stop.wait(2.0)
