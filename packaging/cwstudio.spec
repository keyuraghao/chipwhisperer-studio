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
# Two builds: "app" opens Studio in its own window (pywebview on Windows and macOS), "web" opens it in the browser like a classic local web app and is smaller.
VARIANT = os.environ.get("CWSTUDIO_VARIANT", "app")
if VARIANT not in ("app", "web"):
    raise SystemExit(f"CWSTUDIO_VARIANT must be app or web, not {VARIANT}")
FOLDER = APP_NAME if VARIANT == "app" else APP_NAME + "-Web"
_marker = os.path.join(os.path.abspath(os.path.join(os.path.dirname(SPEC), "..", "build")), f"cwstudio_variant_{VARIANT}", "cwstudio_variant.txt")
os.makedirs(os.path.dirname(_marker), exist_ok=True)
with open(_marker, "w") as _f:
    _f.write("web" if VARIANT == "web" else "app")

datas = [(_marker, ".")]  # tells cwstudio.cli which build this is (the Web build opens the browser by default)
# Frontend + resources
# include_py_files: resources/gtk_window.py is not a module of the bundle but a script run by the system Python for the Linux window; without it the app build always falls back to the browser
datas += collect_data_files("cwstudio", includes=["static/**/*", "resources/*"], include_py_files=True)
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
_usb1_libs = collect_dynamic_libs("usb1")
if any(os.path.basename(b[0]).lower().startswith("libusb") for b in binaries):
    # Both would land at usb1/libusb-1.0.dll on Windows and which one wins is undefined (the 0.5.1 app and Web builds shipped different libusb versions): keep only libusb-package's.
    _usb1_libs = [b for b in _usb1_libs if not os.path.basename(b[0]).lower().startswith("libusb")]
binaries += _usb1_libs
# Unicorn (code map emulation of Arm and RISC-V firmware): only its shared library, not the 33 MB static archive next to it
# The default patterns (lib*.so) miss the versioned libunicorn.so.2 of the Linux wheel, which left the code map without its emulator.
binaries += collect_dynamic_libs("unicorn", search_patterns=["*.dll", "*.dylib", "lib*.so", "lib*.so.*"])
if not any("unicorn" in os.path.basename(b[0]).lower() for b in binaries):
    raise SystemExit("the Unicorn library was not found: the code map would not emulate firmware")

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
# Unicorn imports its CPU architectures by name when an emulator is created (unicorn.unicorn_py3.arch.arm and others), which the import analysis does not see
hiddenimports += collect_submodules("unicorn", filter=lambda name: "unicorn_py2" not in name)
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("websockets")
if sys.platform in ("win32", "darwin") and VARIANT == "app":
    hiddenimports += collect_submodules("webview")  # Studio's own window (pywebview with WebView2 or WebKit)
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
    excludes=(["webview"] if VARIANT == "web" else []) + ["tkinter", "IPython", "jupyter", "notebook", "PyQt5", "PySide2", "PySide6", "PyQt6", "gi", "wx",
              "bokeh", "holoviews", "pandas", "scipy.spatial.cKDTree", "sphinx", "pytest", "playwright"] + MPL_EXCLUDES + PIL_EXCLUDES + UNUSED,
    noarchive=False,
)
# Matplotlib's example data is only used by its gallery scripts.
a.datas = [d for d in a.datas if "mpl-data/sample_data" not in d[0].replace("\\", "/")]
a.datas = [d for d in a.datas if not d[0].replace("\\", "/").endswith(".a")]  # static libraries (Unicorn ships a 33 MB one) are never loaded at run time
a.binaries = [b for b in a.binaries if not b[0].replace("\\", "/").endswith(".a")]
pyz = PYZ(a.pure)
# Strip debug symbols from shared libraries on Linux (libpython alone shrinks by tens of MB); macOS needs its code signatures intact and Windows has no strip.
STRIP = sys.platform.startswith("linux")

# Application icon (drawn from the UI logo by tools/make_icon.py) and, on Windows, version information so Explorer, the taskbar and Task Manager show "ChipWhisperer Studio" and its version.
ICON = os.path.join(ROOT, "packaging", "icon.icns" if sys.platform == "darwin" else "icon.ico")
if not os.path.exists(ICON):
    raise SystemExit(f"missing {ICON}: run python tools/make_icon.py")
VERSION_FILE = None
if sys.platform == "win32":
    from cwstudio import __version__
    nums = tuple(int(x) for x in (__version__.split(".") + ["0", "0", "0"])[:4])
    fields = {"CompanyName": "ChipWhisperer Studio contributors", "FileDescription": "ChipWhisperer Studio", "FileVersion": __version__, "InternalName": APP_NAME, "LegalCopyright": "Apache License 2.0", "OriginalFilename": APP_NAME + ".exe", "ProductName": "ChipWhisperer Studio", "ProductVersion": __version__}
    # PyInstaller's version file format (the output of pyi-grab_version), read on Windows by PyInstaller.utils.win32.versioninfo
    os.makedirs(workpath, exist_ok=True)
    VERSION_FILE = os.path.join(workpath, "version_info.txt")
    with open(VERSION_FILE, "w", encoding="utf-8") as f:
        f.write("VSVersionInfo(\n"
                f"  ffi=FixedFileInfo(filevers={nums}, prodvers={nums}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),\n"
                "  kids=[\n"
                "    StringFileInfo([StringTable('040904B0', [" + ", ".join(f"StringStruct({k!r}, {v!r})" for k, v in fields.items()) + "])]),\n"
                "    VarFileInfo([VarStruct('Translation', [1033, 1200])])\n"
                "  ]\n"
                ")\n")

def make_exe(name, console):
    return EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=name,
        debug=False,
        bootloader_ignore_signals=False,
        strip=STRIP,
        upx=False,
        console=console,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=ICON,
        version=VERSION_FILE,
    )


# Studio opens its own window, so the main executable is a windowed application. On Windows a second, console executable (cw-studio.exe) serves the command line, the MCP server for AI agents and headless use, which need a console.
if VARIANT == "web":  # opens the browser; its console shows the address and messages, and closing it quits Studio
    exes = [make_exe(APP_NAME, console=True)]
else:
    exes = [make_exe(APP_NAME, console=sys.platform.startswith("linux"))]
    if sys.platform == "win32":
        exes.append(make_exe("cw-studio", console=True))
coll = COLLECT(
    *exes,
    a.binaries,
    a.datas,
    strip=STRIP,
    upx=False,
    name=FOLDER,
)
if sys.platform == "darwin":
    # A real macOS application, so Finder and the Dock show Studio's name and icon.
    from cwstudio import __version__ as _v
    app = BUNDLE(
        coll,
        name="ChipWhisperer Studio.app" if VARIANT == "app" else "ChipWhisperer Studio Web.app",
        icon=ICON,
        bundle_identifier="io.github.keyuraghao.chipwhisperer-studio" + ("" if VARIANT == "app" else "-web"),
        version=_v,
        info_plist={"CFBundleDisplayName": "ChipWhisperer Studio" if VARIANT == "app" else "ChipWhisperer Studio Web", "CFBundleShortVersionString": _v, "NSHighResolutionCapable": True, "LSMinimumSystemVersion": "11.0"},
    )
