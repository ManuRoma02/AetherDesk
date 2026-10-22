# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['src/client_viewer.py'],
    pathex=[],
    binaries=[],
    datas=[('assets', 'assets')],
    hiddenimports=[
        'PIL', 'PIL.Image', 'PIL.ImageTk', 'defusedxml', 'xml.etree.ElementTree',
        'cryptography', 'websockets', 'win32clipboard', 'src.ota',
        'src.host_tray', 'src.host_daemon', 'src.signaling_broker', 'src.capture',
        'src.input_handler', 'src.security', 'src.transfer', 'src.protocol', 'src.client'
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
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/icon.ico',
)
