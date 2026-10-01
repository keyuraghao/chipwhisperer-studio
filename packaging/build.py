#!/usr/bin/env python3
"""Build a standalone ChipWhisperer Studio bundle for the current OS.

    python packaging/build.py [--no-venv] [--out dist]

Steps:
1. create an isolated venv (unless --no-venv), install the repo plus pyinstaller and libusb-package;
2. run PyInstaller with cwstudio.spec;
3. copy launch helpers + udev rule + README next to the executable;
4. zip the result as ChipWhispererStudio-<os>-<arch>.zip
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
APP = "ChipWhispererStudio"


def run(cmd, **kw):
    print("+", " ".join(cmd), flush=True)
    subprocess.check_call(cmd, **kw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-venv", action="store_true", help="use the current interpreter instead of a fresh venv")
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--skip-zip", action="store_true")
    args = ap.parse_args()

    if args.no_venv:
        py = sys.executable
    else:
        venv = os.path.join(ROOT, "build", "cwstudio-venv")
        if not os.path.isdir(venv):
            run([sys.executable, "-m", "venv", venv])
        py = os.path.join(venv, "Scripts" if os.name == "nt" else "bin", "python" + (".exe" if os.name == "nt" else ""))
        run([py, "-m", "pip", "install", "--upgrade", "pip", "wheel"])
        run([py, "-m", "pip", "install", "-e", ROOT, "pyinstaller>=6.0", "libusb-package"])

    workdir = os.path.join(ROOT, "build", "cwstudio-pyinstaller")
    run([py, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", args.out, "--workpath", workdir,
         os.path.join(HERE, "cwstudio.spec")], cwd=ROOT)

    bundle = os.path.join(args.out, APP)
    shutil.copy(os.path.join(ROOT, "src", "cwstudio", "resources", "50-newae.rules"), bundle)
    shutil.copy(os.path.join(HERE, "BUNDLE_README.md"), os.path.join(bundle, "README.md"))
    shutil.copy(os.path.join(ROOT, "LICENSE"), os.path.join(bundle, "LICENSE.txt"))
    shutil.copy(os.path.join(ROOT, "NOTICE"), os.path.join(bundle, "NOTICE.txt"))
    shutil.copy(os.path.join(HERE, "icon.png"), os.path.join(bundle, "ChipWhispererStudio.png"))  # for desktop shortcuts and launchers
    if os.name == "nt":
        with open(os.path.join(bundle, "ChipWhispererStudio-simulator.bat"), "w") as f:
            f.write("@echo off\r\n\"%~dp0ChipWhispererStudio.exe\" --simulate %*\r\n")
    else:
        sh = os.path.join(bundle, "chipwhisperer-studio.sh")
        with open(sh, "w") as f:
            f.write("#!/bin/sh\ncd \"$(dirname \"$0\")\" && exec ./ChipWhispererStudio \"$@\"\n")
        os.chmod(sh, 0o755)
    if sys.platform == "darwin":
        make_app(bundle)

    if not args.skip_zip:
        osname = {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}.get(platform.system(), platform.system().lower())
        arch = platform.machine().lower().replace("amd64", "x86_64").replace("aarch64", "arm64")
        zpath = os.path.join(args.out, f"{APP}-{osname}-{arch}.zip")
        print("+ zip", zpath, flush=True)
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for dp, _dn, fns in os.walk(bundle):
                for fn in fns:
                    full = os.path.join(dp, fn)
                    info = zipfile.ZipInfo(os.path.relpath(full, args.out))
                    info.external_attr = (os.stat(full).st_mode & 0xFFFF) << 16
                    info.compress_type = zipfile.ZIP_DEFLATED
                    with open(full, "rb") as fh:
                        z.writestr(info, fh.read())
        print("built", zpath)
    print("done:", bundle)


APP_SCRIPT = """#!/bin/sh
# Opens ChipWhisperer Studio in Terminal (so its address and messages stay visible); the executable sits next to this app.
HERE="$(cd "$(dirname "$0")/../../.." && pwd)"
exec open -a Terminal "$HERE/ChipWhispererStudio"
"""

INFO_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>ChipWhisperer Studio</string>
  <key>CFBundleDisplayName</key><string>ChipWhisperer Studio</string>
  <key>CFBundleIdentifier</key><string>io.github.keyuraghao.chipwhisperer-studio</string>
  <key>CFBundleVersion</key><string>{version}</string>
  <key>CFBundleShortVersionString</key><string>{version}</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>ChipWhisperer Studio</string>
  <key>CFBundleIconFile</key><string>icon.icns</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
"""


def make_app(bundle: str) -> None:
    """macOS shows icons only for .app bundles, not for plain executables: add a small "ChipWhisperer Studio.app" with the icon that starts the executable next to it."""
    sys.path.insert(0, os.path.join(ROOT, "src"))
    from cwstudio import __version__
    app = os.path.join(bundle, "ChipWhisperer Studio.app", "Contents")
    os.makedirs(os.path.join(app, "MacOS"), exist_ok=True)
    os.makedirs(os.path.join(app, "Resources"), exist_ok=True)
    with open(os.path.join(app, "Info.plist"), "w") as f:
        f.write(INFO_PLIST.format(version=__version__))
    exe = os.path.join(app, "MacOS", "ChipWhisperer Studio")
    with open(exe, "w") as f:
        f.write(APP_SCRIPT)
    os.chmod(exe, 0o755)
    shutil.copy(os.path.join(HERE, "icon.icns"), os.path.join(app, "Resources", "icon.icns"))
    if shutil.which("codesign"):  # ad-hoc signature, as PyInstaller gives the executable
        run(["codesign", "--force", "--sign", "-", os.path.dirname(app)])


if __name__ == "__main__":
    main()
