from __future__ import annotations

import ast
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
from unittest.mock import Mock, patch

import numpy as np

# ---------------------------------------------------------------------------
# Hermetic test environment. This block MUST run before gui/app are imported:
# every module derives its data paths (settings.json, logs, GSI token,
# instance.json, sensor cache, deathmatch state ...) from %APPDATA% at import
# time. Redirect it to a throw-away directory so no test can ever read or
# overwrite the real %APPDATA%\CS2Screen of the person running the suite.
# ---------------------------------------------------------------------------
_REAL_APP_DATA = Path(os.environ.get("APPDATA", Path.home())) / "CS2Screen"


def _snapshot_tree(root: Path) -> dict[str, tuple[int, int]]:
    result: dict[str, tuple[int, int]] = {}
    if not root.exists():
        return result
    for path in root.rglob("*"):
        try:
            if path.is_file():
                stat = path.stat()
                result[str(path)] = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            continue
    return result


_REAL_APP_DATA_BEFORE = _snapshot_tree(_REAL_APP_DATA)
_TEST_ROOT = Path(tempfile.mkdtemp(prefix="sabasakal-tests-"))
os.environ["APPDATA"] = str(_TEST_ROOT / "Roaming")
os.environ["LOCALAPPDATA"] = str(_TEST_ROOT / "Local")
(_TEST_ROOT / "Roaming").mkdir(parents=True, exist_ok=True)
(_TEST_ROOT / "Local").mkdir(parents=True, exist_ok=True)


class HardwareAccessBlocked(RuntimeError):
    """A test tried to reach real hardware, processes or scheduled tasks."""


def _blocked(name: str):
    def guard(*_args, **_kwargs):
        raise HardwareAccessBlocked(f"{name} is blocked in tests; patch it explicitly")
    return guard


def _install_hardware_guards() -> None:
    import subprocess as _subprocess
    import winreg as _winreg  # noqa: F401 - Windows only project
    import serial as _serial

    _serial.Serial = _blocked("serial.Serial")  # type: ignore[assignment]
    for name in ("Popen", "run", "call", "check_call", "check_output"):
        setattr(_subprocess, name, _blocked(f"subprocess.{name}"))
    for name in ("SetValueEx", "DeleteValue", "CreateKey", "CreateKeyEx"):
        setattr(_winreg, name, _blocked(f"winreg.{name}"))
    os.startfile = _blocked("os.startfile")  # type: ignore[attr-defined]
    os.kill = _blocked("os.kill")  # type: ignore[assignment]
    import psutil as _psutil
    for name in ("terminate", "kill", "suspend", "send_signal"):
        setattr(_psutil.Process, name, _blocked(f"psutil.Process.{name}"))


_install_hardware_guards()

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image, ImageChops, ImageColor, ImageDraw
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

import gui
import app as screen_app

# No real LCD session may ever be opened by a test, and no test may edit the
# real CS2/Steam configuration.
screen_app._robust_display_class = _blocked("real LCD driver")  # type: ignore[assignment]
for _name in ("install_gsi_config", "install_cs2_console_log_launch_option", "find_cs2_cfg_dir"):
    setattr(screen_app, _name, _blocked(f"app.{_name}"))
gui.install_gsi_config = _blocked("gui.install_gsi_config")  # type: ignore[assignment]
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
            "winpcap", "npcap", "steam_api64.dll", "steamappid", "steamgameid",
        )
        for api in forbidden:
            self.assertNotIn(api, source, api)

    def test_gsi_server_is_loopback_only(self) -> None:
        source = (Path(__file__).parent / "app.py").read_text(encoding="utf-8")
        self.assertIn('ThreadingHTTPServer(("127.0.0.1", GSI_PORT)', source)
        self.assertNotIn('ThreadingHTTPServer(("0.0.0.0", GSI_PORT)', source)

    def test_runtime_never_loads_cs2_or_steam_game_libraries(self) -> None:
        source = "\n".join(
            (Path(__file__).parent / name).read_text(encoding="utf-8").lower()
            for name in ("app.py", "gui.py")
        )
        forbidden = (
            "steam_api64.dll", "steamappid", "steamgameid",
            "--steam-ping-bridge", "steaminternal_findorcreateuserinterface",
        )
        for marker in forbidden:
            self.assertNotIn(marker, source, marker)

    def test_cs2_integration_requests_only_local_player_state(self) -> None:
        config = screen_app.gsi_config_text().lower()
        self.assertIn('"uri" "http://127.0.0.1:', config)
        self.assertNotIn("allplayers_", config)
        self.assertLessEqual(screen_app.GSI_MAX_PAYLOAD_BYTES, 1024 * 1024)

    def test_obsolete_valve_probe_scanner_is_not_shipped(self) -> None:
        source = (Path(__file__).parent / "app.py").read_text(encoding="utf-8").lower()
        self.assertNotIn("getsdRconfig".lower(), source)
        self.assertNotIn("fetch_relay_targets", source)
        specification = (Path(__file__).parent / "Sabasakal-Mini-Screen.spec").read_text(
            encoding="utf-8",
        )
        self.assertNotIn("(str(driver_root / 'library'), 'library')", specification)


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
        with patch.object(gui, "set_windows_autostart") as setter, patch.object(
            gui, "_startup_task_command", return_value=None,
        ):
            self.assertTrue(gui.reconcile_windows_autostart(True))
        setter.assert_called_once_with(True)

    def test_autostart_reconciliation_does_not_crash_on_registry_error(self) -> None:
        with patch.object(
            gui, "set_windows_autostart", side_effect=OSError("registry unavailable"),
        ), patch.object(gui, "_startup_task_command", return_value=None), patch.object(
            gui, "_runtime_log",
        ) as runtime_log:
            self.assertFalse(gui.reconcile_windows_autostart(True))
        self.assertIn("autostart reconciliation failed", runtime_log.call_args.args[0])

    def test_autostart_uses_only_the_elevated_startup_task(self) -> None:
        with patch.object(gui, "_startup_task_is_current", return_value=False), patch.object(
            gui, "_install_windows_startup_task", return_value=True,
        ) as task, patch.object(
            gui, "_remove_windows_startup_shortcut", return_value=True,
        ) as shortcut, patch.object(gui, "_set_windows_run_fallback") as fallback:
            gui.set_windows_autostart(True)
        task.assert_called_once_with()
        shortcut.assert_called_once_with()
        fallback.assert_called_once_with(False)

    def test_all_scheduled_task_commands_are_forced_hidden(self) -> None:
        """A console-backed schtasks call must never steal focus from a game."""
        source = (Path(__file__).parent / "gui.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        scheduled_task_calls = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if not (
                isinstance(function, ast.Attribute)
                and function.attr == "run"
                and isinstance(function.value, ast.Name)
                and function.value.id == "subprocess"
                and node.args
                and isinstance(node.args[0], (ast.List, ast.Tuple))
                and node.args[0].elts
                and isinstance(node.args[0].elts[0], ast.Constant)
                and node.args[0].elts[0].value == "schtasks.exe"
            ):
                continue
            scheduled_task_calls.append(node)
            creation_flags = next(
                (keyword.value for keyword in node.keywords if keyword.arg == "creationflags"),
                None,
            )
            self.assertIsNotNone(creation_flags, f"line {node.lineno}")
            self.assertEqual(ast.unparse(creation_flags), "_hidden_flags()", f"line {node.lineno}")
        self.assertGreaterEqual(len(scheduled_task_calls), 6)

    def test_autostart_does_not_recreate_a_current_task(self) -> None:
        with patch.object(gui, "_startup_task_is_current", return_value=True), patch.object(
            gui, "_install_windows_startup_task",
        ) as install, patch.object(
            gui, "_remove_windows_startup_shortcut", return_value=True,
        ), patch.object(gui, "_set_windows_run_fallback"):
            gui.set_windows_autostart(True)
        install.assert_not_called()

    def test_startup_task_is_elevated_hidden_and_single_instance(self) -> None:
        xml = gui._startup_task_xml()
        self.assertIn("<RunLevel>HighestAvailable</RunLevel>", xml)
        self.assertIn("<LogonType>InteractiveToken</LogonType>", xml)
        self.assertIn("<Hidden>true</Hidden>", xml)
        self.assertIn("<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>", xml)

    def test_autostart_disable_removes_all_legacy_launch_paths(self) -> None:
        with patch.object(gui, "_remove_windows_startup_task", return_value=True) as task, patch.object(
            gui, "_remove_windows_startup_shortcut", return_value=True,
        ) as shortcut, patch.object(
            gui, "_set_windows_run_fallback",
        ) as fallback:
            gui.set_windows_autostart(False)
        task.assert_called_once_with()
        shortcut.assert_called_once_with()
        fallback.assert_called_once_with(False)

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
            "round": {"bomb": "planted"},
        })
        self.assertTrue(game_state.snapshot().bomb_planted)
        self.assertFalse(hasattr(game_state.snapshot(), "bomb_seconds"))

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

    def test_msfs_fast_refresh_budget_is_below_two_tenths(self) -> None:
        self.assertLessEqual(gui.MSFS_TELEMETRY_REFRESH_INTERVAL, 0.15)
        self.assertLessEqual(gui.MSFS_WORKER_INTERVAL, 0.10)

    def test_live_cs2_money_and_deathmatch_stats_refresh_with_gsi(self) -> None:
        self.assertLessEqual(gui.LIVE_GAME_WORKER_INTERVAL, 0.20)
        self.assertFalse(hasattr(gui, "BOMB_WORKER_INTERVAL"))

    def test_matchmaking_ping_uses_only_cs2_console_output(self) -> None:
        self.assertEqual(screen_app.CS2_EXACT_PING_MODE, "console_log")
        monitor = screen_app.MatchmakingPingMonitor()
        self.assertFalse(hasattr(monitor, "fallback"))

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

    def test_matchmaking_monitor_returns_only_cs2_values(self) -> None:
        exact = [screen_app.RelayPing("vie", "Viyana", "", 54.0)]
        monitor = screen_app.MatchmakingPingMonitor()
        monitor.exact = SimpleNamespace(read=lambda: exact, close=lambda: None)
        self.assertEqual(monitor.read(), exact)
        monitor.exact = SimpleNamespace(read=lambda: [], close=lambda: None)
        self.assertEqual(monitor.read(), [])

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

    def test_ram_usage_uses_one_decimal_like_task_manager(self) -> None:
        with patch.object(screen_app, "draw_open_ring") as ring:
            screen_app.render_screen(
                self.sample_metrics() | {"ram_used": 27.4},
                screen_app.GameSnapshot(), False,
            )
        self.assertEqual(ring.call_args_list[2].args[7], "27.4 GB")

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

    def test_component_scales_use_shared_three_level_thresholds(self) -> None:
        colors = {
            "cpu": "#20C080", "warning": "#E0A020",
            "danger": "#E04040", "dim": "#606060",
        }
        for component, warn, critical in (("cpu", 60, 75), ("gpu", 60, 80), ("ram", 70, 85)):
            self.assertEqual(screen_app.load_color(warn - 0.1, component, colors), colors["cpu"])
            self.assertEqual(screen_app.load_color(warn, component, colors), colors["warning"])
            self.assertEqual(screen_app.load_color(critical - 0.1, component, colors), colors["warning"])
            self.assertEqual(screen_app.load_color(critical, component, colors), colors["danger"])
        for component, warn, critical in (("cpu", 60, 75), ("gpu", 55, 65)):
            self.assertEqual(screen_app.temperature_color(warn - 0.1, component, colors), colors["cpu"])
            self.assertEqual(screen_app.temperature_color(warn, component, colors), colors["warning"])
            self.assertEqual(screen_app.temperature_color(critical - 0.1, component, colors), colors["warning"])
            self.assertEqual(screen_app.temperature_color(critical, component, colors), colors["danger"])
            self.assertEqual(screen_app.temperature_color(None, component, colors), colors["dim"])

    def test_both_designs_classify_identical_inputs_identically(self) -> None:
        import screens_new as sn
        import thresholds
        self.assertIs(sn.THRESHOLDS, thresholds.THRESHOLDS)
        colors = {"cpu": "#20C080", "warning": "#E0A020", "danger": "#E04040", "dim": "#606060"}
        classic = {colors["cpu"]: 0, colors["warning"]: 1, colors["danger"]: 2}
        claude = {sn.GREEN: 0, sn.ORANGE: 1, sn.RED: 2}
        for (component, kind), (warn, critical) in thresholds.THRESHOLDS.items():
            for value in (0, warn - 0.1, warn, (warn + critical) / 2, critical - 0.1, critical, 100):
                if kind == "load":
                    old = screen_app.load_color(value, component, colors)
                else:
                    old = screen_app.temperature_color(value, component, colors)
                new = sn.threshold_color(component, kind, value)
                self.assertEqual(classic[old], claude[new], (component, kind, value))
                self.assertEqual(classic[old], thresholds.level(component, kind, value))

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
            screen_app.temperature_unit_color(80, "cpu", colors, 0.1),
            screen_app.temperature_unit_color(80, "cpu", colors, 0.6),
        )

    def test_load_value_blinks_when_each_component_reaches_critical(self) -> None:
        colors = {
            "cpu": "#20C080", "warning": "#E0A020", "danger": "#E04040",
            "dim": "#606060", "bg": "#101010", "text": "#F0F0F0",
        }
        for component, critical in (("cpu", 75), ("gpu", 80), ("ram", 85)):
            self.assertIsNone(screen_app.load_alert_color(critical - 0.1, component, colors, 0.1))
            self.assertNotEqual(
                screen_app.load_alert_color(critical, component, colors, 0.1),
                screen_app.load_alert_color(critical, component, colors, 0.6),
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

        below_visible = frame(74.9, 79.9, 84.9, 0.1)
        below_dim = frame(74.9, 79.9, 84.9, 0.6)
        self.assertIsNone(ImageChops.difference(below_visible, below_dim).getbbox())

        critical_visible = frame(75.0, 80.0, 85.0, 0.1)
        critical_dim = frame(75.0, 80.0, 85.0, 0.6)
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
        self.assertEqual(screen_app.temperature_number_color(74.9, "cpu", colors), colors["text"])
        self.assertNotEqual(
            screen_app.temperature_number_color(75, "cpu", colors, 0.1),
            screen_app.temperature_number_color(75, "cpu", colors, 0.6),
        )
        self.assertEqual(screen_app.temperature_number_color(64.9, "gpu", colors), colors["text"])
        self.assertNotEqual(
            screen_app.temperature_number_color(65, "gpu", colors, 0.1),
            screen_app.temperature_number_color(65, "gpu", colors, 0.6),
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

    def test_gpu_metrics_never_spawns_nvidia_smi(self) -> None:
        with patch.object(
            screen_app.NVIDIA_MONITOR, "read", return_value=(42.0, 55.0, 3072.0),
        ) as read, patch.object(screen_app.subprocess, "check_output") as external:
            self.assertEqual(screen_app.get_gpu_metrics(), (42.0, 55.0, 3072.0))
        read.assert_called_once_with()
        external.assert_not_called()

    def test_nvml_monitor_selects_the_nvidia_gpu_with_most_vram(self) -> None:
        class FakeFunction:
            def __init__(self, callback):
                self.callback = callback
                self.argtypes = None
                self.restype = None

            def __call__(self, *args):
                return self.callback(*args)

        class FakeNvml:
            def __init__(self):
                self.nvmlInit_v2 = FakeFunction(lambda: 0)
                self.nvmlDeviceGetCount_v2 = FakeFunction(self._count)
                self.nvmlDeviceGetHandleByIndex_v2 = FakeFunction(self._handle)
                self.nvmlDeviceGetMemoryInfo = FakeFunction(self._memory)
                self.nvmlDeviceGetUtilizationRates = FakeFunction(self._utilization)
                self.nvmlDeviceGetTemperature = FakeFunction(self._temperature)

            @staticmethod
            def _count(pointer):
                pointer._obj.value = 2
                return 0

            @staticmethod
            def _handle(index, pointer):
                pointer._obj.value = 101 if int(index) == 0 else 202
                return 0

            @staticmethod
            def _memory(handle, pointer):
                pointer._obj.total = (8 if handle.value == 101 else 12) * 1024 ** 3
                pointer._obj.free = 8 * 1024 ** 3
                pointer._obj.used = (1 if handle.value == 101 else 3) * 1024 ** 3
                return 0

            @staticmethod
            def _utilization(handle, pointer):
                pointer._obj.gpu = 37 if handle.value == 202 else 5
                pointer._obj.memory = 12
                return 0

            @staticmethod
            def _temperature(handle, _sensor_type, pointer):
                pointer._obj.value = 54 if handle.value == 202 else 41
                return 0

        monitor = screen_app.NvidiaNvmlMonitor()
        with patch.object(screen_app.ctypes, "WinDLL", return_value=FakeNvml()):
            self.assertEqual(monitor.read(), (37.0, 54.0, 3072.0))
        self.assertEqual(monitor._handle.value, 202)

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
                "cpu_temp": 51.25, "cpu_freq": 4.35, "cpu_power": None,
            })

    def test_stale_cache_never_spawns_legacy_pythonnet_helper(self) -> None:
        monitor = screen_app.EmbeddedSensorMonitor()
        with patch.object(monitor, "_read_elevated_cache", return_value=None), patch.object(
            monitor, "start",
        ) as start:
            self.assertEqual(
                monitor.read(), {"cpu_temp": None, "cpu_freq": None, "cpu_power": None},
            )
        start.assert_not_called()


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
        self.running = False
        self.interval = 2.0
        self.starts = 0
        self.pauses = 0

    def start(self) -> None:
        self.running = True
        self.starts += 1

    def pause(self) -> None:
        self.running = False
        self.pauses += 1

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
    def test_direct_sensor_host_is_a_windowless_native_executable(self) -> None:
        self.assertNotEqual(gui.SENSOR_HOST_SOURCE_PATH, Path(sys.executable))
        self.assertTrue(gui.SENSOR_HOST_SOURCE_PATH.is_file())
        image = gui.SENSOR_HOST_SOURCE_PATH.read_bytes()
        pe_offset = int.from_bytes(image[0x3C:0x40], "little")
        subsystem = int.from_bytes(image[pe_offset + 0x5C:pe_offset + 0x5E], "little")
        self.assertEqual(subsystem, 2)  # IMAGE_SUBSYSTEM_WINDOWS_GUI

    def test_sensor_host_starts_directly_once_with_hidden_io(self) -> None:
        fake = SimpleNamespace(
            pid=42, poll=lambda: None, terminate=lambda: None,
            wait=lambda timeout: None, kill=lambda: None,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "SabasakalSensorHost.exe"
            source.write_bytes(b"native-host")
            previous = gui._SENSOR_HOST_PROCESS
            previous_started_at = gui._SENSOR_HOST_STARTED_AT
            gui._SENSOR_HOST_PROCESS = None
            gui._SENSOR_HOST_STARTED_AT = 0.0
            try:
                with patch.object(gui.sys, "frozen", True, create=True), patch.object(
                    gui, "SENSOR_HOST_SOURCE_PATH", source,
                ), patch.object(gui, "APP_DATA", root), patch.object(
                    gui, "_remove_legacy_sensor_tasks_once",
                ), patch.object(gui.subprocess, "Popen", return_value=fake) as popen:
                    self.assertTrue(gui.ensure_sensor_host())
                    self.assertTrue(gui.ensure_sensor_host())
                popen.assert_called_once()
                command = popen.call_args.args[0]
                self.assertEqual(command[0], str(source))
                self.assertIn("--cache", command)
                self.assertIn("--owner-state", command)
                self.assertEqual(popen.call_args.kwargs["stdin"], gui.subprocess.DEVNULL)
                self.assertEqual(popen.call_args.kwargs["stdout"], gui.subprocess.DEVNULL)
                self.assertEqual(popen.call_args.kwargs["stderr"], gui.subprocess.DEVNULL)
                self.assertEqual(
                    popen.call_args.kwargs["creationflags"], gui._hidden_flags(),
                )
                self.assertEqual(
                    popen.call_args.kwargs["env"]["DOTNET_BUNDLE_EXTRACT_BASE_DIR"],
                    str(root / "sensor-host-runtime"),
                )
            finally:
                gui._SENSOR_HOST_PROCESS = previous
                gui._SENSOR_HOST_STARTED_AT = previous_started_at

    def test_legacy_sensor_tasks_are_removed_once_and_never_started(self) -> None:
        completed = SimpleNamespace(returncode=0)
        previous = gui._LEGACY_SENSOR_TASKS_CLEANED
        gui._LEGACY_SENSOR_TASKS_CLEANED = False
        try:
            with tempfile.TemporaryDirectory() as directory, patch.object(
                gui, "APP_DATA", Path(directory),
            ), patch.object(gui.subprocess, "run", return_value=completed) as run:
                gui._remove_legacy_sensor_tasks_once()
                gui._remove_legacy_sensor_tasks_once()
            self.assertEqual(run.call_count, len(gui.LEGACY_SENSOR_TASK_NAMES) * 2)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertFalse(any("/Run" in command for command in commands))
            self.assertTrue(all(
                call.kwargs["creationflags"] == gui._hidden_flags()
                for call in run.call_args_list
            ))
        finally:
            gui._LEGACY_SENSOR_TASKS_CLEANED = previous

    def test_concurrent_sensor_start_requests_create_one_child(self) -> None:
        fake = SimpleNamespace(
            pid=51, poll=lambda: None, terminate=lambda: None,
            wait=lambda timeout: None, kill=lambda: None,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "SabasakalSensorHost.exe"
            source.write_bytes(b"native-host")
            previous = gui._SENSOR_HOST_PROCESS
            previous_started_at = gui._SENSOR_HOST_STARTED_AT
            gui._SENSOR_HOST_PROCESS = None
            gui._SENSOR_HOST_STARTED_AT = 0.0
            try:
                with patch.object(gui.sys, "frozen", True, create=True), patch.object(
                    gui, "SENSOR_HOST_SOURCE_PATH", source,
                ), patch.object(gui, "APP_DATA", root), patch.object(
                    gui, "_remove_legacy_sensor_tasks_once",
                ), patch.object(gui.subprocess, "Popen", return_value=fake) as popen:
                    threads = [threading.Thread(target=gui.ensure_sensor_host) for _ in range(6)]
                    for thread in threads:
                        thread.start()
                    for thread in threads:
                        thread.join()
                popen.assert_called_once()
            finally:
                gui._SENSOR_HOST_PROCESS = previous
                gui._SENSOR_HOST_STARTED_AT = previous_started_at

    def test_stale_sensor_cache_restarts_direct_host(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(
            gui, "ensure_sensor_host", return_value=True,
        ) as ensure:
            missing = Path(directory) / "missing.json"
            self.assertTrue(gui.nudge_sensor_host_if_stale(missing))
        ensure.assert_called_once_with(restart=True)

    def test_perf_mode_cache_age_is_not_treated_as_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(
            gui, "ensure_sensor_host",
        ) as ensure:
            cache = Path(directory) / "cache.json"
            cache.write_text("{}", encoding="utf-8")
            old = time.time() - 8.0  # a 2 s perf-mode tick plus a slow LHM pass
            os.utime(cache, (old, old))
            self.assertFalse(gui.nudge_sensor_host_if_stale(cache))
        ensure.assert_not_called()

    def test_sensor_host_writer_does_not_wait_for_lhm_update(self) -> None:
        source = (Path(__file__).parent / "sensor-helper" / "Program.cs").read_text(encoding="utf-8")
        sampler, writer = source.split("sampler.Start();", 1)
        self.assertIn("computer.Accept(visitor)", sampler)
        self.assertNotIn("computer.Accept", writer)
        self.assertIn("WriteCache(", writer)
        self.assertIn("Task.Delay(1000)", writer)
        # Cold start: wait for the owner's first heartbeat instead of exiting.
        self.assertIn("ownerWait.ElapsedMilliseconds > 10_000", sampler)

    def test_new_sensor_host_gets_first_sample_grace_period(self) -> None:
        fake = SimpleNamespace(poll=lambda: None)
        previous = gui._SENSOR_HOST_PROCESS
        previous_started_at = gui._SENSOR_HOST_STARTED_AT
        gui._SENSOR_HOST_PROCESS = fake
        gui._SENSOR_HOST_STARTED_AT = time.monotonic()
        try:
            with tempfile.TemporaryDirectory() as directory, patch.object(
                gui, "ensure_sensor_host",
            ) as ensure:
                self.assertFalse(gui.nudge_sensor_host_if_stale(
                    Path(directory) / "missing.json", max_age=5.0,
                ))
            ensure.assert_not_called()
        finally:
            gui._SENSOR_HOST_PROCESS = previous
            gui._SENSOR_HOST_STARTED_AT = previous_started_at

    def test_sensor_owner_uses_pid_identity_and_fresh_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "instance.json"
            process = gui.psutil.Process(os.getpid())
            state_path.write_text(json.dumps({
                "pid": os.getpid(),
                "created_at": process.create_time(),
                "heartbeat": time.time(),
            }), encoding="utf-8")
            self.assertTrue(gui._sensor_owner_alive(state_path, max_age=5.0))
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            payload["heartbeat"] = time.time() - 10.0
            state_path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertFalse(gui._sensor_owner_alive(state_path, max_age=5.0))

    def test_gui_has_no_task_based_or_pythonnet_sensor_entrypoint(self) -> None:
        source = Path(gui.__file__).read_text(encoding="utf-8")
        self.assertNotIn('"--sensor-bridge" in sys.argv', source)
        self.assertNotIn('"--sensor-file-bridge" in sys.argv', source)
        self.assertNotIn("def install_sensor_task", source)
        self.assertNotIn('["schtasks.exe", "/Run", "/TN", SENSOR_TASK_NAME]', source)
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
        for design in ("new", "classic"):
            panel.design_combo.setCurrentIndex(panel.design_combo.findData(design))
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

    def test_display_watchdog_arms_hard_exit_for_a_blocked_usb_worker(self) -> None:
        panel = self.make_panel()
        fake_app = SimpleNamespace(quit=Mock())
        fake_thread = Mock()
        with patch.object(gui.subprocess, "Popen") as popen, patch.object(
            gui.threading, "Thread", return_value=fake_thread,
        ) as thread_class, patch.object(
            gui.QApplication, "instance", return_value=fake_app,
        ), patch.object(
            gui, "_clean_restart_environment", return_value={"RESET": "1"},
        ):
            panel._restart_after_display_stall()
        popen.assert_called_once()
        self.assertEqual(popen.call_args.kwargs["env"], {"RESET": "1"})
        thread_class.assert_called_once_with(
            target=panel._force_exit_after_display_stall,
            name="DisplayStallHardExit",
            daemon=True,
        )
        fake_thread.start.assert_called_once_with()
        fake_app.quit.assert_called_once_with()
        panel.close()

    def test_display_watchdog_hard_exit_is_delayed(self) -> None:
        with patch.object(gui.time, "sleep") as sleep, patch.object(
            gui, "_runtime_log",
        ) as runtime_log, patch.object(gui.os, "_exit") as hard_exit:
            gui.ControlPanel._force_exit_after_display_stall(1.25)
        sleep.assert_called_once_with(1.25)
        self.assertIn("forcing stale process exit", runtime_log.call_args.args[0])
        hard_exit.assert_called_once_with(0)

    def test_packaged_restart_forces_a_fresh_pyinstaller_runtime(self) -> None:
        with patch.object(gui.sys, "frozen", True, create=True), patch.dict(
            gui.os.environ, {"_PYI_APPLICATION_HOME_DIR": r"C:\Temp\_MEI-old"}, clear=True,
        ):
            environment = gui._clean_restart_environment()
        self.assertEqual(environment["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        self.assertEqual(environment["_PYI_APPLICATION_HOME_DIR"], r"C:\Temp\_MEI-old")

    def test_packaging_uses_folder_runtime_instead_of_temporary_mei_extraction(self) -> None:
        specification = Path(gui.__file__).with_name("Sabasakal-Mini-Screen.spec").read_text(
            encoding="utf-8"
        )
        self.assertIn("exclude_binaries=True", specification)
        self.assertIn("coll = COLLECT(", specification)
        self.assertIn("uac_admin=True", specification)

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


class RedesignAndFixScenarios(unittest.TestCase):
    """New design, FACEIT kill switch and robustness fixes."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_threshold_colors_follow_the_specification(self) -> None:
        import screens_new as sn
        cases = (
            ("cpu", "temp", 59.9, sn.GREEN), ("cpu", "temp", 60, sn.ORANGE),
            ("cpu", "temp", 74.9, sn.ORANGE), ("cpu", "temp", 75, sn.RED),
            ("cpu", "load", 59, sn.GREEN), ("cpu", "load", 75, sn.RED),
            ("gpu", "temp", 54, sn.GREEN), ("gpu", "temp", 55, sn.ORANGE),
            ("gpu", "temp", 65, sn.RED), ("gpu", "load", 79, sn.ORANGE),
            ("gpu", "load", 80, sn.RED),
        )
        for component, kind, value, expected in cases:
            self.assertEqual(sn.threshold_color(component, kind, value), expected,
                             (component, kind, value))

    def test_screen_stabilizer_dwell_and_debounce(self) -> None:
        import screens_new as sn
        st = sn.ScreenStabilizer(min_dwell=2.5, confirm=0.6)
        live, death, menu = ("cs2", "live", ""), ("cs2", "death", ""), ("cs2_menu", "", "")
        self.assertEqual(st.update(live, 0.0), live)  # first screen is immediate
        self.assertEqual(st.update(death, 1.0), live)  # dwell not reached
        self.assertEqual(st.update(death, 2.0), live)
        self.assertEqual(st.update(death, 2.6), death)  # dwell + confirm satisfied
        # A short GSI flicker never reaches the panel.
        self.assertEqual(st.update(live, 6.0), death)
        self.assertEqual(st.update(death, 6.3), death)
        self.assertEqual(st.update(live, 6.4), death)
        self.assertEqual(st.update(live, 6.9), death)  # pending restarted at 6.4
        self.assertEqual(st.update(live, 7.1), live)
        # Flapping between two requests resets confirmation each time.
        for t in (10.0, 10.3, 10.6, 10.9, 11.2):
            self.assertEqual(st.update(menu if int(t * 10) % 2 == 0 else live, t), live)
        # FACEIT safety mode is applied at once.
        self.assertEqual(st.update(("faceit", "", ""), 11.3), ("faceit", "", ""))

    def test_large_screen_change_is_sent_as_column_wipe(self) -> None:
        from PIL import Image
        old = Image.new("RGB", (480, 320), "#0B0F14")
        new = Image.new("RGB", (480, 320), "#1C1305")
        kind, regions = gui.plan_new_design_update(new, old)
        self.assertEqual(kind, "wipe")
        self.assertEqual([r[0] for r in regions], sorted(r[0] for r in regions))
        self.assertTrue(all(r[1] == 0 and r[3] == 320 for r in regions))
        small = old.copy()
        small.paste("#FFFFFF", (10, 10, 60, 30))
        kind, regions = gui.plan_new_design_update(small, old)
        self.assertEqual((kind, regions), ("tiles", [(10, 10, 60, 30)]))
        self.assertEqual(gui.plan_new_design_update(old, old.copy()), ("none", []))

    def test_buy_phase_economy_screen_is_removed(self) -> None:
        import screens_new as sn
        for name in ("loss_award", "buy_suggestion", "render_cs_buy", "SUGGESTION_STYLE"):
            self.assertFalse(hasattr(sn, name), name)
        ctx = sn.NewScreenContext({}, (), mode="cs2", cs_screen="buy",
                                  game=screen_app.GameSnapshot(connected=True, in_match=True,
                                                               round_phase="freezetime", money=4150))
        self.assertEqual(sn.render_new_screen(ctx).size, (sn.WIDTH, sn.HEIGHT))

    def test_cs_screen_selection_uses_official_round_phase(self) -> None:
        import screens_new as sn
        snap = screen_app.GameSnapshot
        self.assertEqual(sn.cs_screen_for(snap(round_phase="freezetime")), "live")
        self.assertEqual(sn.cs_screen_for(snap(round_phase="live", alive=False)), "death")
        self.assertEqual(sn.cs_screen_for(snap(round_phase="over", alive=True)), "death")
        self.assertEqual(sn.cs_screen_for(snap(round_phase="live", alive=True)), "live")

    def test_gsi_exposes_loss_streak_and_round_stats(self) -> None:
        state = screen_app.CS2GameState()
        state.update({
            "provider": {"steamid": "me"},
            "map": {"name": "de_mirage", "phase": "live", "mode": "competitive", "round": 13,
                    "team_ct": {"score": 6, "consecutive_round_losses": 2},
                    "team_t": {"score": 7, "consecutive_round_losses": 0}},
            "round": {"phase": "freezetime"},
            "player": {"steamid": "me", "team": "CT", "activity": "playing",
                       "state": {"health": 100, "money": 4150, "round_kills": 0,
                                 "round_killhs": 0, "round_totaldmg": 0},
                       "match_stats": {"kills": 14, "deaths": 9, "assists": 3, "mvps": 2,
                                       "score": 33}},
            "previously": {"player": {"state": {"money": 1}}},
        })
        snap = state.snapshot()
        self.assertEqual((snap.round_number, snap.score_ct, snap.score_t), (14, 6, 7))
        self.assertEqual(snap.loss_streak, 2)
        self.assertEqual(snap.round_phase, "freezetime")
        self.assertEqual((snap.match_kills, snap.match_deaths, snap.match_assists), (14, 9, 3))
        self.assertNotIn("previously", state._data)
        state.update({"map": {"name": "de_inferno", "phase": "warmup"}})
        self.assertNotIn("round", state._data)

    def test_no_bomb_timer_feature_remains(self) -> None:
        source = "\n".join(
            (Path(__file__).parent / name).read_text(encoding="utf-8").lower()
            for name in ("app.py", "gui.py", "screens_new.py")
        )
        for marker in ("bomb_seconds", "draw_bomb_panel", "bomb_timer", "defuse_duration"):
            self.assertNotIn(marker, source, marker)
        self.assertNotIn('"bomb" "1"', screen_app.gsi_config_text())

    def test_faceit_disables_gsi_listener_in_the_worker(self) -> None:
        panel = UiAndConnectionScenarios.make_panel(self)
        panel.stop_event = FastEvent()
        panel.screen_connected = TrackingEvent()
        started: list[object] = []

        class CountingGSI(FakeGSI):
            def start(self) -> None:
                started.append(self)

        patches = list(UiAndConnectionScenarios.worker_patches(self))
        patches[0] = patch.object(gui, "GSIServer", CountingGSI)
        patches[2] = patch.object(gui, "faceit_ac_is_running", return_value=True)
        screen = FakeScreen(panel.stop_event)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
                patches[6], patches[7], patches[8], patches[9], \
                patch.object(gui, "ScreenSession", return_value=screen):
            panel._screen_worker()
        self.assertEqual(started, [])
        self.assertGreaterEqual(screen.show_count, 1)
        panel.close()

    def test_every_new_screen_renders_in_both_languages(self) -> None:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "render_previews", Path(__file__).parent / "tools" / "render_previews.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        import screens_new as sn
        for language in ("tr", "en"):
            for name, context in module.contexts().items():
                context.language = language
                frame = sn.render_new_screen(context)
                self.assertEqual(frame.size, (480, 320), name)

    def test_screen_session_rebuilds_after_write_failure(self) -> None:
        calls: list[str] = []

        class FakeDisplay:
            instances = 0

            def __init__(self, com_port: str) -> None:
                FakeDisplay.instances += 1
                self.number = FakeDisplay.instances
                self.lcd_serial = SimpleNamespace(write_timeout=None, flush=lambda: None)

            def InitializeComm(self): calls.append(f"init{self.number}")
            def ScreenOn(self): calls.append("on")
            def SetOrientation(self, _o): calls.append("orientation")
            def SetBrightness(self, level): calls.append(f"brightness{level}")
            def closeSerial(self): calls.append("close")

            def DisplayPILImage(self, _image, x=0, y=0):
                if self.number == 1:
                    raise OSError("write timeout")
                calls.append("frame")

        with patch.object(screen_app, "_robust_display_class", return_value=FakeDisplay):
            session = screen_app.ScreenSession("COM9", 40)
            session.show_regions(Image.new("RGB", (480, 320)), [(0, 0, 10, 10)])
        self.assertEqual(FakeDisplay.instances, 2)
        self.assertIn("init2", calls)
        self.assertEqual(calls.count("brightness40"), 2)
        self.assertEqual(calls[-1], "frame")
        self.assertEqual(session.display.lcd_serial.write_timeout,
                         screen_app.SERIAL_WRITE_TIMEOUT_SECONDS)

    def test_gsi_rejects_browser_origin_and_unknown_token(self) -> None:
        import http.client
        state = screen_app.CS2GameState()
        with patch.object(screen_app, "GSI_PORT", 38797):
            server = screen_app.GSIServer(state)
        server.start()
        try:
            def post(body, headers=None):
                connection = http.client.HTTPConnection("127.0.0.1", 38797, timeout=3)
                connection.request("POST", "/", json.dumps(body), headers or {})
                return connection.getresponse().status

            token = screen_app.gsi_token()
            self.assertEqual(post({"auth": {"token": token}}, {"Origin": "https://x"}), 403)
            post({"auth": {"token": "cs2-screen-local-v1"}, "map": {"phase": "live"}})
            self.assertEqual(state._data, {})
            post({"auth": {"token": token}, "map": {"phase": "live"}})
            self.assertEqual(state._data.get("map"), {"phase": "live"})
        finally:
            server.close()

    def test_lghub_reader_survives_timeout_mid_frame_and_answers_ping(self) -> None:
        import socket
        import struct
        import battery_runtime
        left, right = socket.socketpair()
        try:
            left.settimeout(0.2)
            message = b'{"msgId":"1"}'
            frame = struct.pack("!BB", 0x81, len(message)) + message
            right.sendall(struct.pack("!BB", 0x89, 1) + b"x" + frame[:4])
            with self.assertRaises(socket.timeout):
                battery_runtime._receive_websocket_message(left)
            self.assertEqual(right.recv(16)[0], 0x8A)
            right.sendall(frame[4:])
            self.assertEqual(battery_runtime._receive_websocket_message(left), message.decode())
        finally:
            left.close()
            right.close()

    def test_flight_phase_tracker_scores_landings(self) -> None:
        from msfs_runtime import FlightPhaseTracker, FlightTelemetry
        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory) / "landings.json"
            tracker = FlightPhaseTracker(history)
            self.assertEqual(tracker.update(FlightTelemetry(
                on_ground=False, altitude_agl_ft=1500, vertical_speed_fpm=-700, gear_down=True,
            ), 0.0).phase, "approach")
            tracker.update(FlightTelemetry(on_ground=False, altitude_agl_ft=5,
                                           vertical_speed_fpm=-140, g_force=1.0), 10.0)
            state = tracker.update(FlightTelemetry(on_ground=True, vertical_speed_fpm=0,
                                                   g_force=1.21), 10.1)
            self.assertEqual(state.phase, "landing")
            self.assertEqual(state.landing.vertical_speed_fpm, -140)
            self.assertEqual(state.landing.rating, "soft")
            tracker.update(FlightTelemetry(on_ground=True), 17.0)
            self.assertTrue(history.is_file())
            self.assertEqual(FlightPhaseTracker(history).history[0].vertical_speed_fpm, -140)
            self.assertEqual(tracker.update(FlightTelemetry(on_ground=True), 80.0).phase, "ground")

    def test_simconnect_dll_only_from_trusted_install_locations(self) -> None:
        import msfs_runtime
        program_files = os.environ.get("ProgramFiles", r"C:\\Program Files")
        self.assertTrue(msfs_runtime.is_trusted_simconnect_location(
            Path(program_files) / "WindowsApps" / "Microsoft.Limitless" / "FlightSimulator2024.exe"
        ))
        self.assertFalse(msfs_runtime.is_trusted_simconnect_location(
            Path(tempfile.gettempdir()) / "FlightSimulator2024.exe"
        ))

    def test_simconnect_xbox_games_root_is_trusted(self) -> None:
        import msfs_runtime
        for drive in ("C:", "D:"):
            self.assertTrue(msfs_runtime.is_trusted_simconnect_location(
                drive + r"\XboxGames\Microsoft Flight Simulator 2024\Content\FlightSimulator2024.exe"
            ))
        self.assertFalse(msfs_runtime.is_trusted_simconnect_location(r"C:\Games\XboxGames\x.exe"))

    def test_simconnect_acl_check_refuses_user_writable_folder(self) -> None:
        import msfs_runtime
        with tempfile.TemporaryDirectory() as directory:
            dll = Path(directory) / "SimConnect_internal.dll"
            dll.write_bytes(b"MZ")
            trusted, reason = msfs_runtime.simconnect_trust_decision(dll)
            self.assertFalse(trusted)
            self.assertIn("writable", reason)

    def test_simconnect_acl_check_accepts_protected_folder(self) -> None:
        import msfs_runtime
        dll = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "kernel32.dll"
        with patch.object(msfs_runtime, "_is_trusted_root", return_value=False):
            trusted, reason = msfs_runtime.simconnect_trust_decision(dll)
        self.assertTrue(trusted, reason)

    def test_simconnect_refusal_is_logged_once(self) -> None:
        import msfs_runtime
        with tempfile.TemporaryDirectory() as directory:
            exe = Path(directory) / "FlightSimulator2024.exe"
            exe.write_bytes(b"MZ")
            (Path(directory) / "SimConnect_internal.dll").write_bytes(b"MZ")
            process = type("P", (), {"info": {"exe": str(exe)}})()
            with patch.object(msfs_runtime, "log_exception") as logged, \
                    patch.object(msfs_runtime, "_last_refusal", None):
                self.assertIsNone(msfs_runtime.find_simconnect_library(process))
                self.assertIsNone(msfs_runtime.find_simconnect_library(process))
            self.assertEqual(logged.call_count, 1)
            self.assertIn("MSFS skipped", logged.call_args[0][1])

    def test_design_setting_is_persisted_and_sanitised(self) -> None:
        self.assertEqual(gui.DEFAULT_SETTINGS["design"], "new")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            with patch.object(gui, "SETTINGS_PATH", path), patch.object(gui, "APP_DATA", Path(directory)):
                path.write_text(json.dumps({"design": "classic"}), encoding="utf-8")
                self.assertEqual(gui.load_settings()["design"], "classic")
                path.write_text(json.dumps({"design": "weird"}), encoding="utf-8")
                self.assertEqual(gui.load_settings()["design"], "new")

    def test_language_change_keeps_live_status_text(self) -> None:
        panel = UiAndConnectionScenarios.make_panel(self)
        panel._set_status("Mini ekran bağlı · canlı", "#FFFFFF")
        panel.language_combo.setCurrentIndex(panel.language_combo.findData("en"))
        self.assertEqual(panel.status_label.text(), "Mini ekran bağlı · canlı")
        self.assertEqual(panel.design_combo.toolTip()[:7], "Claude:")
        panel.close()


# ---------------------------------------------------------------------------
# v1.1.1 regression scenarios
# ---------------------------------------------------------------------------
from app import GameSnapshot  # noqa: E402
from battery_runtime import BatterySnapshot  # noqa: E402


class FakeLcd:
    """Records every driver call the way the Rev-A LcdComm API receives them."""

    instances: list["FakeLcd"] = []
    fail_next_image = False

    def __init__(self, com_port: str) -> None:
        self.com_port = com_port
        self.calls: list[tuple] = []
        self.images: list[tuple[Image.Image, int, int]] = []
        self.lcd_serial = SimpleNamespace(write_timeout=None, flush=lambda: None)
        FakeLcd.instances.append(self)

    def InitializeComm(self): self.calls.append(("init",))  # noqa: E704,N802
    def ScreenOn(self): self.calls.append(("on",))  # noqa: E704,N802
    def ScreenOff(self): self.calls.append(("off",))  # noqa: E704,N802
    def SetOrientation(self, value): self.calls.append(("orientation", value))  # noqa: E704,N802
    def SetBrightness(self, value): self.calls.append(("brightness", value))  # noqa: E704,N802
    def closeSerial(self): self.calls.append(("close",))  # noqa: E704,N802

    def DisplayPILImage(self, image, x=0, y=0):  # noqa: N802
        if FakeLcd.fail_next_image:
            FakeLcd.fail_next_image = False
            raise OSError("write timeout")
        self.calls.append(("image", image.size, x, y))
        self.images.append((image.copy(), x, y))

    def orientations(self) -> list:
        return [call[1] for call in self.calls if call[0] == "orientation"]


def _marker_frame() -> Image.Image:
    """A frame whose four corners are different colours (detects rotation)."""
    frame = Image.new("RGB", (480, 320), (0, 0, 0))
    draw = ImageDraw.Draw(frame)
    draw.rectangle((0, 0, 9, 9), fill=(255, 0, 0))        # top-left red
    draw.rectangle((470, 0, 479, 9), fill=(0, 255, 0))    # top-right green
    draw.rectangle((0, 310, 9, 319), fill=(0, 0, 255))    # bottom-left blue
    draw.rectangle((470, 310, 479, 319), fill=(255, 255, 255))
    return frame


class CycleEvent(FastEvent):
    """Stop event that ends the worker after ``max_cycles`` loop waits."""

    def __init__(self, max_cycles: int, hooks: dict | None = None) -> None:
        super().__init__()
        self.max_cycles = max_cycles
        self.hooks = hooks or {}
        self.cycles = 0

    def wait(self, _timeout: float | None = None) -> bool:
        self.cycles += 1
        hook = self.hooks.get(self.cycles)
        if hook is not None:
            hook()
        if self.cycles >= self.max_cycles:
            self.flag = True
        return self.flag


class RecordingScreen:
    def __init__(self, port, brightness, rotation) -> None:
        self.args = (port, brightness, rotation)
        self.calls: list[tuple] = []
        self.closed = False

    def configure(self, brightness, rotation):
        self.calls.append(("configure", brightness, rotation))
        return None

    def show(self, image) -> None:
        self.calls.append(("show", image.size))

    def show_regions(self, image, regions) -> None:
        self.calls.append(("regions", image.size, tuple(regions)))

    def power_off(self) -> None:
        self.calls.append(("power_off",))

    def close(self) -> None:
        self.closed = True


def _varying_frames():
    counter = {"n": 0}

    def render(*_args, **_kwargs):
        counter["n"] += 1
        shade = (counter["n"] * 37) % 255
        return Image.new("RGB", (480, 320), (shade, 0, 0))
    return render


class HermeticEnvironmentScenarios(unittest.TestCase):
    """Tests must never see or change the real user profile or hardware."""

    def test_all_data_paths_point_into_the_temporary_profile(self) -> None:
        import app_log
        import msfs_runtime
        root = _TEST_ROOT.resolve()
        for path in (
            gui.APP_DATA, gui.SETTINGS_PATH, gui.RUNTIME_LOG_PATH, gui.INSTANCE_STATE_PATH,
            gui.STARTUP_TASK_STATE_PATH, screen_app.APP_DATA, screen_app.GSI_TOKEN_PATH,
            screen_app.SENSOR_CACHE_PATH, screen_app.DEATHMATCH_CACHE_PATH,
            app_log.LOG_DIR, msfs_runtime.LANDING_HISTORY_PATH,
        ):
            self.assertTrue(Path(path).resolve().is_relative_to(root), path)
            self.assertFalse(Path(path).resolve().is_relative_to(_REAL_APP_DATA.resolve()), path)

    def test_hardware_processes_and_tasks_are_blocked(self) -> None:
        import serial
        import subprocess
        import winreg
        import psutil
        with self.assertRaises(HardwareAccessBlocked):
            serial.Serial("COM3", 115200)
        with self.assertRaises(HardwareAccessBlocked):
            subprocess.run(["schtasks.exe", "/Query"])
        with self.assertRaises(HardwareAccessBlocked):
            subprocess.Popen(["SabasakalSensorHost.exe"])
        with self.assertRaises(HardwareAccessBlocked):
            winreg.SetValueEx(None, "x", 0, 1, "y")
        with self.assertRaises(HardwareAccessBlocked):
            psutil.Process(os.getpid()).terminate()
        with self.assertRaises(HardwareAccessBlocked):
            screen_app.ScreenSession("COM3", 50)
        with self.assertRaises(HardwareAccessBlocked):
            gui.install_gsi_config()

    def test_set_windows_autostart_cannot_reach_the_real_scheduler(self) -> None:
        with self.assertRaises(Exception):
            gui.set_windows_autostart(True)


class ZZRealProfileGuardScenarios(unittest.TestCase):
    """Runs last (alphabetical order): nothing may have touched the real profile."""

    # Files a concurrently running real Sabasakal instance legitimately rewrites.
    LIVE_APP_FILES = {
        "runtime.log", "runtime-current.log", "elevated-sensors.json", "instance.json",
        "show-window.request", "show-window.ack", "msfs-landings.json",
    }

    def _real_instance_running(self) -> bool:
        import psutil
        names = {"sabasakal-mini-screen.exe", "sabasakalsensorhost.exe"}
        try:
            return any(
                (process.info.get("name") or "").lower() in names
                for process in psutil.process_iter(["name"])
            )
        except Exception:
            return True

    def test_real_app_data_was_not_modified(self) -> None:
        after = _snapshot_tree(_REAL_APP_DATA)
        live = self._real_instance_running()
        changed = []
        for path in set(after) | set(_REAL_APP_DATA_BEFORE):
            if after.get(path) == _REAL_APP_DATA_BEFORE.get(path):
                continue
            relative = Path(path).relative_to(_REAL_APP_DATA)
            if live and (relative.parts[0] in ("logs", "sensor-host-runtime")
                         or relative.name in self.LIVE_APP_FILES):
                continue
            changed.append(str(relative))
        self.assertEqual(sorted(changed), [], "tests wrote under the real %APPDATA%\\CS2Screen")

    def test_test_profile_received_the_writes(self) -> None:
        gui.save_settings_file(default_settings())
        self.assertTrue((_TEST_ROOT / "Roaming" / "CS2Screen" / "settings.json").is_file())


class Fix111Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        FakeLcd.instances = []
        FakeLcd.fail_next_image = False
        autostart = patch.object(gui, "set_windows_autostart")
        autostart.start()
        self.addCleanup(autostart.stop)

    def make_panel(self, settings: dict | None = None, language: str = "tr") -> gui.ControlPanel:
        data = default_settings()
        data["language"] = language
        if settings:
            data.update(settings)
        with patch.object(gui, "load_settings", return_value=data), patch.object(
            gui, "get_auto_hardware_labels", return_value={
                "cpu": "9800X3D", "gpu": "RTX 4070 Ti", "ram": "32 GB RAM",
            },
        ), patch.object(gui, "get_metrics", return_value={}):
            panel = gui.ControlPanel()
        self.addCleanup(panel.close)
        return panel

    def worker_patches(self, *, cs2=False, faceit=False, msfs=False, game=None, weather=None):
        state = SimpleNamespace(snapshot=lambda: game or GameSnapshot())
        return [
            patch.object(gui, "GSIServer", FakeGSI),
            patch.object(gui, "CS2GameState", return_value=state),
            patch.object(gui, "cs2_is_running", return_value=cs2),
            patch.object(gui, "faceit_ac_is_running", return_value=faceit),
            patch.object(gui, "msfs_session_seconds", return_value=60.0 if msfs else None),
            patch.object(gui, "get_metrics", return_value={}),
            patch.object(gui, "MediaSessionMonitor", side_effect=lambda: FakeMonitor(MediaSnapshot())),
            patch.object(gui, "AudioSpectrumMonitor", side_effect=lambda: FakeMonitor(AudioSpectrumSnapshot())),
            patch.object(gui, "BatteryDeviceMonitor", side_effect=lambda **_k: FakeMonitor(BatterySnapshot())),
            patch.object(gui, "MSFSTelemetryMonitor", side_effect=lambda: FakeMonitor(FlightTelemetry())),
            patch.object(
                gui, "WeatherMonitor",
                side_effect=weather or (lambda *_a: FakeMonitor(WeatherSnapshot())),
            ),
            patch.object(gui.FACEIT_PING_MONITOR, "read", return_value=[]),
            patch.object(gui.STEAM_PING_MONITOR, "read", return_value=[]),
        ]

    def run_worker(self, panel, *, cycles=3, hooks=None, design_renders=None, classic_renders=None,
                   **worker_kwargs):
        """Run ``_screen_worker`` synchronously; return the created screens."""
        panel.stop_event = CycleEvent(cycles, hooks)
        panel.screen_connected = TrackingEvent()
        screens: list[RecordingScreen] = []

        def factory(*args):
            screen = RecordingScreen(*args)
            screens.append(screen)
            return screen

        new_render = _varying_frames()
        classic_render = _varying_frames()

        def record_new(context):
            if design_renders is not None:
                design_renders.append(context)
            return new_render()

        def record_classic(*args, **kwargs):
            if classic_renders is not None:
                classic_renders.append(kwargs)
            return classic_render()

        patches = self.worker_patches(**worker_kwargs) + [
            patch.object(gui, "ScreenSession", side_effect=factory),
            patch.object(gui, "render_new_screen", side_effect=record_new),
            patch.object(gui, "render_screen", side_effect=record_classic),
        ]
        for item in patches:
            item.start()
        try:
            panel._screen_worker()
        finally:
            for item in reversed(patches):
                item.stop()
        return screens


class SettingsRoundTripScenarios(Fix111Base):
    def test_every_gui_setting_survives_widget_file_load_and_worker(self) -> None:
        custom = {"Gece": dict(gui.PALETTES["Kuzey"])}
        panel = self.make_panel({"custom_palettes": custom})
        panel.brightness_slider.setValue(73)
        panel.auto_brightness_check.setChecked(False)
        panel.rotation_combo.setCurrentIndex(panel.rotation_combo.findData("180"))
        panel.design_combo.setCurrentIndex(panel.design_combo.findData("classic"))
        panel.msfs_layout_combo.setCurrentIndex(panel.msfs_layout_combo.findData("classic"))
        panel.autostart_check.setChecked(True)
        panel.language_combo.setCurrentIndex(panel.language_combo.findData("en"))
        for field, city in zip(panel.weather_city_inputs, ("Ankara", "Izmir", "Berlin")):
            field.setText(city)
        panel.hardware_inputs["cpu"].setText("My CPU")
        palette_index = panel.palette_combo.findData("Gece")
        self.assertGreaterEqual(palette_index, 0)
        panel.palette_combo.setCurrentIndex(palette_index)
        self.assertTrue(panel.save_settings(quiet=True))

        saved = json.loads(gui.SETTINGS_PATH.read_text(encoding="utf-8"))
        loaded = gui.load_settings()
        for data in (saved, loaded):
            self.assertEqual(data["brightness"], 73)
            self.assertFalse(data["auto_brightness"])
            self.assertEqual(data["rotation"], "180")
            self.assertEqual(data["design"], "classic")
            self.assertEqual(data["msfs_layout"], "classic")
            self.assertTrue(data["autostart"])
            self.assertEqual(data["language"], "en")
            self.assertEqual(list(data["weather_cities"])[:3], ["Ankara", "Izmir", "Berlin"])
            self.assertEqual(data["palette"], "Gece")
            self.assertIn("Gece", data["custom_palettes"])
            self.assertEqual(data["hardware_labels"]["cpu"], "My CPU")

        # A fresh panel built from the saved file shows the same values ...
        with patch.object(gui, "get_auto_hardware_labels", return_value={
            "cpu": "", "gpu": "", "ram": "",
        }), patch.object(gui, "get_metrics", return_value={}):
            reloaded = gui.ControlPanel()
        self.addCleanup(reloaded.close)
        self.assertEqual(reloaded.rotation_combo.currentData(), "180")
        self.assertEqual(reloaded.design_combo.currentData(), "classic")
        self.assertEqual(reloaded.msfs_layout_combo.currentData(), "classic")
        self.assertEqual(reloaded.brightness_slider.value(), 73)
        self.assertTrue(reloaded.autostart_check.isChecked())
        self.assertEqual(reloaded.language, "en")
        self.assertEqual(reloaded._selected_palette_name(), "Gece")

        # ... and the worker passes them on to the display and renderers.
        renders: list[dict] = []
        cities: list = []

        def weather(names):
            cities.append(list(names))
            return FakeMonitor(WeatherSnapshot())

        screens = self.run_worker(reloaded, classic_renders=renders, weather=weather)
        self.assertEqual(screens[0].args, ("AUTO", 73, "180"))
        self.assertTrue(renders)
        self.assertEqual(renders[0]["msfs_layout"], "classic")
        self.assertEqual(renders[0]["language"], "en")
        self.assertEqual(cities[0][:3], ["Ankara", "Izmir", "Berlin"])

    def test_auto_brightness_reaches_the_display(self) -> None:
        panel = self.make_panel({"auto_brightness": True, "brightness": 20})
        with patch.object(gui, "effective_brightness", return_value=90):
            screens = self.run_worker(panel)
        self.assertEqual(screens[0].args[1], 90)

    def test_invalid_saved_rotation_and_design_fall_back_safely(self) -> None:
        gui.SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        gui.SETTINGS_PATH.write_text(json.dumps({"rotation": "90", "design": "fancy"}), encoding="utf-8")
        loaded = gui.load_settings()
        self.assertEqual(loaded["rotation"], "normal")
        self.assertEqual(loaded["design"], "new")


class OrientationScenarios(Fix111Base):
    def session(self, rotation: str) -> screen_app.ScreenSession:
        with patch.object(screen_app, "_robust_display_class", return_value=FakeLcd):
            return screen_app.ScreenSession("COM7", 50, rotation)

    def landscape(self):
        from library.lcd.lcd_comm import Orientation
        return Orientation.LANDSCAPE

    def assert_panel_matches(self, lcd_image: Image.Image, frame: Image.Image, rotation: str) -> None:
        expected = frame.transpose(Image.Transpose.ROTATE_180) if rotation == "180" else frame
        self.assertIsNone(ImageChops.difference(lcd_image, expected).getbbox())

    def test_every_orientation_on_connect_reconnect_and_change(self) -> None:
        frame = _marker_frame()
        for rotation in screen_app.SUPPORTED_ROTATIONS:
            with self.subTest(rotation=rotation):
                FakeLcd.instances = []
                with patch.object(screen_app, "_robust_display_class", return_value=FakeLcd):
                    session = screen_app.ScreenSession("COM7", 50, rotation)
                    lcd = FakeLcd.instances[0]
                    # Hardware is always driven in landscape; rotation is in pixels.
                    self.assertEqual(lcd.orientations(), [self.landscape()])
                    order = [call[0] for call in lcd.calls]
                    self.assertLess(order.index("on"), order.index("orientation"))
                    session.show(frame)
                    self.assert_panel_matches(lcd.images[-1][0], frame, rotation)
                    self.assertEqual(lcd.images[-1][1:], (0, 0))

                    # Simulated USB failure: rebuilt session keeps orientation.
                    FakeLcd.fail_next_image = True
                    session.show(frame)
                    self.assertEqual(len(FakeLcd.instances), 2)
                    rebuilt = FakeLcd.instances[1]
                    self.assertEqual(rebuilt.orientations(), [self.landscape()])
                    self.assert_panel_matches(rebuilt.images[-1][0], frame, rotation)

                    # Change while running.
                    other = "normal" if rotation == "180" else "180"
                    self.assertTrue(session.configure(50, other))
                    self.assertEqual(rebuilt.orientations()[-1], self.landscape())
                    session.show(frame)
                    self.assert_panel_matches(rebuilt.images[-1][0], frame, other)
                    self.assertFalse(session.configure(60, other))
                    session.close()

    def test_partial_regions_use_rotated_panel_coordinates(self) -> None:
        frame = _marker_frame()
        regions = [(0, 0, 10, 10), (470, 0, 480, 10), (120, 40, 240, 80)]
        expected_positions = {
            "normal": [(0, 0), (470, 0), (120, 40)],
            "180": [(470, 310), (0, 310), (240, 240)],
        }
        for rotation, positions in expected_positions.items():
            with self.subTest(rotation=rotation):
                FakeLcd.instances = []
                session = self.session(rotation)
                session.show_regions(frame, regions)
                lcd = FakeLcd.instances[0]
                self.assertEqual([(x, y) for _image, x, y in lcd.images], positions)
                panel = frame.transpose(Image.Transpose.ROTATE_180) if rotation == "180" else frame
                for (image, x, y) in lcd.images:
                    crop = panel.crop((x, y, x + image.width, y + image.height))
                    self.assertIsNone(ImageChops.difference(image, crop).getbbox())
                # The red top-left marker is sent wherever the panel shows it.
                self.assertEqual(lcd.images[0][0].getpixel((5, 5)), (255, 0, 0))

    def test_partial_updates_rebuild_the_full_rotated_frame(self) -> None:
        import screens_new
        frame = screens_new.render_new_screen(screens_new.NewScreenContext(
            {"cpu_load": 10.0}, (), (), language="tr",
        ))
        for rotation in screen_app.SUPPORTED_ROTATIONS:
            FakeLcd.instances = []
            session = self.session(rotation)
            session.show_regions(frame, list(gui.NEW_DESIGN_TILES))
            canvas = Image.new("RGB", (480, 320))
            for image, x, y in FakeLcd.instances[0].images:
                canvas.paste(image, (x, y))
            self.assert_panel_matches(canvas, frame, rotation)

    def test_frames_of_both_designs_are_panel_sized_for_every_orientation(self) -> None:
        import screens_new
        metrics = {
            "cpu_load": 24.0, "cpu_temp": 46.0, "cpu_freq": 4.6,
            "gpu_load": 18.0, "gpu_temp": 42.0, "gpu_memory": 920.0,
            "ram_load": 52.0, "ram_used": 16.5,
        }
        classic = screen_app.render_screen(metrics, GameSnapshot(), False, dict(gui.PALETTES["Kuzey"]))
        new = screens_new.render_new_screen(screens_new.NewScreenContext({}, (), ()))
        for frame in (classic, new):
            for rotation in screen_app.SUPPORTED_ROTATIONS:
                self.assertEqual(frame.size, (480, 320))
                self.assertEqual(screen_app.orient_frame(frame, rotation).size, (480, 320))

    def test_worker_applies_rotation_on_start_and_when_changed_while_running(self) -> None:
        for design in ("new", "classic"):
            with self.subTest(design=design):
                panel = self.make_panel({"design": design, "rotation": "180"})

                def flip() -> None:
                    panel.settings = dict(panel.settings, rotation="normal")

                screens = self.run_worker(panel, cycles=4, hooks={2: flip})
                self.assertEqual(len(screens), 1)
                screen = screens[0]
                self.assertEqual(screen.args[2], "180")
                configure = [call for call in screen.calls if call[0] == "configure"]
                self.assertEqual(configure[0][2], "normal")
                after = screen.calls[screen.calls.index(configure[0]) + 1]
                self.assertEqual(after[0], "show", "rotation change must resend a full frame")

    def test_gui_preview_follows_rotation(self) -> None:
        panel = self.make_panel({"rotation": "180"})
        self.assertEqual(panel._rotation(), "180")
        panel.rotation_combo.setCurrentIndex(panel.rotation_combo.findData("normal"))
        self.assertEqual(panel._snapshot_settings()["rotation"], "normal")


class DesignSwitchScenarios(Fix111Base):
    def test_classic_design_never_uses_the_new_renderer(self) -> None:
        cases = {
            "desktop": {}, "cs2": {"cs2": True, "game": GameSnapshot(connected=True, in_match=True)},
            "faceit": {"faceit": True}, "msfs": {"msfs": True},
        }
        for name, kwargs in cases.items():
            with self.subTest(mode=name):
                panel = self.make_panel({"design": "classic"})
                new_calls: list = []
                classic_calls: list = []
                self.run_worker(panel, design_renders=new_calls, classic_renders=classic_calls, **kwargs)
                self.assertEqual(new_calls, [])
                self.assertTrue(classic_calls)

    def test_new_design_selects_the_mode_for_every_context(self) -> None:
        cases = {
            "desktop": ({}, "desktop"),
            "cs2-buy": ({"cs2": True, "game": GameSnapshot(connected=True, in_match=True,
                                                          round_phase="freezetime")}, "cs2"),
            "cs2-death": ({"cs2": True, "game": GameSnapshot(connected=True, in_match=True,
                                                            round_phase="live", alive=False)}, "cs2"),
            "cs2-live": ({"cs2": True, "game": GameSnapshot(connected=True, in_match=True,
                                                           round_phase="live", alive=True)}, "cs2"),
            "faceit": ({"faceit": True, "cs2": True}, "faceit"),
            "msfs": ({"msfs": True}, "msfs"),
        }
        expected_screen = {"cs2-buy": "live", "cs2-death": "death", "cs2-live": "live"}
        for name, (kwargs, mode) in cases.items():
            with self.subTest(case=name):
                panel = self.make_panel({"design": "new"})
                new_calls: list = []
                classic_calls: list = []
                with patch.object(gui, "DEATH_SCREEN_DELAY", 0.0):
                    self.run_worker(panel, design_renders=new_calls, classic_renders=classic_calls, **kwargs)
                self.assertEqual(classic_calls, [])
                self.assertTrue(new_calls)
                self.assertEqual(new_calls[-1].mode, mode)
                if name in expected_screen:
                    self.assertEqual(new_calls[-1].cs_screen, expected_screen[name])
                if mode == "faceit":
                    self.assertIsNone(new_calls[-1].game)
                if mode == "msfs":
                    self.assertIsNotNone(new_calls[-1].flight_state)

    def test_switching_design_while_running_resets_frame_caches(self) -> None:
        for start, target in (("new", "classic"), ("classic", "new")):
            with self.subTest(start=start):
                panel = self.make_panel({"design": start})

                def switch() -> None:
                    panel.settings = dict(panel.settings, design=target)

                new_calls: list = []
                classic_calls: list = []
                screens = self.run_worker(
                    panel, cycles=4, hooks={2: switch},
                    design_renders=new_calls, classic_renders=classic_calls,
                )
                shows = [call for call in screens[0].calls if call[0] == "show"]
                # first frame + full frame after the switch + blank stop frame
                self.assertGreaterEqual(len(shows), 3)
                self.assertTrue(new_calls)
                self.assertTrue(classic_calls)

    def test_start_button_saves_and_uses_the_selected_design(self) -> None:
        panel = self.make_panel({"design": "new"})
        panel.design_combo.setCurrentIndex(panel.design_combo.findData("classic"))
        started: list = []
        with patch.object(panel, "_install_gsi"), patch.object(
            gui.threading.Thread, "start", lambda thread: started.append(thread),
        ):
            panel.start_screen()
        self.assertEqual(panel.settings["design"], "classic")
        self.assertEqual(gui.load_settings()["design"], "classic")
        self.assertEqual(len(started), 1)
        panel.worker = None
        new_calls: list = []
        classic_calls: list = []
        self.run_worker(panel, design_renders=new_calls, classic_renders=classic_calls)
        self.assertEqual(new_calls, [])
        self.assertTrue(classic_calls)

    def test_design_and_msfs_combos_are_distinct_and_independent(self) -> None:
        panel = self.make_panel()
        panel.msfs_layout_combo.setCurrentIndex(panel.msfs_layout_combo.findData("classic"))
        self.assertEqual(panel._snapshot_settings()["design"], "new")
        panel.design_combo.setCurrentIndex(panel.design_combo.findData("classic"))
        snapshot = panel._snapshot_settings()
        self.assertEqual((snapshot["design"], snapshot["msfs_layout"]), ("classic", "classic"))


class ScreenCardLayoutScenarios(Fix111Base):
    def card_widgets(self, panel):
        from PySide6.QtWidgets import QLabel
        box = panel.design_combo.parentWidget()
        labels = [w for w in box.findChildren(QLabel) if w.parentWidget() is box and w.isVisible()]
        controls = [
            panel.auto_brightness_check, panel.brightness_slider, panel.rotation_combo,
            panel.autostart_check, panel.msfs_layout_combo, panel.design_combo,
        ]
        return box, labels + controls

    def test_screen_card_rows_do_not_overlap(self) -> None:
        for language in ("tr", "en"):
            for size_name, design in (
                ("default", "new"), ("minimum", "new"),
                ("default", "classic"), ("minimum", "classic"),
            ):
                with self.subTest(language=language, size=size_name, design=design):
                    panel = self.make_panel(language=language)
                    panel.design_combo.setCurrentIndex(panel.design_combo.findData(design))
                    panel.show()
                    if size_name == "minimum":
                        panel.resize(panel.minimumSize())
                    else:
                        panel.resize(*gui.WINDOW_DEFAULT_SIZE)
                    self.app.processEvents()
                    box, widgets = self.card_widgets(panel)
                    box_rect = box.rect().translated(box.mapTo(panel, box.rect().topLeft()))
                    rects = []
                    widgets = [widget for widget in widgets if not widget.isHidden()]
                    if design == "classic":
                        self.assertIn(panel.msfs_layout_combo, widgets)
                    else:
                        self.assertNotIn(panel.msfs_layout_combo, widgets)
                    for widget in widgets:
                        self.assertTrue(widget.isVisible(), widget)
                        top_left = widget.mapTo(panel, widget.rect().topLeft())
                        rect = widget.rect().translated(top_left)
                        self.assertGreater(rect.height(), 0)
                        self.assertTrue(box_rect.contains(rect), (type(widget).__name__, rect, box_rect))
                        rects.append((widget, rect))
                    for index, (first, a) in enumerate(rects):
                        for second, b in rects[index + 1:]:
                            self.assertFalse(
                                a.intersects(b),
                                f"{type(first).__name__}:{getattr(first, 'text', lambda: '')()} overlaps "
                                f"{type(second).__name__}:{getattr(second, 'text', lambda: '')()}",
                            )
                    self.assertGreaterEqual(
                        panel.quick_card.height(), panel.quick_card.minimumSizeHint().height(),
                    )
                    for combo in (panel.rotation_combo, panel.design_combo, panel.msfs_layout_combo):
                        if combo.isHidden():
                            continue
                        # Offscreen font metrics are inflated; only check the
                        # guaranteed minimum width and the shared column.
                        self.assertGreaterEqual(combo.width(), 150)
                        self.assertEqual(combo.x(), panel.rotation_combo.x())
                    panel.hide()


class WorkerLifecycleScenarios(Fix111Base):
    def test_start_stop_cycles_leak_nothing_and_apply_settings(self) -> None:
        panel = self.make_panel()
        baseline = {thread.ident for thread in threading.enumerate()}
        sessions: list[RecordingScreen] = []

        def factory(*args):
            screen = RecordingScreen(*args)
            sessions.append(screen)
            return screen

        patches = self.worker_patches() + [
            patch.object(gui, "ScreenSession", side_effect=factory),
            patch.object(gui, "render_new_screen", side_effect=_varying_frames()),
            patch.object(gui, "render_screen", side_effect=_varying_frames()),
            patch.object(panel, "_install_gsi"),
        ]
        for item in patches:
            item.start()
        try:
            for cycle, (rotation, design) in enumerate((
                ("normal", "new"), ("180", "classic"), ("normal", "classic"), ("180", "new"),
            )):
                panel.rotation_combo.setCurrentIndex(panel.rotation_combo.findData(rotation))
                panel.design_combo.setCurrentIndex(panel.design_combo.findData(design))
                panel.start_screen()
                worker = panel.worker
                self.assertTrue(worker.is_alive())
                deadline = time.monotonic() + 5
                while len(sessions) <= cycle and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(sessions[cycle].args[2], rotation)
                self.assertEqual(panel.settings["design"], design)
                panel.stop_screen()
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
                self.app.processEvents()
                self.assertTrue(sessions[cycle].closed)
                self.assertIn(("power_off",), sessions[cycle].calls)
                self.assertTrue(panel.start_button.isEnabled())
        finally:
            for item in reversed(patches):
                item.stop()
        time.sleep(0.2)
        leaked = [
            thread.name for thread in threading.enumerate()
            if thread.ident not in baseline and thread.is_alive()
            and not thread.name.startswith(("autostart", "Dummy"))
        ]
        self.assertEqual(leaked, [])


class ExitScenarios(Fix111Base):
    def test_exit_without_worker_quits_the_application(self) -> None:
        panel = self.make_panel()
        fake_app = Mock()
        with patch.object(gui.QApplication, "instance", return_value=fake_app):
            panel._exit_application()
        fake_app.quit.assert_called_once()
        self.assertTrue(panel._exit_requested)

    def test_exit_with_running_worker_stops_it_then_quits(self) -> None:
        panel = self.make_panel()
        panel.worker = Mock(is_alive=Mock(return_value=True))
        panel.stop_event = threading.Event()
        fake_app = Mock()
        with patch.object(gui.QTimer, "singleShot") as single_shot:
            panel._exit_application()
        self.assertTrue(panel.stop_event.is_set())
        single_shot.assert_called_once()
        delay, force_exit = single_shot.call_args.args
        self.assertEqual(delay, gui.EXIT_FALLBACK_MS)
        # Worker reports stopped -> application quits.
        panel.worker = Mock(is_alive=Mock(return_value=False))
        with patch.object(gui.QApplication, "instance", return_value=fake_app):
            panel._screen_stopped()
        fake_app.quit.assert_called_once()
        # A blocked worker triggers the hard fallback that kills helpers.
        panel.worker = Mock(is_alive=Mock(return_value=True))
        with patch.object(gui, "shutdown_child_processes") as shutdown, patch.object(
            gui.os, "_exit",
        ) as hard_exit, patch.object(gui, "_cleanup_instance_state"):
            force_exit()
        shutdown.assert_called_once()
        hard_exit.assert_called_once_with(0)

    def test_shutdown_terminates_sensor_host_and_bridge(self) -> None:
        process = Mock()
        process.poll.return_value = None
        with patch.object(gui, "_SENSOR_HOST_PROCESS", process), patch.object(
            gui.SENSOR_MONITOR, "close",
        ) as bridge_close:
            gui.shutdown_child_processes()
            self.assertIsNone(gui._SENSOR_HOST_PROCESS)
        process.terminate.assert_called_once()
        process.wait.assert_called()
        bridge_close.assert_called_once()

    def test_sensor_host_is_killed_when_terminate_times_out(self) -> None:
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [gui.subprocess.TimeoutExpired("host", 2.0), 0]
        with patch.object(gui, "_SENSOR_HOST_PROCESS", process):
            gui.stop_sensor_host()
        process.kill.assert_called_once()

    def test_main_shuts_down_children_when_event_loop_ends(self) -> None:
        fake_app = Mock()
        fake_app.exec.return_value = 0
        fake_panel = Mock(tray_icon=None, settings={"autostart": False})
        with patch.object(gui, "prepare_runtime_working_directory"), \
                patch.object(gui, "_acquire_single_instance", return_value=True), \
                patch.object(gui, "QApplication", return_value=fake_app), \
                patch.object(gui, "ControlPanel", return_value=fake_panel), \
                patch.object(gui, "present_initial_window"), \
                patch.object(gui, "reconcile_windows_autostart"), \
                patch.object(gui, "ensure_sensor_host"), \
                patch.object(gui.QTimer, "singleShot"), \
                patch.object(gui, "_cleanup_instance_state") as cleanup, \
                patch.object(gui, "shutdown_child_processes") as shutdown, \
                patch.object(gui, "_RECOVERED_STALE_INSTANCE", False), \
                patch.object(sys, "argv", ["Sabasakal-Mini-Screen.exe"]):
            self.assertEqual(gui.main(), 0)
        cleanup.assert_called_once()
        shutdown.assert_called_once()
        fake_app.aboutToQuit.connect.assert_called()


class BatteryPresenceAndRamScenarios(unittest.TestCase):
    """Only connected devices are drawn; RAM appears only where it fits."""

    KINDS = ("mouse", "keyboard", "headset")

    @staticmethod
    def devices(kinds, percent=64):
        names = {"mouse": "PRO X", "keyboard": "G915", "headset": "PRO X WIRELESS"}
        return tuple(BatteryDevice(f"id-{kind}", names[kind], percent, kind) for kind in kinds)

    def device_combinations(self):
        import itertools
        for count in range(4):
            for kinds in itertools.combinations(self.KINDS, count):
                yield kinds

    @staticmethod
    def metrics(extreme=False):
        if extreme:
            return {
                "cpu_load": 100.0, "cpu_temp": 100.0, "gpu_load": 100.0, "gpu_temp": 100.0,
                "ram_load": 100.0, "ram_used_gb": 127.9, "ram_total_gb": 128.0,
                "cpu_freq": 5.9, "cpu_power": 250.0, "gpu_memory": 24000.0, "gpu_power": 450.0,
            }
        return {
            "cpu_load": 38.0, "cpu_temp": 62.0, "gpu_load": 22.0, "gpu_temp": 48.0,
            "ram_load": 52.0, "ram_used_gb": 16.6, "ram_total_gb": 32.0,
        }

    def assert_disjoint(self, boxes, label):
        for index, (name_a, a) in enumerate(boxes):
            self.assertGreaterEqual(a[0], 0, (label, name_a, a))
            self.assertLessEqual(a[2], 480, (label, name_a, a))
            self.assertGreaterEqual(a[1], 0, (label, name_a, a))
            self.assertLessEqual(a[3], 320, (label, name_a, a))
            for name_b, b in boxes[index + 1:]:
                overlap = a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]
                self.assertFalse(overlap, (label, name_a, a, name_b, b))

    def test_present_batteries_drops_absent_and_unknown_devices(self) -> None:
        import screens_new as sn
        devices = (
            BatteryDevice("h", "Headset", 18, "headset"),
            SimpleNamespace(kind="keyboard", percent=None),
            BatteryDevice("m", "Mouse", 82, "mouse"),
            BatteryDevice("m2", "Mouse 2", 50, "mouse"),
        )
        items = sn.present_batteries(devices)
        self.assertEqual([(item.kind, item.percent) for item in items], [("mouse", 82), ("headset", 18)])
        self.assertEqual(sn.present_batteries(()), [])

    def test_desktop_layout_for_every_device_combination(self) -> None:
        import screens_new as sn
        from weather_runtime import WeatherReading as Reading
        for language in ("tr", "en"):
            for extreme in (False, True):
                for kinds in self.device_combinations():
                    label = (language, extreme, kinds)
                    ctx = sn.NewScreenContext(
                        self.metrics(extreme), self.devices(kinds),
                        (Reading("Kahramanmaraş", 18.0, 61),), language=language,
                    )
                    layout = sn.main_layout(ctx)
                    self.assertEqual([info.kind for info, _ in layout["batteries"]], list(kinds), label)
                    boxes = [(key, layout[key]) for key in ("clock", "ram", "weather", "cpu", "gpu")
                             if key in layout]
                    boxes += [(f"battery:{info.kind}", box) for info, box in layout["batteries"]]
                    self.assert_disjoint(boxes, label)
                    self.assertIn("ram", layout, label)
                    ram_text = "".join(part[0] for part in layout["ram_parts"])
                    self.assertNotIn("…", ram_text)
                    self.assertIn("/", ram_text)
                    if not kinds:
                        self.assertEqual(layout["cpu"][3], 320 - 14, "CPU/GPU take the battery row")
                    else:
                        widths = {round(box[2] - box[0]) for _info, box in layout["batteries"]}
                        self.assertLessEqual(max(widths) - min(widths), 1, "items share the row evenly")
                        self.assertEqual(layout["batteries"][0][1][0], 14)
                        self.assertEqual(layout["batteries"][-1][1][2], 480 - 14)
                    frame = sn.render_new_screen(ctx)
                    self.assertEqual(frame.size, (480, 320))

    def test_strip_layout_for_every_device_combination(self) -> None:
        import screens_new as sn
        for language in ("tr", "en"):
            for extreme in (False, True):
                for kinds in self.device_combinations():
                    for allow_ram in (True, False):
                        label = (language, extreme, kinds, allow_ram)
                        ctx = sn.NewScreenContext(
                            self.metrics(extreme), self.devices(kinds), language=language,
                            fps=999 if extreme else 287, mode="cs2",
                        )
                        image = Image.new("RGB", (480, 320))
                        layout = sn.draw_strip(image, ctx, "#000000", "#FFFFFF", allow_ram=allow_ram)
                        names = [name for name, _box in layout]
                        self.assertEqual(
                            sorted(name.split(":")[1] for name in names if name.startswith("battery:")),
                            sorted(kinds), label,
                        )
                        self.assertEqual(names[:3], ["cpu", "gpu", "fps"])
                        if not allow_ram:
                            self.assertNotIn("ram", names)
                        self.assert_disjoint(layout, label)
                        boxes = dict(layout)
                        if "ram" in boxes:
                            batteries = [box for name, box in layout if name.startswith("battery:")]
                            if batteries:
                                self.assertLessEqual(boxes["ram"][2] + 12, min(b[0] for b in batteries))
                # With normal values and up to two devices RAM always fits.
                ctx = sn.NewScreenContext(self.metrics(), self.devices(("mouse", "headset")),
                                          language=language, fps=287, mode="cs2")
                layout = sn.draw_strip(Image.new("RGB", (480, 320)), ctx, "#000", "#FFF")
                self.assertIn("ram", dict(layout))

    def test_faceit_and_msfs_have_no_ram_and_reflow_batteries(self) -> None:
        import screens_new as sn
        for kinds in self.device_combinations():
            ctx = sn.NewScreenContext(self.metrics(), self.devices(kinds), mode="faceit")
            items = sn.faceit_battery_layout(ctx)
            self.assertEqual([item[0].kind for item in items], list(kinds))
            spans = [(item[1], item[1] + item[4] + item[6]) for item in items]
            for left, right in spans:
                self.assertGreaterEqual(left, 26 - 0.5)
                self.assertLessEqual(right, 480 - 26 + 0.5)
            for (_l1, r1), (l2, _r2) in zip(spans, spans[1:]):
                self.assertLess(r1, l2)
            frame = sn.render_new_screen(ctx)
            self.assertEqual(frame.size, (480, 320))
        # The MSFS strip never carries RAM.
        from msfs_runtime import FlightPhaseState
        ctx = sn.NewScreenContext(self.metrics(), (), mode="msfs", flight=FlightTelemetry(),
                                  flight_state=FlightPhaseState(phase="cruise"))
        image = Image.new("RGB", (480, 320))
        with patch.object(sn, "draw_strip", wraps=sn.draw_strip) as strip:
            sn.render_new_screen(ctx)
        self.assertFalse(strip.call_args.kwargs.get("allow_ram", True))
        del image

    def test_absent_devices_draw_no_placeholder(self) -> None:
        import screens_new as sn
        drawn: list[str] = []
        original = sn.text

        def capture(draw, xy, value, *args, **kwargs):
            drawn.append(value)
            return original(draw, xy, value, *args, **kwargs)

        for mode in ("desktop", "cs2", "faceit"):
            drawn.clear()
            ctx = sn.NewScreenContext(self.metrics(), self.devices(("mouse",)), mode=mode, fps=200,
                                      game=GameSnapshot(connected=True, in_match=True), cs_screen="live")
            with patch.object(sn, "text", side_effect=capture):
                sn.render_new_screen(ctx)
            joined = " ".join(drawn)
            for absent in ("Klavye", "Klv", "Kulaklık", "Kulak"):
                self.assertNotIn(absent, drawn, (mode, absent))
            self.assertIn("64%", joined)

    def test_classic_design_reflows_for_every_device_count(self) -> None:
        metrics = {
            "cpu_load": 24.0, "cpu_temp": 46.0, "cpu_freq": 4.6,
            "gpu_load": 18.0, "gpu_temp": 42.0, "gpu_memory": 920.0,
            "ram_load": 52.0, "ram_used": 16.5,
        }
        empty = screen_app.render_screen(metrics, GameSnapshot(), False, dict(screen_app.COLORS))
        for kinds in self.device_combinations():
            devices = self.devices(kinds)
            frame = screen_app.render_screen(
                metrics, GameSnapshot(), False, dict(screen_app.COLORS), battery_devices=devices,
            )
            self.assertEqual(frame.size, (480, 320))
            changed = ImageChops.difference(frame, empty).getbbox()
            if not kinds:
                self.assertIsNone(changed, "no device -> no battery rail")
            else:
                self.assertIsNotNone(changed)

    def test_ram_is_sampled_at_most_once_per_second(self) -> None:
        sample = SimpleNamespace(total=32 * 1024 ** 3, available=16 * 1024 ** 3, used=15 * 1024 ** 3)
        screen_app._RAM_SAMPLE.update(at=None, value=None)
        with patch.object(screen_app.psutil, "virtual_memory", return_value=sample) as memory:
            screen_app.sample_virtual_memory(now=100.0)
            screen_app.sample_virtual_memory(now=100.4)
            screen_app.sample_virtual_memory(now=100.99)
            self.assertEqual(memory.call_count, 1)
            screen_app.sample_virtual_memory(now=101.0)
            self.assertEqual(memory.call_count, 2)
        screen_app._RAM_SAMPLE.update(at=None, value=None)

    def test_metrics_report_used_and_total_ram(self) -> None:
        sample = SimpleNamespace(total=32 * 1024 ** 3, available=8 * 1024 ** 3, used=20 * 1024 ** 3)
        with patch.object(screen_app, "sample_virtual_memory", return_value=sample), \
                patch.object(screen_app.psutil, "cpu_percent", return_value=10.0), \
                patch.object(screen_app, "get_gpu_metrics", return_value=(5.0, 40.0, 100.0)), \
                patch.object(screen_app.TASK_MANAGER_MONITOR, "read", return_value=(None, None, 4.5, None)), \
                patch.object(screen_app.SENSOR_MONITOR, "read", return_value={"cpu_temp": 50.0, "cpu_freq": 4.5}), \
                patch.object(screen_app.NVIDIA_MONITOR, "read_power_watts", return_value=None):
            metrics = screen_app.get_metrics()
        self.assertAlmostEqual(metrics["ram_load"], 75.0)
        self.assertAlmostEqual(metrics["ram_used_gb"], 24.0)
        self.assertAlmostEqual(metrics["ram_total_gb"], 32.0)

    def test_ram_threshold_colors(self) -> None:
        import screens_new as sn
        self.assertEqual(sn.threshold_color("ram", "load", 69.9), sn.GREEN)
        self.assertEqual(sn.threshold_color("ram", "load", 70.0), sn.ORANGE)
        self.assertEqual(sn.threshold_color("ram", "load", 84.9), sn.ORANGE)
        self.assertEqual(sn.threshold_color("ram", "load", 85.0), sn.RED)

    def test_battery_previews_render_for_every_count(self) -> None:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "render_previews", Path(__file__).parent / "tools" / "render_previews.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        import screens_new as sn
        contexts = module.battery_contexts()
        for name in module.BATTERY_SCREENS:
            for count in range(4):
                context = contexts[f"{name}-bat{count}"]
                self.assertEqual(len(sn.present_batteries(context.batteries)), count)
                self.assertEqual(sn.render_new_screen(context).size, (480, 320))
        self.assertEqual(len(list(module.classic_battery_frames())), 4)


class MouseDpiScenarios(unittest.TestCase):
    """G HUB DPI next to the mouse battery: read-only and overlap-free."""

    APPS = {"applications": [
        {"applicationId": "desk", "name": "APPLICATION_NAME_DESKTOP", "applicationPath": "", "processId": 0},
        {"applicationId": "cs", "name": "Counter-Strike: Global Offensive",
         "applicationPath": "C:\\Steam\\steamapps\\common\\Counter-Strike Global Offensive\\",
         "processId": 10088},
    ]}
    PROFILES = {"profiles": [
        {"id": "p-desk", "applicationId": "desk", "activeForApplication": True,
         "assignments": [{"slotId": "prox2wirelessmouse_mouse_sensitivity", "cardId": "c-desk"}]},
        {"id": "p-cs", "applicationId": "cs", "activeForApplication": True,
         "assignments": [{"slotId": "prox2wirelessmouse_mouse_sensitivity", "cardId": "c-cs"}]},
    ]}
    CARDS = {
        "c-cs": {"mouseSensitivity": {"presetId": "preset-cs", "activeDpiIndex": 1}},
        "preset-cs": {"mouseSettings": {"advancedDpiTable": {"entries": {
            "1": {"disabled": False, "dpi": 800}, "2": {"disabled": True, "dpi": 1200},
        }}}},
    }

    def fake_lghub(self, dpi: int, active_profile: str, cs_running: bool = True):
        import battery_runtime as br
        sent: list[dict] = []
        replies: list[str] = []
        apps = json.loads(json.dumps(self.APPS))
        if not cs_running:
            apps["applications"][1]["processId"] = 0

        def respond(message: dict) -> dict:
            path = message["path"]
            if path.startswith("/mouse/"):
                return {"dpi": dpi, "dpiIndex": 1}
            if path == "/profile/active":
                return {"id": active_profile}
            if path == "/applications":
                return apps
            if path == "/profiles":
                return self.PROFILES
            if path == "/card":
                return self.CARDS.get(message["payload"]["id"])
            return None

        def send(_stream, text):
            message = json.loads(text)
            sent.append(message)
            payload = respond(message)
            replies.append(json.dumps({
                "msgId": message["msgId"],
                "result": {"code": "SUCCESS" if payload is not None else "NO_SUCH_PATH"},
                "payload": payload,
            }))

        class Stream:
            def __enter__(self): return self  # noqa: E704
            def __exit__(self, *_a): return False  # noqa: E704
            def settimeout(self, _t): pass  # noqa: E704

        patches = (
            patch.object(br, "_connect_lghub_websocket", return_value=Stream()),
            patch.object(br, "_send_websocket_text", side_effect=send),
            patch.object(br, "_receive_websocket_message", side_effect=lambda _s: replies.pop(0)),
        )
        return patches, sent

    def read(self, dpi, active_profile, cs_running=True):
        import battery_runtime as br
        patches, sent = self.fake_lghub(dpi, active_profile, cs_running)
        with patches[0], patches[1], patches[2]:
            reading, profile = br.read_lghub_mouse_dpi("dev3")
        self.assertTrue(sent)
        self.assertEqual({message["verb"] for message in sent}, {"GET"}, "G HUB access must be read-only")
        return reading, profile

    def test_cs2_profile_with_matching_dpi_is_not_a_warning(self) -> None:
        reading, profile = self.read(800, "p-cs")
        self.assertEqual((reading.dpi, reading.game_running, reading.game_profile_active,
                          reading.game_expected_dpi), (800, True, True, 800))
        self.assertFalse(reading.warning)
        self.assertEqual(profile, (True, True, 800))

    def test_desktop_profile_while_cs2_runs_is_a_warning(self) -> None:
        reading, _profile = self.read(1200, "p-desk")
        self.assertFalse(reading.game_profile_active)
        self.assertTrue(reading.warning)

    def test_dpi_mismatch_inside_cs2_profile_is_a_warning(self) -> None:
        reading, _profile = self.read(1600, "p-cs")
        self.assertTrue(reading.warning)

    def test_no_warning_when_cs2_is_not_running(self) -> None:
        reading, _profile = self.read(1200, "p-desk", cs_running=False)
        self.assertFalse(reading.game_running)
        self.assertFalse(reading.warning)

    def test_cached_profile_skips_profile_queries(self) -> None:
        import battery_runtime as br
        patches, sent = self.fake_lghub(800, "p-cs")
        with patches[0], patches[1], patches[2]:
            reading, _ = br.read_lghub_mouse_dpi("dev3", (True, True, 800), refresh_profile=False)
        self.assertEqual([message["path"] for message in sent], ["/mouse/dev3/dpi"])
        self.assertFalse(reading.warning)

    def test_monitor_attaches_dpi_only_to_the_mouse(self) -> None:
        import battery_runtime as br
        monitor = br.BatteryDeviceMonitor()
        monitor._lghub_infos = {"slug-m": {"device_id": "dev3"}}
        monitor._lghub_ready = True
        devices = [
            BatteryDevice("lghub:slug-m", "PRO X", 82, "mouse"),
            BatteryDevice("lghub:slug-h", "PRO X WIRELESS", 18, "headset"),
        ]
        reading = br.MouseDpiReading(1200, True, False, 800)
        with patch.object(br, "read_lghub_mouse_dpi", return_value=(reading, (True, False, 800))) as read:
            result = monitor._with_mouse_dpi(devices)
            again = monitor._with_mouse_dpi(devices)
        read.assert_called_once()  # second call is within DPI_REFRESH_SECONDS
        self.assertEqual((result[0].dpi, result[0].dpi_warning), (1200, True))
        self.assertIsNone(result[1].dpi)
        self.assertEqual(again[0].dpi, 1200)
        with patch.object(br, "read_lghub_mouse_dpi", side_effect=ConnectionError("G HUB closed")):
            monitor._dpi_at = 0.0
            plain = monitor._with_mouse_dpi(devices)
        self.assertIsNone(plain[0].dpi)

    def test_dpi_is_drawn_next_to_the_mouse_without_overlap(self) -> None:
        import screens_new as sn
        mouse = BatteryDevice("m", "PRO X", 82, "mouse", dpi=12000, dpi_warning=True)
        others = (BatteryDevice("k", "G915", 64, "keyboard"), BatteryDevice("h", "Head", 18, "headset"))
        for count in range(1, 4):
            devices = (mouse, *others[: count - 1])
            for language in ("tr", "en"):
                ctx = sn.NewScreenContext(
                    BatteryPresenceAndRamScenarios.metrics(True), devices, language=language,
                    fps=999, mode="cs2",
                )
                info = sn.present_batteries(devices)[0]
                self.assertEqual(sn.battery_value_parts(info)[1], (" · 12000", sn.ORANGE))
                strip = sn.draw_strip(Image.new("RGB", (480, 320)), ctx, "#000", "#FFF")
                BatteryPresenceAndRamScenarios.assert_disjoint(self, strip, ("strip", count))
                self.assertIn("battery:mouse", dict(strip))
                layout = sn.main_layout(ctx)
                for _info, (left, top, right, _bottom) in layout["batteries"]:
                    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
                    label = sn._t(ctx, sn.BATTERY_LABELS[_info.kind][0])
                    needed = (draw.textlength(label, font=sn.font(14))
                              + sn._parts_width(draw, sn.battery_value_parts(_info), 14) + 20 + 6)
                    self.assertLessEqual(needed, right - left, (count, language, _info.kind))
                items = sn.faceit_battery_layout(sn.NewScreenContext({}, devices, language=language))
                spans = [(item[1], item[1] + item[4] + item[6]) for item in items]
                for (_l1, r1), (l2, _r2) in zip(spans, spans[1:]):
                    self.assertLess(r1, l2)
                self.assertLessEqual(spans[-1][1], 480 - 26 + 0.5)
        plain = sn.present_batteries((BatteryDevice("m", "PRO X", 82, "mouse", dpi=800),))[0]
        self.assertEqual(sn.battery_value_parts(plain)[1], (" · 800", sn.TEXT))
        # DPI is never attached to a non-mouse device.
        self.assertIsNone(sn.present_batteries((BatteryDevice("h", "H", 50, "headset", dpi=800),))[0].dpi)

    def test_classic_layout_shows_dpi_only_where_it_fits(self) -> None:
        metrics = {
            "cpu_load": 24.0, "cpu_temp": 46.0, "cpu_freq": 4.6,
            "gpu_load": 18.0, "gpu_temp": 42.0, "gpu_memory": 920.0,
            "ram_load": 52.0, "ram_used": 16.5,
        }
        mouse = BatteryDevice("m", "PRO X", 82, "mouse", dpi=800)
        plain = BatteryDevice("m", "PRO X", 82, "mouse")
        others = (BatteryDevice("k", "G915", 64, "keyboard"), BatteryDevice("h", "Head", 18, "headset"))
        for count in (1, 2, 3):
            with_dpi = screen_app.render_screen(
                metrics, GameSnapshot(), False, dict(screen_app.COLORS),
                battery_devices=(mouse, *others[: count - 1]),
            )
            without = screen_app.render_screen(
                metrics, GameSnapshot(), False, dict(screen_app.COLORS),
                battery_devices=(plain, *others[: count - 1]),
            )
            changed = ImageChops.difference(with_dpi, without).getbbox()
            if count == 3:
                self.assertIsNone(changed)
            else:
                self.assertIsNotNone(changed)
                self.assertGreaterEqual(changed[0], 338)
                self.assertLessEqual(changed[2], 456)



class RealUserInteractionScenarios(unittest.TestCase):
    """Drive the panel like a person would: keyboard/mouse on real widgets."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.worker_settings: list[dict] = []
        self.autostart_calls: list[bool] = []
        gui.SETTINGS_PATH.unlink(missing_ok=True)

    def make_panel(self, saved: dict | None = None) -> gui.ControlPanel:
        if saved is not None:
            gui.APP_DATA.mkdir(parents=True, exist_ok=True)
            gui.SETTINGS_PATH.write_text(
                json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8",
            )
        recorder = self.worker_settings
        autostart = self.autostart_calls
        patches = (
            patch.object(gui, "get_auto_hardware_labels", return_value={"cpu": "", "gpu": "", "ram": ""}),
            patch.object(gui, "get_metrics", return_value={}),
            patch.object(gui.ControlPanel, "_screen_worker", lambda panel: recorder.append(dict(panel.settings))),
            patch.object(gui.ControlPanel, "_install_gsi", lambda panel, quiet=False: None),
            patch.object(gui.ControlPanel, "_apply_autostart_async", lambda panel, enabled: autostart.append(enabled)),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        panel = gui.ControlPanel()
        panel.show()
        self.app.processEvents()
        self.addCleanup(panel.deleteLater)
        return panel

    def choose(self, combo, value) -> None:
        """Select an item with the keyboard, which emits activated like a user."""
        from PySide6.QtTest import QTest
        target = combo.findData(value)
        self.assertGreaterEqual(target, 0, value)
        combo.setFocus()
        QTest.keyClick(combo, Qt.Key.Key_Home)
        for _ in range(target):
            QTest.keyClick(combo, Qt.Key.Key_Down)
        self.app.processEvents()
        self.assertEqual(combo.currentData(), value)

    def click(self, widget) -> None:
        from PySide6.QtTest import QTest
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton)
        self.app.processEvents()

    def type_into(self, field, text: str) -> None:
        from PySide6.QtTest import QTest
        field.setFocus()
        field.selectAll()
        QTest.keyClick(field, Qt.Key.Key_Delete)
        # QTest.keyClicks cannot synthesise non-Latin-1 keys (İ, ş, ğ) and
        # aborts the process; insert() is what an IME commit does.
        field.insert(text)
        QTest.keyClick(field, Qt.Key.Key_Tab)
        self.app.processEvents()

    def saved(self) -> dict:
        return json.loads(gui.SETTINGS_PATH.read_text(encoding="utf-8"))

    def start(self, panel) -> None:
        self.click(panel.start_button)
        if panel.worker is not None:
            panel.worker.join(2)

    def run_full_interaction(self, language: str) -> None:
        from PySide6.QtTest import QTest
        panel = self.make_panel()
        self.choose(panel.language_combo, language)
        self.choose(panel.design_combo, "classic")
        self.choose(panel.rotation_combo, "180")
        self.choose(panel.msfs_layout_combo, "classic")
        panel.brightness_slider.setFocus()
        for _ in range(5):
            QTest.keyClick(panel.brightness_slider, Qt.Key.Key_Right)
        self.click(panel.autostart_check)
        self.assertTrue(panel.autostart_check.isChecked())
        self.start(panel)
        expected = {
            "design": "classic", "rotation": "180", "msfs_layout": "classic",
            "brightness": 60, "autostart": True, "language": language,
        }
        saved = self.saved()
        self.assertEqual({key: saved[key] for key in expected}, expected)
        self.assertEqual(len(self.worker_settings), 1)
        received = self.worker_settings[0]
        self.assertEqual({key: received[key] for key in expected}, expected)
        self.assertIn(True, self.autostart_calls)

    def test_start_button_saves_and_passes_user_choices_in_turkish(self) -> None:
        self.run_full_interaction("tr")

    def test_start_button_saves_and_passes_user_choices_in_english(self) -> None:
        self.run_full_interaction("en")

    def test_new_design_choice_survives_start_from_codex_settings(self) -> None:
        saved = default_settings()
        saved.update(design="classic", rotation="normal")
        panel = self.make_panel(saved)
        self.choose(panel.design_combo, "new")
        self.choose(panel.rotation_combo, "180")
        self.start(panel)
        self.assertEqual((self.saved()["design"], self.saved()["rotation"]), ("new", "180"))
        self.assertEqual(
            (self.worker_settings[0]["design"], self.worker_settings[0]["rotation"]), ("new", "180"),
        )

    def test_design_labels_are_claude_and_codex(self) -> None:
        panel = self.make_panel()
        for language in ("tr", "en"):
            self.choose(panel.language_combo, language)
            labels = {panel.design_combo.itemData(i): panel.design_combo.itemText(i)
                      for i in range(panel.design_combo.count())}
            self.assertEqual(labels, {"new": "Claude", "classic": "Codex"})
        self.assertEqual(panel.design_combo.currentData(), "new")

    def test_msfs_layout_combo_only_visible_for_codex(self) -> None:
        panel = self.make_panel()
        self.assertEqual(panel.design_combo.currentData(), "new")
        self.assertFalse(panel.msfs_layout_combo.isVisible())
        self.assertFalse(panel.msfs_layout_caption.isVisible())
        self.choose(panel.design_combo, "classic")
        self.assertTrue(panel.msfs_layout_combo.isVisible())
        self.assertTrue(panel.msfs_layout_caption.isVisible())
        self.assertTrue(panel.msfs_layout_combo.isEnabled())
        self.assertGreater(
            panel.msfs_layout_combo.geometry().top(), panel.design_combo.geometry().bottom(),
        )
        self.choose(panel.design_combo, "new")
        self.assertFalse(panel.msfs_layout_combo.isVisible())
        self.assertFalse(panel.msfs_layout_caption.isVisible())

    def test_saved_values_are_shown_after_startup_without_reset(self) -> None:
        saved = default_settings()
        saved.update(design="classic", rotation="180", msfs_layout="classic",
                     brightness=70, autostart=True, language="en")
        panel = self.make_panel(saved)
        self.assertEqual(panel.design_combo.currentData(), "classic")
        self.assertEqual(panel.design_combo.currentText(), "Codex")
        self.assertEqual(panel.rotation_combo.currentData(), "180")
        self.assertEqual(panel.msfs_layout_combo.currentData(), "classic")
        self.assertTrue(panel.msfs_layout_combo.isVisible())
        self.assertEqual(panel.brightness_slider.value(), 70)
        self.assertTrue(panel.autostart_check.isChecked())
        self.assertEqual(panel.language_combo.currentData(), "en")
        self.start(panel)
        self.assertEqual(self.saved()["design"], "classic")
        self.assertEqual(self.worker_settings[0]["rotation"], "180")

    def test_rotation_can_change_while_running(self) -> None:
        panel = self.make_panel()
        self.start(panel)
        panel.worker = Mock(is_alive=Mock(return_value=True))
        panel._set_editing_enabled(False)
        self.assertTrue(panel.rotation_combo.isEnabled())
        self.choose(panel.rotation_combo, "180")
        self.assertEqual(panel.settings["rotation"], "180")
        self.assertEqual(self.saved()["rotation"], "180")
        panel.worker = None

    def test_turkish_city_names_round_trip_and_mojibake_is_repaired(self) -> None:
        saved = default_settings()
        saved["design"] = "classic"
        saved["weather_cities"] = ["Batman", "MuÄŸla", "Ä°stanbul"]
        panel = self.make_panel(saved)
        self.assertEqual([f.text() for f in panel.weather_city_inputs],
                         ["Batman", "Muğla", "İstanbul"])
        self.type_into(panel.weather_city_inputs[0], "Şanlıurfa")
        self.click(panel.save_button)
        raw = gui.SETTINGS_PATH.read_bytes()
        self.assertIn("Şanlıurfa".encode("utf-8"), raw)
        self.assertIn("Muğla".encode("utf-8"), raw)
        self.assertEqual(self.saved()["weather_cities"], ["Şanlıurfa", "Muğla", "İstanbul"])
        self.assertEqual(gui.load_settings()["weather_cities"], ["Şanlıurfa", "Muğla", "İstanbul"])

    def test_claude_design_uses_single_city_and_keeps_codex_list(self) -> None:
        saved = default_settings()
        saved["weather_cities"] = ["Batman", "Muğla", "İstanbul"]
        panel = self.make_panel(saved)
        self.assertEqual(panel.design_combo.currentData(), "new")
        visible = [field.isVisible() for field in panel.weather_city_inputs]
        self.assertEqual(visible, [True, False, False])
        self.type_into(panel.weather_city_inputs[0], "İzmir")
        self.start(panel)
        self.assertEqual(self.saved()["weather_cities"], ["İzmir", "Muğla", "İstanbul"])
        self.assertEqual(gui.weather_cities_for_design(self.worker_settings[0]), ("İzmir",))
        self.choose(panel.design_combo, "classic") if panel._editing_enabled else None

    def test_codex_design_shows_three_cities_live(self) -> None:
        panel = self.make_panel()
        self.choose(panel.design_combo, "classic")
        self.assertEqual([f.isVisible() for f in panel.weather_city_inputs], [True, True, True])
        self.choose(panel.design_combo, "new")
        self.assertEqual([f.isVisible() for f in panel.weather_city_inputs], [True, False, False])
        self.choose(panel.design_combo, "classic")
        self.start(panel)
        self.assertEqual(len(gui.weather_cities_for_design(self.worker_settings[0])), 3)


class TaskManagerParity113Tests(unittest.TestCase):
    """v1.1.3: CPU/GPU/RAM numbers follow Windows Task Manager."""

    def test_default_sampling_is_one_second_without_extra_averaging(self) -> None:
        monitor = screen_app.WindowsTaskManagerMonitor()
        self.assertEqual(monitor.sample_interval, 1.0)
        self.assertEqual(monitor._cpu_window.maxlen, 1)
        self.assertEqual(monitor._gpu_window.maxlen, 1)

    def test_gpu_sums_processes_per_engine_type_then_takes_max(self) -> None:
        luid = "luid_0x00000000_0x0000d1b5"
        rows = [
            (f"pid_100_{luid}_phys_0_eng_0_engtype_3D", 30.0),
            (f"pid_200_{luid}_phys_0_eng_0_engtype_3D", 25.5),
            (f"pid_300_{luid}_phys_0_eng_0_engtype_3D", 4.0),
            (f"pid_100_{luid}_phys_0_eng_3_engtype_VideoDecode", 40.0),
            (f"pid_100_{luid}_phys_0_eng_5_engtype_Copy", 12.0),
            (f"pid_200_{luid}_phys_0_eng_6_engtype_Copy", 10.0),
        ]
        total = screen_app.WindowsTaskManagerMonitor._gpu_engine_total(rows, luid)
        self.assertAlmostEqual(total, 59.5)

    def test_gpu_video_engine_can_be_the_busiest(self) -> None:
        rows = [
            ("pid_1_luid_0x0_0x1_phys_0_eng_0_engtype_3D", 3.0),
            ("pid_2_luid_0x0_0x1_phys_0_eng_3_engtype_VideoDecode", 22.0),
            ("pid_3_luid_0x0_0x1_phys_0_eng_3_engtype_VideoDecode", 18.0),
        ]
        self.assertEqual(screen_app.WindowsTaskManagerMonitor._gpu_engine_total(rows), 40.0)

    def test_gpu_other_adapter_is_ignored_and_total_is_capped(self) -> None:
        rows = [
            ("pid_1_luid_0x0_0xaaaa_phys_0_eng_0_engtype_3D", 99.0),
            ("pid_2_luid_0x0_0xbbbb_phys_0_eng_0_engtype_3D", 70.0),
            ("pid_3_luid_0x0_0xbbbb_phys_0_eng_0_engtype_3D", 45.0),
            ("pid_4_luid_0x0_0xbbbb_phys_0_eng_0_engtype_3D", float("nan")),
        ]
        self.assertEqual(
            screen_app.WindowsTaskManagerMonitor._gpu_engine_total(rows, "luid_0x0_0xbbbb"), 100.0,
        )

    def test_ram_in_use_is_total_minus_available(self) -> None:
        gib = 1024 ** 3
        memory = SimpleNamespace(total=32 * gib, available=19.7 * gib, used=10 * gib, percent=31.0)
        with patch.object(screen_app.psutil, "cpu_percent", return_value=1.0), patch.object(
            screen_app, "sample_virtual_memory", return_value=memory,
        ), patch.object(screen_app, "get_gpu_metrics", return_value=(1.0, 50.0, 0.0)), patch.object(
            screen_app.TASK_MANAGER_MONITOR, "read", return_value=(5.0, 6.0, 4.0, None),
        ), patch.object(
            screen_app.SENSOR_MONITOR, "read", return_value={"cpu_temp": None, "cpu_freq": None},
        ), patch.object(screen_app.NVIDIA_MONITOR, "read_power_watts", return_value=None):
            metrics = screen_app.get_metrics()
        self.assertAlmostEqual(metrics["ram_used"], 12.3)
        self.assertAlmostEqual(metrics["ram_used_gb"], 12.3)
        self.assertAlmostEqual(metrics["ram_load"], 12.3 / 32 * 100)
        self.assertEqual(f"{metrics['ram_used']:.1f} GB", "12.3 GB")

    def test_metric_sources_are_logged_once(self) -> None:
        with patch.dict(screen_app._METRIC_SOURCES_LOGGED, {"done": False, "calls": 0}), \
                patch.object(screen_app, "log_info") as log:
            for _ in range(3):
                screen_app._log_metric_sources_once("cpuX", "gpuX", "NVML", True)
        log.assert_called_once()
        self.assertIn("cpu=cpuX", log.call_args[0][1])


class Cs2MenuMode113Tests(unittest.TestCase):
    """v1.1.3: CS2 running never falls back to the desktop screen."""

    def test_mode_selection(self) -> None:
        import screens_new as sn
        self.assertEqual(sn.new_design_mode(False, True, False), "cs2_menu")
        self.assertEqual(sn.new_design_mode(False, True, True), "cs2")
        self.assertEqual(sn.new_design_mode(True, True, True), "faceit")
        self.assertEqual(sn.new_design_mode(True, False, False), "faceit")
        self.assertEqual(sn.new_design_mode(False, False, False, True), "msfs")
        self.assertEqual(sn.new_design_mode(False, False, False), "desktop")

    def test_round_screens(self) -> None:
        import screens_new as sn

        def game(**kw):
            return screen_app.GameSnapshot(connected=True, in_match=True, **kw)

        self.assertEqual(sn.cs_screen_for(game(round_phase="freezetime", alive=True)), "live")
        self.assertEqual(sn.cs_screen_for(game(round_phase="live", alive=True)), "live")
        self.assertEqual(sn.cs_screen_for(game(round_phase="live", alive=False)), "death")
        self.assertEqual(sn.cs_screen_for(game(round_phase="over", alive=True)), "death")
        self.assertEqual(sn.cs_screen_for(game(round_phase="", alive=True)), "live")

    def test_menu_status_text(self) -> None:
        import screens_new as sn
        self.assertEqual(sn.cs2_menu_status(False, True), "restart")
        self.assertEqual(sn.cs2_menu_status(False, False), "waiting")
        self.assertEqual(sn.cs2_menu_status(True, False), "menu")

    def test_menu_screen_renders_cs2_palette_with_pings(self) -> None:
        import screens_new as sn
        from i18n import translate
        metrics = {"cpu_load": 40.0, "gpu_load": 55.0, "cpu_temp": 60.0, "gpu_temp": 65.0,
                   "ram_load": 40.0}
        for mode in ("cs2_menu", "cs2"):
            ctx = sn.NewScreenContext(metrics, mode=mode, status="menu",
                                      pings=(("FRA", 20.0), ("VIE", 90.0)))
            frame = sn.render_new_screen(ctx)
            self.assertEqual(frame.size, (480, 320))
            self.assertEqual(frame.getpixel((240, 318)), sn._rgb(sn.MODE_CS2[0]))
            self.assertEqual(frame.getpixel((2, 2)), sn._rgb(sn.MODE_CS2[1]))
            self.assertIn("pings", sn.cs_menu_layout(ctx))
        ctx = sn.NewScreenContext(metrics, mode="cs2_menu", status="restart", language="en")
        sn.render_new_screen(ctx)
        self.assertNotIn("pings", sn.cs_menu_layout(ctx))
        self.assertEqual(translate("CS2'yi yeniden başlat · oyun verisi için", "en"),
                         "Restart CS2 to enable game data")

    def test_restart_needed_when_cfg_newer_than_cs2(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cfg = Path(directory) / screen_app.GSI_CONFIG_NAME
            cfg.write_text("x", encoding="utf-8")
            mtime = cfg.stat().st_mtime
            with patch.dict(screen_app.GSI_AUTH_STATE, {"rejected_at": None}):
                self.assertTrue(screen_app.gsi_restart_needed(mtime - 3 * 3600, cfg))
                self.assertFalse(screen_app.gsi_restart_needed(mtime + 60, cfg))
                self.assertFalse(screen_app.gsi_restart_needed(mtime - 3600, cfg, connected=True))
                self.assertFalse(screen_app.gsi_restart_needed(0.0, cfg))
            with patch.dict(screen_app.GSI_AUTH_STATE, {"rejected_at": 1000.0}):
                self.assertTrue(screen_app.gsi_restart_needed(mtime + 60, cfg, now=1005.0))
                self.assertFalse(screen_app.gsi_restart_needed(mtime + 60, cfg, now=1100.0))

    def test_gsi_token_is_stable_across_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gsi-token.txt"
            with patch.object(screen_app, "GSI_TOKEN_PATH", path), \
                    patch.object(screen_app, "_GSI_TOKEN_CACHE", None):
                first = screen_app.gsi_token()
            with patch.object(screen_app, "GSI_TOKEN_PATH", path), \
                    patch.object(screen_app, "_GSI_TOKEN_CACHE", None):
                second = screen_app.gsi_token()
            self.assertEqual(first, second)
            self.assertEqual(path.read_text(encoding="ascii"), first)


class GsiReplay113Tests(unittest.TestCase):
    """Replay real CS2 POSTs with Valve's HTTP client headers."""

    TOKEN = "R524F" + "x" * 38

    def setUp(self) -> None:
        import http.client
        self.http_client = http.client
        self.state = screen_app.CS2GameState()
        self.server = screen_app.http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), screen_app._GSIHandler,
        )
        self.server.game_state = self.state
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        patcher = patch.object(screen_app, "_GSI_TOKEN_CACHE", self.TOKEN)
        patcher.start()
        self.addCleanup(patcher.stop)
        state_patch = patch.dict(screen_app.GSI_AUTH_STATE, {"rejected_at": None})
        state_patch.start()
        self.addCleanup(state_patch.stop)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def post(self, payload: dict, chunked: bool = False, extra: dict | None = None) -> int:
        body = json.dumps(payload, indent="\t").encode("utf-8")
        connection = self.http_client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        headers = {"User-Agent": "Valve/Steam HTTP Client 1.0", "Content-Type": "application/json",
                   "Accept": "text/html,*/*;q=0.9", "Accept-Charset": "ISO-8859-1,utf-8,*;q=0.7"}
        headers.update(extra or {})
        if chunked:
            connection.putrequest("POST", "/")
            for key, value in headers.items():
                connection.putheader(key, value)
            connection.putheader("Transfer-Encoding", "chunked")
            connection.endheaders()
            connection.send(b"%x\r\n%s\r\n0\r\n\r\n" % (len(body), body))
        else:
            connection.request("POST", "/", body=body, headers=headers)
        status = connection.getresponse().status
        connection.close()
        return status

    def menu_payload(self, token: str | None = None) -> dict:
        return {
            "provider": {"name": "Counter-Strike: Global Offensive", "appid": 730, "version": 14080,
                         "steamid": "76561198000000001", "timestamp": 1760100000},
            "player": {"steamid": "76561198000000001", "name": "erkan", "activity": "menu"},
            "auth": {"token": token or self.TOKEN},
        }

    def match_payload(self, phase: str = "live", health: int = 100) -> dict:
        payload = self.menu_payload()
        payload["map"] = {"mode": "competitive", "name": "de_mirage", "phase": phase, "round": 3,
                          "team_ct": {"score": 2, "consecutive_round_losses": 0},
                          "team_t": {"score": 1, "consecutive_round_losses": 1}}
        payload["round"] = {"phase": "freezetime" if phase == "freeze" else "live"}
        payload["player"] = {
            "steamid": "76561198000000001", "name": "erkan", "team": "CT", "activity": "playing",
            "state": {"health": health, "armor": 100, "money": 3200, "round_kills": 0,
                      "round_killhs": 0},
            "match_stats": {"kills": 3, "assists": 1, "deaths": 2, "mvps": 0, "score": 8},
        }
        if phase == "freeze":
            payload["map"]["phase"] = "live"
        return payload

    def test_menu_payload_is_accepted_and_selects_cs2_menu(self) -> None:
        import screens_new as sn
        self.assertEqual(self.post(self.menu_payload()), 200)
        snapshot = self.state.snapshot()
        self.assertTrue(snapshot.connected)
        self.assertFalse(snapshot.in_match)
        self.assertEqual(screen_app.GSI_AUTH_STATE["last_status"], "accepted")
        self.assertEqual(sn.new_design_mode(False, True, snapshot.in_match), "cs2_menu")
        self.assertEqual(sn.cs2_menu_status(snapshot.connected, False), "menu")
        self.assertIn("last=accepted", screen_app.gsi_request_summary())

    def test_match_payloads_select_round_screens(self) -> None:
        import screens_new as sn
        for phase, health, expected in (
            ("warmup", 100, "live"), ("live", 100, "live"), ("freeze", 100, "live"),
            ("live", 0, "death"),
        ):
            self.assertEqual(self.post(self.match_payload(phase, health)), 200)
            snapshot = self.state.snapshot()
            self.assertTrue(snapshot.in_match, phase)
            self.assertEqual(sn.new_design_mode(False, True, snapshot.in_match), "cs2")
            self.assertEqual(sn.cs_screen_for(snapshot), expected, phase)

    def test_chunked_body_is_accepted(self) -> None:
        self.assertEqual(self.post(self.menu_payload(), chunked=True), 200)
        self.assertTrue(self.state.snapshot().connected)

    def test_old_token_is_rejected_and_triggers_restart_hint(self) -> None:
        self.assertEqual(self.post(self.menu_payload(token="legacy-fixed-token-value")), 200)
        self.assertFalse(self.state.snapshot().connected)
        self.assertEqual(screen_app.GSI_AUTH_STATE["last_reason"], "token mismatch")
        self.assertTrue(screen_app.gsi_restart_needed(cs2_started_at=0.0))

    def test_browser_origin_is_rejected(self) -> None:
        status = self.post(self.menu_payload(), extra={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertFalse(self.state.snapshot().connected)


class BatteryDiscoveryRetry113Tests(unittest.TestCase):
    def test_aborted_winrt_scan_keeps_known_devices_and_backs_off(self) -> None:
        import battery_runtime
        monitor = battery_runtime.BatteryDeviceMonitor(interval=2.0)
        known = battery_runtime.BatteryDevice("win:1", "Headset", 70, "headset", False)
        monitor._windows_devices = [known]
        calls = {"n": 0}
        waits: list[float] = []

        async def aborted():
            calls["n"] += 1
            raise OSError(-2147467260, "operation aborted")

        def fake_wait(timeout=None):
            waits.append(timeout)
            if len(waits) >= 4:
                monitor._stop.set()
            return monitor._stop.is_set()

        with patch.object(monitor, "_scan_windows", aborted), \
                patch.object(monitor._stop, "wait", side_effect=fake_wait):
            monitor._scan_windows_loop()
        devices, error = monitor._current_windows_devices()
        self.assertEqual(devices, [])  # dropped only after the 4th failure
        self.assertIn("OSError", error)
        self.assertLess(waits[0], waits[1])
        self.assertLessEqual(max(waits), battery_runtime.WINDOWS_SCAN_MAX_BACKOFF)

    def test_single_aborted_scan_does_not_hide_devices(self) -> None:
        import battery_runtime
        monitor = battery_runtime.BatteryDeviceMonitor(interval=2.0)
        known = battery_runtime.BatteryDevice("win:1", "Headset", 70, "headset", False)
        monitor._windows_devices = [known]

        async def aborted():
            raise OSError(-2147467260, "operation aborted")

        def stop_wait(timeout=None):
            monitor._stop.set()
            return True

        with patch.object(monitor, "_scan_windows", aborted), \
                patch.object(monitor._stop, "wait", side_effect=stop_wait):
            monitor._scan_windows_loop()
        self.assertEqual(monitor._current_windows_devices()[0], [known])

    def test_windows_scan_never_uses_association_endpoint_inquiry(self) -> None:
        import asyncio
        import battery_runtime
        kinds_seen = []

        class Dev:
            def __init__(self, name, percent):
                self.name, self.id, self.is_enabled = name, "id-" + name, True
                self.properties = {"System.Devices.BatteryLife": percent,
                                   "System.Devices.ContainerId": "c-" + name,
                                   "System.Devices.Connected": True}

        async def fake_find(aqs, props, kind):
            kinds_seen.append(kind)
            if kind == battery_runtime.DeviceInformationKind.ASSOCIATION_ENDPOINT:
                raise OSError(-2147467260, "operation aborted")  # E_ABORT after ~60 s inquiry
            return [Dev("Headset", 55)] if kind == battery_runtime.DeviceInformationKind.DEVICE_CONTAINER else []

        devices = asyncio.run(battery_runtime.BatteryDeviceMonitor._scan_windows(find=fake_find))
        self.assertNotIn(battery_runtime.DeviceInformationKind.ASSOCIATION_ENDPOINT, kinds_seen)
        self.assertEqual([(d.name, d.percent) for d in devices], [("Headset", 55)])

    def test_one_aborted_kind_keeps_other_kinds(self) -> None:
        import asyncio
        import battery_runtime
        K = battery_runtime.DeviceInformationKind

        class Dev:
            name, id, is_enabled = "Speaker", "id-s", True
            properties = {"System.Devices.BatteryLife": 40, "System.Devices.Connected": True}

        async def fake_find(aqs, props, kind):
            if kind == K.DEVICE:
                raise OSError(-2147467260, "operation aborted")
            return [Dev()]

        devices = asyncio.run(battery_runtime.BatteryDeviceMonitor._scan_windows(find=fake_find))
        self.assertEqual([d.percent for d in devices], [40])

        async def all_abort(aqs, props, kind):
            raise OSError(-2147467260, "operation aborted")

        with self.assertRaises(OSError):
            asyncio.run(battery_runtime.BatteryDeviceMonitor._scan_windows(find=all_abort))

    def test_distinct_error_is_logged_at_most_every_five_minutes(self) -> None:
        seen: dict[str, float] = {}
        self.assertTrue(gui.should_log_distinct(seen, "E_ABORT", now=0.0))
        self.assertFalse(gui.should_log_distinct(seen, "E_ABORT", now=120.0))
        self.assertTrue(gui.should_log_distinct(seen, "other", now=120.0))
        self.assertTrue(gui.should_log_distinct(seen, "E_ABORT", now=301.0))


class PerformanceMode113Tests(unittest.TestCase):
    def test_priority_is_below_normal_in_game_and_restored(self) -> None:
        process = Mock()
        self.assertTrue(gui.set_process_priority(True, process))
        process.nice.assert_called_with(gui.psutil.BELOW_NORMAL_PRIORITY_CLASS)
        self.assertTrue(gui.set_process_priority(False, process))
        process.nice.assert_called_with(gui.psutil.NORMAL_PRIORITY_CLASS)

    def test_audio_pause_clears_spectrum_without_blocking(self) -> None:
        monitor = AudioSpectrumMonitor()
        monitor._snapshot = AudioSpectrumSnapshot(bars=(0.9,) * monitor.band_count, active=True)
        started = time.monotonic()
        monitor.pause()
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertFalse(monitor.snapshot().active)
        self.assertFalse(monitor.running)

    def test_in_game_intervals(self) -> None:
        self.assertGreaterEqual(gui.PERFORMANCE_BATTERY_INTERVAL, 5 * gui.NORMAL_BATTERY_INTERVAL)
        self.assertEqual(screen_app.WindowsTaskManagerMonitor().sample_interval, 1.0)

    def test_sensor_host_skips_superio_and_polls_slowly(self) -> None:
        source = (Path(screen_app.__file__).parent / "sensor-helper" / "Program.cs").read_text(
            encoding="utf-8")
        self.assertIn("IsMotherboardEnabled = false", source)
        self.assertIn("IsStorageEnabled = false", source)
        self.assertIn("ProcessPriorityClass.BelowNormal", source)
        self.assertIn("GameRunning() ? 2000 : 1000", source)


class HeadsetPresence115Tests(unittest.TestCase):
    """v1.1.5: a powered-off peripheral must disappear even if a source still
    answers with its last battery value."""

    HEADSET = {"state": "NOT_CONNECTED", "name": "PRO X WIRELESS",
               "kind": "headset", "device_id": "dev2"}
    MOUSE = {"state": "ACTIVE", "name": "PRO X SUPERLIGHT 2",
             "kind": "mouse", "device_id": "dev3"}

    def run_once(self, infos, probe_result, db_devices=None, windows=()):
        import battery_runtime as br
        monitor = br.BatteryDeviceMonitor(interval=2.0)
        monitor._lghub_ready = True
        monitor._lghub_infos = {slug: dict(info) for slug, info in infos.items()}
        monitor._windows_devices = list(windows)
        if db_devices is None:
            db_devices = [
                br.BatteryDevice(f"lghub:{slug}", info["name"], 54, info["kind"])
                for slug, info in infos.items()
            ]
        probed = {}

        def fake_probe(candidates):
            probed.update(candidates)
            return {slug: probe_result[slug] for slug in candidates if slug in probe_result}

        def fake_scan(connected_slugs=None, device_infos=None):
            return [d for d in db_devices
                    if d.stable_id.removeprefix("lghub:") in (connected_slugs or set())]

        def stop_wait(timeout=None):
            monitor._stop.set()
            return True

        with patch.object(br, "probe_lghub_input_batteries", fake_probe), \
                patch.object(monitor, "_scan_lghub", side_effect=fake_scan), \
                patch.object(monitor, "_with_mouse_dpi", side_effect=lambda d: d), \
                patch.object(monitor._refresh, "wait", side_effect=stop_wait):
            monitor._run()
        return monitor, [d.name for d in monitor.snapshot().devices], probed

    def test_headset_off_with_dongle_present_is_hidden(self) -> None:
        # Live G HUB: list says NOT_CONNECTED, battery endpoint still SUCCESS 54%.
        monitor, names, probed = self.run_once(
            {"proxwirelessheadset": self.HEADSET, "prox2wirelessmouse": self.MOUSE},
            {"proxwirelessheadset": (True, 54), "prox2wirelessmouse": (True, 60)},
        )
        self.assertEqual(names, ["PRO X SUPERLIGHT 2"])
        self.assertNotIn("proxwirelessheadset", probed)
        self.assertEqual(monitor._lghub_infos["proxwirelessheadset"]["state"], "NOT_CONNECTED")

    def test_ghub_disconnect_event_hides_headset_immediately(self) -> None:
        import battery_runtime as br
        monitor = br.BatteryDeviceMonitor(interval=30.0)
        monitor._replace_lghub_infos({"proxwirelessheadset": dict(self.HEADSET, state="ACTIVE")})
        monitor._refresh.clear()
        monitor._update_lghub_infos(br.parse_lghub_device_infos({
            "path": "/devices/state/changed",
            "payload": {"slotPrefix": "proxwirelessheadset", "state": "NOT_CONNECTED",
                        "displayName": "PRO X WIRELESS", "deviceType": "HEADSET", "id": "dev2"},
        }))
        self.assertTrue(monitor._refresh.is_set())  # wakes _run instantly
        _monitor, names, _ = self.run_once(
            dict(monitor._lghub_infos), {"proxwirelessheadset": (True, 54)},
        )
        self.assertEqual(names, [])

    def test_headset_back_on_reappears(self) -> None:
        _m, names, _ = self.run_once(
            {"proxwirelessheadset": dict(self.HEADSET, state="ACTIVE")},
            {"proxwirelessheadset": (True, 77)},
        )
        self.assertEqual(names, ["PRO X WIRELESS"])

    def test_mouse_sleeping_stays_mouse_off_disappears(self) -> None:
        # Sleeping: G HUB keeps the mouse ACTIVE (battery may be momentarily unknown).
        _m, names, _ = self.run_once(
            {"prox2wirelessmouse": self.MOUSE}, {"prox2wirelessmouse": (True, None)},
        )
        self.assertEqual(names, ["PRO X SUPERLIGHT 2"])
        # Off: G HUB reports NOT_CONNECTED; a stale SUCCESS must not revive it.
        _m, names, _ = self.run_once(
            {"prox2wirelessmouse": dict(self.MOUSE, state="NOT_CONNECTED")},
            {"prox2wirelessmouse": (True, 60)},
        )
        self.assertEqual(names, [])

    def test_battery_endpoint_missing_path_downgrades_active_device(self) -> None:
        monitor, names, _ = self.run_once(
            {"prox2wirelessmouse": self.MOUSE}, {"prox2wirelessmouse": (False, None)},
        )
        self.assertEqual(names, [])
        self.assertEqual(monitor._lghub_infos["prox2wirelessmouse"]["state"], "NOT_CONNECTED")

    def test_windows_cache_kept_after_failures_cannot_resurrect_lghub_off_device(self) -> None:
        import battery_runtime as br
        cached = br.BatteryDevice("win:c1", "PRO X WIRELESS", 54, "headset")
        _m, names, _ = self.run_once(
            {"proxwirelessheadset": self.HEADSET}, {}, windows=[cached],
        )
        self.assertEqual(names, [])

    def test_windows_scan_requires_positive_connection_state(self) -> None:
        import asyncio
        import battery_runtime as br

        class Dev:
            def __init__(self, name, props):
                self.name, self.id, self.is_enabled = name, "id-" + name, True
                self.properties = {"System.Devices.BatteryLife": 50, **props}

        devices = [
            Dev("Stale", {}),
            Dev("Off", {"System.Devices.Connected": False}),
            Dev("DevOff", {br.DEVPKEY_DEVICE_IS_CONNECTED: False, "System.Devices.Connected": True}),
            Dev("BtOn", {"System.Devices.Aep.IsConnected": True}),
            Dev("On", {br.DEVPKEY_DEVICE_IS_CONNECTED: True}),
        ]

        async def fake_find(aqs, props, kind):
            self.assertIn(br.DEVPKEY_DEVICE_IS_CONNECTED, props)
            return devices

        found = asyncio.run(br.BatteryDeviceMonitor._scan_windows(fake_find, ("k",)))
        self.assertEqual([d.name for d in found], ["BtOn", "On"])


class StartupAndQuit115Tests(unittest.TestCase):
    def test_first_watchdog_pass_is_a_normal_start(self) -> None:
        previous = gui._SENSOR_HOST_SESSION_STARTED
        gui._SENSOR_HOST_SESSION_STARTED = False
        try:
            with tempfile.TemporaryDirectory() as directory, patch.object(
                gui, "ensure_sensor_host", return_value=True,
            ) as ensure, patch.object(gui, "_runtime_log") as log:
                self.assertFalse(gui.nudge_sensor_host_if_stale(Path(directory) / "old.json"))
            ensure.assert_called_once_with(restart=False)
            text = " ".join(str(c.args[0]) for c in log.call_args_list)
            self.assertNotIn("restarted", text)
            self.assertIn("first start", text)
        finally:
            gui._SENSOR_HOST_SESSION_STARTED = previous

    @staticmethod
    def schtasks(command):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if "/Query" in argv and "/XML" in argv:
                if command is None:
                    return SimpleNamespace(returncode=1, stdout=b"", stderr=b"")
                xml = ('<?xml version="1.0" encoding="UTF-16"?><Task><Actions><Exec>'
                       f'<Command>{command}</Command><Arguments>--autorun</Arguments>'
                       '</Exec></Actions></Task>')
                return SimpleNamespace(returncode=0, stdout=xml.encode("utf-16"), stderr=b"")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return calls, fake_run

    def reconcile(self, enabled, command, current):
        calls, fake_run = self.schtasks(command)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(gui.subprocess, "run", side_effect=fake_run), \
                patch.object(gui, "APP_DATA", Path(directory)), \
                patch.object(gui, "STARTUP_TASK_STATE_PATH", Path(directory) / "s.json"), \
                patch.object(gui, "_startup_action", return_value=(current, "--autorun")), \
                patch.object(gui, "_set_windows_run_fallback"), \
                patch.object(gui, "_remove_windows_startup_shortcut", return_value=True), \
                patch.object(gui, "_runtime_log") as log:
            self.assertTrue(gui.reconcile_windows_autostart(enabled))
        verbs = [next((a for a in argv if a in ("/Create", "/Delete")), None) for argv in calls]
        return [v for v in verbs if v], " ".join(str(c.args[0]) for c in log.call_args_list)

    def test_enabled_task_pointing_to_old_exe_is_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            current = str(Path(directory) / "v115" / "Sabasakal-Mini-Screen.exe")
            verbs, log = self.reconcile(True, r"C:\gone\v1.0.12\Sabasakal-Mini-Screen.exe", current)
        self.assertEqual(verbs, ["/Create"])
        self.assertIn("startup task repaired", log)

    def test_disabled_task_pointing_to_missing_exe_is_removed(self) -> None:
        verbs, log = self.reconcile(False, r"C:\gone\v1.0.12\Sabasakal-Mini-Screen.exe", r"C:\x\a.exe")
        self.assertEqual(verbs, ["/Delete"])
        self.assertIn("stale startup task removed", log)

    def test_disabled_task_pointing_to_other_existing_exe_is_repointed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old = Path(directory) / "old.exe"
            old.write_bytes(b"")
            verbs, _log = self.reconcile(False, str(old), str(Path(directory) / "new.exe"))
        self.assertEqual(verbs, ["/Create"])

    def test_disabled_without_task_does_nothing(self) -> None:
        verbs, _log = self.reconcile(False, None, r"C:\x\a.exe")
        self.assertEqual(verbs, [])

    def test_disabled_task_already_current_is_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            exe = Path(directory) / "a.exe"
            exe.write_bytes(b"")
            verbs, _log = self.reconcile(False, str(exe), str(exe))
        self.assertEqual(verbs, [])

    def test_quit_request_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exited = []
            panel = SimpleNamespace(_exit_application=lambda: exited.append(True))
            with patch.object(gui, "APP_DATA", root), \
                    patch.object(gui, "QUIT_REQUEST_PATH", root / "quit.request"), \
                    patch.object(gui, "QUIT_ACK_PATH", root / "quit.ack"), \
                    patch.object(gui, "_runtime_log"):

                def primary():
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        if gui.ControlPanel._consume_quit_request(panel):
                            return
                        time.sleep(0.05)

                thread = threading.Thread(target=primary)
                thread.start()
                self.assertTrue(gui.request_running_instance_quit(timeout_seconds=3))
                thread.join()
            self.assertEqual(exited, [True])
            self.assertFalse((root / "quit.request").exists())

    def test_unanswered_quit_request_times_out_and_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(gui, "APP_DATA", root), \
                    patch.object(gui, "QUIT_REQUEST_PATH", root / "quit.request"), \
                    patch.object(gui, "QUIT_ACK_PATH", root / "quit.ack"):
                self.assertFalse(gui.request_running_instance_quit(timeout_seconds=0.3))
            self.assertFalse((root / "quit.request").exists())

    def test_quit_flag_is_handled_before_single_instance(self) -> None:
        source = Path(gui.__file__).read_text(encoding="utf-8")
        main_src = source.split("def main() -> int:", 1)[1]
        self.assertLess(main_src.index("args.quit"), main_src.index("_acquire_single_instance"))



def _test_render_bug_keeps_usb_session(self) -> None:
    """A non-USB error in one frame cycle must not reconnect the panel."""
    panel = self.make_panel()
    panel.stop_event = FastEvent()
    panel.screen_connected = TrackingEvent()
    screen = FakeScreen(panel.stop_event)
    sessions: list[object] = []
    calls = {"n": 0}

    def metrics():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("sensor bug")
        return {}

    patches = list(self.worker_patches())
    patches[4] = patch.object(gui, "get_metrics", side_effect=metrics)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6],             patches[7], patches[8], patches[9], patch.object(
                gui, "ScreenSession", side_effect=lambda *_a: sessions.append(1) or screen):
        panel._screen_worker()
    self.assertEqual(len(sessions), 1)
    self.assertNotIn("close", screen.events[:-1])
    panel.close()


UiAndConnectionScenarios.test_render_bug_keeps_usb_session = _test_render_bug_keeps_usb_session


import itertools  # noqa: E402
from i18n import translate  # noqa: E402
from msfs_runtime import FlightPhaseTracker  # noqa: E402
from battery_runtime import BatteryDevice  # noqa: E402
from weather_runtime import WeatherReading  # noqa: E402


class Audit116Scenarios(unittest.TestCase):
    def test_msfs_restart_in_air_does_not_record_fake_landing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tracker = FlightPhaseTracker(Path(directory) / "landings.json")
            tracker.update(FlightTelemetry(connected=True, on_ground=False, altitude_agl_ft=8000,
                                           vertical_speed_fpm=-2400), 0.0)
            tracker.update(FlightTelemetry(), 1.0)  # sim closed/crashed mid-air
            state = tracker.update(FlightTelemetry(connected=True, on_ground=True), 2.0)
            tracker.update(FlightTelemetry(connected=True, on_ground=True), 20.0)
            self.assertNotEqual(state.phase, "landing")
            self.assertEqual(tracker.history, ())

    def test_explicit_reset_after_msfs_closed(self) -> None:
        tracker = FlightPhaseTracker(None)
        tracker.update(FlightTelemetry(on_ground=False, vertical_speed_fpm=-900), 0.0)
        tracker.reset()
        self.assertEqual(tracker.update(FlightTelemetry(on_ground=True), 1.0).phase, "ground")
        self.assertEqual(tracker.history, ())

    def test_route_total_resets_for_next_flight_after_landing_window(self) -> None:
        tracker = FlightPhaseTracker(None)
        tracker.update(FlightTelemetry(on_ground=False, destination_ete_s=3600, ground_speed_kt=500), 0.0)
        tracker.update(FlightTelemetry(on_ground=True, vertical_speed_fpm=-150), 1.0)
        tracker.update(FlightTelemetry(on_ground=True, ground_speed_kt=0), 10.0)
        self.assertIsNotNone(tracker.update(FlightTelemetry(on_ground=True, ground_speed_kt=0), 30.0).route_total_nm)
        later = tracker.update(FlightTelemetry(on_ground=True, ground_speed_kt=0), 100.0)
        self.assertIsNone(later.route_total_nm)

    def test_removed_weather_city_disappears_even_without_network(self) -> None:
        import weather_runtime as wr
        monitor = wr.WeatherMonitor(["İstanbul", "Ankara"])
        monitor._snapshot = wr.WeatherSnapshot(readings=(
            wr.WeatherReading("İstanbul", 20.0, 0), wr.WeatherReading("Ankara", 10.0, 0)))
        monitor.set_cities(["Ankara"])
        self.assertEqual([r.city for r in monitor.snapshot().readings], ["Ankara"])

    def test_codex_battery_and_weather_text_stay_inside_their_cells(self) -> None:
        from PIL import ImageDraw
        boxes: list[tuple] = []
        original = ImageDraw.ImageDraw.text

        def record(draw, xy, value, fill=None, font=None, anchor=None, *a, **k):
            if font is not None and str(value).strip():
                boxes.append((draw.textbbox(xy, value, font=font, anchor=anchor), str(value)))
            return original(draw, xy, value, fill, font, anchor, *a, **k)

        devices = (
            BatteryDevice("a", "Mouse", 100, "mouse"),
            BatteryDevice("b", "Headset", 100, "headset"),
            BatteryDevice("c", "Keyboard", 100, "keyboard"),
        )
        weather = (WeatherReading("Kahramanmaraş Afşin Çağlayancerit", -12.0, 3),)
        metrics = {"cpu_load": 100.0, "gpu_load": 100.0, "ram_load": 100.0, "cpu_temp": 99.0,
                   "gpu_temp": 99.0, "cpu_freq": 5.8, "gpu_memory": 24000.0, "ram_used_gb": 127.9,
                   "ram_total_gb": 128.0, "ram_used": 127.9}
        for language in ("tr", "en"):
            for count in (1, 3):
                boxes.clear()
                with patch.object(ImageDraw.ImageDraw, "text", record):
                    screen_app.render_screen(metrics, None, False, battery_devices=devices[:count],
                                             weather_readings=weather, language=language)
                for (box, value) in boxes:
                    self.assertGreaterEqual(box[0], 0, value)
                    self.assertLessEqual(box[2], 480, value)
                for (b1, t1), (b2, t2) in itertools.combinations(boxes, 2):
                    overlap_x = min(b1[2], b2[2]) - max(b1[0], b2[0])
                    overlap_y = min(b1[3], b2[3]) - max(b1[1], b2[1])
                    self.assertFalse(overlap_x > 2 and overlap_y > 2, (language, count, t1, t2))

    def test_design_tooltip_and_system_audio_label_are_translated(self) -> None:
        self.assertEqual(
            translate("Claude: oyuna ve uçuş evresine göre değişen sade ekranlar. Codex: önceki görünüm.", "en")[:7],
            "Claude:")
        self.assertEqual(translate("Sistem sesi", "en") != "Sistem sesi", True)

    def test_wrong_typed_palette_does_not_discard_other_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps({"palette": [], "rotation": "180", "language": "en"}), encoding="utf-8")
            with patch.object(gui, "SETTINGS_PATH", path):
                settings = gui.load_settings()
            self.assertEqual(settings["rotation"], "180")
            self.assertEqual(settings["language"], "en")

    def test_display_watchdog_outlasts_a_full_reconnect(self) -> None:
        worst = (screen_app.SERIAL_WRITE_TIMEOUT_SECONDS
                 + screen_app.SERIAL_RECONNECT_ATTEMPTS * 4.0 + 3.0)
        self.assertGreater(gui.DISPLAY_STALL_SECONDS, worst)

    def test_stop_blank_frame_never_reconnects(self) -> None:
        session = screen_app.ScreenSession.__new__(screen_app.ScreenSession)
        session.display = None
        session.rotation = "normal"
        with patch.object(screen_app.ScreenSession, "_reconnect") as reconnect:
            with self.assertRaises(OSError):
                session.show_once(Image.new("RGB", (480, 320)))
        reconnect.assert_not_called()


class FixedLayoutAndTearingScenarios(unittest.TestCase):
    """v1.1.8: tabular fixed slots, widget-aligned dirty rects, deathmatch."""

    @staticmethod
    def metrics(value):
        return {
            "cpu_load": value, "cpu_temp": value, "gpu_load": value, "gpu_temp": value,
            "ram_load": value, "ram_used": value / 10.0, "ram_total_gb": 32.0,
            "cpu_freq": 4.5, "gpu_memory": 8000.0,
        }

    @staticmethod
    def devices(value):
        return (
            BatteryDevice("m", "M", min(99, value), "mouse"),
            BatteryDevice("h", "H", min(99, value), "headset"),
        )

    def test_strip_positions_do_not_move_with_values(self) -> None:
        import screens_new as sn
        for fps in (None, 9, 144):
            layouts = []
            for value in (1, 9, 11, 33, 99, 100):
                ctx = sn.NewScreenContext(self.metrics(value), self.devices(value), fps=fps, mode="cs2")
                layouts.append(sn.draw_strip(Image.new("RGB", (480, 320)), ctx, "#000", "#FFF"))
            self.assertTrue(all(layout == layouts[0] for layout in layouts), fps)

    def test_strip_dividers_fixed_and_never_crossed(self) -> None:
        import screens_new as sn
        seen = None
        for value in (1, 9, 99, 100):
            ctx = sn.NewScreenContext(self.metrics(value), self.devices(value), fps=144, mode="cs2")
            layout = sn.draw_strip(Image.new("RGB", (480, 320)), ctx, "#000", "#FFF")
            rules = [box for name, box in layout if name.startswith("divider:")]
            texts = [box for name, box in layout if not name.startswith("divider:")]
            self.assertEqual(len(rules), 4)  # cpu|gpu|fps|ram|batteries
            seen = seen or rules
            self.assertEqual(rules, seen)
            for r in rules:
                for b in texts:
                    self.assertTrue(b[2] + sn.STRIP_PAD <= r[0] + 0.5 or r[2] + sn.STRIP_PAD - 0.5 <= b[0], (r, b))
            # Labels are centred on their value block (both drawn on the box centre).
            for name, box in layout:
                if name in ("cpu", "gpu", "fps", "ram"):
                    label = name.upper()
                    width = sn._tlen(label, sn.font(12), 1.0)
                    self.assertLessEqual(width, box[2] - box[0] + 1)

    def test_desktop_and_faceit_positions_do_not_move_with_values(self) -> None:
        import screens_new as sn
        mains, faceits = [], []
        for value in (1, 11, 33, 100):
            ctx = sn.NewScreenContext(self.metrics(value), self.devices(value), mode="desktop")
            layout = sn.main_layout(ctx)
            mains.append((layout["clock"], layout["ram"], layout["cpu"], layout["gpu"]))
            faceits.append([item[1:3] for item in sn.faceit_battery_layout(ctx)])
        self.assertEqual(len(set(mains)), 1)
        self.assertTrue(all(f == faceits[0] for f in faceits))

    def test_digits_are_tabular(self) -> None:
        import screens_new as sn
        typeface = sn.font(22, True)
        widths = {sn._tlen(f"{d}{d}%", typeface) for d in "0123456789"}
        self.assertEqual(len(widths), 1)

    def test_unavailable_fps_and_metrics_are_hidden(self) -> None:
        import screens_new as sn
        ctx = sn.NewScreenContext({}, (), fps=None, mode="cs2")
        layout = sn.draw_strip(Image.new("RGB", (480, 320)), ctx, "#000", "#FFF")
        self.assertNotIn("fps", dict(layout))
        seen = []
        original = ImageDraw.ImageDraw.text

        def capture(draw, xy, value, *args, **kwargs):
            seen.append(str(value))
            return original(draw, xy, value, *args, **kwargs)

        with patch.object(ImageDraw.ImageDraw, "text", capture):
            sn.render_new_screen(sn.NewScreenContext({}, (), mode="desktop"))
            sn.draw_strip(Image.new("RGB", (480, 320)), ctx, "#000", "#FFF")
        self.assertFalse([t for t in seen if "—" in t])

    def test_dirty_rects_never_split_a_widget(self) -> None:
        import screens_new as sn
        for before, after in ((11, 33), (9, 10), (99, 100), (33, 11)):
            old_ctx = sn.NewScreenContext(self.metrics(before), self.devices(before), fps=60, mode="cs2")
            new_ctx = sn.NewScreenContext(self.metrics(after), self.devices(after), fps=61, mode="cs2")
            old = sn.render_new_screen(old_ctx)
            new = sn.render_new_screen(new_ctx)
            kind, regions = gui.plan_new_design_update(new, old, budget=10 ** 9)
            self.assertEqual(kind, "tiles")
            # Fake LCD: apply each rect in order; the final frame matches.
            panel = old.copy()
            for region in regions:
                self.assertEqual(region[0] % 2, 0)
                self.assertEqual((region[2] - region[0]) % 2, 0)
                panel.paste(new.crop(region), region)
            self.assertIsNone(ImageChops.difference(panel, new).getbbox())
            for i, a in enumerate(regions):
                for b in regions[i + 1:]:
                    self.assertFalse(gui._near(a, b, 1), (a, b))
            # Every strip widget lies in at most one rect: never half old/new.
            for name, box in sn.draw_strip(Image.new("RGB", (480, 320)), new_ctx, "#000", "#FFF"):
                hits = [r for r in regions
                        if not (r[2] <= box[0] or box[2] <= r[0] or r[3] <= box[1] or box[3] <= r[1])]
                self.assertLessEqual(len(hits), 1, (name, hits))

    def test_cycle_budget_defers_whole_widgets(self) -> None:
        import screens_new as sn
        old = sn.render_new_screen(sn.NewScreenContext(self.metrics(11), self.devices(11), mode="desktop"))
        new = sn.render_new_screen(sn.NewScreenContext(self.metrics(88), self.devices(88), mode="desktop"))
        _kind, all_regions = gui.plan_new_design_update(new, old, budget=10 ** 9)
        sent = old.copy()
        for _cycle in range(30):
            kind, regions = gui.plan_new_design_update(new, sent, budget=2000)
            if kind == "none":
                break
            area = sum(gui._rect_area(r) for r in regions)
            self.assertTrue(len(regions) == 1 or area <= 2000)
            for region in regions:
                self.assertIn(region, all_regions)
                sent.paste(new.crop(region), region)
        self.assertIsNone(ImageChops.difference(sent, new).getbbox())
        self.assertGreater(len(all_regions), 1)

    def test_deathmatch_never_shows_the_death_screen(self) -> None:
        import screens_new as sn
        dead = GameSnapshot(connected=True, in_match=True, game_mode="deathmatch", alive=False)
        self.assertEqual(sn.cs_screen_for(dead), "live")
        gate = sn.DeathScreenGate(3.0)
        comp = GameSnapshot(connected=True, in_match=True, game_mode="competitive", alive=False)
        self.assertEqual(gate.update(sn.cs_screen_for(comp), 0.0), "live")
        self.assertEqual(gate.update(sn.cs_screen_for(comp), 2.9), "live")
        self.assertEqual(gate.update(sn.cs_screen_for(comp), 3.1), "death")

    def test_deathmatch_replay_sends_only_small_value_updates(self) -> None:
        import random
        import screens_new as sn
        rng = random.Random(7)
        stabilizer = sn.ScreenStabilizer()
        gate = sn.DeathScreenGate(3.0)
        sent = None
        pixels = 0
        kinds = []
        now, alive, next_toggle = 0.0, True, 4.0
        while now < 60.0:
            if now >= next_toggle:
                alive = not alive
                next_toggle = now + rng.uniform(3.0, 8.0)
            game = GameSnapshot(connected=True, in_match=True, game_mode="deathmatch",
                                alive=alive, round_phase="live")
            mode, cs_screen, _phase = stabilizer.update(
                ("cs2", gate.update(sn.cs_screen_for(game), now), ""), now)
            metrics = {"cpu_load": rng.randint(5, 100), "cpu_temp": rng.randint(50, 90),
                       "gpu_load": rng.randint(5, 100), "gpu_temp": rng.randint(50, 90),
                       "ram_load": 60.0}
            frame = sn.render_new_screen(sn.NewScreenContext(
                metrics, (), mode=mode, game=game, cs_screen=cs_screen, fps=None,
                now=__import__("datetime").datetime(2026, 10, 12, 14, 52)))
            if sent is None:
                sent = frame.copy()
            else:
                kind, regions = gui.plan_new_design_update(frame, sent)
                kinds.append(kind)
                for region in regions:
                    pixels += gui._rect_area(region)
                    sent.paste(frame.crop(region), region)
            now += 0.5
        self.assertNotIn("wipe", kinds)
        # Desktop view (rings + values) with fully random metrics every 0.5 s:
        # still partial updates only, well under half of the bus.
        self.assertLess(pixels * 2, 256_000 * 60 * 0.40)


class CsLiveDesktopViewTests(unittest.TestCase):
    def test_cs_live_matches_desktop_without_alerts(self):
        import screens_new as sn
        ctx = sn.NewScreenContext(metrics={}, language="en", mode="cs2")
        self.assertEqual(sn.render_cs_live(ctx).tobytes(), sn.render_main(ctx).tobytes())


if __name__ == "__main__":
    unittest.main(verbosity=2)

