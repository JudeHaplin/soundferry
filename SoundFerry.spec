# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all, collect_data_files

datas = [('assets/soundferry.ico', 'assets'),
         ('assets/tray_idle.png', 'assets'),
         ('assets/tray_busy.png', 'assets'),
         ('assets/tray_on.png', 'assets'),
         ('assets/tray_err.png', 'assets')]
binaries = []
hiddenimports = ['soundferry.proc_loopback', 'soundferry.autostart', 'soundferry.tray', 'soundferry.shell',
                 'pystray', 'pystray._win32',
                 'webview.platforms.edgechromium', 'webview.platforms.winforms',
                 'clr_loader', 'clr_loader.netfx', 'pythonnet']
tmp_ret = collect_all('pyatv')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pyaudiowpatch')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('soxr')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# pywebview：只收集 WebView2 相关的 DLL 数据文件（不含其他平台后端）
datas += collect_data_files('webview', include_py_files=False)
tmp_ret = collect_all('clr_loader')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    [],
    exclude_binaries=True,
    name='SoundFerry',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets/soundferry.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='SoundFerry',
)
