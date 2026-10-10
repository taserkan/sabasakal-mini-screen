"""Brand-independent Windows battery discovery for connected peripherals."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import secrets
import socket
import weakref
import sqlite3
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from app_log import log_exception

from winrt.windows.devices.enumeration import DeviceInformation, DeviceInformationKind


# DEVPKEY_Device_IsConnected: live link state of a device node.
DEVPKEY_DEVICE_IS_CONNECTED = "{83da6326-97a6-4088-9453-a1923f573b29} 15"
WINDOWS_CONNECTION_PROPERTIES = (
    "System.Devices.Aep.IsConnected",
    "System.Devices.Connected",
    DEVPKEY_DEVICE_IS_CONNECTED,
)
BATTERY_PROPERTIES = (
    "System.Devices.BatteryLife",
    "System.Devices.BatteryPlusCharging",
    "System.Devices.Category",
    "System.Devices.Aep.IsConnected",
    "System.Devices.Connected",
    DEVPKEY_DEVICE_IS_CONNECTED,
    "System.Devices.Aep.ContainerId",
    "System.Devices.ContainerId",
    "System.Devices.ModelName",
    "System.Devices.Manufacturer",
)
LGHUB_DB = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "LGHUB" / "settings.db"
LGHUB_DEVICE_NAMES = {
    "prox2wirelessmouse": ("PRO X SUPERLIGHT 2", "mouse"),
    "proxwirelessheadset": ("PRO X WIRELESS", "headset"),
}
LGHUB_HOST = "127.0.0.1"
LGHUB_PORT = 9010
LGHUB_CONNECTED_STATE = "ACTIVE"
LGHUB_EVENT_GRACE_SECONDS = 6.0


@dataclass(frozen=True)
class BatteryDevice:
    stable_id: str
    name: str
    percent: int
    kind: str = "other"
    charging: bool = False
    # Current sensor DPI reported by G HUB (mice only) and whether it does
    # not match the CS2 per-game profile while CS2 runs.
    dpi: int | None = None
    dpi_warning: bool = False

    @property
    def signature(self) -> tuple:
        return (
            self.stable_id, self.name, self.percent, self.kind, self.charging,
            self.dpi, self.dpi_warning,
        )


@dataclass(frozen=True)
class BatterySnapshot:
    devices: tuple[BatteryDevice, ...] = ()
    updated_at: float = 0.0
    error: str = ""

    @property
    def signature(self) -> tuple[tuple[str, str, int, str, bool], ...]:
        return tuple(device.signature for device in self.devices)


def classify_device(name: str, category: object = "", device_id: str = "") -> str:
    if isinstance(category, (list, tuple)):
        category = " ".join(str(item) for item in category)
    text = f"{name} {category} {device_id}".casefold()
    if any(token in text for token in ("mouse", "fare", "pointing")):
        return "mouse"
    if any(token in text for token in (
        "headset", "headphone", "kulaklık", "kulaklik", "earbud", "airpod",
    )):
        return "headset"
    if any(token in text for token in ("keyboard", "klavye")):
        return "keyboard"
    return "other"


def _clean_name(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def select_battery_devices(
    devices: list[BatteryDevice] | tuple[BatteryDevice, ...], limit: int = 3,
) -> tuple[BatteryDevice, ...]:
    priority = {"mouse": 0, "headset": 1, "keyboard": 2, "other": 3}
    best: dict[str, BatteryDevice] = {}
    for device in devices:
        key = device.stable_id.casefold() or device.name.casefold()
        previous = best.get(key)
        if previous is None or priority.get(device.kind, 3) < priority.get(previous.kind, 3):
            best[key] = device
    ordered = sorted(
        best.values(),
        key=lambda item: (priority.get(item.kind, 3), item.percent, item.name.casefold()),
    )
    return tuple(ordered[: max(0, limit)])


def _recv_exact(stream: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.recv(remaining)
        if not chunk:
            raise ConnectionError("G HUB closed the local connection")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _send_websocket_text(stream: socket.socket, text: str) -> None:
    _send_websocket_frame(stream, 0x1, text.encode("utf-8"))


WEBSOCKET_MAX_MESSAGE_BYTES = 4 * 1024 * 1024
LGHUB_MAX_PENDING_LISTS = 8


class _WebSocketReader:
    """Frame parser with a persistent buffer.

    A socket timeout in the middle of a frame used to discard the bytes that
    were already read, after which every following frame was parsed from the
    wrong offset. Bytes now stay buffered until a complete frame exists.
    """

    def __init__(self, stream: socket.socket) -> None:
        self.stream = stream
        self.buffer = bytearray()
        self.fragments = bytearray()

    def _fill(self) -> None:
        chunk = self.stream.recv(65536)
        if not chunk:
            raise ConnectionError("G HUB closed the local connection")
        self.buffer.extend(chunk)

    def _parse_frame(self) -> tuple[bool, int, bytes] | None:
        data = self.buffer
        if len(data) < 2:
            return None
        first, second = data[0], data[1]
        masked = bool(second & 0x80)
        length = second & 0x7F
        offset = 2
        if length == 126:
            if len(data) < 4:
                return None
            length = struct.unpack_from("!H", data, 2)[0]
            offset = 4
        elif length == 127:
            if len(data) < 10:
                return None
            length = struct.unpack_from("!Q", data, 2)[0]
            offset = 10
        if length > WEBSOCKET_MAX_MESSAGE_BYTES:
            raise ConnectionError(f"G HUB websocket frame too large ({length} bytes)")
        mask = b""
        if masked:
            if len(data) < offset + 4:
                return None
            mask = bytes(data[offset:offset + 4])
            offset += 4
        if len(data) < offset + length:
            return None
        payload = bytes(data[offset:offset + length])
        del data[:offset + length]
        if masked:
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        return bool(first & 0x80), first & 0x0F, payload

    def receive(self) -> str:
        while True:
            frame = self._parse_frame()
            if frame is None:
                self._fill()  # socket.timeout propagates; buffer is kept
                continue
            final, opcode, payload = frame
            if opcode == 0x8:
                raise ConnectionError("G HUB closed the local websocket")
            if opcode == 0x9:  # ping -> pong with the same payload
                _send_websocket_frame(self.stream, 0xA, payload[:125])
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2, 0x0):
                if len(self.fragments) + len(payload) > WEBSOCKET_MAX_MESSAGE_BYTES:
                    self.fragments.clear()
                    raise ConnectionError("G HUB websocket message too large")
                self.fragments.extend(payload)
                if final:
                    message = bytes(self.fragments)
                    self.fragments.clear()
                    return message.decode("utf-8")


_READERS: "weakref.WeakKeyDictionary[socket.socket, _WebSocketReader]" = weakref.WeakKeyDictionary()


def _receive_websocket_message(stream: socket.socket) -> str:
    reader = _READERS.get(stream)
    if reader is None:
        reader = _WebSocketReader(stream)
        _READERS[stream] = reader
    return reader.receive()


def _send_websocket_frame(stream: socket.socket, opcode: int, payload: bytes) -> None:
    mask = secrets.token_bytes(4)
    length = len(payload)
    if length < 126:
        header = struct.pack("!BB", 0x80 | opcode, 0x80 | length)
    elif length <= 0xFFFF:
        header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, length)
    else:
        header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, length)
    masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    stream.sendall(header + mask + masked)


def windows_device_connected(properties: dict) -> bool:
    states = [properties.get(key) for key in WINDOWS_CONNECTION_PROPERTIES]
    if any(state is False for state in states):
        return False
    return any(state is True for state in states)


def parse_lghub_connected_slugs(message: object) -> set[str]:
    return {
        slug for slug, info in parse_lghub_device_infos(message).items()
        if info["state"] == LGHUB_CONNECTED_STATE
    }


def parse_lghub_device_infos(message: object) -> dict[str, dict[str, str]]:
    """Normalize G HUB list replies and live device-state broadcasts by slot."""
    if not isinstance(message, dict):
        return {}
    payload = message.get("payload")
    if not isinstance(payload, dict):
        return {}
    infos = payload.get("deviceInfos")
    if not isinstance(infos, list):
        infos = [payload] if payload.get("slotPrefix") else []
    normalized: dict[str, dict[str, str]] = {}
    for info in infos:
        if not isinstance(info, dict):
            continue
        state = _clean_name(info.get("state")).upper()
        slug = _clean_name(info.get("slotPrefix"))
        if not slug or not state:
            continue
        name = _clean_name(
            info.get("givenName") or info.get("displayName")
            or info.get("extendedDisplayName") or slug
        )
        device_type = _clean_name(info.get("deviceType"))
        normalized[slug] = {
            "state": state,
            "name": name,
            "kind": classify_device(name, device_type, _clean_name(info.get("id"))),
            "device_id": _clean_name(info.get("id")),
        }
    return normalized


def _connect_lghub_websocket(
    host: str = LGHUB_HOST, port: int = LGHUB_PORT, timeout: float = 1.5,
) -> socket.socket:
    stream = socket.create_connection((host, port), timeout=timeout)
    try:
        stream.settimeout(timeout)
        websocket_key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        request = (
            "GET / HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {websocket_key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Sec-WebSocket-Protocol: json\r\n\r\n"
        )
        stream.sendall(request.encode("ascii"))
        response = bytearray()
        while b"\r\n\r\n" not in response:
            response.extend(_recv_exact(stream, 1))
            if len(response) > 16384:
                raise ConnectionError("Invalid G HUB websocket response")
        status = bytes(response).split(b"\r\n", 1)[0]
        if b" 101 " not in status:
            raise ConnectionError(status.decode("ascii", errors="replace"))
        return stream
    except Exception:
        stream.close()
        raise


# ---------------------------------------------------------------------------
# Mouse DPI (read-only G HUB queries; never changes a G HUB setting)
# ---------------------------------------------------------------------------

LGHUB_GAME_APP_MARKERS = ("counter-strike",)
DPI_REFRESH_SECONDS = 2.0
DPI_PROFILE_REFRESH_SECONDS = 10.0


@dataclass(frozen=True)
class MouseDpiReading:
    dpi: int | None = None
    game_running: bool = False
    game_profile_active: bool | None = None
    game_expected_dpi: int | None = None

    @property
    def warning(self) -> bool:
        if not self.game_running or self.dpi is None:
            return False
        if self.game_profile_active is False:
            return True
        return self.game_expected_dpi is not None and self.dpi != self.game_expected_dpi


def _lghub_get(stream: socket.socket, path: str, payload: dict | None = None,
               max_messages: int = 40) -> dict | None:
    """One read-only GET; returns the payload on SUCCESS, else None."""
    message_id = str(secrets.randbelow(2_000_000_000) + 1)
    request = {"msgId": message_id, "verb": "GET", "path": path}
    if payload is not None:
        request["payload"] = payload
    _send_websocket_text(stream, json.dumps(request, separators=(",", ":")))
    for _ in range(max_messages):
        message = json.loads(_receive_websocket_message(stream))
        if not isinstance(message, dict) or str(message.get("msgId", "")) != message_id:
            continue
        result = message.get("result")
        code = _clean_name(result.get("code") if isinstance(result, dict) else "").upper()
        body = message.get("payload")
        return body if code == "SUCCESS" and isinstance(body, dict) else None
    return None


def parse_lghub_dpi_state(payload: dict | None) -> int | None:
    try:
        dpi = int((payload or {}).get("dpi"))
    except (TypeError, ValueError):
        return None
    return dpi if 50 <= dpi <= 100_000 else None


def parse_lghub_game_profile(
    applications: dict | None, profiles: dict | None, active_profile_id: str,
) -> tuple[bool, bool | None, str]:
    """(game running, game profile active, game mouse-sensitivity card id)."""
    game_ids: set[str] = set()
    running = False
    for app in (applications or {}).get("applications", []) or []:
        if not isinstance(app, dict):
            continue
        haystack = f"{app.get('name', '')} {app.get('applicationPath', '')}".casefold()
        if any(marker in haystack for marker in LGHUB_GAME_APP_MARKERS):
            game_ids.add(str(app.get("applicationId") or app.get("databaseId") or ""))
            try:
                running = running or int(app.get("processId") or 0) > 0
            except (TypeError, ValueError):
                pass
    game_ids.discard("")
    if not game_ids:
        return running, None, ""
    active_is_game: bool | None = None
    card_id = ""
    for profile in (profiles or {}).get("profiles", []) or []:
        if not isinstance(profile, dict):
            continue
        in_game = str(profile.get("applicationId", "")) in game_ids
        if str(profile.get("id", "")) == active_profile_id:
            active_is_game = in_game
        if in_game and profile.get("activeForApplication") and not card_id:
            for assignment in profile.get("assignments", []) or []:
                if str(assignment.get("slotId", "")).endswith("_mouse_sensitivity"):
                    card_id = str(assignment.get("cardId", ""))
                    break
    if active_profile_id and active_is_game is None:
        active_is_game = False
    return running, active_is_game, card_id


def parse_lghub_expected_dpi(sensitivity_card: dict | None, preset_card: dict | None) -> int | None:
    try:
        index = int((sensitivity_card or {})["mouseSensitivity"]["activeDpiIndex"])
        entry = (preset_card or {})["mouseSettings"]["advancedDpiTable"]["entries"][str(index)]
        return int(entry["dpi"])
    except (KeyError, TypeError, ValueError):
        return None


def read_lghub_mouse_dpi(
    device_id: str, cached_profile: tuple | None = None, refresh_profile: bool = True,
    host: str = LGHUB_HOST, port: int = LGHUB_PORT, timeout: float = 0.75,
) -> tuple[MouseDpiReading, tuple | None]:
    """Read the live DPI and the CS2 per-game profile DPI (GET requests only)."""
    with _connect_lghub_websocket(host, port, timeout) as stream:
        stream.settimeout(timeout)
        dpi = parse_lghub_dpi_state(_lghub_get(stream, f"/mouse/{device_id}/dpi"))
        profile = cached_profile
        if refresh_profile or profile is None:
            active = _lghub_get(stream, "/profile/active") or {}
            running, active_is_game, card_id = parse_lghub_game_profile(
                _lghub_get(stream, "/applications"), _lghub_get(stream, "/profiles"),
                str(active.get("id", "")),
            )
            expected = None
            if card_id:
                card = _lghub_get(stream, "/card", {"id": card_id})
                preset_id = str(((card or {}).get("mouseSensitivity") or {}).get("presetId", ""))
                preset = _lghub_get(stream, "/card", {"id": preset_id}) if preset_id else None
                expected = parse_lghub_expected_dpi(card, preset)
            profile = (running, active_is_game, expected)
    running, active_is_game, expected = profile
    return MouseDpiReading(dpi, running, active_is_game, expected), profile


def scan_lghub_connected_slugs(
    host: str = LGHUB_HOST, port: int = LGHUB_PORT, timeout: float = 1.5,
) -> set[str]:
    """Ask G HUB's local read-only endpoint which devices are connected now."""
    with _connect_lghub_websocket(host, port, timeout) as stream:
        message_id = str(secrets.randbelow(2_000_000_000) + 1)
        _send_websocket_text(stream, json.dumps({
            "msgId": message_id, "verb": "GET", "path": "/devices/list",
        }, separators=(",", ":")))
        for _ in range(6):
            message = json.loads(_receive_websocket_message(stream))
            if isinstance(message, dict) and message.get("msgId") == message_id:
                return parse_lghub_connected_slugs(message)
        raise TimeoutError("G HUB did not return its connected device list")


def probe_lghub_input_batteries(
    device_infos: dict[str, dict[str, str]],
    host: str = LGHUB_HOST, port: int = LGHUB_PORT, timeout: float = 0.75,
) -> dict[str, tuple[bool, int | None]]:
    """Read G HUB's live battery endpoint for every connected peripheral.

    Some headsets only keep an old ``battery/.../warning`` row in settings.db;
    G HUB's own UI instead reads this endpoint. Using it for all device kinds
    keeps the mini display in sync while retaining the database as fallback.
    """
    candidates = {
        slug: info for slug, info in device_infos.items()
        if info.get("device_id")
    }
    if not candidates:
        return {}
    with _connect_lghub_websocket(host, port, timeout) as stream:
        stream.settimeout(timeout)
        pending: dict[str, str] = {}
        for slug, info in candidates.items():
            message_id = str(secrets.randbelow(2_000_000_000) + 1)
            pending[message_id] = slug
            _send_websocket_text(stream, json.dumps({
                "msgId": message_id, "verb": "GET",
                "path": f"/battery/{info['device_id']}/state",
            }, separators=(",", ":")))
        results: dict[str, tuple[bool, int | None]] = {}
        while pending:
            try:
                message = json.loads(_receive_websocket_message(stream))
            except socket.timeout:
                break
            if not isinstance(message, dict):
                continue
            message_id = str(message.get("msgId", ""))
            slug = pending.pop(message_id, None)
            if slug is None:
                continue
            result = message.get("result")
            code = _clean_name(result.get("code") if isinstance(result, dict) else "").upper()
            payload = message.get("payload")
            if code == "SUCCESS" and isinstance(payload, dict):
                try:
                    percent = int(payload.get("percentage"))
                except (TypeError, ValueError):
                    percent = None
                if percent is not None and not 0 <= percent <= 100:
                    percent = None
                results[slug] = (True, percent)
            elif code == "NO_SUCH_PATH":
                results[slug] = (False, None)
        return results


WINDOWS_SCAN_KEEP_FAILURES = 3
WINDOWS_SCAN_MAX_BACKOFF = 120.0
# Cache-backed kinds only; see BatteryDeviceMonitor._scan_windows.
WINDOWS_SCAN_KINDS = (
    DeviceInformationKind.DEVICE_CONTAINER,
    DeviceInformationKind.DEVICE,
)


class BatteryDeviceMonitor:
    """Poll Windows' standard battery property without blocking the screen loop."""

    def __init__(self, interval: float = 30.0, limit: int = 3) -> None:
        self.interval = max(2.0, float(interval))
        self.limit = max(1, int(limit))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lghub_thread: threading.Thread | None = None
        self._windows_thread: threading.Thread | None = None
        self._lghub_lock = threading.Lock()
        self._lghub_infos: dict[str, dict[str, str]] = {}
        self._lghub_event_until: dict[str, float] = {}
        self._lghub_ready = False
        self._last_logitech_devices: list[BatteryDevice] = []
        self._windows_devices: list[BatteryDevice] = []
        self._windows_error = ""
        self._refresh = threading.Event()
        self._snapshot = BatterySnapshot()
        self._dpi_profile: tuple | None = None
        self._dpi_profile_at = 0.0
        self._dpi_reading: tuple[str, MouseDpiReading] | None = None
        self._dpi_at = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._refresh.clear()
        self._lghub_thread = threading.Thread(
            target=self._listen_lghub, name="LogitechDevices", daemon=True,
        )
        self._lghub_thread.start()
        self._windows_thread = threading.Thread(
            target=self._scan_windows_loop, name="WindowsBatteryDevices", daemon=True,
        )
        self._windows_thread.start()
        self._thread = threading.Thread(target=self._run, name="BatteryDevices", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._refresh.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        if self._lghub_thread and self._lghub_thread is not threading.current_thread():
            self._lghub_thread.join(timeout=2.0)
        if self._windows_thread and self._windows_thread is not threading.current_thread():
            self._windows_thread.join(timeout=2.0)

    def snapshot(self) -> BatterySnapshot:
        with self._lock:
            return self._snapshot

    def _publish(self, snapshot: BatterySnapshot) -> None:
        with self._lock:
            self._snapshot = snapshot

    def _replace_lghub_infos(self, infos: dict[str, dict[str, str]]) -> None:
        with self._lghub_lock:
            if self._lghub_ready and self._lghub_infos and not infos:
                # G HUB can briefly return an empty list while rewriting state.
                # Real "all disconnected" lists still contain non-ACTIVE devices.
                return
            now = time.monotonic()
            merged = {slug: dict(info) for slug, info in infos.items()}
            for slug, deadline in tuple(self._lghub_event_until.items()):
                if deadline <= now:
                    self._lghub_event_until.pop(slug, None)
                    continue
                current = self._lghub_infos.get(slug)
                if current is not None:
                    # A live broadcast is newer than a periodically reconciled list.
                    merged[slug] = dict(current)
            changed = not self._lghub_ready or self._lghub_infos != merged
            self._lghub_infos = merged
            self._lghub_ready = True
        if changed:
            self._refresh.set()

    def _update_lghub_infos(self, infos: dict[str, dict[str, str]]) -> None:
        if not infos:
            return
        with self._lghub_lock:
            changed = False
            for slug, info in infos.items():
                if self._lghub_infos.get(slug) != info:
                    self._lghub_infos[slug] = info
                    changed = True
                self._lghub_event_until[slug] = (
                    time.monotonic() + LGHUB_EVENT_GRACE_SECONDS
                )
            self._lghub_ready = True
        if changed:
            self._refresh.set()

    def _current_lghub_infos(self) -> tuple[bool, dict[str, dict[str, str]]]:
        with self._lghub_lock:
            return self._lghub_ready, {
                slug: dict(info) for slug, info in self._lghub_infos.items()
            }

    def _with_mouse_dpi(self, devices: list[BatteryDevice]) -> list[BatteryDevice]:
        """Attach the live DPI to the connected G HUB mouse (read-only)."""
        mouse = next((device for device in devices if device.kind == "mouse"), None)
        if mouse is None:
            self._dpi_reading = None
            return devices
        _ready, infos = self._current_lghub_infos()
        info = infos.get(mouse.stable_id.removeprefix("lghub:"), {})
        device_id = info.get("device_id", "")
        if not device_id:
            return devices
        now = time.monotonic()
        if self._dpi_reading is None or self._dpi_reading[0] != device_id or now - self._dpi_at >= DPI_REFRESH_SECONDS:
            refresh_profile = now - self._dpi_profile_at >= DPI_PROFILE_REFRESH_SECONDS
            try:
                reading, profile = read_lghub_mouse_dpi(
                    device_id, self._dpi_profile, refresh_profile,
                )
                self._dpi_profile = profile
                if refresh_profile:
                    self._dpi_profile_at = now
                self._dpi_reading = (device_id, reading)
            except (OSError, ConnectionError, TimeoutError, json.JSONDecodeError, ValueError):
                self._dpi_reading = None
            self._dpi_at = now
        if self._dpi_reading is None or self._dpi_reading[1].dpi is None:
            return devices
        reading = self._dpi_reading[1]
        return [
            BatteryDevice(
                device.stable_id, device.name, device.percent, device.kind, device.charging,
                reading.dpi, reading.warning,
            ) if device is mouse else device
            for device in devices
        ]

    def _current_windows_devices(self) -> tuple[list[BatteryDevice], str]:
        with self._lock:
            return list(self._windows_devices), self._windows_error

    def _stabilize_lghub_devices(
        self, scanned: list[BatteryDevice], connected_slugs: set[str],
    ) -> list[BatteryDevice]:
        if connected_slugs and not scanned:
            stable = [
                device for device in self._last_logitech_devices
                if device.stable_id.removeprefix("lghub:") in connected_slugs
            ]
        else:
            stable = list(scanned)
        self._last_logitech_devices = list(stable)
        return stable

    def _without_lghub_disconnected(
        self, devices: list[BatteryDevice],
    ) -> list[BatteryDevice]:
        """Never let a Windows cache entry (fresh or kept across failed scans)
        resurrect a peripheral G HUB positively reports as disconnected."""
        ready, infos = self._current_lghub_infos()
        if not ready:
            return devices
        offline = {
            info.get("name", "").casefold() for info in infos.values()
            if info.get("state") != LGHUB_CONNECTED_STATE and info.get("name")
        }
        online = {
            info.get("name", "").casefold() for info in infos.values()
            if info.get("state") == LGHUB_CONNECTED_STATE
        }
        offline -= online
        return [device for device in devices if device.name.casefold() not in offline]

    def _scan_windows_loop(self) -> None:
        """Isolate slow WinRT enumeration so it can never block G HUB events."""
        failures = 0
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                devices = asyncio.run(self._scan_windows())
                error = ""
                failures = 0
            except Exception as exc:
                # WinRT/BLE enumeration is occasionally cancelled (E_ABORT,
                # "operation aborted").  Keep the devices that were already
                # known for a few attempts instead of blanking the screen.
                failures += 1
                with self._lock:
                    known = list(self._windows_devices)
                devices = known if failures <= WINDOWS_SCAN_KEEP_FAILURES else []
                error = f"{type(exc).__name__}: {exc}"
            with self._lock:
                self._windows_devices = devices
                self._windows_error = error
            self._refresh.set()
            elapsed = time.monotonic() - started
            delay = self.interval * (2 ** min(failures, 4)) if failures else self.interval
            self._stop.wait(max(0.5, min(WINDOWS_SCAN_MAX_BACKOFF, delay) - elapsed))

    def _listen_lghub(self) -> None:
        """Keep G HUB's model-independent connection state live via broadcasts."""
        failures = 0
        while not self._stop.is_set():
            try:
                with _connect_lghub_websocket(timeout=1.0) as stream:
                    stream.settimeout(0.5)
                    pending_lists: set[str] = set()
                    next_reconcile = 0.0
                    _send_websocket_text(stream, json.dumps({
                        "msgId": "device-state", "verb": "SUBSCRIBE",
                        "path": "/devices/state/changed",
                    }, separators=(",", ":")))
                    while not self._stop.is_set():
                        now = time.monotonic()
                        if now >= next_reconcile:
                            list_id = str(secrets.randbelow(2_000_000_000) + 1)
                            if len(pending_lists) >= LGHUB_MAX_PENDING_LISTS:
                                # Replies that never arrive must not grow the set forever.
                                pending_lists.clear()
                            pending_lists.add(list_id)
                            _send_websocket_text(stream, json.dumps({
                                "msgId": list_id, "verb": "GET",
                                "path": "/devices/list",
                            }, separators=(",", ":")))
                            next_reconcile = now + 2.0
                        try:
                            message = json.loads(_receive_websocket_message(stream))
                        except socket.timeout:
                            continue
                        if not isinstance(message, dict):
                            continue
                        failures = 0
                        infos = parse_lghub_device_infos(message)
                        if message.get("msgId") in pending_lists:
                            pending_lists.discard(str(message.get("msgId")))
                            self._replace_lghub_infos(infos)
                        elif message.get("path") == "/devices/state/changed":
                            self._update_lghub_infos(infos)
            except Exception as exc:
                # G HUB not running is the normal case (ConnectionRefused);
                # anything else is logged. Back off up to 30 s.
                if not isinstance(exc, ConnectionRefusedError):
                    log_exception("battery", "G HUB websocket failed", exc)
                failures += 1
                with self._lghub_lock:
                    was_ready = self._lghub_ready
                    self._lghub_ready = False
                    self._lghub_infos = {}
                    self._lghub_event_until = {}
                if was_ready:
                    self._refresh.set()
                self._stop.wait(min(30.0, 1.0 * (2 ** min(failures - 1, 5))))

    @staticmethod
    async def _scan_windows(find=None, kinds=None) -> list[BatteryDevice]:
        """Read battery levels from the PnP cache (containers + device nodes).

        AssociationEndpoint enumeration is deliberately not used: with any AQS
        it starts an active Bluetooth/WSD/UPnP inquiry that takes 30-60 s and
        Windows aborts it from time to time (E_ABORT 0x80004004, "operation
        aborted"), which v1.1.3 logged every few minutes. Paired Bluetooth
        devices that report a battery expose the same System.Devices.BatteryLife
        on their device container, which is served from cache instantly.
        One failing kind no longer discards the others; only a scan where
        every kind fails raises.
        """
        if find is None:
            find = DeviceInformation.find_all_async_with_kind_aqs_filter_and_additional_properties
        if kinds is None:
            kinds = WINDOWS_SCAN_KINDS
        found: list[BatteryDevice] = []
        errors: list[Exception] = []
        for kind in kinds:
            try:
                devices = await find("", BATTERY_PROPERTIES, kind)
            except OSError as exc:
                errors.append(exc)
                continue
            for device in devices:
                properties = dict(device.properties)
                raw_percent = properties.get("System.Devices.BatteryLife")
                try:
                    percent = int(raw_percent)
                except (TypeError, ValueError):
                    continue
                if not 0 <= percent <= 100:
                    continue
                # The PnP cache keeps the last BatteryLife of a powered-off
                # peripheral (its dongle stays present). Only a positive live
                # connection state proves the value is current.
                if not device.is_enabled or not windows_device_connected(properties):
                    continue
                name = _clean_name(
                    device.name
                    or properties.get("System.Devices.ModelName")
                    or "Kablosuz cihaz"
                )
                container = (
                    properties.get("System.Devices.Aep.ContainerId")
                    or properties.get("System.Devices.ContainerId")
                )
                stable_id = _clean_name(container) or _clean_name(device.id)
                found.append(BatteryDevice(
                    stable_id=stable_id,
                    name=name,
                    percent=percent,
                    kind=classify_device(
                        name, properties.get("System.Devices.Category"), device.id,
                    ),
                    charging=bool(properties.get("System.Devices.BatteryPlusCharging", False)),
                ))
        if errors and len(errors) == len(kinds):
            raise errors[0]
        return found

    @staticmethod
    def _scan_lghub(
        path: Path = LGHUB_DB, connected_slugs: set[str] | None = None,
        device_infos: dict[str, dict[str, str]] | None = None,
    ) -> list[BatteryDevice]:
        if not path.exists():
            return []
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
        try:
            row = connection.execute(
                "select file from data order by _id desc limit 1"
            ).fetchone()
        finally:
            connection.close()
        if not row:
            return []
        payload = row[0]
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        data = json.loads(payload)
        if not isinstance(data, dict):
            return []

        records: dict[str, tuple[int, int, bool]] = {}
        for key, value in data.items():
            match = re.fullmatch(r"battery/([^/]+)/(percentage|warning)", str(key))
            if not match or not isinstance(value, dict):
                continue
            try:
                percent = int(value.get("percentage"))
            except (TypeError, ValueError):
                continue
            if not 0 <= percent <= 100:
                continue
            slug, record_type = match.groups()
            # The dedicated percentage record is the live value shown by G HUB.
            rank = 0 if record_type == "percentage" else 1
            charging = bool(value.get("charging", False))
            previous = records.get(slug)
            if previous is None or rank < previous[1]:
                records[slug] = (percent, rank, charging)

        devices: list[BatteryDevice] = []
        for slug, (percent, _rank, charging) in records.items():
            if connected_slugs is not None and slug not in connected_slugs:
                continue
            info = (device_infos or {}).get(slug, {})
            mapped_name, mapped_kind = LGHUB_DEVICE_NAMES.get(
                slug, (_clean_name(info.get("name") or slug), _clean_name(info.get("kind"))),
            )
            if not mapped_kind:
                mapped_kind = classify_device(mapped_name, device_id=slug)
            devices.append(BatteryDevice(
                stable_id=f"lghub:{slug}",
                name=mapped_name,
                percent=percent,
                kind=mapped_kind,
                charging=charging,
            ))
        return devices

    @classmethod
    async def _scan(cls) -> list[BatteryDevice]:
        windows_devices = await cls._scan_windows()
        try:
            connected_slugs = scan_lghub_connected_slugs()
            logitech_devices = cls._scan_lghub(connected_slugs=connected_slugs)
        except (
            OSError, sqlite3.Error, UnicodeError, json.JSONDecodeError,
            ConnectionError, TimeoutError,
        ):
            logitech_devices = []
        return [*logitech_devices, *windows_devices]

    def _run(self) -> None:
        while not self._stop.is_set():
            self._refresh.clear()
            started = time.monotonic()
            logitech_devices: list[BatteryDevice] = []
            logitech_error = ""
            try:
                ready, device_infos = self._current_lghub_infos()
                live_percentages: dict[str, int] = {}
                if ready and device_infos:
                    # G HUB's battery endpoint keeps answering SUCCESS with the
                    # last percentage of a powered-off headset whose dongle is
                    # still plugged in. Presence therefore comes only from the
                    # device list/state events; the probe may only downgrade.
                    active_infos = {
                        slug: info for slug, info in device_infos.items()
                        if info.get("state") == LGHUB_CONNECTED_STATE
                    }
                    live_states = probe_lghub_input_batteries(active_infos)
                    state_updates: dict[str, dict[str, str]] = {}
                    for slug, (connected, percent) in live_states.items():
                        if not connected:
                            info = dict(device_infos[slug])
                            info["state"] = "NOT_CONNECTED"
                            device_infos[slug] = info
                            state_updates[slug] = info
                        elif percent is not None:
                            live_percentages[slug] = percent
                    self._update_lghub_infos(state_updates)
                connected_slugs = (
                    {
                        slug for slug, info in device_infos.items()
                        if info.get("state") == LGHUB_CONNECTED_STATE
                    }
                    if ready else scan_lghub_connected_slugs()
                )
                scanned_devices = self._scan_lghub(
                    connected_slugs=connected_slugs, device_infos=device_infos,
                )
                # Keep the last valid percentages during a transient SQLite
                # rewrite, but only for devices G HUB still marks ACTIVE.
                logitech_devices = self._stabilize_lghub_devices(
                    scanned_devices, connected_slugs,
                )
                if live_percentages:
                    logitech_devices = [
                        BatteryDevice(
                            device.stable_id, device.name,
                            live_percentages.get(
                                device.stable_id.removeprefix("lghub:"), device.percent,
                            ),
                            device.kind, device.charging,
                        )
                        for device in logitech_devices
                    ]
                    self._last_logitech_devices = list(logitech_devices)
            except (
                OSError, sqlite3.Error, UnicodeError, json.JSONDecodeError,
                ConnectionError, TimeoutError,
            ) as exc:
                logitech_error = f"{type(exc).__name__}: {exc}"
                ready, device_infos = self._current_lghub_infos()
                if ready:
                    connected_slugs = {
                        slug for slug, info in device_infos.items()
                        if info.get("state") == LGHUB_CONNECTED_STATE
                    }
                    logitech_devices = [
                        device for device in self._last_logitech_devices
                        if device.stable_id.removeprefix("lghub:") in connected_slugs
                    ]
            logitech_devices = self._with_mouse_dpi(logitech_devices)
            windows_devices, windows_error = self._current_windows_devices()
            windows_devices = self._without_lghub_disconnected(windows_devices)
            self._publish(BatterySnapshot(
                devices=select_battery_devices(
                    [*logitech_devices, *windows_devices], self.limit,
                ),
                updated_at=time.monotonic(),
                error=logitech_error or windows_error,
            ))
            elapsed = time.monotonic() - started
            self._refresh.wait(max(0.1, self.interval - elapsed))
