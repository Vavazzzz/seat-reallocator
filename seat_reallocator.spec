# PyInstaller spec for Seat Reallocator desktop app
# Build with: pyinstaller seat_reallocator.spec
#
# After any refactor that moves modules (e.g. into a new sub-package), verify
# that all lazy imports inside gui.py still resolve before rebuilding.

import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_data_files

block_cipher = None

# ── Locate bundled dependencies ────────────────────────────────────────────

import pulp
import customtkinter

_pulp_root = Path(pulp.__file__).parent
_cbc_exe   = _pulp_root / 'solverdir' / 'cbc' / 'win' / 'i64' / 'cbc.exe'

# customtkinter ships theme JSON + images that must travel with the binary
_ctk_datas, _ctk_bins, _ctk_hidden = collect_all('customtkinter')

# ── Analysis ───────────────────────────────────────────────────────────────

a = Analysis(
    ['gui.py'],
    pathex=[],
    binaries=[
        (str(_cbc_exe), 'pulp/solverdir/cbc/win/i64'),
        *_ctk_bins,
    ],
    datas=[
        *_ctk_datas,
        # Include the full pulp solverdir so the CBC path detection works
        (str(_pulp_root / 'solverdir'), 'pulp/solverdir'),
    ],
    hiddenimports=[
        *_ctk_hidden,
        # pulp
        'pulp.apis',
        'pulp.apis.coin_api',
        # pandas internals commonly missed
        'pandas._libs.tslibs.np_datetime',
        'pandas._libs.tslibs.nattype',
        'pandas._libs.tslibs.timedeltas',
        'pandas._libs.skiplist',
        # openpyxl
        'openpyxl.styles.builtins',
        # numpy
        'numpy.core._methods',
        'numpy.lib.format',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=['matplotlib', 'scipy', 'PIL', 'tkinter.test'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='SeatReallocator',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,          # no terminal window
    disable_windowed_traceback=False,
    argv_emulation=False,
    icon=None,              # replace with 'icon.ico' if you have one
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='SeatReallocator',
)
