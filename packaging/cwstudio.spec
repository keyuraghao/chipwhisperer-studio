# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for ChipWhisperer Studio.

Build with:  pyinstaller packaging/cwstudio.spec (run from the repository root, inside a venv that has `pip install -e . pyinstaller libusb-package`)
"""
import os
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, collect_dynamic_libs

ROOT = os.path.abspath(os.path.join(os.path.dirname(SPEC), ".."))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

APP_NAME = "ChipWhispererStudio"

datas = []
# Frontend + resources
datas += collect_data_files("cwstudio", includes=["static/**/*", "resources/*"])
# ChipWhisperer package data (bitstreams, firmware for programmers, etc.)
datas += collect_data_files("chipwhisperer")
# CA bundle fallback for HTTPS when the OS trust store is unavailable
datas += collect_data_files("certifi")

binaries = []
# libusb: on Windows the libusb1 wheel ships libusb-1.0.dll next to usb1/ (hook handles it); on Linux/macOS copy the library from libusb-package into usb1/ where python-libusb1 looks first.
try:
    import libusb_package
    lib = libusb_package.find_library("usb-1.0")
    if lib and os.path.isfile(lib):
        binaries.append((lib, "usb1"))
except Exception as e:  # noqa: BLE001
    print("WARNING: libusb-package not available, relying on system libusb:", e)
binaries += collect_dynamic_libs("usb1")

hiddenimports = []
hiddenimports += collect_submodules("chipwhisperer")
hiddenimports += collect_submodules("cwstudio")
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("websockets")
hiddenimports += collect_submodules("mcp", filter=lambda name: not name.startswith("mcp.cli"))  # mcp.cli needs the optional typer extra
datas += collect_data_files("mcp")
hiddenimports += ["truststore", "certifi", "multipart", "python_multipart", "serial", "serial.tools.list_ports", "usb1", "configobj", "ecpy",
                  "anyio._backends._asyncio", "h11", "httptools", "watchfiles", "uvloop"]

a = Analysis(
    [os.path.join(ROOT, "packaging", "launcher.py")],
    pathex=[SRC],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "IPython", "jupyter", "notebook", "PyQt5", "PySide2", "PySide6", "PyQt6",
              "bokeh", "holoviews", "pandas", "scipy.spatial.cKDTree", "sphinx", "pytest", "playwright"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,           # keep a console so users can see the URL / errors; use --no-browser etc.
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(ROOT, "packaging", "icon.ico") if os.path.exists(os.path.join(ROOT, "packaging", "icon.ico")) else None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)
