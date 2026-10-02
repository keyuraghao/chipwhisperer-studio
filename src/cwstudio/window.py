"""Studio's own application window, so it does not depend on the user's web browser.

The UI is the same web page in every case, shown by the operating system's web engine:

- Windows: Microsoft Edge WebView2 (part of Windows 10 and 11), through pywebview.
- macOS: WebKit (part of macOS), through pywebview.
- Linux: WebKitGTK from the distribution, in a small GTK helper (``resources/gtk_window.py``) run by the system Python, so Studio does not bundle GTK.

``open_window()`` blocks until the window is closed, or until ``should_close()`` turns true (Studio was shut down from the UI, an agent or a script), and then closes the window itself. ``WindowUnavailable`` explains what is missing so the caller can fall back to the browser.
"""
from __future__ import annotations

import inspect
import logging
import os
import shutil
import subprocess
import sys
import threading
from typing import Callable, List, Optional

log = logging.getLogger("cwstudio.window")
HERE = os.path.dirname(os.path.abspath(__file__))
ICON = os.path.join(HERE, "resources", "icon.png")
ICON_ICO = os.path.join(HERE, "resources", "icon.ico")  # Windows Forms (pywebview on Windows) only takes .ico files for the window icon
GTK_HELPER = os.path.join(HERE, "resources", "gtk_window.py")
TITLE = "ChipWhisperer Studio"
LINUX_HINT = "Install WebKitGTK for Python once to get Studio's own window: sudo apt install python3-gi gir1.2-webkit2-4.1 (Debian, Ubuntu, Kali), sudo dnf install python3-gobject webkit2gtk4.1 (Fedora) or sudo pacman -S python-gobject webkit2gtk-4.1 (Arch)."


class WindowUnavailable(RuntimeError):
    pass


def open_window(url: str, width: int = 1500, height: int = 950, should_close: Optional[Callable[[], bool]] = None) -> None:
    """Show Studio at url in its own window and return when the user closes it, or close it once should_close() returns true."""
    should_close = should_close or (lambda: False)
    if sys.platform.startswith("linux"):
        _open_gtk(url, width, height, should_close)
    else:
        _open_pywebview(url, width, height, should_close)


# ----- Linux: GTK helper run by the system Python ---------------------------------------
def _gtk_pythons() -> List[str]:
    """Python interpreters that may have the distribution's PyGObject (never the bundled one, which has no GTK)."""
    seen, out = set(), []
    for c in (os.environ.get("CWSTUDIO_GTK_PYTHON"), "/usr/bin/python3", shutil.which("python3"), "/usr/local/bin/python3"):
        if c and os.path.exists(c) and os.path.realpath(c) not in seen:
            seen.add(os.path.realpath(c))
            out.append(c)
    return out


def system_env(environ=None) -> dict:
    """The environment for programs of the operating system (the system Python, xdg-open, the web browser). The standalone bundle points LD_LIBRARY_PATH at its own libraries and keeps the user's value in LD_LIBRARY_PATH_ORIG; system programs must get the original back, or they load the bundle's libraries and fail."""
    env = dict(os.environ if environ is None else environ)
    for k in ("PYTHONPATH", "PYTHONHOME"):  # the bundle sets these for its own Python; the system one must not see them
        env.pop(k, None)
    for k in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
        orig = env.pop(k + "_ORIG", None)
        if orig:
            env[k] = orig
        elif getattr(sys, "frozen", False) or k == "LD_LIBRARY_PATH":
            env.pop(k, None)
    return env


_helper_env = system_env


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


def _open_gtk(url: str, width: int, height: int, should_close: Callable[[], bool]) -> None:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise WindowUnavailable("there is no graphical display (no DISPLAY or WAYLAND_DISPLAY)")
    py = find_gtk_python()
    cmd = [py, GTK_HELPER, "--url", url, "--title", TITLE, "--icon", ICON, "--width", str(width), "--height", str(height)]
    proc = subprocess.Popen(cmd, env=_helper_env())
    try:
        while proc.poll() is None and not should_close():
            try:
                proc.wait(0.25)
            except subprocess.TimeoutExpired:
                pass
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
def storage_dir() -> str:
    """Where the web view keeps Studio's settings (localStorage: theme, last tab, panel choices) between runs."""
    base = os.environ.get("LOCALAPPDATA") if sys.platform == "win32" else None
    return os.path.join(base, "ChipWhispererStudio", "WebView") if base else os.path.join(os.path.expanduser("~"), "ChipWhispererStudio", ".webview")


def _check_webview2() -> None:
    """pywebview silently falls back to Internet Explorer (MSHTML) without the Edge WebView2 runtime, and Studio's UI does not run there: use the browser instead."""
    try:
        from webview.platforms import winforms  # type: ignore
    except Exception as e:  # noqa: BLE001 (pythonnet or .NET missing)
        raise WindowUnavailable(f"pywebview cannot load Windows Forms ({type(e).__name__}: {e})") from None
    if getattr(winforms, "renderer", "edgechromium") != "edgechromium":
        raise WindowUnavailable("the Microsoft Edge WebView2 Runtime is not installed (get it from https://developer.microsoft.com/microsoft-edge/webview2/)")


def pywebview_start_kwargs(start) -> dict:
    """Arguments for webview.start(), keeping only those this pywebview version accepts."""
    kw = {"gui": "edgechromium" if sys.platform == "win32" else None,
          "icon": ICON_ICO if sys.platform == "win32" else ICON,
          "private_mode": False}  # pywebview's default private mode forgets localStorage on every start (and on macOS clears it)
    if sys.platform == "win32":  # the WebView2 profile folder (macOS WebKit keeps its own store per application)
        kw["storage_path"] = storage_dir()
    try:
        params = inspect.signature(start).parameters
    except (TypeError, ValueError):
        return kw
    if any(p.kind == p.VAR_KEYWORD for p in params.values()):
        return kw
    return {k: v for k, v in kw.items() if k in params}


def _open_pywebview(url: str, width: int, height: int, should_close: Callable[[], bool]) -> None:
    try:
        import webview  # type: ignore
    except ImportError as e:
        raise WindowUnavailable(f"pywebview is not installed ({e}); install it with: pip install pywebview") from None
    if sys.platform == "win32":
        _check_webview2()
    for key in ("ALLOW_DOWNLOADS", "OPEN_EXTERNAL_LINKS_IN_BROWSER"):  # downloads: trace sets, firmware images, notebooks, PNG exports; links to the wiki and other sites open in the default browser
        try:
            webview.settings[key] = True
        except (AttributeError, KeyError, TypeError):
            pass
    win = webview.create_window(TITLE, url, width=width, height=height, min_size=(900, 600), text_select=True)
    done = threading.Event()

    def watch():  # runs beside the GUI loop: close the window when Studio was shut down from the UI, an agent or a script
        while not done.wait(0.25):
            if should_close():
                try:
                    win.destroy()
                except Exception:  # noqa: BLE001 (the window is already gone)
                    pass
                return
    kwargs = pywebview_start_kwargs(webview.start)
    if kwargs.get("storage_path"):
        try:
            os.makedirs(kwargs["storage_path"], exist_ok=True)
        except OSError:
            kwargs.pop("storage_path")
    try:
        webview.start(watch, **kwargs)
    except Exception as e:  # noqa: BLE001 (for example WebView2 missing on an old Windows)
        raise WindowUnavailable(f"the system web view could not start ({type(e).__name__}: {e})") from None
    finally:
        done.set()


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
