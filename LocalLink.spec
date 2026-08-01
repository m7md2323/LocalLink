# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []

# Bundle the .env so the frozen .exe finds it on first run. The
# destination "." is the bundle root, which is where sys._MEIPASS
# points at runtime in --onefile mode (see engine/bootstrap.py
# _load_dotenv). Only add the entry if the .env actually exists on
# the build host — otherwise PyInstaller aborts with "data file not
# found".
if os.path.isfile(".env"):
    datas.append((".env", "."))

# textual pulls in a fair amount of dynamic data (CSS, markup
# templates, etc.) — collect_all is the supported way to ensure it
# all lands in the bundle.
tmp_ret = collect_all("textual")
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]

# Some stdlib + 3rd-party modules are referenced only via
# ``__import__`` or importlib, so PyInstaller's static analysis
# misses them. Listing them explicitly prevents "ModuleNotFoundError"
# at startup.
hiddenimports += [
    "engine",
    "engine.mesh",
    "engine.mesh.discovery",
    "engine.mesh.server",
    "engine.network",
    "engine.network.client",
    "engine.security",
    "engine.security.keys",
    "engine.security.crypto",
    "engine.storage",
    "engine.storage.connection",
    "engine.storage.database",
    "engine.storage.models",
    "engine.api",
    "engine.bootstrap",
    "cli",
    "cli.main",
    "pathlib",
    "socket",
    "hashlib",
    "sqlite3",
    "uuid",
]


a = Analysis(
    ["run.py"],
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
    a.binaries,
    a.datas,
    [],
    name="LocalLink",
    debug=False,
    # UPX compression is OFF. UPX triggers false positives in
    # Windows Defender / Norton / Kaspersky that look exactly like
    # a crash ("the application failed to initialize properly"), and
    # some AV products silently quarantine or delete the .exe. The
    # size difference (~50MB vs ~80MB) is not worth the headache.
    upx=False,
    upx_exclude=[],
    bootloader_ignore_signals=False,
    strip=False,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
