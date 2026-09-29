# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

driver_root = (
    Path('third_party/turing-smart-screen-python')
    if Path('third_party/turing-smart-screen-python/library').is_dir()
    else Path('../turing-driver')
)

datas = [
    ('sabasakal-logo.ico', '.'),
    ('sabasakal-logo.png', '.'),
    ('cs2-hero.jpg', '.'),
    ('icons/png', 'icons/png'),
    ('icons/LICENSES.md', 'icons'),
    (str(driver_root / 'library'), 'library'),
    ('vendor', 'vendor'),
]
binaries = [
    ('sensor-helper/publish/SabasakalSensorHost.exe', 'sensor-helper'),
]
hiddenimports = [
    'serial.tools.list_ports', 'logging.handlers',
    '_portaudiowpatch', 'winrt._winrt', 'winrt._winrt_windows_media_control',
    'winrt._winrt_windows_devices_enumeration',
]


a = Analysis(
    ['gui.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pythonnet', 'clr', 'clr_loader'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Sabasakal-Mini-Screen',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    icon='sabasakal-logo.ico',
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
