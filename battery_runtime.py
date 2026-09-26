"""Brand-independent Windows battery discovery for connected peripherals."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import secrets
import socket
import sqlite3
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from winrt.windows.devices.enumeration import DeviceInformation, DeviceInformationKind


BATTERY_PROPERTIES = (
    "System.Devices.BatteryLife",
    "System.Devices.BatteryPlusCharging",
    "System.Devices.Category",
    "System.Devices.Aep.IsConnected",
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

    @property
    def signature(self) -> tuple[str, str, int, str, bool]:
        return self.stable_id, self.name, self.percent, self.kind, self.charging


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
    payload = text.encode("utf-8")
    mask = secrets.token_bytes(4)
    length = len(payload)
    if length < 126:
        header = struct.pack("!BB", 0x81, 0x80 | length)
    elif length <= 0xFFFF:
        header = struct.pack("!BBH", 0x81, 0x80 | 126, length)
    else:
        header = struct.pack("!BBQ", 0x81, 0x80 | 127, length)
    masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    stream.sendall(header + mask + masked)


def _receive_websocket_message(stream: socket.socket) -> str:
    message = bytearray()
    while True:
        first, second = _recv_exact(stream, 2)
        final = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", _recv_exact(stream, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", _recv_exact(stream, 8))[0]
        mask = _recv_exact(stream, 4) if masked else b""
        payload = _recv_exact(stream, length)
        if masked:
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        if opcode == 0x8:
            raise ConnectionError("G HUB closed the local websocket")
        if opcode in (0x1, 0x0):
            message.extend(payload)
            if final:
                return message.decode("utf-8")


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
            if message.get("msgId") == message_id:
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

    def _scan_windows_loop(self) -> None:
        """Isolate slow WinRT enumeration so it can never block G HUB events."""
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                devices = asyncio.run(self._scan_windows())
                error = ""
            except Exception as exc:
                devices = []
                error = f"{type(exc).__name__}: {exc}"
            with self._lock:
                self._windows_devices = devices
                self._windows_error = error
            self._refresh.set()
            elapsed = time.monotonic() - started
            self._stop.wait(max(0.5, self.interval - elapsed))

    def _listen_lghub(self) -> None:
        """Keep G HUB's model-independent connection state live via broadcasts."""
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
                        infos = parse_lghub_device_infos(message)
                        if message.get("msgId") in pending_lists:
                            pending_lists.discard(str(message.get("msgId")))
                            self._replace_lghub_infos(infos)
                        elif message.get("path") == "/devices/state/changed":
                            self._update_lghub_infos(infos)
            except (
                OSError, UnicodeError, json.JSONDecodeError,
                ConnectionError, TimeoutError,
            ):
                with self._lghub_lock:
                    was_ready = self._lghub_ready
                    self._lghub_ready = False
                    self._lghub_infos = {}
                    self._lghub_event_until = {}
                if was_ready:
                    self._refresh.set()
                self._stop.wait(1.0)

    @staticmethod
    async def _scan_windows() -> list[BatteryDevice]:
        found: list[BatteryDevice] = []
        kinds = (
            DeviceInformationKind.ASSOCIATION_ENDPOINT,
            DeviceInformationKind.DEVICE_CONTAINER,
            DeviceInformationKind.DEVICE,
        )
        for kind in kinds:
            devices = await DeviceInformation.find_all_async_with_kind_aqs_filter_and_additional_properties(
                "", BATTERY_PROPERTIES, kind,
            )
            for device in devices:
                properties = dict(device.properties)
                raw_percent = properties.get("System.Devices.BatteryLife")
                try:
                    percent = int(raw_percent)
                except (TypeError, ValueError):
                    continue
                if not 0 <= percent <= 100:
                    continue
                connected = properties.get("System.Devices.Aep.IsConnected")
                if connected is False or not device.is_enabled:
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
                    live_states = probe_lghub_input_batteries(device_infos)
                    state_updates: dict[str, dict[str, str]] = {}
                    for slug, (connected, percent) in live_states.items():
                        info = dict(device_infos[slug])
                        info["state"] = LGHUB_CONNECTED_STATE if connected else "NOT_CONNECTED"
                        device_infos[slug] = info
                        state_updates[slug] = info
                        if connected and percent is not None:
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
            windows_devices, windows_error = self._current_windows_devices()
            self._publish(BatterySnapshot(
                devices=select_battery_devices(
                    [*logitech_devices, *windows_devices], self.limit,
                ),
                updated_at=time.monotonic(),
                error=logitech_error or windows_error,
            ))
            elapsed = time.monotonic() - started
            self._refresh.wait(max(0.1, self.interval - elapsed))
