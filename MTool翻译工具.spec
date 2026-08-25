# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files

datas = []
datas += collect_data_files('tkinterdnd2')
datas += collect_data_files('pygtrans')


a = Analysis(
    ['MTool翻译工具.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=['tkinterdnd2', 'tkinterdnd2.tkdnd', 'pygtrans', 'pygtrans.Translate', 'pygtrans.TranslateResponse', 'pygtrans.DetectResponse', 'pygtrans.Null'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='MTool翻译工具',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
