from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image, ImageChops, ImageColor, ImageDraw
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

import gui
import app as screen_app
from battery_runtime import (
    BatteryDevice,
    BatteryDeviceMonitor,
    classify_device,
    parse_lghub_connected_slugs,
    parse_lghub_device_infos,
    probe_lghub_input_batteries,
    select_battery_devices,
)
from media_runtime import (
    AudioSpectrumMonitor,
    AudioSpectrumSnapshot,
    MediaSessionMonitor,
    MediaSnapshot,
    media_service_name,
    platform_name,
    sanitise_media_text,
)
from msfs_runtime import FlightTelemetry
from weather_runtime import (
    DEFAULT_WEATHER_CITIES, WeatherReading, WeatherSnapshot,
    normalise_city_names, resolve_city,
)


def default_settings() -> dict:
    return json.loads(json.dumps(gui.DEFAULT_SETTINGS))


class AntiCheatSafetyScenarios(unittest.TestCase):
    """Keep the CS2/FACEIT integration inside public, non-invasive APIs."""

    def test_runtime_has_no_game_memory_injection_or_input_automation_apis(self) -> None:
        source = "\n".join(
            (Path(__file__).parent / name).read_text(encoding="utf-8").lower()
            for name in ("app.py", "gui.py")
        )
        forbidden = (
            "readprocessmemory", "writeprocessmemory", "createremotethread",
            "virtualallocex", "setwindowshookex", "sendinput", "keybd_event",
            "mouse_event", "import pymem", "import pyautogui", "import scapy",
            "winpcap", "npcap",
        )
        for api in forbidden:
            self.assertNotIn(api, source, api)


class SettingsScenarios(unittest.TestCase):
    def load_from(self, payload: str | None) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "settings.json"
            if payload is not None:
                path.write_text(payload, encoding="utf-8")
            with patch.object(gui, "APP_DATA", root), patch.object(gui, "SETTINGS_PATH", path):
                return gui.load_settings()

    def test_missing_settings_uses_defaults(self) -> None:
        settings = self.load_from(None)
        self.assertEqual(settings["palette"], "Kuzey")
        self.assertEqual(settings["interval"], 0.5)
        self.assertEqual(settings["msfs_layout"], "cockpit")
        self.assertEqual(settings["hardware_labels"], {"cpu": "", "gpu": "", "ram": ""})
        self.assertEqual(tuple(settings["weather_cities"]), DEFAULT_WEATHER_CITIES)
        self.assertIs(settings["auto_brightness"], False)
        self.assertEqual(settings["language"], "tr")

    def test_language_choice_is_loaded_and_invalid_values_fall_back(self) -> None:
        self.assertEqual(self.load_from('{"language": "en"}')["language"], "en")
        self.assertEqual(self.load_from('{"language": "de"}')["language"], "tr")

    def test_msfs_layout_choice_is_loaded_and_invalid_values_fall_back(self) -> None:
        self.assertEqual(
            self.load_from('{"msfs_layout": "classic"}')["msfs_layout"],
            "classic",
        )
        self.assertEqual(
            self.load_from('{"msfs_layout": "unknown"}')["msfs_layout"],
            "cockpit",
        )

    def test_weather_city_choices_are_sanitised_and_limited_to_three(self) -> None:
        settings = self.load_from(json.dumps({
            "weather_cities": ["Ankara", "İzmir", "Antalya", "Bursa"],
        }))
        self.assertEqual(settings["weather_cities"], ["Ankara", "İzmir", "Antalya"])
        self.assertEqual(
            normalise_city_names("Ankara, Ankara, İzmir"),
            ("Ankara", "İzmir"),
        )
        with patch("weather_runtime.urllib.request.urlopen") as opener:
            self.assertEqual(resolve_city("Batman"), (37.8812, 41.1351))
            opener.assert_not_called()

    def test_truncated_json_does_not_close_application(self) -> None:
        settings = self.load_from('{"palette": "Bakır"')
        self.assertEqual(settings["palette"], "Kuzey")

    def test_json_array_does_not_close_application(self) -> None:
        settings = self.load_from("[]")
        self.assertEqual(settings["brightness"], 55)

    def test_wrong_types_are_sanitised(self) -> None:
        payload = json.dumps({
            "palette": "olmayan",
            "brightness": "parlak",
            "rotation": 180,
            "autostart": "yes",
            "port": "",
            "colors": {"cpu": "not-a-colour"},
            "hardware_labels": {"cpu": 123, "gpu": "ABCDEFGHIJKLMNO"},
        })
        settings = self.load_from(payload)
        self.assertEqual(settings["palette"], "Kuzey")
        self.assertEqual(settings["brightness"], 55)
        self.assertEqual(settings["rotation"], "normal")
        self.assertIs(settings["autostart"], False)
        self.assertEqual(settings["port"], "AUTO")
        self.assertEqual(settings["colors"]["cpu"], gui.PALETTES["Kuzey"]["cpu"])
        self.assertEqual(settings["hardware_labels"]["cpu"], "")
        self.assertEqual(settings["hardware_labels"]["gpu"], "ABCDEFGHIJKL")

    def test_brightness_is_clamped(self) -> None:
        self.assertEqual(self.load_from('{"brightness": 500}')["brightness"], 100)
        self.assertEqual(self.load_from('{"brightness": -20}')["brightness"], 0)

    def test_automatic_brightness_uses_day_and_night_boundaries(self) -> None:
        self.assertEqual(gui.automatic_brightness(gui.dt.datetime(2026, 1, 1, 6, 59)), 30)
        self.assertEqual(gui.automatic_brightness(gui.dt.datetime(2026, 1, 1, 7, 0)), 90)
        self.assertEqual(gui.automatic_brightness(gui.dt.datetime(2026, 1, 1, 18, 59)), 90)
        self.assertEqual(gui.automatic_brightness(gui.dt.datetime(2026, 1, 1, 19, 0)), 30)
        self.assertEqual(
            gui.effective_brightness(
                {"brightness": 55, "auto_brightness": True},
                gui.dt.datetime(2026, 1, 1, 12, 0),
            ),
            90,
        )
        self.assertEqual(
            gui.effective_brightness({"brightness": 55, "auto_brightness": False}),
            55,
        )

    def test_atomic_save_leaves_no_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "settings.json"
            with patch.object(gui, "APP_DATA", root), patch.object(gui, "SETTINGS_PATH", path):
                gui.save_settings_file(default_settings())
            self.assertTrue(path.exists())
            self.assertFalse(path.with_suffix(".tmp").exists())
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["palette"], "Kuzey")

    def test_autostart_reconciliation_repairs_enabled_entry(self) -> None:
        with patch.object(gui, "set_windows_autostart") as setter:
            self.assertTrue(gui.reconcile_windows_autostart(True))
        setter.assert_called_once_with(True)

    def test_autostart_reconciliation_does_not_crash_on_registry_error(self) -> None:
        with patch.object(
            gui, "set_windows_autostart", side_effect=OSError("registry unavailable"),
        ), patch.object(gui, "_runtime_log") as runtime_log:
            self.assertFalse(gui.reconcile_windows_autostart(True))
        self.assertIn("autostart reconciliation failed", runtime_log.call_args.args[0])

    def test_startup_task_xml_is_resilient_and_targets_current_app(self) -> None:
        with patch.object(gui, "_startup_action", return_value=(r"C:\Apps\Sabasakal.exe", "--autorun")):
            xml = gui._startup_task_xml()
        self.assertIn("<LogonTrigger>", xml)
        self.assertIn("<Delay>PT7S</Delay>", xml)
        self.assertIn("<StartWhenAvailable>true</StartWhenAvailable>", xml)
        self.assertIn("<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>", xml)
        self.assertIn("<RestartOnFailure>", xml)
        self.assertIn(r"<Command>C:\Apps\Sabasakal.exe</Command>", xml)
        self.assertIn("<Arguments>--autorun</Arguments>", xml)

    def test_autostart_uses_task_and_registry_fallback(self) -> None:
        with patch.object(gui, "_install_windows_startup_task", return_value=True) as task, patch.object(
            gui, "_install_windows_startup_shortcut", return_value=True,
        ) as shortcut, patch.object(
            gui, "_set_windows_run_fallback",
        ) as fallback:
            gui.set_windows_autostart(True)
        task.assert_called_once_with()
        shortcut.assert_called_once_with()
        fallback.assert_called_once_with(True)

    def test_autostart_survives_task_failure_when_fallback_succeeds(self) -> None:
        with patch.object(gui, "_install_windows_startup_task", return_value=False), patch.object(
            gui, "_install_windows_startup_shortcut", return_value=True,
        ), patch.object(
            gui, "_set_windows_run_fallback",
        ):
            gui.set_windows_autostart(True)

    def test_autostart_disable_removes_both_launch_paths(self) -> None:
        with patch.object(gui, "_remove_windows_startup_task", return_value=True) as task, patch.object(
            gui, "_remove_windows_startup_shortcut", return_value=True,
        ) as shortcut, patch.object(
            gui, "_set_windows_run_fallback",
        ) as fallback:
            gui.set_windows_autostart(False)
        task.assert_called_once_with()
        shortcut.assert_called_once_with()
        fallback.assert_called_once_with(False)

    def test_startup_shortcut_targets_current_executable(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(
            gui, "_windows_startup_shortcut_path",
            return_value=Path(directory) / gui.STARTUP_SHORTCUT_NAME,
        ), patch.object(
            gui, "_startup_action", return_value=(r"C:\Apps\Sabasakal.exe", "--autorun"),
        ), patch.object(gui.subprocess, "run") as run:
            def create_shortcut(command, **_kwargs):
                script = command[-1]
                self.assertIn(r"C:\Apps\Sabasakal.exe", script)
                self.assertIn("--autorun", script)
                (Path(directory) / gui.STARTUP_SHORTCUT_NAME).touch()
                return SimpleNamespace(returncode=0, stderr="", stdout="")

            run.side_effect = create_shortcut
            self.assertTrue(gui._install_windows_startup_shortcut())

    def test_runtime_working_directory_is_writable_and_not_inherited(self) -> None:
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                target = Path(directory) / "runtime"
                self.assertEqual(gui.prepare_runtime_working_directory(target), target)
                self.assertEqual(Path.cwd(), target)
                (target / "log.log").write_text("driver log", encoding="utf-8")
                self.assertTrue((target / "log.log").is_file())
            finally:
                os.chdir(previous)

    def test_ready_palette_collection_has_safe_size_and_contrast(self) -> None:
        self.assertGreaterEqual(len(gui.PALETTES), 15)
        self.assertLessEqual(len(gui.PALETTES), 20)
        required = set(gui.PALETTES["Kuzey"])

        def luminance(color: str) -> float:
            channels = []
            for value in ImageColor.getrgb(color):
                channel = value / 255.0
                channels.append(
                    channel / 12.92
                    if channel <= 0.04045
                    else ((channel + 0.055) / 1.055) ** 2.4
                )
            return (0.2126 * channels[0]) + (0.7152 * channels[1]) + (0.0722 * channels[2])

        def contrast(first: str, second: str) -> float:
            first_luminance, second_luminance = luminance(first), luminance(second)
            return (max(first_luminance, second_luminance) + 0.05) / (
                min(first_luminance, second_luminance) + 0.05
            )

        for name, palette in gui.PALETTES.items():
            self.assertEqual(set(palette), required, name)
            self.assertGreaterEqual(contrast(palette["text"], palette["panel"]), 4.5, name)
            self.assertGreaterEqual(contrast(palette["muted"], palette["panel"]), 3.0, name)
            for key in ("cpu", "warning", "danger"):
                self.assertGreaterEqual(contrast(palette[key], palette["panel"]), 3.0, f"{name}:{key}")


class PerformanceCounterScenarios(unittest.TestCase):
    def test_ring_arc_has_no_wrap_seam_above_eighty_four_percent(self) -> None:
        image = Image.new("RGB", (160, 160), "#000000")
        app_bbox = (27, 26, 133, 132)
        screen_app.draw_smooth_arc(
            image, app_bbox, 135.0, 397.5, "#FFFFFF", 8,
        )
        cx, cy = 80, 79
        for angle_degrees in range(357, 364):
            angle = np.deg2rad(angle_degrees)
            for radius in range(47, 52):
                x = round(cx + radius * np.cos(angle))
                y = round(cy + radius * np.sin(angle))
                self.assertGreater(
                    min(image.getpixel((x, y))), 120,
                    f"arc seam at angle={angle_degrees}, radius={radius}",
                )

    def test_short_ring_arc_has_flat_complete_start_cap(self) -> None:
        image = Image.new("RGB", (160, 160), "#000000")
        screen_app.draw_smooth_progress_ring(
            image, (27, 26, 133, 132), 135.0, 405.0, 159.3,
            "#20352D", "#FFFFFF", 8,
        )
        cx, cy = 80, 79
        # Sample just inside the anti-aliased radial edge; the complete stroke
        # thickness must already be present without tapering into a wedge.
        start_angle = np.deg2rad(136.0)
        for radius in range(47, 52):
            x = round(cx + radius * np.cos(start_angle))
            y = round(cy + radius * np.sin(start_angle))
            self.assertGreater(
                min(image.getpixel((x, y))), 120,
                f"incomplete flat cap at radius={radius}",
            )
        before_start = np.deg2rad(133.5)
        x = round(cx + 49 * np.cos(before_start))
        y = round(cy + 49 * np.sin(before_start))
        self.assertLess(max(image.getpixel((x, y))), 80)

    def test_media_ring_batches_cover_every_antialiased_arc_pixel(self) -> None:
        for center_x, regions in zip(
            (80, 240, 400), gui.MEDIA_RING_REGION_GROUPS,
        ):
            image = Image.new("RGB", (480, 320), "#000000")
            screen_app.draw_smooth_progress_ring(
                image,
                (center_x - 53, 26, center_x + 53, 132),
                135.0,
                405.0,
                405.0,
                "#FFFFFF",
                "#FFFFFF",
                8,
            )
            for y in range(20, 140):
                for x in range(center_x - 60, center_x + 61):
                    if max(image.getpixel((x, y))) == 0:
                        continue
                    self.assertTrue(
                        any(
                            left <= x < right and top <= y < bottom
                            for left, top, right, bottom in regions
                        ),
                        f"uncovered ring pixel at {(x, y)}",
                    )

    def test_live_value_tiles_never_overwrite_open_ring_start_caps(self) -> None:
        for center_x, regions in zip((80, 240, 400), gui.FAST_VALUE_REGION_GROUPS):
            start_x = round(center_x + 53 * np.cos(np.deg2rad(135.0)))
            start_y = round(79 + 53 * np.sin(np.deg2rad(135.0)))
            self.assertFalse(any(
                left <= start_x < right and top <= start_y < bottom
                for left, top, right, bottom in regions
            ))

    def test_hardware_labels_use_heavier_font(self) -> None:
        family, style = screen_app.FONTS["hardware_label"].getname()
        self.assertTrue(family)
        self.assertIn("bold", style.casefold())

    def test_battery_refresh_supports_two_second_interval(self) -> None:
        self.assertEqual(BatteryDeviceMonitor(interval=2.0).interval, 2.0)

    def test_slow_windows_scan_never_blocks_lghub_updates(self) -> None:
        release = threading.Event()

        async def slow_windows_scan():
            release.wait(3.0)
            return []

        monitor = BatteryDeviceMonitor(interval=2.0)
        mouse = BatteryDevice(
            "lghub:testmouse", "Test Mouse", 75, "mouse",
        )
        with patch.object(
            monitor, "_listen_lghub", side_effect=lambda: monitor._stop.wait(3.0),
        ), patch.object(
            monitor, "_scan_windows", side_effect=slow_windows_scan,
        ), patch.object(
            monitor, "_scan_lghub", return_value=[mouse],
        ), patch(
            "battery_runtime.scan_lghub_connected_slugs", return_value={"testmouse"},
        ):
            monitor.start()
            deadline = time.monotonic() + 1.0
            while not monitor.snapshot().devices and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(monitor.snapshot().devices, (mouse,))
            release.set()
            monitor.stop()

    def test_battery_selection_prioritizes_mouse_headset_and_keyboard(self) -> None:
        devices = [
            BatteryDevice("speaker", "Speaker", 10, "other"),
            BatteryDevice("keyboard", "Razer Keyboard", 80, "keyboard"),
            BatteryDevice("headset", "Wireless Headset", 55, "headset"),
            BatteryDevice("mouse", "Wireless Mouse", 70, "mouse"),
            BatteryDevice("phone", "Phone", 20, "other"),
        ]
        selected = select_battery_devices(devices)
        self.assertEqual([device.kind for device in selected], ["mouse", "headset", "keyboard"])
        self.assertEqual(classify_device("Razer Basilisk Mouse"), "mouse")

    def test_lghub_battery_values_are_read_and_named(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.db"
            connection = sqlite3.connect(path)
            connection.execute(
                "create table data(_id integer primary key, file blob not null)"
            )
            payload = {
                "battery/prox2wirelessmouse/percentage": {"percentage": 85},
                "battery/prox2wirelessmouse/warning": {"percentage": 90},
                "battery/proxwirelessheadset/percentage": {"percentage": 52},
            }
            connection.execute("insert into data(file) values (?)", (json.dumps(payload),))
            connection.commit()
            connection.close()
            devices = BatteryDeviceMonitor._scan_lghub(path)
        self.assertEqual(
            [(device.name, device.percent, device.kind) for device in devices],
            [
                ("PRO X SUPERLIGHT 2", 85, "mouse"),
                ("PRO X WIRELESS", 52, "headset"),
            ],
        )

    def test_lghub_disconnected_devices_are_filtered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.db"
            connection = sqlite3.connect(path)
            connection.execute(
                "create table data(_id integer primary key, file blob not null)"
            )
            payload = {
                "battery/prox2wirelessmouse/percentage": {"percentage": 85},
                "battery/proxwirelessheadset/percentage": {"percentage": 52},
            }
            connection.execute("insert into data(file) values (?)", (json.dumps(payload),))
            connection.commit()
            connection.close()
            devices = BatteryDeviceMonitor._scan_lghub(
                path, {"proxwirelessheadset"},
            )
        self.assertEqual([device.name for device in devices], ["PRO X WIRELESS"])

    def test_lghub_connected_device_list_only_accepts_active_state(self) -> None:
        message = {"payload": {"deviceInfos": [
            {"slotPrefix": "proxwirelessheadset", "state": "ACTIVE"},
            {"slotPrefix": "prox2wirelessmouse", "state": "ABSENT"},
            {"slotPrefix": "sleepingkeyboard", "state": "INACTIVE"},
        ]}}
        self.assertEqual(
            parse_lghub_connected_slugs(message), {"proxwirelessheadset"},
        )

    def test_lghub_live_state_broadcast_is_generic_for_keyboard(self) -> None:
        message = {
            "path": "/devices/state/changed",
            "payload": {
                "id": "dev-keyboard", "slotPrefix": "g915keyboard",
                "state": "NOT_CONNECTED", "deviceType": "KEYBOARD",
                "displayName": "G915 TKL",
            },
        }
        infos = parse_lghub_device_infos(message)
        self.assertEqual(infos["g915keyboard"], {
            "state": "NOT_CONNECTED", "name": "G915 TKL",
            "kind": "keyboard", "device_id": "dev-keyboard",
        })
        monitor = BatteryDeviceMonitor(interval=2.0)
        monitor._update_lghub_infos(infos)
        ready, current = monitor._current_lghub_infos()
        self.assertTrue(ready)
        self.assertEqual(current["g915keyboard"]["state"], "NOT_CONNECTED")

    def test_live_disconnect_is_not_overwritten_by_stale_device_list(self) -> None:
        monitor = BatteryDeviceMonitor(interval=2.0)
        active = {"g915keyboard": {
            "state": "ACTIVE", "name": "G915 TKL", "kind": "keyboard",
            "device_id": "dev-keyboard",
        }}
        disconnected = {"g915keyboard": {
            **active["g915keyboard"], "state": "NOT_CONNECTED",
        }}
        monitor._replace_lghub_infos(active)
        monitor._update_lghub_infos(disconnected)
        monitor._replace_lghub_infos(active)
        ready, current = monitor._current_lghub_infos()
        self.assertTrue(ready)
        self.assertEqual(current["g915keyboard"]["state"], "NOT_CONNECTED")

    def test_transient_empty_lghub_list_keeps_last_device_state(self) -> None:
        monitor = BatteryDeviceMonitor(interval=2.0)
        active = {"testmouse": {
            "state": "ACTIVE", "name": "Test Mouse", "kind": "mouse",
            "device_id": "dev-mouse",
        }}
        monitor._replace_lghub_infos(active)
        monitor._replace_lghub_infos({})
        _ready, current = monitor._current_lghub_infos()
        self.assertEqual(current, active)

    def test_transient_empty_battery_database_keeps_connected_device(self) -> None:
        monitor = BatteryDeviceMonitor(interval=2.0)
        mouse = BatteryDevice("lghub:testmouse", "Test Mouse", 75, "mouse")
        self.assertEqual(
            monitor._stabilize_lghub_devices([mouse], {"testmouse"}), [mouse],
        )
        self.assertEqual(
            monitor._stabilize_lghub_devices([], {"testmouse"}), [mouse],
        )
        self.assertEqual(monitor._stabilize_lghub_devices([], set()), [])

    def test_all_non_active_lghub_states_are_hidden(self) -> None:
        states = (
            "ABSENT", "DISABLED", "PRESENT", "NOT_CONNECTED", "PENDING",
            "LOADING_RESOURCES", "REQUIRES_UPDATE", "INITIALIZING", "BLOCKED",
        )
        message = {"payload": {"deviceInfos": [
            {
                "slotPrefix": f"device{index}", "state": state,
                "deviceType": "MOUSE", "displayName": f"Mouse {index}",
            }
            for index, state in enumerate(states)
        ] + [{
            "slotPrefix": "activekeyboard", "state": "ACTIVE",
            "deviceType": "KEYBOARD", "displayName": "Active Keyboard",
        }]}}
        self.assertEqual(
            parse_lghub_connected_slugs(message), {"activekeyboard"},
        )

    def test_malformed_lghub_messages_are_safe(self) -> None:
        for message in (None, [], {}, {"payload": None}, {"payload": {}}):
            self.assertEqual(parse_lghub_device_infos(message), {})
            self.assertEqual(parse_lghub_connected_slugs(message), set())

    def test_all_lghub_peripherals_use_live_battery_endpoint(self) -> None:
        class FakeSocket:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def settimeout(self, _timeout):
                pass

        replies: list[str] = []

        def send(_stream, raw):
            request = json.loads(raw)
            is_mouse = request["path"].endswith("mouse/state")
            is_headset = request["path"].endswith("headset/state")
            connected = is_mouse or is_headset
            replies.append(json.dumps({
                "msgId": request["msgId"],
                "result": {"code": "SUCCESS" if connected else "NO_SUCH_PATH"},
                "payload": {
                    "percentage": 81 if is_mouse else 83,
                } if connected else None,
            }))

        infos = {
            "mouse": {"kind": "mouse", "device_id": "mouse"},
            "keyboard": {"kind": "keyboard", "device_id": "keyboard"},
            "headset": {"kind": "headset", "device_id": "headset"},
        }
        with patch(
            "battery_runtime._connect_lghub_websocket", return_value=FakeSocket(),
        ), patch(
            "battery_runtime._send_websocket_text", side_effect=send,
        ), patch(
            "battery_runtime._receive_websocket_message",
            side_effect=lambda _stream: replies.pop(0),
        ):
            states = probe_lghub_input_batteries(infos)
        self.assertEqual(states, {
            "mouse": (True, 81), "keyboard": (False, None),
            "headset": (True, 83),
        })

    def test_unknown_lghub_device_uses_live_name_and_kind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.db"
            connection = sqlite3.connect(path)
            connection.execute(
                "create table data(_id integer primary key, file blob not null)"
            )
            payload = {
                "battery/g915keyboard/percentage": {"percentage": 67},
            }
            connection.execute("insert into data(file) values (?)", (json.dumps(payload),))
            connection.commit()
            connection.close()
            devices = BatteryDeviceMonitor._scan_lghub(
                path, {"g915keyboard"}, {
                    "g915keyboard": {
                        "state": "ACTIVE", "name": "G915 TKL",
                        "kind": "keyboard", "device_id": "dev-keyboard",
                    },
                },
            )
        self.assertEqual(
            [(device.name, device.percent, device.kind) for device in devices],
            [("G915 TKL", 67, "keyboard")],
        )

    def test_battery_panel_is_conditional_and_does_not_touch_spectrum(self) -> None:
        spectrum = AudioSpectrumSnapshot(bars=tuple([0.6] * 16), active=True)
        media = MediaSnapshot(platform="Chrome", title="Uzun bir medya başlığı", status="playing")
        colors = dict(gui.PALETTES["Kuzey"])
        plain = screen_app.render_screen(
            self.sample_metrics(), screen_app.GameSnapshot(), False, colors,
            media_snapshot=media, audio_spectrum=spectrum,
        )
        devices = (
            BatteryDevice("mouse", "Razer Mouse", 72, "mouse"),
            BatteryDevice("headset", "PRO X", 18, "headset"),
        )
        with_battery = screen_app.render_screen(
            self.sample_metrics(), screen_app.GameSnapshot(), False, colors,
            media_snapshot=media, audio_spectrum=spectrum, battery_devices=devices,
        )
        self.assertIsNotNone(ImageChops.difference(
            plain.crop(gui.BATTERY_PANEL_REGION), with_battery.crop(gui.BATTERY_PANEL_REGION),
        ).getbbox())
        battery_patch = screen_app.render_media_visual_patch(
            spectrum, colors, with_battery=True,
        )
        self.assertIsNone(ImageChops.difference(
            with_battery.crop(screen_app.MEDIA_VIS_REGION_WITH_BATTERY), battery_patch,
        ).getbbox())
        plain_center = sum((screen_app.MEDIA_VIS_REGION[0], screen_app.MEDIA_VIS_REGION[2])) / 2
        battery_center = sum((
            screen_app.MEDIA_VIS_REGION_WITH_BATTERY[0],
            screen_app.MEDIA_VIS_REGION_WITH_BATTERY[2],
        )) / 2
        self.assertGreater(plain_center, battery_center)

    def test_battery_panel_remains_visible_in_idle_cs2_and_msfs_modes(self) -> None:
        devices = (BatteryDevice("mouse", "Test Mouse", 72, "mouse"),)
        game_state = screen_app.GameSnapshot(
            in_match=True, money=3200, next_win_money=6450,
            next_loss_money=5100,
        )
        scenarios = (
            (screen_app.GameSnapshot(), False, {}),
            (game_state, True, {}),
            (
                screen_app.GameSnapshot(), False,
                {
                    "msfs_active": True,
                    "flight_telemetry": FlightTelemetry(
                        12500.0, 145.0, 850.0, 278.0, True, 1.0,
                    ),
                },
            ),
        )
        for snapshot, game_active, extras in scenarios:
            plain = screen_app.render_screen(
                self.sample_metrics(), snapshot, game_active, **extras,
            )
            with_battery = screen_app.render_screen(
                self.sample_metrics(), snapshot, game_active,
                battery_devices=devices, **extras,
            )
            self.assertIsNotNone(ImageChops.difference(
                plain.crop(gui.BATTERY_PANEL_REGION),
                with_battery.crop(gui.BATTERY_PANEL_REGION),
            ).getbbox())

    def test_idle_panel_has_no_cs2_ready_placeholder(self) -> None:
        frame = screen_app.render_screen(
            self.sample_metrics(), screen_app.GameSnapshot(), False,
        )
        inner_panel = frame.crop((24, 174, 456, 294))
        self.assertEqual(len(set(inner_panel.get_flattened_data())), 1)

    def test_idle_weather_changes_only_the_bottom_panel(self) -> None:
        idle = screen_app.render_screen(
            self.sample_metrics(), screen_app.GameSnapshot(), False,
        )
        weather = screen_app.render_screen(
            self.sample_metrics(), screen_app.GameSnapshot(), False,
            weather_readings=(
                WeatherReading("Batman", 24.0, 0),
                WeatherReading("Muğla", 19.0, 61),
                WeatherReading("İstanbul", 16.0, 3),
            ),
        )
        self.assertIsNone(ImageChops.difference(
            idle.crop((0, 0, 480, 160)), weather.crop((0, 0, 480, 160)),
        ).getbbox())
        self.assertIsNotNone(ImageChops.difference(
            idle.crop(gui.BOTTOM_REGION), weather.crop(gui.BOTTOM_REGION),
        ).getbbox())
        self.assertEqual(screen_app.weather_condition_kind(0), "clear")
        self.assertEqual(screen_app.weather_condition_kind(61), "rain")
        self.assertEqual(screen_app.weather_condition_kind(75), "snow")
        self.assertEqual(screen_app.weather_condition_kind(45), "fog")

    def test_packaged_icon_set_is_available_for_every_runtime_panel(self) -> None:
        for icon_name in (
            "bolt-cutter", "dynamite", "mouse", "keyboard", "headphones",
            "gamepad-2", "sun", "moon", "cloud", "cloud-rain",
            "snowflake", "cloud-lightning", "cloud-fog",
        ):
            icon = screen_app._loaded_icon_alpha(icon_name)
            self.assertIsNotNone(icon, icon_name)
            self.assertEqual(icon.mode, "RGBA")

    def test_msfs_process_age_detects_flight_simulator_2024(self) -> None:
        process = SimpleNamespace(info={
            "name": "FlightSimulator2024.exe", "create_time": 100.0,
        })
        with patch.object(
            screen_app.psutil, "process_iter", return_value=[process],
        ), patch.object(screen_app.time, "time", return_value=160.0):
            self.assertEqual(screen_app.msfs_session_seconds(), 60.0)

    def test_msfs_panel_has_priority_over_background_media(self) -> None:
        extras = {
            "msfs_active": True,
            "flight_telemetry": FlightTelemetry(
                12500.0, 145.0, 850.0, 278.0, True, 1.0,
            ),
        }
        plain = screen_app.render_screen(
            self.sample_metrics(), screen_app.GameSnapshot(), False, **extras,
        )
        with_media = screen_app.render_screen(
            self.sample_metrics(), screen_app.GameSnapshot(), False,
            media_snapshot=MediaSnapshot(
                platform="Spotify", title="Test", status="playing",
            ),
            audio_spectrum=AudioSpectrumSnapshot(
                bars=tuple([0.8] * 16), active=True,
            ),
            **extras,
        )
        self.assertIsNone(ImageChops.difference(plain, with_media).getbbox())

    def test_msfs_live_values_change_only_inside_telemetry_region(self) -> None:
        devices = (BatteryDevice("mouse", "Test Mouse", 72, "mouse"),)
        for battery_devices, region in (
            ((), gui.MSFS_TELEMETRY_REGION),
            (devices, gui.MSFS_TELEMETRY_REGION_WITH_BATTERY),
        ):
            common = {
                "msfs_active": True,
                "battery_devices": battery_devices,
            }
            first = screen_app.render_screen(
                self.sample_metrics(), screen_app.GameSnapshot(), False,
                flight_telemetry=FlightTelemetry(
                    1200.0, 85.0, 300.0, 90.0, True, 1.0,
                ),
                **common,
            )
            second = screen_app.render_screen(
                self.sample_metrics(), screen_app.GameSnapshot(), False,
                flight_telemetry=FlightTelemetry(
                    1280.0, 92.0, 450.0, 97.0, True, 2.0,
                ),
                **common,
            )
            changed = ImageChops.difference(first, second).getbbox()
            self.assertIsNotNone(changed)
            left, top, right, bottom = changed
            self.assertGreaterEqual(left, region[0])
            self.assertGreaterEqual(top, region[1])
            self.assertLessEqual(right, region[2])
            self.assertLessEqual(bottom, region[3])

    def test_msfs_layouts_are_distinct_and_keep_battery_rail_unchanged(self) -> None:
        devices = (BatteryDevice("mouse", "Test Mouse", 72, "mouse"),)
        common = {
            "msfs_active": True,
            "battery_devices": devices,
            "flight_telemetry": FlightTelemetry(
                12500.0, 145.0, 850.0, 278.0, True, 1.0,
            ),
        }
        classic = screen_app.render_screen(
            self.sample_metrics(), screen_app.GameSnapshot(), False,
            msfs_layout="classic", **common,
        )
        cockpit = screen_app.render_screen(
            self.sample_metrics(), screen_app.GameSnapshot(), False,
            msfs_layout="cockpit", **common,
        )
        self.assertIsNotNone(ImageChops.difference(
            classic.crop(gui.MSFS_PANEL_REGION_WITH_BATTERY),
            cockpit.crop(gui.MSFS_PANEL_REGION_WITH_BATTERY),
        ).getbbox())
        self.assertIsNone(ImageChops.difference(
            classic.crop(gui.BATTERY_PANEL_REGION),
            cockpit.crop(gui.BATTERY_PANEL_REGION),
        ).getbbox())

    def test_msfs_panel_has_theme_aware_aircraft_backdrop(self) -> None:
        frame = screen_app.render_screen(
            self.sample_metrics(), screen_app.GameSnapshot(), False,
            msfs_active=True, flight_telemetry=FlightTelemetry(),
            msfs_layout="cockpit",
        )
        self.assertNotEqual(frame.getpixel((240, 200)), ImageColor.getrgb(screen_app.COLORS["panel"]))

    def test_msfs_metric_units_are_converted_and_formatted_in_turkish(self) -> None:
        self.assertEqual(screen_app.format_airspeed_kmh(146.0), "270 km/sa")
        self.assertEqual(screen_app.format_altitude_km(12540.0), "3,82 km")

    def test_bomb_panel_uses_kit_icon_and_defuse_duration_instead_of_kit_text(self) -> None:
        self.assertEqual(screen_app._defuse_duration_label(True), "5 sn çözme")
        self.assertEqual(screen_app._defuse_duration_label(False), "10 sn çözme")
        with patch.object(screen_app, "_draw_defuse_kit_icon") as icon:
            screen_app.render_screen(
                self.sample_metrics(),
                screen_app.GameSnapshot(
                    connected=True, in_match=True, team="CT", has_defuse_kit=True,
                    bomb_planted=True, bomb_seconds=27.0,
                ),
                True,
            )
        icon.assert_called_once()
        self.assertTrue(icon.call_args.kwargs["available"])
        with patch.object(screen_app, "_draw_bomb_icon") as bomb_icon:
            screen_app.render_screen(
                self.sample_metrics(),
                screen_app.GameSnapshot(
                    connected=True, in_match=True, team="T",
                    bomb_planted=True, bomb_seconds=27.0,
                ),
                True,
            )
        bomb_icon.assert_called_once()

    def test_ct_kit_and_t_bomb_icons_are_visually_distinct(self) -> None:
        colors = dict(gui.PALETTES["Kuzey"])
        kit = Image.new("RGB", (36, 36), colors["panel_2"])
        bomb = Image.new("RGB", (36, 36), colors["panel_2"])
        screen_app._draw_defuse_kit_icon(
            kit, ImageDraw.Draw(kit), 5, 5, colors["cpu"], colors, available=True,
        )
        screen_app._draw_bomb_icon(
            bomb, ImageDraw.Draw(bomb), 5, 5, colors["warning"], colors,
        )
        self.assertIsNotNone(ImageChops.difference(kit, bomb).getbbox())

    def test_bomb_bar_colors_are_interpreted_for_each_team(self) -> None:
        colors = dict(gui.PALETTES["Kuzey"])
        self.assertEqual(screen_app._bomb_color(20.0, "CT", False, colors), colors["cpu"])
        self.assertEqual(screen_app._bomb_color(8.0, "CT", False, colors), colors["danger"])
        self.assertEqual(screen_app._bomb_color(8.0, "CT", True, colors), colors["warning"])
        self.assertEqual(screen_app._bomb_color(4.0, "CT", True, colors), colors["danger"])
        self.assertEqual(screen_app._bomb_color(8.0, "T", False, colors), colors["warning"])
        self.assertEqual(screen_app._bomb_color(3.0, "T", False, colors), colors["cpu"])
        self.assertEqual(
            screen_app._bomb_color(20.0, "T", False, colors, defusing=True),
            colors["danger"],
        )

    def test_normal_battery_semantics_are_neutral_in_every_palette(self) -> None:
        for name, colors in gui.PALETTES.items():
            self.assertEqual(screen_app._battery_color(80, colors), colors["text"], name)
            self.assertEqual(screen_app._battery_color(35, colors), colors["warning"], name)
            self.assertEqual(screen_app._battery_color(15, colors), colors["danger"], name)

    def test_competitive_economy_starts_with_the_1900_loss_award(self) -> None:
        self.assertEqual(
            screen_app.calculate_economy(
                800, "CT", {"mode": "competitive", "round_wins": {}}, False,
            ),
            (4050, 2700, 1900),
        )

    def test_economy_win_reduces_loss_counter_instead_of_resetting_it(self) -> None:
        map_data = {
            "mode": "competitive",
            "round_wins": {
                "1": "t_win_elimination",
                "2": "t_win_bomb",
                "3": "ct_win_elimination",
            },
        }
        # Start at one, two losses raise it to three, and the win lowers it to
        # two: the next loss is therefore $2400, not a reset $1400/$1900.
        self.assertEqual(
            screen_app.calculate_economy(3000, "CT", map_data, False),
            (6250, 5400, 2400),
        )

    def test_wingman_uses_2v2_awards_and_8000_money_cap(self) -> None:
        map_data = {
            "mode": "scrimcomp2v2",
            "round_wins": {"1": "ct_win_elimination"},
        }
        self.assertEqual(
            screen_app.calculate_economy(7000, "T", map_data, False),
            (8000, 8000, 2300),
        )
        self.assertEqual(
            screen_app.calculate_economy(4000, "T", map_data, True),
            (6750, 6900, 2900),
        )

    def test_wingman_halftime_does_not_mix_old_side_results(self) -> None:
        map_data = {
            "mode": "competitive2v2",
            "round_wins": {
                **{str(index): "t_win_elimination" for index in range(1, 9)},
                "9": "ct_win_elimination",
            },
        }
        # At 8 rounds the sides switch. Only round 9 belongs to the current
        # CT side, so its win keeps the next loss at Wingman's base $2000.
        self.assertEqual(
            screen_app.calculate_economy(3000, "CT", map_data, False),
            (5750, 5000, 2000),
        )

    def test_economy_is_hidden_in_non_competitive_modes(self) -> None:
        for mode in ("casual", "deathmatch", "armsrace", "retake"):
            self.assertEqual(
                screen_app.calculate_economy(
                    3000, "CT", {"mode": mode, "round_wins": {}}, False,
                ),
                (None, None, None),
                mode,
            )

    def test_faceit_custom_server_can_use_competitive_economy_override(self) -> None:
        map_data = {"mode": "custom", "round_wins": {}}
        self.assertEqual(
            screen_app.calculate_economy(3000, "CT", map_data, False),
            (None, None, None),
        )
        self.assertEqual(
            screen_app.calculate_economy(
                3000, "CT", map_data, False, force_competitive=True,
            ),
            (6250, 4900, 1900),
        )

    def test_deathmatch_reports_kills_and_headshot_percentage(self) -> None:
        game_state = screen_app.CS2GameState()
        game_state.update({
            "provider": {"steamid": "local-player"},
            "player": {
                "steamid": "local-player", "team": "CT", "activity": "playing",
                "state": {"money": 0, "round_kills": 25, "round_killhs": 10},
                "match_stats": {"kills": 25},
            },
            "map": {"phase": "live", "mode": "deathmatch", "round_wins": {}},
        })
        snapshot = game_state.snapshot()
        self.assertTrue(snapshot.in_match)
        self.assertEqual(snapshot.kills, 25)
        self.assertEqual(snapshot.headshot_kills, 10)
        self.assertEqual(snapshot.headshot_percent, 40)
        self.assertIsNone(snapshot.next_win_money)

    def test_deathmatch_zero_kills_has_zero_headshot_percentage(self) -> None:
        game_state = screen_app.CS2GameState()
        game_state.update({
            "provider": {"steamid": "local-player"},
            "player": {
                "steamid": "local-player", "team": "T", "activity": "playing",
                "state": {"round_kills": 0, "round_killhs": 0},
            },
            "map": {"phase": "live", "mode": "teamdeathmatch", "round_wins": {}},
        })
        self.assertEqual(game_state.snapshot().headshot_percent, 0)

    def test_deathmatch_headshot_percentage_matches_cs2_truncation(self) -> None:
        game_state = screen_app.CS2GameState()
        game_state.update({
            "provider": {"steamid": "local-player"},
            "player": {
                "steamid": "local-player", "team": "CT", "activity": "playing",
                "state": {"round_kills": 17, "round_killhs": 12},
            },
            "map": {"phase": "live", "mode": "deathmatch", "round_wins": {}},
        })
        snapshot = game_state.snapshot()
        self.assertEqual(snapshot.kills, 17)
        self.assertEqual(snapshot.headshot_kills, 12)
        self.assertEqual(snapshot.headshot_percent, 70)

    def test_deathmatch_totals_survive_respawns_and_keep_headshots(self) -> None:
        game_state = screen_app.CS2GameState()

        def update(kills: int, headshots: int, phase: str = "live") -> None:
            game_state.update({
                "provider": {"steamid": "local-player"},
                "player": {
                    "steamid": "local-player", "team": "CT", "activity": "playing",
                    "state": {"round_kills": kills, "round_killhs": headshots},
                },
                "map": {"name": "de_dust2", "phase": phase, "mode": "deathmatch"},
            })

        update(3, 1)
        self.assertEqual((game_state.snapshot().kills, game_state.snapshot().headshot_kills), (3, 1))
        update(0, 0)  # death/respawn
        self.assertEqual((game_state.snapshot().kills, game_state.snapshot().headshot_kills), (3, 1))
        update(2, 1)  # two kills in the next life
        snapshot = game_state.snapshot()
        self.assertEqual((snapshot.kills, snapshot.headshot_kills), (5, 2))
        self.assertEqual(snapshot.headshot_percent, 40)

    def test_deathmatch_warmup_to_live_resets_totals_once(self) -> None:
        game_state = screen_app.CS2GameState()

        def update(kills: int, headshots: int, phase: str) -> None:
            game_state.update({
                "provider": {"steamid": "local-player"},
                "player": {
                    "steamid": "local-player", "team": "T", "activity": "playing",
                    "state": {"round_kills": kills, "round_killhs": headshots},
                },
                "map": {"name": "de_mirage", "phase": phase, "mode": "deathmatch"},
            })

        update(4, 2, "warmup")
        self.assertEqual(game_state.snapshot().kills, 4)
        update(0, 0, "live")
        self.assertEqual((game_state.snapshot().kills, game_state.snapshot().headshot_kills), (0, 0))
        update(1, 1, "live")
        self.assertEqual((game_state.snapshot().kills, game_state.snapshot().headshot_kills), (1, 1))
        update(0, 0, "live")
        self.assertEqual((game_state.snapshot().kills, game_state.snapshot().headshot_kills), (1, 1))

    def test_deathmatch_full_clock_jump_ends_warmup_when_map_phase_stays_live(self) -> None:
        game_state = screen_app.CS2GameState()

        def update(kills: int, headshots: int, seconds: float) -> None:
            game_state.update({
                "provider": {"steamid": "local-player"},
                "player": {
                    "steamid": "local-player", "team": "CT", "activity": "playing",
                    "state": {"round_kills": kills, "round_killhs": headshots},
                },
                "map": {"name": "de_nuke", "phase": "live", "mode": "deathmatch"},
                "phase_countdowns": {"phase": "live", "phase_ends_in": str(seconds)},
            })

        update(5, 2, 7.5)
        self.assertEqual(game_state.snapshot().kills, 5)
        update(0, 0, 599.8)
        self.assertEqual((game_state.snapshot().kills, game_state.snapshot().headshot_kills), (0, 0))
        update(2, 1, 580.0)
        self.assertEqual((game_state.snapshot().kills, game_state.snapshot().headshot_kills), (2, 1))

    def test_deathmatch_cumulative_stats_reset_ends_warmup_on_valve_server(self) -> None:
        game_state = screen_app.CS2GameState()

        def update(
            life_kills: int, life_headshots: int, match_kills: int, deaths: int,
        ) -> None:
            game_state.update({
                "provider": {"steamid": "local-player"},
                "player": {
                    "steamid": "local-player", "team": "CT", "activity": "playing",
                    "state": {
                        "round_kills": life_kills, "round_killhs": life_headshots,
                    },
                    "match_stats": {
                        "kills": match_kills, "deaths": deaths, "score": match_kills * 2,
                    },
                },
                # Matches the observed Valve DM payload: the phase stays live
                # and phase_countdowns is absent on both sides of warm-up.
                "map": {"name": "de_dust2", "phase": "live", "mode": "deathmatch"},
            })

        update(2, 1, 2, 1)  # warm-up
        self.assertEqual(game_state.snapshot().kills, 2)
        update(0, 0, 0, 0)  # real ten-minute match starts
        self.assertEqual((game_state.snapshot().kills, game_state.snapshot().headshot_kills), (0, 0))
        update(1, 1, 1, 0)
        self.assertEqual((game_state.snapshot().kills, game_state.snapshot().headshot_kills), (1, 1))

    def test_deathmatch_respawn_does_not_look_like_match_reset(self) -> None:
        game_state = screen_app.CS2GameState()

        def update(
            life_kills: int, life_hs: int, total_kills: int, deaths: int,
            health: int,
        ) -> None:
            game_state.update({
                "provider": {"steamid": "local-player"},
                "player": {
                    "steamid": "local-player", "team": "T", "activity": "playing",
                    "state": {
                        "round_kills": life_kills, "round_killhs": life_hs,
                        "health": health,
                    },
                    "match_stats": {"kills": total_kills, "deaths": deaths, "score": 10},
                },
                "map": {"name": "de_inferno", "phase": "live", "mode": "deathmatch"},
            })

        update(3, 1, 3, 0, 100)
        update(3, 1, 3, 1, 0)
        update(0, 0, 3, 1, 100)
        self.assertEqual((game_state.snapshot().kills, game_state.snapshot().headshot_kills), (3, 1))
        update(2, 1, 5, 1, 100)
        self.assertEqual((game_state.snapshot().kills, game_state.snapshot().headshot_kills), (5, 2))

    def test_deathmatch_alive_counter_reset_ends_warmup_when_stats_are_omitted(self) -> None:
        game_state = screen_app.CS2GameState()

        def update(kills: int, headshots: int, health: int = 100) -> None:
            game_state.update({
                "provider": {"steamid": "local-player"},
                "player": {
                    "steamid": "local-player", "team": "CT", "activity": "playing",
                    "state": {
                        "health": health, "round_kills": kills,
                        "round_killhs": headshots,
                    },
                },
                "map": {"name": "de_mirage", "phase": "live", "mode": "deathmatch"},
            })

        update(2, 1)  # warm-up kills
        self.assertEqual(game_state.snapshot().kills, 2)
        update(0, 0)  # official match starts while the player remains alive
        snapshot = game_state.snapshot()
        self.assertEqual((snapshot.kills, snapshot.headshot_kills), (0, 0))
        self.assertEqual(game_state._dm_last_reset_reason, "alive-counter-reset")

    def test_deathmatch_final_result_is_held_for_seven_seconds(self) -> None:
        game_state = screen_app.CS2GameState()
        live_payload = {
            "provider": {"steamid": "local-player"},
            "player": {
                "steamid": "local-player", "team": "CT", "activity": "playing",
                "state": {"round_kills": 4, "round_killhs": 2},
                "match_stats": {"kills": 24, "deaths": 12, "score": 55},
            },
            "map": {"name": "de_dust2", "phase": "live", "mode": "deathmatch"},
        }
        gameover_payload = {
            **live_payload,
            "map": {"name": "de_dust2", "phase": "gameover", "mode": "deathmatch"},
        }
        with patch.object(screen_app.time, "monotonic", return_value=100.0):
            game_state.update(live_payload)
            self.assertTrue(game_state.snapshot().in_match)
        with patch.object(screen_app.time, "monotonic", return_value=105.0):
            game_state.update(gameover_payload)
        with patch.object(screen_app.time, "monotonic", return_value=111.9):
            held = game_state.snapshot()
            self.assertTrue(held.in_match)
            self.assertEqual(held.kills, 24)
            self.assertEqual(held.headshot_percent, 8)
        with patch.object(screen_app.time, "monotonic", return_value=112.1):
            self.assertFalse(game_state.snapshot().in_match)

    def test_deathmatch_totals_survive_a_quick_application_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "deathmatch-state.json"

            def update(state: screen_app.CS2GameState, kills: int, headshots: int) -> None:
                state.update({
                    "provider": {"steamid": "local-player"},
                    "player": {
                        "steamid": "local-player", "team": "CT", "activity": "playing",
                        "state": {"round_kills": kills, "round_killhs": headshots},
                    },
                    "map": {"name": "de_dust2", "phase": "live", "mode": "deathmatch"},
                })

            first = screen_app.CS2GameState(cache_path)
            update(first, 7, 3)
            self.assertTrue(cache_path.exists())

            restarted = screen_app.CS2GameState(cache_path)
            # The player died while the app was restarting, then collected
            # two kills (one headshot) in the new life.
            update(restarted, 2, 1)
            snapshot = restarted.snapshot()
            self.assertEqual((snapshot.kills, snapshot.headshot_kills), (9, 4))
            self.assertEqual(snapshot.headshot_percent, 44)

    def test_stale_deathmatch_restart_cache_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "deathmatch-state.json"
            cache_path.write_text(json.dumps({
                "saved_at": time.time() - 60,
                "identity": "deathmatch|de_dust2",
                "phase": "live",
                "kills": 40,
                "headshots": 20,
                "life_kills": 4,
                "life_headshots": 2,
            }), encoding="utf-8")
            state = screen_app.CS2GameState(cache_path)
            state.update({
                "provider": {"steamid": "local-player"},
                "player": {
                    "steamid": "local-player", "team": "T", "activity": "playing",
                    "state": {"round_kills": 1, "round_killhs": 1},
                },
                "map": {"name": "de_dust2", "phase": "live", "mode": "deathmatch"},
            })
            self.assertEqual((state.snapshot().kills, state.snapshot().headshot_kills), (1, 1))

    def test_game_state_reports_the_local_players_ct_or_t_side(self) -> None:
        game_state = screen_app.CS2GameState()
        for team in ("CT", "T"):
            game_state.update({
                "provider": {"steamid": "local-player"},
                "player": {
                    "steamid": "local-player", "team": team, "activity": "playing",
                    "state": {"money": 3200, "defusekit": team == "CT"},
                },
                "map": {"phase": "live", "round_wins": {}},
            })
            snapshot = game_state.snapshot()
            self.assertEqual(snapshot.team, team)
            self.assertTrue(snapshot.in_match)

        game_state.update({
            "provider": {"steamid": "local-player"},
            "player": {
                "steamid": "local-player", "team": "T", "activity": "playing",
                "state": {"money": 3200},
            },
            "map": {"phase": "live", "round_wins": {}},
            "bomb": {"state": "defusing"},
        })
        self.assertTrue(game_state.snapshot().bomb_defusing)

    def test_death_packet_without_player_does_not_flash_back_to_ping_panel(self) -> None:
        game_state = screen_app.CS2GameState()
        game_state.update({
            "provider": {"steamid": "local-player"},
            "player": {
                "steamid": "local-player", "team": "T", "activity": "playing",
                "state": {"money": 2450},
            },
            "map": {
                "phase": "live", "mode": "scrimcomp2v2", "round_wins": {},
            },
        })
        game_state.update({
            "provider": {"steamid": "local-player"},
            "player": {},
            "map": {"phase": "live", "mode": "scrimcomp2v2", "round_wins": {}},
        })
        snapshot = game_state.snapshot()
        self.assertTrue(snapshot.in_match)
        self.assertEqual(snapshot.team, "T")
        self.assertEqual(snapshot.money, 2450)

    def test_spectated_player_packet_keeps_local_players_economy(self) -> None:
        game_state = screen_app.CS2GameState()
        game_state.update({
            "provider": {"steamid": "local-player"},
            "player": {
                "steamid": "local-player", "team": "CT", "activity": "playing",
                "state": {"money": 3100, "defusekit": True},
            },
            "map": {
                "phase": "live", "mode": "scrimcomp2v2", "round_wins": {},
            },
        })
        game_state.update({
            "provider": {"steamid": "local-player"},
            "player": {
                "steamid": "teammate", "team": "CT", "activity": "playing",
                "state": {"money": 700},
            },
            "map": {"phase": "live", "mode": "scrimcomp2v2", "round_wins": {}},
        })
        snapshot = game_state.snapshot()
        self.assertTrue(snapshot.in_match)
        self.assertEqual(snapshot.money, 3100)
        self.assertTrue(snapshot.has_defuse_kit)

    def test_bomb_timer_uses_cs2_phase_countdown_instead_of_packet_arrival_time(self) -> None:
        game_state = screen_app.CS2GameState()
        payload = {
            "provider": {"steamid": "local-player"},
            "player": {
                "steamid": "local-player", "team": "CT", "activity": "playing",
                "state": {"money": 3200},
            },
            "map": {"phase": "live", "round_wins": {}},
            "bomb": {"state": "planted"},
            "phase_countdowns": {"phase": "bomb", "phase_ends_in": "39.64"},
        }
        with patch("app.time.monotonic", side_effect=(100.0, 100.0)):
            game_state.update(payload)
            snapshot = game_state.snapshot()
        self.assertAlmostEqual(snapshot.bomb_seconds, 39.64, places=2)

    def test_bomb_timer_fallback_compensates_gsi_throttle_delay(self) -> None:
        game_state = screen_app.CS2GameState()
        payload = {
            "provider": {"steamid": "local-player"},
            "player": {
                "steamid": "local-player", "team": "T", "activity": "playing",
                "state": {"money": 3200},
            },
            "map": {"phase": "live", "round_wins": {}},
            "bomb": {"state": "planted"},
        }
        with patch("app.time.monotonic", side_effect=(200.0, 200.0)):
            game_state.update(payload)
            snapshot = game_state.snapshot()
        self.assertAlmostEqual(
            snapshot.bomb_seconds,
            40.0 - screen_app.GSI_THROTTLE_SECONDS,
            places=2,
        )

    def test_msfs_fast_refresh_budget_is_below_two_tenths(self) -> None:
        self.assertLessEqual(gui.MSFS_TELEMETRY_REFRESH_INTERVAL, 0.15)
        self.assertLessEqual(gui.MSFS_WORKER_INTERVAL, 0.10)

    def test_live_cs2_money_and_deathmatch_stats_refresh_with_gsi(self) -> None:
        self.assertLessEqual(gui.LIVE_GAME_WORKER_INTERVAL, 0.20)
        self.assertLessEqual(gui.BOMB_WORKER_INTERVAL, 0.20)

    def test_steam_matchmaking_ping_refresh_is_fresh_without_probe_spam(self) -> None:
        self.assertEqual(screen_app.STEAM_MATCHMAKING_PING_MODE, "direct_pop")
        self.assertLessEqual(screen_app.STEAM_PING_DATA_MAX_AGE_SECONDS, 10.0)
        self.assertGreaterEqual(screen_app.STEAM_PING_DATA_MAX_AGE_SECONDS, 5.0)
        self.assertLessEqual(screen_app.STEAM_PING_OUTPUT_INTERVAL_SECONDS, 1.0)

    def test_cs2_console_ping_parser_reads_game_table_and_excludes_falkenstein(self) -> None:
        parsed = screen_app.CS2ConsolePingMonitor.parse_ping_text(
            "vie: 54ms via direct route\n"
            "fra: 57ms via vie (front=54ms, back=3ms)\n"
            "ams: 63ms via direct route\n"
            "fsn: 59ms via dfra (front=57ms, back=2ms)\n"
        )
        self.assertEqual(parsed, {"vie": 54, "fra": 57, "ams": 63})

    def test_cs2_console_ping_parser_reads_latest_native_ping_location(self) -> None:
        parsed = screen_app.CS2ConsolePingMonitor.parse_ping_text(
            "[SteamNetSockets] Ping location: vie=53+5,dvie=55+5/53+5,"
            "fra=59+5/60+5,dfra=70+7/60+5,ams=64+6/64+5,waw=78+7/65+5\n"
        )
        self.assertEqual(parsed["vie"], 53)
        self.assertEqual(parsed["fra"], 59)
        self.assertEqual(parsed["ams"], 64)
        self.assertNotIn("dvie", parsed)

    def test_matchmaking_monitor_prefers_cs2_values_and_falls_back_cleanly(self) -> None:
        exact = [screen_app.RelayPing("vie", "Viyana", "", 54.0)]
        fallback = [screen_app.RelayPing("fra", "Frankfurt", "", 60.0)]
        monitor = screen_app.MatchmakingPingMonitor()
        monitor.exact = SimpleNamespace(read=lambda: exact, close=lambda: None)
        monitor.fallback = SimpleNamespace(read=lambda: fallback, close=lambda: None)
        self.assertEqual(monitor.read(), exact)
        monitor.exact = SimpleNamespace(read=lambda: [], close=lambda: None)
        self.assertEqual(monitor.read(), fallback)

    def test_cs2_console_log_launch_option_preserves_existing_user_options(self) -> None:
        original = '''"Software"\n{\n\t"730"\n\t{\n\t\t"LastPlayed" "1"\n\t\t"LaunchOptions" "+exec autoexec.cfg -novid"\n\t\t"cloud" { "state" "ok" }\n\t}\n}\n'''
        updated, changed = screen_app.add_cs2_console_log_launch_option(original)
        self.assertTrue(changed)
        self.assertIn('"LaunchOptions" "+exec autoexec.cfg -novid -condebug"', updated)
        unchanged, changed_again = screen_app.add_cs2_console_log_launch_option(updated)
        self.assertFalse(changed_again)
        self.assertEqual(unchanged, updated)

    def test_cs2_console_log_setup_removes_unsupported_vconsole_switch(self) -> None:
        original = '''"730" { "LaunchOptions" "-novid -vconsole" }'''
        updated, changed = screen_app.add_cs2_console_log_launch_option(original)
        self.assertTrue(changed)
        self.assertNotIn("-vconsole", updated)
        self.assertIn("-condebug", updated)

    def test_bomb_timer_uses_small_fast_regions_instead_of_full_panel(self) -> None:
        self.assertLessEqual(gui.BOMB_WORKER_INTERVAL, 0.20)
        full_area = (
            (gui.BOTTOM_REGION[2] - gui.BOTTOM_REGION[0])
            * (gui.BOTTOM_REGION[3] - gui.BOTTOM_REGION[1])
        )
        live_area = sum(
            (right - left) * (bottom - top)
            for left, top, right, bottom in (gui.BOMB_TIMER_REGION, gui.BOMB_BAR_REGION)
        )
        self.assertLess(live_area, full_area / 2)

    def test_media_updates_stay_within_serial_frame_budget(self) -> None:
        regions = (
            screen_app.MEDIA_VIS_REGION,
            screen_app.MEDIA_VIS_REGION_WITH_BATTERY,
            *gui.FAST_VALUE_BOXES,
            *gui.MEDIA_GAUGE_REGIONS,
            *(
                region
                for group in gui.MEDIA_RING_REGION_GROUPS
                for region in group
            ),
        )
        areas = [(right - left) * (bottom - top) for left, top, right, bottom in regions]
        self.assertLessEqual(max(areas), 9000)
        self.assertLessEqual(areas[0], 5100)
        self.assertLessEqual(max(
            (right - left) * (bottom - top)
            for left, top, right, bottom in gui.FAST_VALUE_BOXES
        ), 9000)

    def test_sensor_and_percent_fonts_remain_readable(self) -> None:
        self.assertGreaterEqual(screen_app.FONTS["sensor"].size, 21)
        self.assertGreaterEqual(screen_app.FONTS["gauge_unit"].size, 17)
        self.assertGreaterEqual(screen_app.FONTS["detail"].size, 12)
        self.assertLess(screen_app.FONTS["ram_sensor"].size, screen_app.FONTS["sensor"].size)
        self.assertGreaterEqual(screen_app.FONTS["weather_city"].size, 12)

    def test_ram_usage_is_rounded_before_rendering(self) -> None:
        with patch.object(screen_app, "draw_open_ring") as ring:
            screen_app.render_screen(
                self.sample_metrics() | {"ram_used": 27.4},
                screen_app.GameSnapshot(), False,
            )
        self.assertEqual(ring.call_args_list[2].args[7], "27 GB")

    def test_unchanged_value_tile_does_not_consume_a_spectrum_cycle(self) -> None:
        frame = Image.new("RGB", (480, 320), "black")
        region = gui.FAST_VALUE_BOXES[0]
        self.assertFalse(gui._region_has_changed(frame, frame.copy(), region))
        changed = frame.copy()
        changed.putpixel((region[0] + 1, region[1] + 1), (255, 255, 255))
        self.assertTrue(gui._region_has_changed(changed, frame, region))

    def test_tight_changed_regions_send_only_exact_changed_pixels(self) -> None:
        sent = Image.new("RGB", (100, 100), "black")
        current = sent.copy()
        current.putpixel((12, 15), (255, 255, 255))
        current.putpixel((80, 81), (255, 255, 255))
        regions = ((0, 0, 50, 50), (50, 50, 100, 100))
        self.assertEqual(
            gui._tight_changed_regions(current, sent, regions),
            [(12, 15, 13, 16), (80, 81, 81, 82)],
        )
        self.assertEqual(gui._tight_changed_regions(sent, sent.copy(), regions), [])

    def test_top_dirty_tiles_cover_hardware_area_once(self) -> None:
        covered_area = sum(
            (right - left) * (bottom - top)
            for left, top, right, bottom in gui.TOP_DIRTY_TILES
        )
        self.assertEqual(covered_area, 480 * 160)
        self.assertLessEqual(max(
            (right - left) * (bottom - top)
            for left, top, right, bottom in gui.TOP_DIRTY_TILES
        ), 4200)
        for y in range(160):
            for x in range(480):
                self.assertEqual(sum(
                    left <= x < right and top <= y < bottom
                    for left, top, right, bottom in gui.TOP_DIRTY_TILES
                ), 1)

    def test_small_load_change_uses_far_less_than_full_top_transfer(self) -> None:
        old_metrics = self.sample_metrics()
        new_metrics = dict(old_metrics)
        new_metrics["cpu_load"] = min(
            100.0, float(old_metrics["cpu_load"]) + 1.0,
        )
        old_frame = screen_app.render_screen(
            old_metrics, screen_app.GameSnapshot(), False,
        )
        new_frame = screen_app.render_screen(
            new_metrics, screen_app.GameSnapshot(), False,
        )
        regions = gui._tight_changed_regions(
            new_frame, old_frame, gui.TOP_DIRTY_TILES,
        )
        transferred_area = sum(
            (right - left) * (bottom - top)
            for left, top, right, bottom in regions
        )
        self.assertLess(transferred_area, (480 * 160) // 5)

    def test_dirty_tile_transfer_reconstructs_complete_hardware_area(self) -> None:
        old_metrics = self.sample_metrics()
        new_metrics = dict(old_metrics)
        new_metrics.update({
            "cpu_load": 93.0, "cpu_temp": 86.0, "cpu_freq": 5.05,
            "gpu_load": 84.0, "gpu_temp": 79.0, "gpu_memory": 9472.0,
            "ram_load": 88.0, "ram_used": 28.7,
        })
        old_frame = screen_app.render_screen(
            old_metrics, screen_app.GameSnapshot(), False, animation_time=0.0,
        )
        new_frame = screen_app.render_screen(
            new_metrics, screen_app.GameSnapshot(), False, animation_time=0.0,
        )
        patched = old_frame.copy()
        regions = gui._tight_changed_regions(
            new_frame, old_frame, gui.TOP_DIRTY_TILES,
        )
        for region in regions:
            patched.paste(new_frame.crop(region), region)
        self.assertIsNone(ImageChops.difference(
            patched.crop(gui.TOP_REGION), new_frame.crop(gui.TOP_REGION),
        ).getbbox())

    def test_safe_value_tiles_fully_replace_old_temperature_glyphs(self) -> None:
        old_metrics = self.sample_metrics()
        new_metrics = dict(old_metrics)
        new_metrics.update({"cpu_temp": 45.0, "gpu_temp": 46.0, "ram_used": 27.4})
        old_frame = screen_app.render_screen(
            old_metrics, screen_app.GameSnapshot(), False,
        )
        new_frame = screen_app.render_screen(
            new_metrics, screen_app.GameSnapshot(), False,
        )
        patched = old_frame.copy()
        for region in gui.FAST_VALUE_BOXES:
            patched.paste(new_frame.crop(region), region)
        self.assertIsNone(ImageChops.difference(
            patched.crop(gui.TOP_REGION), new_frame.crop(gui.TOP_REGION),
        ).getbbox())

    def test_fast_media_patch_matches_full_frame_spectrum(self) -> None:
        spectrum = AudioSpectrumSnapshot(
            bars=tuple(index / 15 for index in range(16)), active=True,
        )
        colors = dict(gui.PALETTES["Kuzey"])
        full = screen_app.render_screen(
            self.sample_metrics(), screen_app.GameSnapshot(), False, colors,
            media_snapshot=MediaSnapshot(platform="Chrome", title="Test", status="playing"),
            audio_spectrum=spectrum,
        )
        patch = screen_app.render_media_visual_patch(spectrum, colors)
        self.assertIsNone(ImageChops.difference(full.crop(screen_app.MEDIA_VIS_REGION), patch).getbbox())

    def test_media_title_must_stabilise_before_repainting_panel(self) -> None:
        monitor = MediaSessionMonitor()
        first = MediaSnapshot(
            platform="Chrome", source_id="chrome.exe", title="İlk",
            artist="Sanatçı", status="playing",
        )
        changed = MediaSnapshot(
            platform="Chrome", source_id="chrome.exe", title="Yeni",
            artist="Sanatçı", status="playing",
        )
        monitor._publish(first)
        with patch("media_runtime.time.monotonic", side_effect=(10.0, 10.8)):
            monitor._publish(changed)
            self.assertEqual(monitor.snapshot().title, "İlk")
            monitor._publish(changed)
        self.assertEqual(monitor.snapshot().title, "Yeni")

    @staticmethod
    def sample_metrics() -> dict[str, float]:
        return {
            "cpu_load": 24.0, "cpu_temp": 46.0, "cpu_freq": 4.6,
            "gpu_load": 18.0, "gpu_temp": 42.0, "gpu_memory": 920.0,
            "ram_load": 52.0, "ram_used": 16.5,
        }

    def test_audio_analyzer_creates_frequency_bands_from_live_pcm_shape(self) -> None:
        monitor = AudioSpectrumMonitor()
        rate = 48000
        timeline = np.arange(512, dtype=np.float32) / rate
        mono = np.sin(2 * np.pi * 440 * timeline) * 0.35
        stereo = np.column_stack((mono, mono))
        pcm = (stereo * 32767).astype(np.int16).tobytes()
        bars, level = monitor._analyze(pcm, 2, rate)
        self.assertEqual(len(bars), 16)
        self.assertGreater(float(np.max(bars)), 0.5)
        self.assertGreater(level, 0.5)

    def test_audio_analyzer_interpolates_empty_low_frequency_bands(self) -> None:
        monitor = AudioSpectrumMonitor()
        rate = 48000
        timeline = np.arange(512, dtype=np.float32) / rate
        mono = np.sin(2 * np.pi * 90 * timeline) * 0.5
        stereo = np.column_stack((mono, mono))
        pcm = (stereo * 32767).astype(np.int16).tobytes()
        bars, _level = monitor._analyze(pcm, 2, rate)
        self.assertGreater(float(bars[0]), 0.01)
        self.assertGreater(float(bars[2]), 0.01)

    def test_browser_media_uses_service_name_when_detectable(self) -> None:
        kick = MediaSnapshot(
            platform="Chrome", title="pazalt Stream - Watch Live on Kick",
            status="playing",
        )
        unknown = MediaSnapshot(platform="Chrome", title="Yerel ses", status="playing")
        self.assertEqual(media_service_name(kick), "Kick")
        self.assertEqual(media_service_name(unknown), "Medya")

    def test_unsupported_media_emoji_are_removed_without_harming_turkish(self) -> None:
        self.assertEqual(
            sanitise_media_text("  Şu an çalıyor 🎧  ·  Çağrı Şinci 🔥 "),
            "Şu an çalıyor · Çağrı Şinci",
        )
        self.assertEqual(sanitise_media_text("Müzik\ufe0f  Başlığı"), "Müzik Başlığı")

    def test_media_panel_uses_metadata_and_never_overrides_game_mode(self) -> None:
        media = MediaSnapshot(
            platform="Chrome", title="Test videosu", artist="Test kanalı",
            status="playing", source_id="chrome.exe", updated_at=1.0,
        )
        spectrum = AudioSpectrumSnapshot(
            bars=tuple(index / 15 for index in range(16)), level=0.8,
            active=True, input_latency_ms=23.0, updated_at=1.0,
        )
        idle = screen_app.render_screen(self.sample_metrics(), screen_app.GameSnapshot(), False)
        media_frame = screen_app.render_screen(
            self.sample_metrics(), screen_app.GameSnapshot(), False,
            media_snapshot=media, audio_spectrum=spectrum,
        )
        self.assertIsNone(
            ImageChops.difference(idle.crop((0, 0, 480, 160)), media_frame.crop((0, 0, 480, 160))).getbbox(),
        )
        self.assertIsNotNone(
            ImageChops.difference(idle.crop((14, 160, 467, 307)), media_frame.crop((14, 160, 467, 307))).getbbox(),
        )

        game_state = screen_app.GameSnapshot(in_match=True, money=3000, next_win_money=6500)
        game_plain = screen_app.render_screen(self.sample_metrics(), game_state, True)
        game_with_media = screen_app.render_screen(
            self.sample_metrics(), game_state, True,
            media_snapshot=media, audio_spectrum=spectrum,
        )
        self.assertIsNone(ImageChops.difference(game_plain, game_with_media).getbbox())

    def test_platform_name_is_readable_for_common_media_sources(self) -> None:
        self.assertEqual(platform_name("SpotifyAB.SpotifyMusic_xxx!Spotify"), "Spotify")
        self.assertEqual(platform_name("chrome.exe"), "Chrome")
        self.assertEqual(platform_name("vlc.exe"), "VLC")

    def test_component_scales_use_separate_limits_and_shared_theme_tokens(self) -> None:
        colors = {
            "cpu": "#20C080", "warning": "#E0A020",
            "danger": "#E04040", "dim": "#606060",
        }
        for component, normal_end, warning_end, critical_end in (
            ("cpu", 75, 92, 100),
            ("gpu", 70, 80, 95),
            ("ram", 70, 85, 100),
        ):
            self.assertEqual(screen_app.load_color(normal_end, component, colors), colors["cpu"])
            self.assertEqual(
                screen_app.load_color(warning_end, component, colors).lower(),
                colors["warning"].lower(),
            )
            self.assertEqual(
                screen_app.load_color(critical_end, component, colors).lower(),
                colors["danger"].lower(),
            )
        for component, normal_end, warning_end, critical_start, critical_end in (
            ("cpu", 55, 70, 85, 95),
            ("gpu", 50, 65, 78, 90),
        ):
            self.assertEqual(screen_app.temperature_color(normal_end, component, colors), colors["cpu"])
            self.assertEqual(
                screen_app.temperature_color(warning_end, component, colors).lower(),
                colors["warning"].lower(),
            )
            self.assertEqual(
                screen_app.temperature_color((warning_end + critical_start) / 2, component, colors).lower(),
                colors["warning"].lower(),
            )
            self.assertEqual(
                screen_app.temperature_color(critical_end, component, colors).lower(),
                colors["danger"].lower(),
            )
            self.assertEqual(screen_app.temperature_color(None, component, colors), colors["dim"])
        self.assertNotEqual(
            screen_app.temperature_color(80, "cpu", colors),
            screen_app.temperature_color(80, "gpu", colors),
        )

    def test_temperature_unit_animation_only_activates_when_attention_is_needed(self) -> None:
        colors = {
            "cpu": "#20C080", "warning": "#E0A020", "danger": "#E04040",
            "dim": "#606060", "bg": "#101010", "text": "#F0F0F0",
        }
        self.assertEqual(
            screen_app.temperature_unit_color(50, "cpu", colors, 0.0),
            screen_app.temperature_unit_color(50, "cpu", colors, 0.4),
        )
        self.assertNotEqual(
            screen_app.temperature_unit_color(65, "cpu", colors, 0.0),
            screen_app.temperature_unit_color(65, "cpu", colors, 0.4),
        )
        self.assertNotEqual(
            screen_app.temperature_unit_color(90, "cpu", colors, 0.1),
            screen_app.temperature_unit_color(90, "cpu", colors, 0.6),
        )

    def test_load_value_blinks_when_each_component_enters_critical_transition(self) -> None:
        colors = {
            "cpu": "#20C080", "warning": "#E0A020", "danger": "#E04040",
            "dim": "#606060", "bg": "#101010", "text": "#F0F0F0",
        }
        for component, critical_start in (("cpu", 92), ("gpu", 80), ("ram", 85)):
            self.assertIsNone(screen_app.load_alert_color(critical_start, component, colors, 0.1))
            self.assertNotEqual(
                screen_app.load_alert_color(critical_start + 0.1, component, colors, 0.1),
                screen_app.load_alert_color(critical_start + 0.1, component, colors, 0.6),
            )

    def test_rendered_load_values_blink_without_animating_below_threshold(self) -> None:
        def frame(cpu: float, gpu: float, ram: float, animation_time: float) -> Image.Image:
            return screen_app.render_screen(
                {
                    "cpu_load": cpu, "cpu_temp": 40.0, "cpu_freq": 4.5,
                    "gpu_load": gpu, "gpu_temp": 40.0, "gpu_memory": 800.0,
                    "ram_load": ram, "ram_used": 16.0,
                },
                screen_app.GameSnapshot(), False, animation_time=animation_time,
            )

        below_visible = frame(92.0, 80.0, 85.0, 0.1)
        below_dim = frame(92.0, 80.0, 85.0, 0.6)
        self.assertIsNone(ImageChops.difference(below_visible, below_dim).getbbox())

        critical_visible = frame(93.0, 81.0, 86.0, 0.1)
        critical_dim = frame(93.0, 81.0, 86.0, 0.6)
        for box in ((48, 51, 119, 101), (208, 51, 279, 101), (368, 51, 439, 101)):
            self.assertIsNotNone(
                ImageChops.difference(
                    critical_visible.crop(box), critical_dim.crop(box),
                ).getbbox(),
            )

        # The ring arc itself remains steady; only the central value flashes.
        self.assertIsNone(
            ImageChops.difference(
                critical_visible.crop((20, 35, 48, 120)),
                critical_dim.crop((20, 35, 48, 120)),
            ).getbbox(),
        )

    def test_temperature_number_blinks_when_critical_transition_begins(self) -> None:
        colors = {
            "cpu": "#20C080", "warning": "#E0A020", "danger": "#E04040",
            "dim": "#606060", "bg": "#101010", "text": "#F0F0F0",
        }
        self.assertEqual(screen_app.temperature_number_color(85, "cpu", colors), colors["text"])
        self.assertNotEqual(
            screen_app.temperature_number_color(85.1, "cpu", colors, 0.1),
            screen_app.temperature_number_color(85.1, "cpu", colors, 0.6),
        )
        self.assertEqual(screen_app.temperature_number_color(78, "gpu", colors), colors["text"])
        self.assertNotEqual(
            screen_app.temperature_number_color(78.1, "gpu", colors, 0.1),
            screen_app.temperature_number_color(78.1, "gpu", colors, 0.6),
        )

    def test_hardware_names_are_short_and_portable(self) -> None:
        self.assertEqual(screen_app._short_cpu_name("AMD Ryzen 7 9800X3D 8-Core Processor"), "9800X3D")
        self.assertEqual(screen_app._short_gpu_name("NVIDIA GeForce RTX 4070 Ti"), "RTX 4070 Ti")
        self.assertEqual(screen_app.normalise_hardware_label("  Çok   Uzun Donanım Adı  "), "Çok Uzun Don")

    def test_task_manager_window_covers_one_second(self) -> None:
        monitor = screen_app.WindowsTaskManagerMonitor(sample_interval=0.25)
        self.assertEqual(monitor._cpu_window.maxlen, 4)
        self.assertEqual(monitor._gpu_window.maxlen, 4)

    def test_gpu_process_rows_are_summed_per_engine_then_busiest_is_used(self) -> None:
        rows = [
            ("pid_10_luid_0x00000000_0x00000001_phys_0_eng_0_engtype_3D", 21.0),
            ("pid_20_luid_0x00000000_0x00000001_phys_0_eng_0_engtype_3D", 14.0),
            ("pid_20_luid_0x00000000_0x00000001_phys_0_eng_1_engtype_Copy", 9.0),
        ]
        self.assertEqual(screen_app.WindowsTaskManagerMonitor._gpu_engine_total(rows), 35.0)

    def test_gpu_counter_rejects_invalid_rows_and_clamps_total(self) -> None:
        rows = [
            ("invalid", 80.0),
            ("pid_1_luid_0x0_0x1_phys_0_eng_0_engtype_3D", -5.0),
            ("pid_2_luid_0x0_0x1_phys_0_eng_0_engtype_3D", 140.0),
        ]
        self.assertEqual(screen_app.WindowsTaskManagerMonitor._gpu_engine_total(rows), 100.0)

    def test_only_selected_discrete_gpu_is_counted(self) -> None:
        rows = [
            ("pid_1_luid_0x0_0xaaaa_phys_0_eng_0_engtype_3D", 72.0),
            ("pid_2_luid_0x0_0xbbbb_phys_0_eng_0_engtype_3D", 18.0),
        ]
        value = screen_app.WindowsTaskManagerMonitor._gpu_engine_total(
            rows, "luid_0x0_0xbbbb",
        )
        self.assertEqual(value, 18.0)

    def test_vram_uses_only_selected_discrete_adapter(self) -> None:
        rows = [
            ("luid_0x0_0xaaaa_phys_0", 64 * 1024 ** 2),
            ("luid_0x0_0xbbbb_phys_0", 768 * 1024 ** 2),
        ]
        value = screen_app.WindowsTaskManagerMonitor._dedicated_memory_mb(
            rows, "luid_0x0_0xbbbb",
        )
        self.assertEqual(value, 768.0)

    def test_metrics_prefers_task_manager_counters_and_task_manager_ram_formula(self) -> None:
        memory = SimpleNamespace(total=1000, available=375, used=700, percent=70.0)
        with patch.object(screen_app.psutil, "cpu_percent", return_value=99.0), patch.object(
            screen_app.psutil, "virtual_memory", return_value=memory
        ), patch.object(screen_app, "get_gpu_metrics", return_value=(88.0, 51.0, 2048.0)), patch.object(
            screen_app.TASK_MANAGER_MONITOR, "read", return_value=(12.5, 34.5, 4.65, 1536.0)
        ), patch.object(
            screen_app.SENSOR_MONITOR, "read", return_value={"cpu_temp": 48.0, "cpu_freq": 4.2}
        ):
            metrics = screen_app.get_metrics()
        self.assertEqual(metrics["cpu_load"], 12.5)
        self.assertEqual(metrics["gpu_load"], 34.5)
        self.assertEqual(metrics["cpu_freq"], 4.65)
        self.assertEqual(metrics["gpu_memory"], 1536.0)
        self.assertEqual(metrics["ram_load"], 62.5)

    def test_stale_elevated_sensor_cache_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sensors.json"
            path.write_text(json.dumps({
                "timestamp": 1.0, "cpu_temp": 50.0, "cpu_freq": 4.5,
            }), encoding="utf-8")
            self.assertIsNone(screen_app.EmbeddedSensorMonitor._read_elevated_cache(path))

    def test_fresh_elevated_sensor_cache_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sensors.json"
            path.write_text(json.dumps({
                "timestamp": time.time(), "cpu_temp": 51.25, "cpu_freq": 4.35,
            }), encoding="utf-8")
            self.assertEqual(screen_app.EmbeddedSensorMonitor._read_elevated_cache(path), {
                "cpu_temp": 51.25, "cpu_freq": 4.35,
            })


class LocalizationScenarios(unittest.TestCase):
    @staticmethod
    def metrics() -> dict[str, float]:
        return {
            "cpu_load": 48.0, "cpu_temp": 63.0, "cpu_freq": 5.05,
            "gpu_load": 76.0, "gpu_temp": 71.0, "gpu_memory": 8640.0,
            "ram_load": 64.0, "ram_used": 20.2,
        }

    def rendered_text(self, **kwargs) -> set[str]:
        seen: list[str] = []
        original = ImageDraw.ImageDraw.text

        def capture(draw, xy, text, *args, **options):
            seen.append(str(text))
            return original(draw, xy, text, *args, **options)

        with patch.object(ImageDraw.ImageDraw, "text", new=capture):
            screen_app.render_screen(
                self.metrics(), kwargs.pop("snapshot", screen_app.GameSnapshot()),
                kwargs.pop("panel_active", False), gui.PALETTES["Kuzey"],
                language="en", **kwargs,
            )
        return set(seen)

    def test_english_is_applied_to_every_mini_display_mode(self) -> None:
        idle = self.rendered_text(
            weather_readings=(WeatherReading("Ankara", 22.0, 0),),
            battery_devices=(BatteryDevice("k", "Wireless Keyboard", 73, "keyboard"),),
        )
        self.assertIn("in use", idle)

        media = self.rendered_text(
            media_snapshot=MediaSnapshot(),
            audio_spectrum=AudioSpectrumSnapshot(
                bars=(0.5,) * 16, level=0.5, active=True,
            ),
        )
        self.assertIn("System audio", media)
        self.assertIn("Audio playing", media)

        matchmaking = self.rendered_text(
            panel_active=True,
            matchmaking_pings=[
                screen_app.RelayPing("fra", "Frankfurt", "127.0.0.1", 57.0)
            ],
        )
        self.assertIn("Before matchmaking", matchmaking)
        self.assertIn("Valve route", matchmaking)

        economy = self.rendered_text(
            panel_active=True,
            snapshot=screen_app.GameSnapshot(
                connected=True, in_match=True, team="CT", money=4250,
                next_win_money=7500, next_loss_money=6150, loss_bonus=1900,
            ),
        )
        self.assertIn("Defence economy", economy)
        self.assertIn("Now", economy)
        self.assertIn("If you win", economy)

        bomb_ct = self.rendered_text(
            panel_active=True,
            snapshot=screen_app.GameSnapshot(
                connected=True, in_match=True, team="CT", money=4250,
                has_defuse_kit=True, bomb_planted=True, bomb_seconds=8.0,
            ),
        )
        self.assertIn("CT · Defuse the C4", bomb_ct)
        self.assertIn("Money", bomb_ct)

        bomb_t = self.rendered_text(
            panel_active=True,
            snapshot=screen_app.GameSnapshot(
                connected=True, in_match=True, team="T", money=4250,
                bomb_planted=True, bomb_seconds=4.0,
            ),
        )
        self.assertIn("T · Defend the bomb", bomb_t)

        deathmatch = self.rendered_text(
            panel_active=True,
            snapshot=screen_app.GameSnapshot(
                connected=True, in_match=True, game_mode="deathmatch",
                kills=42, headshot_kills=21, headshot_percent=50,
            ),
        )
        self.assertIn("Deathmatch", deathmatch)
        self.assertIn("KILLS", deathmatch)
        self.assertIn("HEADSHOTS", deathmatch)

        flight = FlightTelemetry(
            altitude_ft=12540.0, airspeed_kt=146.0,
            vertical_speed_fpm=820.0, heading_deg=278.0,
            connected=True, updated_at=1.0,
        )
        for layout in ("classic", "cockpit"):
            msfs = self.rendered_text(
                msfs_active=True, flight_telemetry=flight, msfs_layout=layout,
            )
            self.assertIn("Altitude", msfs)
            self.assertIn("Airspeed", msfs)
            self.assertIn("Vertical speed", msfs)
            self.assertIn("Heading", msfs)


class FastEvent:
    def __init__(self) -> None:
        self.flag = False

    def clear(self) -> None:
        self.flag = False

    def set(self) -> None:
        self.flag = True

    def is_set(self) -> bool:
        return self.flag

    def wait(self, _timeout: float | None = None) -> bool:
        return self.flag


class TrackingEvent(FastEvent):
    def __init__(self) -> None:
        super().__init__()
        self.set_count = 0

    def set(self) -> None:
        self.set_count += 1
        super().set()


class FakeGSI:
    def __init__(self, _state) -> None:
        pass

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass


class FakeMonitor:
    def __init__(self, value) -> None:
        self.value = value

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def snapshot(self):
        return self.value


class FakeScreen:
    def __init__(
        self, stop_event: FastEvent, fail_show: bool = False,
        fail_power_off: bool = False,
    ) -> None:
        self.stop_event = stop_event
        self.fail_show = fail_show
        self.fail_power_off = fail_power_off
        self.show_count = 0
        self.power_off_count = 0
        self.shown_images = []
        self.events: list[str] = []
        self.closed = False

    def configure(self, *_args) -> None:
        pass

    def show(self, image) -> None:
        self.show_count += 1
        self.events.append("show")
        self.shown_images.append(image.copy())
        if self.fail_show:
            raise OSError("USB disconnected")
        self.stop_event.set()

    def show_regions(self, _image, _regions) -> None:
        self.stop_event.set()

    def power_off(self) -> None:
        self.power_off_count += 1
        self.events.append("power_off")
        if self.fail_power_off:
            raise OSError("screen-off command failed")

    def close(self) -> None:
        self.events.append("close")
        self.closed = True


class UiAndConnectionScenarios(unittest.TestCase):
    def test_sensor_task_uses_a_stable_per_user_host_path(self) -> None:
        executable, arguments = gui._sensor_task_action()
        self.assertEqual(Path(executable), gui.SENSOR_HOST_PATH)
        self.assertEqual(arguments, "--sensor-file-bridge")
        self.assertNotEqual(Path(executable), Path(sys.executable))

    def test_protected_sensor_host_copy_is_installed_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "portable.exe"
            target = root / "stable" / "SabasakalSensorHost.exe"
            source.write_bytes(b"version-one")
            self.assertTrue(gui._install_sensor_host_copy(source, target))
            self.assertEqual(target.read_bytes(), b"version-one")
            self.assertTrue(gui._sensor_host_is_current(source, target))

            source.write_bytes(b"version-two-is-new")
            stat = source.stat()
            os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
            self.assertFalse(gui._sensor_host_is_current(source, target))
            self.assertTrue(gui._install_sensor_host_copy(source, target))
            self.assertEqual(target.read_bytes(), b"version-two-is-new")

    def test_stale_sensor_cache_restarts_existing_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "elevated-sensors.json"
            completed = SimpleNamespace(returncode=0)
            with patch.object(gui.subprocess, "run", return_value=completed) as run:
                self.assertTrue(gui.nudge_sensor_task_if_stale(missing))
                run.assert_called_once()

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def make_panel(self) -> gui.ControlPanel:
        with patch.object(gui, "load_settings", side_effect=default_settings), patch.object(
            gui, "get_auto_hardware_labels", return_value={
                "cpu": "9800X3D", "gpu": "RTX 4070 Ti", "ram": "32 GB RAM",
            }
        ), patch.object(
            gui, "get_metrics", return_value={}
        ):
            return gui.ControlPanel()

    def test_hardware_fields_have_safe_limit_and_automatic_placeholders(self) -> None:
        panel = self.make_panel()
        self.assertEqual({key: field.maxLength() for key, field in panel.hardware_inputs.items()}, {
            "cpu": 12, "gpu": 12, "ram": 12,
        })
        self.assertEqual(panel.hardware_inputs["gpu"].placeholderText(), "RTX 4070 Ti")
        self.assertTrue(all(field.height() == 34 for field in panel.hardware_inputs.values()))
        panel.hardware_inputs["cpu"].setText("123456789012345")
        self.assertEqual(panel.hardware_inputs["cpu"].text(), "123456789012")
        panel.close()

    def test_palette_controls_edit_the_shared_scale(self) -> None:
        panel = self.make_panel()
        self.assertEqual(set(panel.swatches), {"cpu", "warning", "danger", "bg", "text", "muted"})
        self.assertEqual(panel.swatches["cpu"].text(), "Normal")
        self.assertEqual(panel.swatches["warning"].text(), "Uyarı")
        self.assertEqual(panel.swatches["danger"].text(), "Kritik")
        self.assertEqual(panel.swatches["text"].text(), "Ana yazı")
        self.assertEqual(panel.swatches["muted"].text(), "İkincil yazı")
        self.assertIn("qlineargradient", panel.swatches["cpu"].styleSheet())
        self.assertIn("stop:0.79", panel.swatches["cpu"].styleSheet())
        self.assertEqual(panel.autostart_check.text(), "Windows açıldığında çalıştır")
        panel.close()

    def test_msfs_layout_selector_lists_both_designs(self) -> None:
        panel = self.make_panel()
        choices = {
            panel.msfs_layout_combo.itemText(index)
            for index in range(panel.msfs_layout_combo.count())
        }
        self.assertEqual(choices, set(gui.MSFS_LAYOUTS))
        self.assertEqual(panel.msfs_layout_combo.currentText(), "Kokpit şeridi")
        self.assertGreaterEqual(panel.msfs_layout_combo.width(), 190)
        self.assertEqual(panel.msfs_layout_combo.height(), 34)
        panel.msfs_layout_combo.setCurrentText("Klasik")
        self.assertEqual(panel._snapshot_settings()["msfs_layout"], "classic")
        panel.close()

    def test_weather_cities_are_editable_without_coordinates(self) -> None:
        panel = self.make_panel()
        self.assertEqual(
            [field.text() for field in panel.weather_city_inputs],
            ["Batman", "Muğla", "İstanbul"],
        )
        for field, city in zip(panel.weather_city_inputs, ("Ankara", "İzmir", "Antalya")):
            field.setText(city)
        self.assertEqual(
            panel._snapshot_settings()["weather_cities"],
            ["Ankara", "İzmir", "Antalya"],
        )
        panel.close()

    def test_lowercase_city_names_use_turkish_title_casing(self) -> None:
        self.assertEqual(
            normalise_city_names(["ankara", "istanbul", "izmir"]),
            ("Ankara", "İstanbul", "İzmir"),
        )
        panel = self.make_panel()
        panel.weather_city_inputs[0].setText("muğla")
        panel._normalise_city_field(panel.weather_city_inputs[0])
        self.assertEqual(panel.weather_city_inputs[0].text(), "Muğla")
        panel.close()

    def test_rotation_is_a_clear_zero_or_180_degree_list(self) -> None:
        panel = self.make_panel()
        self.assertEqual(
            [panel.rotation_combo.itemText(index) for index in range(panel.rotation_combo.count())],
            ["0°", "180°"],
        )
        panel.rotation_combo.setCurrentIndex(1)
        self.assertEqual(panel._snapshot_settings()["rotation"], "180")
        panel.close()

    def test_automatic_brightness_is_clear_and_disables_manual_slider(self) -> None:
        panel = self.make_panel()
        self.assertIn("07.00", panel.auto_brightness_check.toolTip())
        self.assertIn("19.00", panel.auto_brightness_check.toolTip())
        self.assertIn("sürgü", panel.auto_brightness_info.toolTip())
        self.assertEqual(panel.auto_brightness_info.text(), "i")
        panel.auto_brightness_check.setChecked(True)
        self.assertFalse(panel.brightness_slider.isEnabled())
        self.assertIn("QSlider::handle:horizontal:disabled", panel.styleSheet())
        self.assertIn(panel.brightness_value.text(), {"%30", "%90"})
        self.assertTrue(panel._snapshot_settings()["auto_brightness"])
        panel.auto_brightness_check.setChecked(False)
        self.assertTrue(panel.brightness_slider.isEnabled())
        panel.close()

    def test_control_panel_brand_and_primary_actions_are_turkish(self) -> None:
        panel = self.make_panel()
        self.assertEqual(gui.APP_DISPLAY_NAME, "Sabasakal Mini Ekran 3,5″")
        self.assertIn(gui.APP_DISPLAY_NAME, panel.windowTitle())
        self.assertEqual(panel.start_button.text(), "Kaydet ve başlat")
        self.assertEqual(panel.save_button.text(), "Yalnızca kaydet")
        self.assertEqual(panel.reset_button.text(), "Varsayılana dön")
        self.assertIn("Tek renk", gui.PALETTES)
        self.assertNotIn("Mono", gui.PALETTES)
        panel.close()

    def test_language_selector_translates_the_complete_control_panel(self) -> None:
        panel = self.make_panel()
        with patch.object(panel, "save_settings", return_value=True):
            panel.language_combo.setCurrentIndex(panel.language_combo.findData("en"))
        self.app.processEvents()
        self.assertEqual(panel.language, "en")
        self.assertIn("Sabasakal Mini Screen", panel.windowTitle())
        self.assertIn("Settings", panel.windowTitle())
        self.assertEqual(panel.start_button.text(), "Save and start")
        self.assertEqual(panel.stop_button.text(), "Stop")
        self.assertEqual(panel.save_button.text(), "Save only")
        self.assertEqual(panel.reset_button.text(), "Restore defaults")
        self.assertEqual(panel.autostart_check.text(), "Start with Windows")
        self.assertEqual(panel.swatches["warning"].text(), "Warning")
        self.assertEqual(panel.swatches["danger"].text(), "Critical")
        self.assertEqual(panel.preview_buttons["daily"].text(), "Daily")
        self.assertEqual(panel.preview_buttons["deathmatch"].text(), "Deathmatch")
        self.assertEqual(panel.preview_buttons["wireless"].text(), "Wireless")
        self.assertEqual(panel.palette_combo.currentText(), "North")
        self.assertEqual(panel.palette_combo.currentData(), "Kuzey")
        self.assertEqual(panel.msfs_layout_combo.currentText(), "Cockpit strip")
        self.assertEqual(panel.msfs_layout_combo.currentData(), "cockpit")
        self.assertEqual(panel.weather_city_inputs[0].placeholderText(), "City 1")
        snapshot = panel._snapshot_settings()
        self.assertEqual(snapshot["language"], "en")
        self.assertEqual(snapshot["palette"], "Kuzey")
        self.assertEqual(snapshot["msfs_layout"], "cockpit")
        with patch.object(panel, "save_settings", return_value=True):
            panel.language_combo.setCurrentIndex(panel.language_combo.findData("tr"))
        self.assertEqual(panel.start_button.text(), "Kaydet ve başlat")
        panel.close()

    def test_preview_stays_at_native_35_inch_resolution_when_window_grows(self) -> None:
        panel = self.make_panel()
        panel.resize(1500, 1000)
        panel.show()
        self.app.processEvents()
        self.assertEqual(panel.preview_label.width(), screen_app.WIDTH)
        self.assertEqual(panel.preview_label.height(), screen_app.HEIGHT)
        self.assertFalse(hasattr(panel, "settings_tabs"))
        self.assertTrue(all(button.width() == 96 for button in panel.preview_buttons.values()))
        panel.close()

    def test_color_picker_expands_inline_and_uses_simple_choices(self) -> None:
        panel = self.make_panel()
        self.assertTrue(panel.inline_color_panel.isHidden())
        panel._choose_color("cpu")
        self.assertFalse(panel.inline_color_panel.isHidden())
        self.assertEqual(panel.inline_color_panel.width(), 205)
        self.assertIsNotNone(panel.swatches["cpu"].graphicsEffect())
        self.assertIsNone(panel.swatches["warning"].graphicsEffect())
        self.assertEqual(len(panel.simple_color_buttons), 12)
        panel._apply_simple_color(2)
        self.assertEqual(panel.settings["colors"]["cpu"], gui.SIMPLE_ACCENT_COLORS[2])
        self.assertIn("✓", [button.text() for button in panel.simple_color_buttons])
        self.assertIn(gui.SIMPLE_ACCENT_COLORS[2], panel.selected_color_chip.styleSheet())
        self.assertEqual(panel.palette_combo.currentText(), "Özel")
        panel._choose_color("bg")
        panel._apply_simple_color(1)
        self.assertEqual(panel.settings["colors"]["bg"], gui.SIMPLE_BACKGROUND_COLORS[1])
        panel.close()

    def test_each_saved_custom_palette_gets_its_own_user_chosen_name(self) -> None:
        panel = self.make_panel()
        panel.settings["custom_palettes"] = {
            "Paletim": dict(panel.settings["colors"]),
        }
        with patch.object(
            gui.QInputDialog, "getText", return_value=("Uçuş Temam", True),
        ) as dialog, patch.object(panel, "save_settings"):
            panel._save_palette()
        self.assertEqual(dialog.call_args.kwargs["text"], "Paletim 2")
        self.assertIn("Uçuş Temam", panel.settings["custom_palettes"])
        self.assertEqual(panel.palette_combo.currentText(), "Uçuş Temam")
        panel.close()

    def test_all_builtin_palette_text_roles_remain_readable(self) -> None:
        def luminance(color: str) -> float:
            channels = [value / 255 for value in ImageColor.getrgb(color)]
            linear = [
                value / 12.92 if value <= 0.04045
                else ((value + 0.055) / 1.055) ** 2.4
                for value in channels
            ]
            return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

        def contrast(first: str, second: str) -> float:
            bright, dark = sorted((luminance(first), luminance(second)), reverse=True)
            return (bright + 0.05) / (dark + 0.05)

        for name, colors in gui.PALETTES.items():
            for surface in ("bg", "panel", "panel_2"):
                self.assertGreaterEqual(contrast(colors["text"], colors[surface]), 7.0, name)
                self.assertGreaterEqual(contrast(colors["muted"], colors[surface]), 4.5, name)

    def test_mouse_click_buttons_do_not_keep_native_focus_rectangle(self) -> None:
        panel = self.make_panel()
        for button in panel.findChildren(gui.QPushButton):
            self.assertEqual(button.focusPolicy(), Qt.FocusPolicy.TabFocus)
        panel.close()

    def test_preview_offers_each_runtime_screen_as_a_separate_view(self) -> None:
        panel = self.make_panel()
        self.assertEqual(
            set(panel.preview_buttons),
            {"daily", "cs2_ct", "cs2_t", "deathmatch", "msfs", "weather", "wireless"},
        )
        for mode in panel.preview_buttons:
            panel._set_preview_mode(mode)
            self.assertEqual(panel.preview_mode, mode)
            self.assertIsNotNone(panel._preview_image)
        self.assertIn("Kablosuz", panel.preview_context_label.text())
        panel.close()

    def test_cs2_connection_is_automatic_without_a_manual_button(self) -> None:
        panel = self.make_panel()
        self.assertFalse(hasattr(panel, "gsi_button"))
        self.assertTrue(callable(panel._install_gsi))
        panel.close()

    def test_sabasakal_logo_assets_are_valid_for_window_and_exe(self) -> None:
        logo_png = Path(gui.__file__).with_name("sabasakal-logo.png")
        logo_ico = Path(gui.__file__).with_name("sabasakal-logo.ico")
        self.assertTrue(logo_png.is_file())
        self.assertTrue(logo_ico.is_file())
        with Image.open(logo_png) as image:
            self.assertEqual(image.size, (1024, 1024))
            self.assertEqual(image.mode, "RGBA")
        with Image.open(logo_ico) as image:
            self.assertEqual(image.format, "ICO")

    def test_tray_only_instance_can_receive_a_restore_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request_path = Path(directory) / "show-window.request"
            ack_path = Path(directory) / "show-window.ack"
            with patch.object(gui, "APP_DATA", request_path.parent), patch.object(
                gui, "ACTIVATE_REQUEST_PATH", request_path,
            ), patch.object(gui, "ACTIVATE_ACK_PATH", ack_path):
                token = gui._request_existing_window()
                self.assertTrue(token)
                self.assertTrue(request_path.is_file())
                self.assertEqual(request_path.read_text(encoding="ascii"), token)

    def test_duplicate_launch_restores_through_qt_before_native_window_api(self) -> None:
        with patch.object(gui, "_request_existing_window", return_value="token") as request, patch.object(
            gui, "_wait_for_activation_ack", return_value=True,
        ) as wait, patch.object(gui, "_activate_existing_window") as native:
            self.assertTrue(gui._restore_existing_instance_window())
        request.assert_called_once_with()
        wait.assert_called_once_with("token")
        native.assert_not_called()

    def test_duplicate_launch_uses_native_activation_only_as_fallback(self) -> None:
        with patch.object(gui, "_request_existing_window", return_value="token"), patch.object(
            gui, "_wait_for_activation_ack", return_value=False,
        ), patch.object(gui, "_activate_existing_window", return_value=True) as native:
            self.assertTrue(gui._restore_existing_instance_window())
        native.assert_called_once_with(timeout_seconds=0.8)

    def test_duplicate_autorun_never_reveals_existing_window(self) -> None:
        def create_mutex(*_args):
            return 123

        # ctypes assigns signatures to these callables at runtime.
        create_mutex.argtypes = None
        create_mutex.restype = None
        kernel = SimpleNamespace(CreateMutexW=create_mutex, CloseHandle=lambda _handle: None)
        with patch.object(gui.ctypes, "WinDLL", return_value=kernel), patch.object(
            gui.ctypes, "get_last_error", return_value=183,
        ), patch.object(gui, "_restore_existing_instance_window") as restore, patch.object(
            gui, "_runtime_log",
        ) as runtime_log:
            self.assertFalse(gui._acquire_single_instance(activate_existing=False))
        restore.assert_not_called()
        self.assertIn("duplicate autorun ignored", runtime_log.call_args.args[0])

    def test_instance_heartbeat_records_exact_process_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "instance.json"
            with patch.object(gui, "APP_DATA", root), patch.object(
                gui, "INSTANCE_STATE_PATH", state_path,
            ):
                gui._write_instance_state()
                state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["pid"], os.getpid())
            self.assertGreater(state["created_at"], 0)
            self.assertLess(abs(state["heartbeat"] - time.time()), 2.0)

    def test_display_watchdog_restarts_only_a_stalled_worker(self) -> None:
        panel = self.make_panel()
        panel.worker = SimpleNamespace(is_alive=lambda: True)
        panel._screen_progress_at = time.monotonic() - gui.DISPLAY_STALL_SECONDS - 1.0
        with patch.object(gui, "_write_instance_state"), patch.object(
            panel, "_restart_after_display_stall",
        ) as restart:
            panel._liveness_tick()
        restart.assert_called_once_with()
        panel._screen_progress_at = time.monotonic()
        with patch.object(gui, "_write_instance_state"), patch.object(
            panel, "_restart_after_display_stall",
        ) as restart:
            panel._liveness_tick()
        restart.assert_not_called()
        panel.close()

    def test_system_tray_has_open_start_stop_and_exit_actions(self) -> None:
        with patch.object(gui.QSystemTrayIcon, "isSystemTrayAvailable", return_value=True):
            panel = self.make_panel()
        self.assertIsNotNone(panel.tray_icon)
        action_texts = {
            action.text()
            for action in panel.tray_icon.contextMenu().actions()
            if not action.isSeparator()
        }
        self.assertEqual(action_texts, {
            "Sabasakal Mini Ekran'ı aç",
            "Mini ekranı başlat",
            "Mini ekranı durdur",
            "Çıkış",
        })
        panel.tray_icon.hide()
        panel.tray_icon = None
        panel.close()

    def test_startup_launch_stays_hidden_when_tray_is_ready(self) -> None:
        calls: list[str] = []
        panel = SimpleNamespace(
            tray_icon=object(),
            hide=lambda: calls.append("hide"),
            showMinimized=lambda: calls.append("minimized"),
            showNormal=lambda: calls.append("normal"),
            raise_=lambda: calls.append("raise"),
            activateWindow=lambda: calls.append("activate"),
        )
        gui.present_initial_window(panel, autorun=True)
        self.assertEqual(calls, ["hide"])

    def test_startup_launch_is_recoverably_minimized_until_tray_exists(self) -> None:
        calls: list[str] = []
        panel = SimpleNamespace(
            tray_icon=None,
            hide=lambda: calls.append("hide"),
            showMinimized=lambda: calls.append("minimized"),
            showNormal=lambda: calls.append("normal"),
            raise_=lambda: calls.append("raise"),
            activateWindow=lambda: calls.append("activate"),
        )
        gui.present_initial_window(panel, autorun=True)
        self.assertEqual(calls, ["minimized"])

    def test_manual_launch_opens_and_activates_the_window(self) -> None:
        calls: list[str] = []
        panel = SimpleNamespace(
            tray_icon=object(),
            hide=lambda: calls.append("hide"),
            showMinimized=lambda: calls.append("minimized"),
            showNormal=lambda: calls.append("normal"),
            raise_=lambda: calls.append("raise"),
            activateWindow=lambda: calls.append("activate"),
        )
        gui.present_initial_window(panel, autorun=False)
        self.assertEqual(calls, ["normal", "raise", "activate"])

    def test_system_shutdown_waits_for_worker_cleanup(self) -> None:
        panel = self.make_panel()

        class ShutdownWorker:
            def __init__(self) -> None:
                self.running = True
                self.join_timeout = None

            def is_alive(self) -> bool:
                return self.running

            def join(self, timeout=None) -> None:
                self.join_timeout = timeout
                self.running = False

        worker = ShutdownWorker()
        panel.worker = worker
        panel.prepare_system_shutdown()
        self.assertTrue(panel.stop_event.is_set())
        self.assertTrue(panel._exit_requested)
        self.assertEqual(worker.join_timeout, 4.0)
        panel.prepare_system_shutdown()
        self.assertEqual(worker.join_timeout, 4.0)
        panel.close()

    def test_constructing_panel_does_not_touch_real_startup_registry(self) -> None:
        with patch.object(gui, "reconcile_windows_autostart") as reconcile:
            panel = self.make_panel()
        reconcile.assert_not_called()
        panel.close()

    def test_start_locks_settings_and_stop_unlocks_them(self) -> None:
        panel = self.make_panel()
        initial_states = {id(control): control.isEnabled() for control in panel.edit_controls}
        panel._screen_worker = lambda: panel.stop_event.wait(0.2)
        with patch.object(panel, "save_settings", return_value=True), patch.object(
            panel, "_install_gsi", return_value=True
        ):
            panel.start_screen()
        self.assertTrue(all(not control.isEnabled() for control in panel.edit_controls))
        self.assertTrue(panel.stop_button.isEnabled())
        self.assertFalse(panel.start_button.isEnabled())
        self.assertLess(panel.quick_card_effect.opacity(), 0.5)
        self.assertNotIn("qlineargradient", panel.swatches["cpu"].styleSheet())
        panel._worker_status("Mini ekran bağlı")
        self.assertIn("ayarlar kilitli", panel.status_label.text())
        panel.stop_screen()
        panel.worker.join(timeout=1)
        panel._screen_stopped()
        restored_states = {id(control): control.isEnabled() for control in panel.edit_controls}
        self.assertEqual(restored_states, initial_states)
        panel.close()

    def test_start_failure_remains_visible_in_status(self) -> None:
        panel = self.make_panel()
        with patch.object(panel, "save_settings", return_value=True), patch.object(
            panel, "_install_gsi", return_value=True
        ), patch("gui.threading.Thread.start", side_effect=RuntimeError("thread failed")):
            panel.start_screen()
        self.assertIn("başlatılamadı", panel.status_label.text())
        panel.close()

    def worker_patches(self):
        return (
            patch.object(gui, "GSIServer", FakeGSI),
            patch.object(gui, "cs2_is_running", return_value=False),
            patch.object(gui, "faceit_ac_is_running", return_value=False),
            patch.object(gui, "msfs_session_seconds", return_value=None),
            patch.object(gui, "get_metrics", return_value={}),
            patch.object(gui, "render_screen", return_value=Image.new("RGB", (480, 320))),
            patch.object(gui, "MediaSessionMonitor", side_effect=lambda: FakeMonitor(MediaSnapshot())),
            patch.object(gui, "AudioSpectrumMonitor", side_effect=lambda: FakeMonitor(AudioSpectrumSnapshot())),
            patch.object(
                gui, "MSFSTelemetryMonitor",
                side_effect=lambda: FakeMonitor(FlightTelemetry()),
            ),
            patch.object(
                gui, "WeatherMonitor",
                side_effect=lambda *_args: FakeMonitor(WeatherSnapshot()),
            ),
        )

    def test_com_connection_retries_then_recovers(self) -> None:
        panel = self.make_panel()
        panel.stop_event = FastEvent()
        panel.screen_connected = TrackingEvent()
        attempts: list[object] = []
        recovered = FakeScreen(panel.stop_event)

        def factory(*_args):
            attempts.append(object())
            if len(attempts) == 1:
                raise OSError("COM3 not ready")
            return recovered

        patches = self.worker_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patch.object(
            gui, "ScreenSession", side_effect=factory
        ):
            panel._screen_worker()
        self.assertEqual(len(attempts), 2)
        self.assertEqual(recovered.show_count, 2)
        self.assertEqual(recovered.power_off_count, 1)
        self.assertEqual(recovered.events[-3:], ["power_off", "show", "close"])
        self.assertIsNone(recovered.shown_images[-1].getbbox())
        self.assertEqual(panel.screen_connected.set_count, 1)
        self.assertTrue(recovered.closed)
        panel.close()

    def test_usb_write_failure_closes_and_reconnects(self) -> None:
        panel = self.make_panel()
        panel.stop_event = FastEvent()
        panel.screen_connected = TrackingEvent()
        broken = FakeScreen(panel.stop_event, fail_show=True)
        recovered = FakeScreen(panel.stop_event)
        screens = [broken, recovered]

        patches = self.worker_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patch.object(
            gui, "ScreenSession", side_effect=lambda *_args: screens.pop(0)
        ):
            panel._screen_worker()
        self.assertTrue(broken.closed)
        self.assertEqual(recovered.show_count, 2)
        self.assertEqual(recovered.power_off_count, 1)
        self.assertEqual(recovered.events[-3:], ["power_off", "show", "close"])
        self.assertIsNone(recovered.shown_images[-1].getbbox())
        self.assertEqual(panel.screen_connected.set_count, 1)
        self.assertTrue(recovered.closed)
        panel.close()

    def test_black_frame_is_still_sent_when_power_off_command_fails(self) -> None:
        panel = self.make_panel()
        panel.stop_event = FastEvent()
        screen = FakeScreen(panel.stop_event, fail_power_off=True)
        patches = self.worker_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patch.object(
            gui, "ScreenSession", return_value=screen,
        ):
            panel._screen_worker()
        self.assertEqual(screen.power_off_count, 1)
        self.assertEqual(screen.show_count, 2)
        self.assertIsNone(screen.shown_images[-1].getbbox())
        self.assertTrue(screen.closed)
        panel.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
