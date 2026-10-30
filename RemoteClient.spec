# -*- mode: python ; coding: utf-8 -*-
import os
import sys

# Deterministic inclusion of native aether_core.dll engine
spec_dir = os.path.dirname(os.path.abspath(SPEC)) if 'SPEC' in locals() else os.getcwd()
dll_candidates = [
    os.path.join(spec_dir, 'native', 'aether_core', 'target', 'release', 'aether_core.dll'),
    os.path.join(spec_dir, 'dist', 'aether_core.dll'),
    os.path.join(spec_dir, 'aether_core.dll'),
]
native_dll = None
for candidate in dll_candidates:
    if os.path.isfile(candidate):
        native_dll = candidate
        break

if not native_dll:
    raise SystemExit(
        "\n[ERRORE PACKAGING BLOCCANTE] aether_core.dll non trovata.\n"
        "La pipeline nativa H.264/WebRTC richiede il binario compilato prima del packaging PyInstaller.\n"
        "Eseguire 'cargo build --release --manifest-path native/aether_core/Cargo.toml' o utilizzare 'scripts\\build_exe.bat'."
    )

binaries = [(native_dll, '.')]

a = Analysis(
    ['src/client_viewer.py'],
    pathex=[],
    binaries=binaries,
    datas=[('assets', 'assets'), ('scripts', 'scripts')],
    hiddenimports=[
        'PIL', 'PIL.Image', 'PIL.ImageTk', 'defusedxml', 'xml.etree.ElementTree',
        'certifi', 'cryptography', 'websockets', 'win32clipboard', 'src.ota',
        'src.host_tray', 'src.host_daemon', 'src.host_service', 'src.signaling_broker', 'src.capture',
        'src.input_handler', 'src.security', 'src.transfer', 'src.protocol', 'src.client', 'src.discovery', 'src.native_engine'
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['cv2', 'numpy', 'scipy', 'matplotlib', 'pytest'],
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
    name='AetherDesk',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/icon.ico',
)
