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
    # get_library_path() returns the bundled file on every OS; find_library("usb-1.0") misses libusb-1.0.dylib on macOS, which left the 0.3.0 and 0.4.0 macOS bundles without libusb.
    lib = libusb_package.get_library_path()
    if lib and os.path.isfile(str(lib)):
        binaries.append((str(lib), "usb1"))
    elif sys.platform != "win32":
        raise SystemExit("libusb-package has no libusb library for this platform; the bundle would not reach the hardware")
except Exception as e:  # noqa: BLE001
    print("WARNING: libusb-package not available, relying on system libusb:", e)
binaries += collect_dynamic_libs("usb1")

hiddenimports = []
# ChipWhisperer's modules are listed from the filesystem: collect_submodules imports each package, and chipwhisperer.capture.trace fails to import without pkg_resources (Studio supplies a stand-in at run time).
import importlib.util
_cw_dir = list(importlib.util.find_spec("chipwhisperer").submodule_search_locations)[0]
for _dir, _subdirs, _files in os.walk(_cw_dir):
    _subdirs[:] = [d for d in _subdirs if d != "__pycache__" and os.path.isfile(os.path.join(_dir, d, "__init__.py"))]
    _rel = os.path.relpath(_dir, _cw_dir)
    _pkg = "chipwhisperer" if _rel == "." else "chipwhisperer." + _rel.replace(os.sep, ".")
    for _f in _files:
        if _f.endswith(".py"):
            hiddenimports.append(_pkg if _f == "__init__.py" else _pkg + "." + _f[:-3])
hiddenimports += collect_submodules("cwstudio")
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("websockets")
hiddenimports += ["truststore", "certifi", "serial", "serial.tools.list_ports", "usb1", "configobj", "ecpy", "anyio._backends._asyncio", "h11"]
# Notebooks can save figures as SVG and PDF, and user code may import any common standard module even though Studio itself does not.
hiddenimports += ["matplotlib.backends.backend_svg", "matplotlib.backends.backend_pdf", "matplotlib.backends.backend_ps"]
hiddenimports += ["zoneinfo", "tomllib", "optparse", "sqlite3", "statistics", "fractions", "decimal", "difflib", "pprint", "timeit", "cProfile", "pstats", "bisect", "heapq",
                  "secrets", "hmac", "binascii", "colorsys", "gzip", "bz2", "lzma", "pickle", "copy", "textwrap", "string", "dataclasses", "functools", "itertools", "configparser", "getpass", "unittest", "doctest"]

# Matplotlib draws notebook figures with the Agg backend only; GUI toolkits and their backends are left out.
MPL_EXCLUDES = ["matplotlib.backends.backend_" + b for b in ("tkagg", "tkcairo", "qtagg", "qtcairo", "qt5agg", "qt5cairo", "gtk3agg", "gtk3cairo", "gtk4agg", "gtk4cairo", "wxagg", "wxcairo", "wx", "macosx", "webagg", "webagg_core", "nbagg")]
# Notebook figures are PNG, so Pillow's AVIF, WebP, colour management and Tk modules (with about 6 MB of codec libraries) are left out.
PIL_EXCLUDES = ["PIL._avif", "PIL.AvifImagePlugin", "PIL._webp", "PIL.WebPImagePlugin", "PIL._imagingcms", "PIL.ImageCms", "PIL._imagingtk", "PIL.ImageTk"]
# Studio has its own HTTP router and MCP server, and uvicorn runs with its pure Python HTTP parser, so none of these belong in the bundle even when installed in the build environment.
UNUSED = ["fastapi", "pydantic", "pydantic_core", "mcp", "mcp_types", "jsonschema", "jsonschema_specifications", "referencing", "rpds", "httpx", "httpx2", "httpcore", "httpcore2", "sse_starlette",
          "opentelemetry", "jwt", "cryptography", "multipart", "python_multipart", "uvloop", "httptools", "watchfiles", "yaml", "dotenv", "Cython", "pyximport", "setuptools", "pkg_resources"]

a = Analysis(
    [os.path.join(ROOT, "packaging", "launcher.py")],
    pathex=[SRC],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "IPython", "jupyter", "notebook", "PyQt5", "PySide2", "PySide6", "PyQt6", "gi", "wx",
              "bokeh", "holoviews", "pandas", "scipy.spatial.cKDTree", "sphinx", "pytest", "playwright"] + MPL_EXCLUDES + PIL_EXCLUDES + UNUSED,
    noarchive=False,
)
# Matplotlib's example data is only used by its gallery scripts.
a.datas = [d for d in a.datas if "mpl-data/sample_data" not in d[0].replace("\\", "/")]
pyz = PYZ(a.pure)
# Strip debug symbols from shared libraries on Linux (libpython alone shrinks by tens of MB); macOS needs its code signatures intact and Windows has no strip.
STRIP = sys.platform.startswith("linux")

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=STRIP,
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
    strip=STRIP,
    upx=False,
    name=APP_NAME,
)
