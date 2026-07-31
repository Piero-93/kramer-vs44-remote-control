# -*- mode: python ; coding: utf-8 -*-
#
# Part of kramer-vs44-remote-control. Copyright (C) 2026 Piero Biagini.
# Licensed under the GNU GPL v3 or later. See LICENSE for details.
#
# PyInstaller build description for the Tkinter GUI.
#
#   pyinstaller --clean --noconfirm packaging/kramer-gui.spec
#
# A spec file rather than a command line, for one reason above the others: the
# options differ per platform - a windowed subsystem and an icon and a version
# resource on Windows, none of that on Linux - and expressing that as two flag
# lists in two CI jobs is a duplication that drifts. Here it is one `if`.
#
# Note that a spec silently ignores most command-line flags: --onefile and
# --windowed become arguments to EXE() below, so changing them on the command
# line does nothing. This file is the only place they are set.

import re
import sys
from pathlib import Path

HERE = Path(SPECPATH).resolve()          # SPECPATH is injected by PyInstaller
ROOT = HERE.parent
WINDOWS = sys.platform == "win32"

# One source of truth for the version: read it out of the module rather than
# passing it in, so a build can never disagree with what the program reports.
VERSION = re.search(r'^VERSION\s*=\s*"([^"]+)"',
                    (ROOT / "kramer_paths.py").read_text(encoding="utf-8"),
                    re.MULTILINE).group(1)

# Windows wants a four-part version; 0.1.0 becomes (0, 1, 0, 0).
version_tuple = tuple(int(p) for p in VERSION.split(".")[:3]) + (0,)

version_resource = None
if WINDOWS:
    # An unsigned executable with no metadata at all looks worse to SmartScreen
    # and tells whoever opens Task Manager nothing. This costs one generated
    # file and removes that.
    version_resource = HERE / "version_info.txt"
    version_resource.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={version_tuple}, prodvers={version_tuple},
                    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,
                    date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
        StringStruct('FileDescription', 'Kramer VS-44HN matrix control'),
        StringStruct('FileVersion', '{VERSION}'),
        StringStruct('InternalName', 'kramer-gui'),
        StringStruct('LegalCopyright',
                     'Copyright (C) 2026 Piero Biagini. GNU GPL v3 or later.'),
        StringStruct('OriginalFilename', 'kramer-gui.exe'),
        StringStruct('ProductName', 'kramer-vs44-remote-control'),
        StringStruct('ProductVersion', '{VERSION}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""", encoding="utf-8")

a = Analysis(
    [str(ROOT / "kramer_gui.py")],
    pathex=[str(ROOT)],
    binaries=[],
    # The window icon, read back at runtime through kramer_paths.resource_path.
    # Shipping it also means the frozen resource lookup is exercised by simply
    # starting the program.
    datas=[(str(HERE / "kramer.png"), "packaging")],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    # Trimmed deliberately, not exhaustively: a smaller binary is a smaller thing
    # for an antivirus to be suspicious about, but an over-eager exclude list
    # produces a build that fails only at runtime. Nothing here is imported by
    # this program, directly or through the standard library modules it uses.
    excludes=[
        "unittest", "doctest", "pydoc", "pdb", "lib2to3", "distutils",
        "setuptools", "pip", "sqlite3", "test", "tkinter.test",
        "multiprocessing", "asyncio", "xmlrpc", "pickletools",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="kramer-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,                # predictability over a couple of megabytes
    # UPX is off on purpose. It is one of the strongest antivirus heuristics
    # there is, and this binary is already unsigned; compressing it would trade a
    # few megabytes for a materially higher chance of being quarantined.
    upx=False,
    console=not WINDOWS,        # no console window on Windows; meaningless on Linux
    icon=str(HERE / "kramer.ico") if WINDOWS else None,
    version=str(version_resource) if version_resource else None,
)
