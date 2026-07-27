# PyInstaller spec: freeze dls-plc-tools into a single Windows .exe.
#
# Built in CI on a windows-latest runner (.github/workflows/_windows-exe.yml)
# and attached to the GitHub release, so IT-locked Windows boxes that can't run
# uv/pip just download and run the one file. Build locally with:
#
#     uv sync && uv pip install pyinstaller
#     uv run pyinstaller --clean --noconfirm packaging/dls-plc-tools.spec
#
# Notes:
# - collect_submodules(aphyt): the subcommands lazy-import aphyt *inside*
#   functions and aphyt imports its CIP classes dynamically, so PyInstaller's
#   static graph misses them without forcing the whole package in. Same defence
#   for openpyxl. PySide6 is handled by PyInstaller's bundled Qt hooks.
# - console=True: this is a CLI (`read`) with a GUI subcommand (`gui`). A
#   console build keeps `read`'s stdout working; GUI users get a harmless
#   console window alongside. Don't flip to windowed or the CLI goes mute.
import os

from PyInstaller.utils.hooks import collect_submodules

script = os.path.join(SPECPATH, os.pardir, "src", "dls_plc_tools", "__main__.py")

hiddenimports = collect_submodules("aphyt") + collect_submodules("openpyxl")

a = Analysis(
    [script],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="dls-plc-tools",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
)
