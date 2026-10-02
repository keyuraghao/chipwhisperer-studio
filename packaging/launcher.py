"""PyInstaller entry point for ChipWhisperer Studio.

Kept separate from cwstudio.cli so the frozen executable can apply bundle-specific fix-ups (libusb location, multiprocessing guard) before the application imports anything.
"""
import multiprocessing
import os
import sys


def _fix_libusb_path():
    """Make python-libusb1 find the bundled libusb: it loads it from usb1/ by full path; on Windows the DLL folders also go on PATH, which Windows searches at load time."""
    if not getattr(sys, "frozen", False) or os.name != "nt":
        return
    base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    for d in (os.path.join(base, "usb1"), base):
        if os.path.isdir(d):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


def _restore_library_path():
    """Give programs Studio starts (compilers, make, OpenOCD, sigrok-cli, xdg-open, the browser) the user's library path back.

    The bundle's bootloader points LD_LIBRARY_PATH at the bundle's own libraries (libstdc++, libz, OpenSSL) and keeps the user's value in LD_LIBRARY_PATH_ORIG. The running Studio does not need it any more (the dynamic loader reads it once, at start), but every child would inherit it and load the wrong libraries. Studio's own children (the decode worker process, --ccwrap) start through the bootloader, which sets it again.
    """
    if not getattr(sys, "frozen", False) or os.name == "nt":
        return
    for k in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
        orig = os.environ.pop(k + "_ORIG", None)
        if orig:
            os.environ[k] = orig
        else:
            os.environ.pop(k, None)


if __name__ == "__main__":
    multiprocessing.freeze_support()  # a decode worker process of the Logic tab runs its task here and exits, never a second Studio
    _restore_library_path()  # before --ccwrap too: it runs the real compiler
    if sys.argv[1:2] == ["--ccwrap"]:  # compiler wrapper for clang firmware builds; keep it fast and quiet
        from cwstudio.ccwrap import main as ccwrap_main
        sys.exit(ccwrap_main(sys.argv[2:]))
    _fix_libusb_path()
    from cwstudio.cli import main
    sys.exit(main())
