"""Studio's own application window, so it does not depend on the user's web browser.

The UI is the same web page in every case, shown by the operating system's web engine:

- Windows: Microsoft Edge WebView2 (part of Windows 10 and 11), through pywebview.
- macOS: WebKit (part of macOS), through pywebview.
- Linux: WebKitGTK from the distribution, in a small GTK helper (``resources/gtk_window.py``) run by the system Python, so Studio does not bundle GTK.

``open_window()`` blocks until the window is closed. ``WindowUnavailable`` explains what is missing so the caller can fall back to the browser.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from typing import List, Optional

log = logging.getLogger("cwstudio.window")
HERE = os.path.dirname(os.path.abspath(__file__))
ICON = os.path.join(HERE, "resources", "icon.png")
GTK_HELPER = os.path.join(HERE, "resources", "gtk_window.py")
TITLE = "ChipWhisperer Studio"
LINUX_HINT = "Install WebKitGTK for Python once to get Studio's own window: sudo apt install python3-gi gir1.2-webkit2-4.1 (Debian, Ubuntu, Kali), sudo dnf install python3-gobject webkit2gtk4.1 (Fedora) or sudo pacman -S python-gobject webkit2gtk-4.1 (Arch)."


class WindowUnavailable(RuntimeError):
    pass


def open_window(url: str, width: int = 1500, height: int = 950) -> None:
    """Show Studio at url in its own window and return when the user closes it."""
    if sys.platform.startswith("linux"):
        _open_gtk(url, width, height)
    else:
        _open_pywebview(url, width, height)


# ----- Linux: GTK helper run by the system Python ---------------------------------------
def _gtk_pythons() -> List[str]:
    """Python interpreters that may have the distribution's PyGObject (never the bundled one, which has no GTK)."""
    seen, out = set(), []
    for c in (os.environ.get("CWSTUDIO_GTK_PYTHON"), "/usr/bin/python3", shutil.which("python3"), "/usr/local/bin/python3"):
        if c and os.path.exists(c) and os.path.realpath(c) not in seen:
            seen.add(os.path.realpath(c))
            out.append(c)
    return out


def _helper_env() -> dict:
    env = dict(os.environ)
    for k in ("PYTHONPATH", "PYTHONHOME", "LD_LIBRARY_PATH"):  # the bundle sets these for its own Python; the system one must not see them
        env.pop(k, None)
    return env


def find_gtk_python() -> str:
    errors = []
    for py in _gtk_pythons():
        try:
            r = subprocess.run([py, GTK_HELPER, "--check"], capture_output=True, text=True, timeout=20, env=_helper_env())
        except (OSError, subprocess.TimeoutExpired) as e:
            errors.append(f"{py}: {e}")
            continue
        if r.returncode == 0 and r.stdout.startswith("OK"):
            log.debug("window: %s (%s)", py, r.stdout.strip())
            return py
        errors.append(f"{py}: {(r.stdout or r.stderr).strip()[:200]}")
    raise WindowUnavailable("WebKitGTK is not available (" + "; ".join(errors or ["no python3 found"]) + "). " + LINUX_HINT)


def _open_gtk(url: str, width: int, height: int) -> None:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise WindowUnavailable("there is no graphical display (no DISPLAY or WAYLAND_DISPLAY)")
    py = find_gtk_python()
    cmd = [py, GTK_HELPER, "--url", url, "--title", TITLE, "--icon", ICON, "--width", str(width), "--height", str(height)]
    proc = subprocess.Popen(cmd, env=_helper_env())
    try:
        proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()


# ----- Windows and macOS: pywebview with the system web engine ------------------------------
def _open_pywebview(url: str, width: int, height: int) -> None:
    try:
        import webview  # type: ignore
    except ImportError as e:
        raise WindowUnavailable(f"pywebview is not installed ({e}); install it with: pip install pywebview") from None
    try:
        webview.settings["ALLOW_DOWNLOADS"] = True  # trace sets, firmware images, notebooks, PNG exports
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True  # the wiki and other sites open in the default browser
    except (AttributeError, KeyError, TypeError):
        pass
    webview.create_window(TITLE, url, width=width, height=height, min_size=(900, 600), text_select=True)
    try:
        webview.start(gui="edgechromium" if sys.platform == "win32" else None, icon=ICON)
    except Exception as e:  # noqa: BLE001 (for example WebView2 missing on an old Windows)
        raise WindowUnavailable(f"the system web view could not start ({type(e).__name__}: {e})") from None


def available() -> Optional[str]:
    """None if a window can be opened, otherwise the reason."""
    try:
        if sys.platform.startswith("linux"):
            if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
                return "there is no graphical display"
            find_gtk_python()
        else:
            import webview  # type: ignore # noqa: F401
    except (WindowUnavailable, ImportError) as e:
        return str(e)
    return None
