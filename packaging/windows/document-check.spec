# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


ROOT_DIR = Path(SPECPATH).parents[1]


a = Analysis(
    [str(ROOT_DIR / "windows_launcher.py")],
    pathex=[str(ROOT_DIR)],
    binaries=[],
    datas=[
        (str(ROOT_DIR / "app" / "templates"), "app/templates"),
        (str(ROOT_DIR / "app" / "static"), "app/static"),
        (str(ROOT_DIR / "config.local.example.json"), "."),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tests"],
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
    name="DocumentCheck",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
